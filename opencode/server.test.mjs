import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { TaskMonitor } from './monitor.mjs';

const key = Symbol.for('local.opencode.task-lifecycle.v3');

async function plugin(build) {
  const source = (await readFile(new URL('./index.js', import.meta.url), 'utf8'))
    .replace("import { OpenCode } from '@opencode/client';", 'const OpenCode = {};')
    .replace("import { Service } from '@opencode/client/service';", 'const Service = { discover: () => new Promise(() => {}) };')
    .replace(/^const build = .*;$/m, `const build = '${build}';`)
    .replace(/from '(\.\/[^']+)'/g, (_, path) => `from '${new URL(path, import.meta.url).href}'`);
  return (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}#${build}`)).default;
}

for (const previous of [{ build: '1.0.2', revision: 3, next: '1.0.0' }, { build: '1.0.0', revision: 4, next: '1.0.1' }]) {
  test(`loading release ${previous.next} upgrades the process-owned ${previous.build}/r${previous.revision} monitor`, async (t) => {
    const controller = new AbortController();
    const shared = { refs: 1, controller, build: previous.build, revision: previous.revision,
      monitor: new TaskMonitor({ load: async () => { throw new Error('No fixture session'); }, notifier: { publish: async () => ({ status: 'disabled' }) } }) };
    globalThis[key] = shared;
    t.after(() => { shared.controller.abort(); delete globalThis[key]; });
    let methods;
    const stop = await (await plugin(previous.next)).setup({ rpc: { register: async (_, value) => { methods = value; } } });
    t.after(stop);
    assert.equal((await methods.health()).version, previous.next);
    assert.equal(controller.signal.aborted, true);
    assert.equal((await methods.health()).references, 2);
  });
}

test('a stale location cannot downgrade a newer process-owned public release', async (t) => {
  const shared = { refs: 1, controller: new AbortController(), build: '1.0.1', revision: 4 };
  globalThis[key] = shared;
  t.after(() => { shared.controller.abort(); delete globalThis[key]; });
  let methods;
  const stop = await (await plugin('1.0.0')).setup({ rpc: { register: async (_, value) => { methods = value; } } });
  t.after(stop);
  assert.equal((await methods.health()).version, '1.0.1');
  assert.equal(shared.controller.signal.aborted, false);
});

test('invalid configuration rejects plugin setup without a detached monitor failure', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'lifecycle-invalid-'));
  const path = join(directory, 'config.json');
  await writeFile(path, '{malformed');
  const previous = process.env.HERDR_LIFECYCLE_CONFIG;
  process.env.HERDR_LIFECYCLE_CONFIG = path;
  t.after(async () => {
    if (previous === undefined) delete process.env.HERDR_LIFECYCLE_CONFIG;
    else process.env.HERDR_LIFECYCLE_CONFIG = previous;
    globalThis[key]?.controller.abort();
    delete globalThis[key];
    await rm(directory, { recursive: true, force: true });
  });
  await assert.rejects((await plugin('1.0.9')).setup({ rpc: { register: async () => {} } }),
    { message: 'Invalid lifecycle configuration' });
  assert.equal(globalThis[key], undefined);
});
