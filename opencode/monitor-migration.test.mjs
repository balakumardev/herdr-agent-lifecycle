import assert from 'node:assert/strict';
import { test } from 'node:test';
import { TaskMonitor } from './monitor.mjs';

function snapshot() {
  const root = { id: 'ses_root', title: 'Task', outcome: 'succeeded', time: { created: 1, idle: 100 }, location: { directory: '/project' } };
  return { root, sessions: [root], active: [], inbox: [], forms: [], permissions: [], shells: [],
    lastAssistant: { id: 'msg_final', type: 'assistant', finish: 'stop', time: { completed: 99 }, content: [{ type: 'text', text: 'Final root answer.' }] } };
}

const success = { type: 'session.execution.succeeded', data: { sessionID: 'ses_root' }, created: 100 };
const flush = monitor => Promise.all([...monitor.publishing.values()]);

test('upgrading the live monitor retains a pending completion and its retry deadline', async () => {
  let now = 1000;
  const load = async () => snapshot();
  const old = new TaskMonitor({ now: () => now, load, notifier: { publish: async () => ({ status: 'failed', retryAt: 31_000 }) } });
  old.observe(success);
  await old.inspect('ses_root');
  await flush(old);
  const requests = [];
  const current = new TaskMonitor({ previous: old, now: () => now, load, notifier: { publish: async message => {
    requests.push(message); return { status: 'sent' };
  } } });
  await current.refresh();
  await flush(current);
  assert.equal(requests.length, 0);
  now = 31_000;
  await current.refresh();
  await flush(current);
  assert.deepEqual(requests.map(request => request.message), ['Final root answer.']);
});

test('upgrading revokes an old transport admission and leaves historical completions silent', async () => {
  let isCurrent;
  let finish;
  const waiting = new Promise(resolve => { finish = resolve; });
  const load = async () => snapshot();
  const old = new TaskMonitor({ load, notifier: { publish: async (_message, options) => {
    isCurrent = options.isCurrent;
    await waiting;
    return { status: 'cancelled' };
  } } });
  old.observe(success);
  await old.inspect('ses_root');
  assert.equal(isCurrent(), true);
  new TaskMonitor({ previous: old, load, notifier: { publish: async () => ({ status: 'sent' }) } });
  try { assert.equal(isCurrent(), false); } finally { finish(); await flush(old); }

  let requests = 0;
  const notifier = { publish: async () => { requests++; return { status: 'sent' }; } };
  const historical = new TaskMonitor({ load, notifier });
  await historical.inspect('ses_root');
  const replacement = new TaskMonitor({ previous: historical, load, notifier });
  await replacement.refresh();
  await flush(replacement);
  assert.equal(requests, 0);
});
