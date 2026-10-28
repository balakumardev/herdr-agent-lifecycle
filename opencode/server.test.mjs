import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { TaskMonitor } from './monitor.mjs';
import { Effect, JsonSchema, Schema, SchemaRepresentation } from 'effect';

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

test('a partial live upgrade remains valid through an older location RPC registration', async (t) => {
  const root = { id: 'ses_root', title: 'Task', outcome: 'succeeded', location: { directory: '/project' }, time: { idle: 100 } };
  const snapshot = { root, sessions: [root], active: [], inbox: [], forms: [], permissions: [], shells: [],
    lastAssistant: { id: 'msg_final', type: 'assistant', finish: 'stop', time: { completed: 99 }, content: [{ type: 'text', text: 'Finished.' }] } };
  const shared = { refs: 1, controller: new AbortController(), build: '1.0.2', revision: 4,
    monitor: new TaskMonitor({ load: async () => structuredClone(snapshot), notifier: { publish: async () => ({ status: 'disabled' }) } }) };
  globalThis[key] = shared;
  t.after(() => { shared.controller.abort(); delete globalThis[key]; });
  await shared.monitor.inspect(root.id);
  let oldDefinition;
  let oldMethods;
  t.after(await (await plugin('1.0.2')).setup({ rpc: { register: async (definition, methods) => {
    oldDefinition = structuredClone(definition);
    // Existing v1.0.2 locations keep this schema when the shared monitor upgrades.
    oldDefinition.methods.snapshot.output.properties.state.enum = ['working', 'blocked', 'idle'];
    oldMethods = methods;
  } } }));
  t.after(await (await plugin('1.0.3')).setup({ rpc: { register: async () => {} } }));
  let release;
  shared.monitor.load = () => new Promise(resolve => { release = resolve; });
  const pending = oldMethods.snapshot({ sessionID: root.id });
  shared.monitor.observe({ type: 'session.inbox.enqueued', data: { sessionID: root.id } });
  release(structuredClone(snapshot));
  const result = await pending;
  assert.equal((await oldMethods.health()).version, '1.0.3');
  const codec = Schema.make(SchemaRepresentation.fromJsonSchemaDocument(
    JsonSchema.fromSchemaDraft2020_12(oldDefinition.methods.snapshot.output)).ast);
  await assert.doesNotReject(() => Effect.runPromise(Schema.decodeUnknownEffect(codec)(result)),
    'the retained registration must not turn a refreshing snapshot into rpc.invalid_output');
  assert.equal(result.reason, 'refreshing');
  assert.equal(result.complete, false);
  assert.equal(result.completionKey, undefined);
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
