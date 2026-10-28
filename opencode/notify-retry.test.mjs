import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { once } from 'node:events';
import { chmod, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { createNotifier } from './notify.mjs';

const notice = { key: 'done:ses_retry:msg_final', title: 'Done: test', message: 'The final assistant reply.' };
const epoch = Date.parse('2026-10-07T04:00:00Z');

async function fixture(t, status = 503, retryAfter) {
  const directory = await mkdtemp(join(tmpdir(), 'retry-test-'));
  const state = { now: epoch, status, retryAfter, requests: [] };
  const server = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    state.requests.push({ time: state.now, body });
    await state.beforeResponse?.();
    response.statusCode = state.status;
    if (state.retryAfter !== undefined) response.setHeader('Retry-After', state.retryAfter);
    response.end('upstream response stays private');
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  });
  const options = {
    stateDirectory: join(directory, 'state'), topicFile: join(directory, 'topic.sh'),
    endpoint: `http://127.0.0.1:${server.address().port}`, timeoutMs: 500,
    now: () => state.now, random: () => 0,
  };
  await writeFile(options.topicFile, 'TOPIC="local-fixture-only"\n');
  return { state, options, notifier: createNotifier(options) };
}

test('repeated refreshes cannot restart retry batches or grow the receipt log', async t => {
  const { state, options, notifier } = await fixture(t);
  const first = await notifier.publish(notice);
  assert.equal(first.status, 'failed');
  assert.equal(state.requests.length, 1, 'one admission must perform one HTTP attempt');
  assert.equal(first.retryAt, epoch + 30_000);
  for (let i = 0; i < 100; i++) assert.equal((await notifier.publish(notice)).status, 'deferred');
  assert.equal(state.requests.length, 1);
  const lines = (await readFile(join(options.stateDirectory, 'receipts.jsonl'), 'utf8')).trim().split('\n');
  assert.equal(lines.length, 1, 'cooldown checks are not new attempts');
});

test('a fresh process honors persisted failure pacing and recovery deduplicates delivery', async t => {
  const { state, options, notifier } = await fixture(t);
  await notifier.publish(notice);
  const { now, random, ...serializable } = options;
  const { stdout } = await promisify(execFile)(process.execPath, ['--input-type=module', '-e', `
    import { createNotifier } from ${JSON.stringify(new URL('./notify.mjs', import.meta.url).href)};
    console.log(JSON.stringify(await createNotifier({ ...${JSON.stringify(serializable)}, now: () => ${epoch}, random: () => 0 }).publish(${JSON.stringify(notice)})));
  `]);
  assert.equal(JSON.parse(stdout).status, 'deferred');
  assert.equal(state.requests.length, 1);
  state.now += 30_000;
  state.status = 200;
  assert.equal((await createNotifier(options).publish(notice)).status, 'sent');
  assert.equal((await createNotifier(options).publish(notice)).status, 'duplicate');
  assert.equal(state.requests.length, 2);
  assert.equal(state.requests[1].body, 'The final assistant reply.');
});

test('exponential cooldown survives new instances and grows across consecutive failures', async t => {
  const { state, options } = await fixture(t);
  for (const wait of [30_000, 60_000, 120_000, 240_000, 480_000, 900_000, 900_000]) {
    const result = await createNotifier(options).publish(notice);
    assert.equal(result.retryAt, state.now + wait);
    const requests = state.requests.length;
    state.now = result.retryAt - 1;
    assert.equal((await createNotifier(options).publish(notice)).status, 'deferred');
    assert.equal(state.requests.length, requests);
    state.now++;
  }
  assert.equal(state.requests.length, 7);
});

test('429 cooldown protects other sessions and honors Retry-After seconds', async t => {
  const { state, notifier } = await fixture(t, 429, '120');
  const first = await notifier.publish(notice);
  assert.equal(first.retryAt, epoch + 120_000);
  const other = { ...notice, key: 'question:ses_other:frm_waiting' };
  assert.equal((await notifier.publish(other)).status, 'deferred');
  assert.equal(state.requests.length, 1);
  state.now += 120_000;
  state.status = 200;
  assert.equal((await notifier.publish(other)).status, 'sent');
  assert.equal((await notifier.publish(notice)).status, 'sent');
  assert.equal(state.requests.length, 3);
});

test('Retry-After HTTP-date remains a minimum even beyond the exponential cap', async t => {
  const { state, notifier } = await fixture(t, 503, 'Wed, 07 Oct 2026 05:00:00 GMT');
  const result = await notifier.publish(notice);
  assert.equal(result.retryAt, epoch + 3_600_000);
  state.now += 3_599_999;
  assert.equal((await notifier.publish(notice)).status, 'deferred');
  assert.equal(state.requests.length, 1);
});

test('missing or invalid Retry-After on 429 still backs off for at least a minute', async t => {
  for (const header of [undefined, 'garbage', '-1', '0']) await t.test(String(header), async t => {
    const { notifier } = await fixture(t, 429, header);
    const result = await notifier.publish(notice);
    assert.ok(result.retryAt >= epoch + 60_000);
  });
});

test('permanent HTTP errors stay terminal across reloads without pretending they were sent', async t => {
  const { state, options, notifier } = await fixture(t, 401);
  const result = await notifier.publish(notice);
  assert.equal(result.status, 'failed');
  assert.equal(result.terminal, true);
  state.status = 200;
  state.now += 86_400_000;
  for (let i = 0; i < 5; i++) {
    const retry = await createNotifier(options).publish(notice);
    assert.equal(retry.status, 'failed');
    assert.equal(retry.terminal, true);
  }
  assert.equal(state.requests.length, 1);
  assert.equal((await notifier.publish({ ...notice, key: 'done:ses_new:msg_new' })).status, 'sent');
});

test('concurrent different sessions allow one endpoint attempt before a rate-limit cooldown', async t => {
  const { state, options } = await fixture(t, 429);
  await Promise.all(Array.from({ length: 12 }, (_, i) => createNotifier(options).publish({ ...notice, key: `done:ses_${i}:msg_final` })));
  assert.equal(state.requests.length, 1);
});

test('a revoked notice is checked immediately before HTTP admission', async t => {
  const { state, notifier } = await fixture(t, 200);
  const result = await notifier.publish(notice, { isCurrent: () => false });
  assert.equal(result.status, 'cancelled');
  assert.equal(state.requests.length, 0);
});

test('an observed server cooldown survives a failed metadata write and is repaired before later admission', async t => {
  const { state, options, notifier } = await fixture(t, 429, '7200');
  let endpointDirectory;
  state.beforeResponse = async () => {
    const name = (await readdir(options.stateDirectory)).find(name => name.startsWith('endpoint-'));
    endpointDirectory = join(options.stateDirectory, name);
    await chmod(endpointDirectory, 0o500);
    state.beforeResponse = undefined;
  };
  let failed;
  try {
    failed = await notifier.publish(notice);
    assert.equal(failed.status, 'failed');
    assert.equal(failed.reason, 'storage');
    assert.equal(failed.retryAt, epoch + 7_200_000);
  } finally { if (endpointDirectory) await chmod(endpointDirectory, 0o700); }
  state.now += 31_000;
  const next = await createNotifier(options).publish({ ...notice, key: 'done:ses_other:msg_new' });
  assert.equal(next.status, 'deferred');
  assert.equal(next.retryAt, epoch + 7_200_000);
  assert.equal(state.requests.length, 1);
  const { now, random, ...serializable } = options;
  const { stdout } = await promisify(execFile)(process.execPath, ['--input-type=module', '-e', `
    import { createNotifier } from ${JSON.stringify(new URL('./notify.mjs', import.meta.url).href)};
    console.log(JSON.stringify(await createNotifier({ ...${JSON.stringify(serializable)}, now: () => ${epoch + 31_000} }).publish(${JSON.stringify(notice)})));
  `]);
  assert.equal(JSON.parse(stdout).status, 'deferred');
  assert.equal(state.requests.length, 1);
});

test('a resumed expired publisher cannot shorten or clear a newer endpoint cooldown', async t => {
  for (const succeeds of [false, true]) await t.test(succeeds ? 'late success' : 'late timeout', async t => {
    const { state, options } = await fixture(t, 429, '120');
    const { now, random, ...serializable } = options;
    const child = spawn(process.execPath, ['--input-type=module', '-e', `
      import { createNotifier } from ${JSON.stringify(new URL('./notify.mjs', import.meta.url).href)};
      const result = await createNotifier({ ...${JSON.stringify(serializable)}, now: () => ${epoch}, random: () => 0,
        timeoutMs: ${succeeds ? 10_000 : 100}, leaseMs: 300,
        fetchImpl: async () => { console.log('admitted'); await new Promise(resolve => setTimeout(resolve, 400)); return new Response(null, { status: 200 }); },
      }).publish(${JSON.stringify(notice)});
      console.log(JSON.stringify({ status: result.status }));
    `], { stdio: ['ignore', 'pipe', 'pipe'] });
    const closed = once(child, 'close');
    t.after(async () => { child.kill('SIGKILL'); await closed; });
    const [output] = await once(child.stdout, 'data');
    assert.match(output.toString(), /admitted/);
    child.kill('SIGSTOP');
    await delay(400);
    const newer = await createNotifier(options).publish({ ...notice, key: 'done:ses_newer:msg_final' });
    assert.equal(newer.retryAt, epoch + 120_000);
    child.kill('SIGCONT');
    await closed;
    if (succeeds) {
      assert.equal((await createNotifier(options).publish(notice)).status, 'duplicate', 'an acknowledged late success must remain delivered');
    }
    state.now += 31_000;
    const { stdout } = await promisify(execFile)(process.execPath, ['--input-type=module', '-e', `
      import { createNotifier } from ${JSON.stringify(new URL('./notify.mjs', import.meta.url).href)};
      console.log(JSON.stringify(await createNotifier({ ...${JSON.stringify(serializable)}, now: () => ${epoch + 31_000}, random: () => 0 })
        .publish(${JSON.stringify({ ...notice, key: 'done:ses_later:msg_final' })})));
    `]);
    const later = JSON.parse(stdout);
    assert.equal(later.status, 'deferred');
    assert.equal(later.retryAt, epoch + 120_000);
    assert.equal(state.requests.length, 1);
  });
});
