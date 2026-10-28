import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { TaskMonitor } from './monitor.mjs';
import { createNotifier } from './notify.mjs';

function fixture() {
  const root = { id: 'ses_root', title: 'Example', outcome: 'succeeded', time: { created: 1, idle: 100 }, location: { directory: '/project' } };
  const snapshot = {
    root, sessions: [root], active: [], inbox: [], forms: [], permissions: [], shells: [],
    lastAssistant: { id: 'msg_final', type: 'assistant', finish: 'stop', time: { completed: 99 }, content: [{ type: 'text', text: 'Done.' }] },
  };
  const published = [];
  const monitor = new TaskMonitor({ load: async () => structuredClone(snapshot), notifier: { publish: async (message) => { published.push(message); return { status: 'sent' }; } } });
  const event = (type, data) => ({ type, data, created: 101 });
  return { monitor, snapshot, published, event };
}

async function delivered(monitor) {
  await Promise.all([...monitor.publishing.values()]);
}

test('a slow notification request cannot hold up a lifecycle status snapshot', async () => {
  const { snapshot, event } = fixture();
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const monitor = new TaskMonitor({ load: async () => snapshot, notifier: { publish: () => pending } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  try {
    const result = await Promise.race([monitor.inspect('ses_root'), delay(100).then(() => 'blocked-on-http')]);
    assert.notEqual(result, 'blocked-on-http');
    assert.equal(result.complete, true);
  } finally { finish({ status: 'sent' }); await delivered(monitor); }
});

test('monitor respects retry deadlines and stops a deferred final alert when new work arrives', async () => {
  const { snapshot, event } = fixture();
  let now = 1000;
  const attempts = [];
  const monitor = new TaskMonitor({ now: () => now, load: async () => structuredClone(snapshot), notifier: { publish: async message => {
    attempts.push(message);
    return { status: 'failed', retryAt: 31_000 };
  } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  await delivered(monitor);
  for (let i = 0; i < 10; i++) { now += 1500; await monitor.refresh(); await delivered(monitor); }
  assert.equal(attempts.length, 1);
  snapshot.active = ['ses_root'];
  monitor.observe(event('session.execution.started', { sessionID: 'ses_root' }));
  now = 31_001;
  await monitor.refresh();
  await delivered(monitor);
  assert.equal(attempts.length, 1);
});

test('publication rechecks lifecycle eligibility after asynchronous transport admission', async () => {
  const { snapshot, event } = fixture();
  let finish;
  let began;
  const pending = new Promise(resolve => { finish = resolve; });
  const admitted = new Promise(resolve => { began = resolve; });
  let current;
  const monitor = new TaskMonitor({ load: async () => snapshot, notifier: { publish: async (_message, options) => {
    current = options?.isCurrent;
    began();
    await pending;
    return { status: current?.() ? 'sent' : 'cancelled' };
  } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  const inspection = monitor.inspect('ses_root');
  await admitted;
  try {
    assert.equal(typeof current, 'function');
    assert.equal(current(), true);
    monitor.observe(event('session.inbox.enqueued', { sessionID: 'ses_root', inboxID: 'new_work' }));
    assert.equal(current(), false);
  } finally { finish(); await inspection; await delivered(monitor); }
  assert.equal(monitor.sent.size, 0);
});

test('authoritative reconciliation revokes a queued completion even when its activity event was lost', async () => {
  const { snapshot, event } = fixture();
  let current;
  let finish;
  const waiting = new Promise(resolve => { finish = resolve; });
  const monitor = new TaskMonitor({ load: async () => structuredClone(snapshot), notifier: { publish: async (_message, options) => {
    current = options.isCurrent;
    await waiting;
    return { status: current() ? 'sent' : 'cancelled' };
  } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  try {
    assert.equal(current(), true);
    snapshot.active = ['ses_root'];
    snapshot.lastAssistant.finish = 'tool-calls';
    assert.equal((await monitor.inspect('ses_root', true)).state, 'working');
    assert.equal(current(), false);
  } finally { finish(); await delivered(monitor); }
  assert.equal(monitor.sent.size, 0);
});

test('authoritative reconciliation revokes an answered question without its reply event', async () => {
  const { snapshot, event } = fixture();
  snapshot.forms = [{ id: 'frm_waiting', sessionID: 'ses_root', fields: [], title: 'Question' }];
  let current;
  let finish;
  const waiting = new Promise(resolve => { finish = resolve; });
  const monitor = new TaskMonitor({ load: async () => structuredClone(snapshot), notifier: { publish: async (_message, options) => {
    current = options.isCurrent;
    await waiting;
    return { status: current() ? 'sent' : 'cancelled' };
  } } });
  monitor.observe(event('form.created', { form: snapshot.forms[0] }));
  await monitor.inspect('ses_root');
  try {
    assert.equal(current(), true);
    snapshot.forms = [];
    await monitor.inspect('ses_root', true);
    assert.equal(current(), false);
  } finally { finish(); await delivered(monitor); }
  assert.equal(monitor.sent.size, 0);
});

test('an older acknowledgment cannot erase a newer completion waiting for endpoint admission', async () => {
  const { snapshot, event } = fixture();
  let now = 1000;
  let finish;
  const oldDelivery = new Promise(resolve => { finish = resolve; });
  const acknowledged = [];
  let newerAttempts = 0;
  const monitor = new TaskMonitor({ now: () => now, load: async () => structuredClone(snapshot), notifier: { publish: async message => {
    if (message.key === 'done:ses_root:msg_final') {
      await oldDelivery;
      acknowledged.push(message.key);
      return { status: 'sent' };
    }
    if (++newerAttempts === 1) return { status: 'deferred', reason: 'endpoint_inflight', retryAt: 2500 };
    acknowledged.push(message.key);
    return { status: 'sent' };
  } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  snapshot.root.time.idle = 200;
  snapshot.lastAssistant.id = 'msg_newer_final';
  snapshot.lastAssistant.time.completed = 199;
  await monitor.inspect('ses_root', true);
  finish();
  await delivered(monitor);
  await Promise.resolve();
  now = 2500;
  await monitor.refresh();
  await delivered(monitor);
  assert.deepEqual(acknowledged, ['done:ses_root:msg_final', 'done:ses_root:msg_newer_final']);
});

test('startup inspection does not replay historical completion notifications', async () => {
  const { monitor, published } = fixture();
  const result = await monitor.inspect('ses_root');
  assert.equal(result.complete, true);
  assert.equal(published.length, 0);
});

test('Done carries only the final root text and keeps the public snapshot metadata-only', async () => {
  const { monitor, snapshot, published, event } = fixture();
  snapshot.lastAssistant.content = [
    { type: 'reasoning', text: 'Private reasoning: do not publish.' },
    { type: 'tool', name: 'shell', state: { status: 'completed', output: [{ type: 'text', text: 'Private tool output.' }] } },
    { type: 'text', text: '**Implemented the fix.**' },
    { type: 'text', text: 'Verification: all checks passed. ✅' },
  ];
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  const result = await monitor.inspect('ses_root');
  const body = '**Implemented the fix.**\n\nVerification: all checks passed. ✅';
  assert.deepEqual(published, [{ key: 'done:ses_root:msg_final', title: 'Done: Example', message: body }]);
  assert.equal(result.complete, true);
  for (const value of [result, await monitor.inspect('ses_root'), monitor.cache.get('ses_root')]) {
    assert.doesNotMatch(JSON.stringify(value), /Implemented the fix|Verification:|Private reasoning|Private tool output/);
  }
});

test('a latest interim root reply is not a completion body', async () => {
  const { monitor, snapshot, published, event } = fixture();
  snapshot.lastAssistant.finish = 'tool-calls';
  snapshot.lastAssistant.content = [{ type: 'text', text: 'Interim: checking the tests.' }];
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  assert.equal((await monitor.inspect('ses_root')).complete, false);
  assert.equal(published.length, 0);
  snapshot.lastAssistant = {
    id: 'msg_final_after_tools', type: 'assistant', finish: 'stop', time: { completed: 139 },
    content: [{ type: 'text', text: 'Final: the tests passed.' }],
  };
  snapshot.root.time.idle = 140;
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  assert.deepEqual(published.map(({ message }) => message), ['Final: the tests passed.']);
});

test('an unreadable final body remains pending and retries without a fallback or new eligibility', async () => {
  for (const content of [undefined, [], [{ type: 'text', text: ' \n ' }, { type: 'reasoning', text: 'Only thinking.' }]]) {
    const { monitor, snapshot, published, event } = fixture();
    snapshot.lastAssistant.content = content;
    monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
    assert.equal((await monitor.inspect('ses_root')).complete, false);
    assert.equal(published.length, 0);
    snapshot.lastAssistant.content = [{ type: 'text', text: 'The final response is now readable.' }];
    await monitor.refresh();
    assert.deepEqual(published.map(({ message }) => message), ['The final response is now readable.']);
    await monitor.refresh();
    assert.equal(published.length, 1);
  }
});

test('completion bodies fit ntfy in UTF-8 with an explicit ellipsis only when truncated', async (t) => {
  for (const [name, text, expected] of [
    ['short Unicode', '✅ Fixed.\n\n修复完成 — résumé', '✅ Fixed.\n\n修复完成 — résumé'],
    ['exact byte limit', 'a'.repeat(4000), 'a'.repeat(4000)],
    ['ASCII overflow', 'a'.repeat(4001), 'a'.repeat(3997) + '…'],
    ['two-byte boundary', 'é'.repeat(2001), 'é'.repeat(1998) + '…'],
    ['three-byte boundary', '界'.repeat(1334), '界'.repeat(1332) + '…'],
    ['four-byte boundary', '🙂'.repeat(1001), '🙂'.repeat(999) + '…'],
    ['mixed boundary', 'a'.repeat(3995) + '💡 more', 'a'.repeat(3995) + '…'],
  ]) await t.test(name, async () => {
    const { monitor, snapshot, published, event } = fixture();
    snapshot.lastAssistant.content = [{ type: 'text', text }];
    monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
    await monitor.inspect('ses_root');
    assert.equal(published.length, 1);
    assert.equal(published[0].message, expected);
    assert.ok(Buffer.byteLength(published[0].message, 'utf8') <= 4000);
    assert.equal(Buffer.from(published[0].message).toString('utf8'), published[0].message);
  });
});

test('the existing notifier sends the final reply but persists only metadata and deduplicates it', async (t) => {
  const directory = await mkdtemp('/tmp/opencode/final-reply-test-');
  t.after(() => rm(directory, { recursive: true, force: true }));
  const topicFile = join(directory, 'topic.sh');
  const stateDirectory = join(directory, 'state');
  await writeFile(topicFile, 'TOPIC="local-test-only"\n');
  const requests = [];
  const options = {
    stateDirectory, topicFile,
    fetchImpl: async (_url, init) => { requests.push(init.body); return new Response(null, { status: 200 }); },
  };
  const { snapshot, event } = fixture();
  const body = 'Private final answer: the actual task is finished. ✅';
  snapshot.lastAssistant.content = [{ type: 'text', text: body }];
  const monitor = new TaskMonitor({ load: async () => snapshot, notifier: createNotifier(options) });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  const status = await monitor.inspect('ses_root');
  await delivered(monitor);
  const reloaded = new TaskMonitor({ load: async () => snapshot, notifier: createNotifier(options) });
  reloaded.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await reloaded.inspect('ses_root');
  await delivered(reloaded);
  assert.deepEqual(requests, [body]);
  const receipts = (await readFile(join(stateDirectory, 'receipts.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(receipts.map(({ status }) => status), ['sent', 'duplicate']);
  const sent = JSON.parse(await readFile(join(stateDirectory, receipts[0].keyHash, 'sent.json'), 'utf8'));
  for (const record of [...receipts, sent]) {
    assert.deepEqual(Object.keys(record).filter((key) => !['keyHash', 'time', 'event', 'status', 'reason', 'httpStatus', 'attempts'].includes(key)), []);
    assert.equal(JSON.stringify(record).includes(body), false);
  }
  assert.equal(JSON.stringify(status).includes(body), false);
});

test('a root success with active children stays silent until a later root completion', async () => {
  const { monitor, snapshot, published, event } = fixture();
  snapshot.sessions.push({ id: 'ses_child', parentID: 'ses_root', time: { created: 10 }, location: { directory: '/other' } });
  snapshot.active = ['ses_child'];
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  assert.equal((await monitor.inspect('ses_root')).state, 'working');
  assert.equal(published.length, 0);
  snapshot.active = [];
  snapshot.sessions[1].time.idle = 120;
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_child' }));
  assert.equal((await monitor.inspect('ses_root')).reason, 'awaiting-parent');
  assert.equal(published.length, 0);
  snapshot.root.time.idle = 140;
  snapshot.lastAssistant.time.completed = 139;
  snapshot.lastAssistant.content = [{ type: 'text', text: 'Final root answer after the child settled.' }];
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  assert.equal((await monitor.inspect('ses_root')).complete, true);
  assert.equal(published.length, 1);
  assert.equal(published[0].key, 'done:ses_root:msg_final');
  assert.equal(published[0].message, 'Final root answer after the child settled.');
});

test('native child question forms generate one blocked notification for the root', async () => {
  const { monitor, snapshot, published, event } = fixture();
  snapshot.sessions.push({ id: 'ses_child', parentID: 'ses_root', time: { created: 10 } });
  const form = { id: 'frm_question', sessionID: 'ses_child', title: 'An answer is needed', fields: [] };
  snapshot.forms = [form];
  monitor.observe(event('form.created', { form }));
  await Promise.all([monitor.inspect('ses_child'), monitor.inspect('ses_root')]);
  assert.equal(published.length, 1);
  assert.equal(published[0].key, 'question:ses_root:frm_question');
  snapshot.forms = [];
  monitor.observe(event('form.cancelled', { sessionID: 'ses_child', id: form.id }));
  await monitor.inspect('ses_root');
  assert.equal(published.length, 1);
});

test('permission, failure and interruption events do not publish Done or question alerts', async () => {
  for (const type of ['permission.asked', 'session.execution.failed', 'session.execution.interrupted']) {
    const { monitor, published, event } = fixture();
    monitor.observe(event(type, { sessionID: 'ses_root', id: 'request_1' }));
    await monitor.inspect('ses_root');
    assert.equal(published.length, 0, type);
  }
});

test('repeated snapshots and duplicate native events do not multiply notifications', async () => {
  const { monitor, published, event } = fixture();
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await Promise.all(Array.from({ length: 4 }, () => monitor.inspect('ses_root')));
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  assert.equal(published.length, 1);
});

test('a new prompt while snapshot reads are in flight invalidates a would-be Done', async () => {
  const { snapshot, event } = fixture();
  let release;
  const read = new Promise((resolve) => { release = resolve; });
  const published = [];
  const monitor = new TaskMonitor({ load: () => read, notifier: { publish: async (message) => { published.push(message); return { status: 'sent' }; } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  const pending = monitor.inspect('ses_root');
  monitor.observe(event('session.inbox.enqueued', { sessionID: 'ses_root', inboxID: 'msg_new' }));
  release(snapshot);
  assert.equal((await pending).complete, false);
  assert.equal(published.length, 0);
});

test('failed notification attempts can retry instead of being marked delivered', async () => {
  const { snapshot, event } = fixture();
  const attempts = [];
  let now = 1000;
  const monitor = new TaskMonitor({ now: () => now, load: async () => snapshot, notifier: { publish: async (message) => {
    attempts.push(message); return { status: attempts.length === 1 ? 'failed' : 'sent' };
  } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  await delivered(monitor);
  now += 30_000;
  await monitor.refresh();
  await delivered(monitor);
  assert.equal(attempts.length, 2);
});

test('background command settlement requires the parent to resume before Done', async () => {
  const { monitor, published, event } = fixture();
  monitor.observe(event('shell.created', { info: { id: 'sh_job', metadata: { sessionID: 'ses_root' } } }));
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  monitor.observe(event('shell.exited', { id: 'sh_job', status: 'exited' }));
  assert.equal((await monitor.inspect('ses_root')).reason, 'awaiting-parent');
  assert.equal(published.length, 0);
});

test('authoritative reconciliation recovers a completion missed during event disconnection', async () => {
  const { monitor, snapshot, published } = fixture();
  snapshot.active = ['ses_root'];
  await monitor.inspect('ses_root');
  snapshot.active = [];
  snapshot.root.time.idle = 150;
  snapshot.lastAssistant.time.completed = 149;
  await monitor.refresh();
  assert.equal(published.length, 1);
});

test('another publisher being in flight is not a successful delivery receipt', async () => {
  const { snapshot, event } = fixture();
  let attempts = 0;
  const monitor = new TaskMonitor({ load: async () => snapshot, notifier: { publish: async () =>
    ++attempts === 1 ? { status: 'duplicate', reason: 'inflight' } : { status: 'sent' },
  } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  await monitor.inspect('ses_root');
  await monitor.refresh();
  assert.equal(attempts, 2);
});

test('a new execution gets a fresh snapshot while an older notification is in flight', async () => {
  const { snapshot, event } = fixture();
  let release;
  let began;
  const delivering = new Promise((resolve) => { began = resolve; });
  const delivery = new Promise((resolve) => { release = resolve; });
  let loads = 0;
  const monitor = new TaskMonitor({ load: async () => { loads++; return structuredClone(snapshot); }, notifier: { publish: async () => { began(); return delivery; } } });
  monitor.observe(event('session.execution.succeeded', { sessionID: 'ses_root' }));
  const old = monitor.inspect('ses_root');
  await delivering;
  snapshot.active = ['ses_root'];
  monitor.observe(event('session.execution.started', { sessionID: 'ses_root' }));
  const fresh = monitor.inspect('ses_root');
  release({ status: 'sent' });
  assert.equal((await fresh).state, 'working');
  assert.equal((await old).complete, true, 'the old snapshot preceded new activity and was not held by delivery');
  await delivered(monitor);
  assert.equal(loads, 2);
});

test('a failed first read of a newly observed question is retried', async () => {
  const { snapshot, event } = fixture();
  const form = { id: 'frm_new', sessionID: 'ses_root', fields: [], title: 'Question' };
  snapshot.forms = [form];
  let loads = 0;
  const published = [];
  const monitor = new TaskMonitor({ load: async () => {
    if (++loads === 1) throw new Error('Transient read failure');
    return snapshot;
  }, notifier: { publish: async (message) => { published.push(message); return { status: 'sent' }; } }, logger: { warn() {} } });
  monitor.observe(event('form.created', { form }));
  await monitor.refresh();
  await monitor.refresh();
  assert.equal(loads, 2);
  assert.equal(published.length, 1);
});

test('an invalidated recovery snapshot cannot erase a missed-completion watermark', async () => {
  const { monitor, snapshot, published } = fixture();
  snapshot.active = ['ses_root'];
  await monitor.inspect('ses_root');
  snapshot.active = [];
  snapshot.root.time.idle = 150;
  snapshot.lastAssistant.time.completed = 149;
  let release;
  monitor.load = () => new Promise((resolve) => { release = resolve; });
  const invalid = monitor.inspect('ses_root', true);
  monitor.observe({ type: 'server.connected' });
  release(snapshot);
  assert.equal((await invalid).complete, false);
  monitor.load = async () => structuredClone(snapshot);
  await monitor.inspect('ses_root', true);
  assert.equal(published.length, 1);
});

test('a failed startup-seeded inspection is retried without a new event or TUI request', async () => {
  const { snapshot } = fixture();
  snapshot.forms = [{ id: 'frm_pending_on_boot', sessionID: 'ses_root', fields: [], title: 'Existing question' }];
  let loads = 0;
  const published = [];
  const monitor = new TaskMonitor({ load: async () => {
    if (++loads === 1) throw new Error('Initial hydration failed');
    return snapshot;
  }, notifier: { publish: async (message) => { published.push(message); return { status: 'sent' }; } }, logger: { warn() {} } });
  await assert.rejects(monitor.inspect('ses_root'), /Initial hydration failed/);
  await monitor.refresh();
  assert.equal(loads, 2);
  assert.equal(published.length, 1);
});
