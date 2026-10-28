import assert from 'node:assert/strict';
import { test } from 'node:test';
import { classifyTask, loadTask } from './lifecycle.mjs';

function snapshot(overrides = {}) {
  const root = { id: 'ses_root', title: 'Example task', outcome: 'succeeded', time: { created: 1, idle: 100 }, location: { directory: '/project' } };
  return {
    root, sessions: [root], active: [], inbox: [], forms: [], permissions: [], shells: [],
    lastAssistant: { id: 'msg_final', type: 'assistant', finish: 'stop', time: { completed: 99 }, content: [{ type: 'text', text: 'Finished.' }] },
    ...overrides,
  };
}

test('an idle parent stays working while a child runs in another worktree', () => {
  const input = snapshot();
  input.sessions.push({ id: 'ses_child', parentID: 'ses_root', time: { created: 10 }, location: { directory: '/another-worktree' } });
  input.active = ['ses_child'];
  const result = classifyTask(input);
  assert.equal(result.state, 'working');
  assert.equal(result.complete, false);
  assert.equal(result.rootID, 'ses_root');
  assert.equal(result.activeCount, 1);
});

test('last-child completion waits for a new parent response, including across reload', () => {
  const input = snapshot();
  input.sessions.push({ id: 'ses_child', parentID: 'ses_root', outcome: 'succeeded', time: { created: 10, idle: 110 } });
  assert.equal(classifyTask(input).reason, 'awaiting-parent');
  assert.equal(classifyTask(input).complete, false);
  input.root.time.idle = 130;
  input.lastAssistant.time.completed = 129;
  assert.equal(classifyTask(input).complete, true);
});

test('nested subagents contribute to activity without changing the root identity', () => {
  const input = snapshot();
  input.sessions.push({ id: 'ses_child', parentID: 'ses_root', time: { created: 10, idle: 20 } }, { id: 'ses_grandchild', parentID: 'ses_child', time: { created: 15 } });
  input.active = ['ses_grandchild'];
  assert.equal(classifyTask(input).state, 'working');
  assert.equal(classifyTask(input).rootID, 'ses_root');
});

test('queued continuation prevents done even between executions', () => {
  assert.equal(classifyTask(snapshot({ inbox: [{ id: 'msg_resume', sessionID: 'ses_root', type: 'synthetic' }] })).complete, false);
  assert.equal(classifyTask(snapshot({ inbox: [{ id: 'msg_resume', sessionID: 'ses_root', type: 'synthetic' }] })).state, 'working');
});

test('a real pending question takes precedence over busy siblings', () => {
  const input = snapshot({ active: ['ses_root'], forms: [{ id: 'frm_question', sessionID: 'ses_root', title: 'Choose an option', fields: [{ type: 'string', key: 'answer' }] }] });
  const result = classifyTask(input);
  assert.equal(result.state, 'blocked');
  assert.deepEqual(result.questionIDs, ['frm_question']);
  assert.equal(result.complete, false);
  input.forms = [];
  assert.equal(classifyTask(input).state, 'working');
});

test('permission prompts block Herdr without being classified as question notifications', () => {
  const result = classifyTask(snapshot({ permissions: [{ id: 'perm_1', sessionID: 'ses_root' }] }));
  assert.equal(result.state, 'blocked');
  assert.deepEqual(result.questionIDs, []);
});

test('only a final successful root response is genuinely done', () => {
  const result = classifyTask(snapshot());
  assert.equal(result.state, 'idle');
  assert.equal(result.complete, true);
  assert.equal(result.completionKey, 'done:ses_root:msg_final');
  const input = snapshot();
  input.lastAssistant.finish = 'tool-calls';
  assert.equal(classifyTask(input).complete, false);
  input.lastAssistant.finish = 'stop';
  input.lastAssistant.content.push({ type: 'tool', name: 'shell', state: { status: 'running' } });
  assert.equal(classifyTask(input).complete, false);
});

test('running owned background commands prevent done', () => {
  assert.equal(classifyTask(snapshot({ shells: [{ id: 'sh_build', status: 'running', metadata: { sessionID: 'ses_root' } }] })).state, 'working');
});

test('failed or interrupted runs never masquerade as successful completion', () => {
  for (const outcome of ['failed', 'interrupted']) {
    const input = snapshot();
    input.root.outcome = outcome;
    assert.equal(classifyTask(input).state, 'blocked');
    assert.equal(classifyTask(input).complete, false);
  }
});

test('an empty session is idle without a completion notification', () => {
  const input = snapshot();
  delete input.root.outcome;
  delete input.root.time.idle;
  input.lastAssistant = undefined;
  assert.equal(classifyTask(input).state, 'idle');
  assert.equal(classifyTask(input).complete, false);
});

test('background work settling after the last root answer requires continuation', () => {
  assert.equal(classifyTask(snapshot({ settledAfter: 120 })).reason, 'awaiting-parent');
});

test('an authoritative exited-shell timestamp preserves the parent-continuation barrier', () => {
  const input = snapshot({ shells: [{ id: 'sh_build', status: 'exited', metadata: { sessionID: 'ses_root' }, time: { started: 10, completed: 120 } }] });
  assert.equal(classifyTask(input).complete, false);
  assert.equal(classifyTask(input).reason, 'awaiting-parent');
});

// An API fixture at the external boundary. Changing pagination, omitting a
// descendant, or using the legacy wrapped-message shape must change the result.
function clientFixture() {
  const root = snapshot().root;
  const child = { id: 'ses_child', parentID: 'ses_root', outcome: 'succeeded', time: { created: 10, idle: 20 }, location: { directory: '/other' } };
  const grandchild = { id: 'ses_grandchild', parentID: 'ses_child', time: { created: 15 }, location: { directory: '/other' } };
  const unrelated = { id: 'ses_other_root', time: { created: 2 }, location: { directory: '/elsewhere' } };
  const infos = new Map([root, child, grandchild, unrelated].map((s) => [s.id, s]));
  return {
    debug: { location: { list: async () => ['/project', '/other', '/elsewhere'].map((directory) => ({ directory })) } },
    session: {
      get: async ({ sessionID }) => { assert(infos.has(sessionID)); return infos.get(sessionID); },
      list: async ({ parentID, cursor }) => {
        if (parentID === 'ses_root' && !cursor) return { data: [child], cursor: { next: 'page-two' } };
        if (parentID === 'ses_child' && !cursor) return { data: [grandchild], cursor: {} };
        return { data: [], cursor: {} };
      },
      active: async () => ({ ses_grandchild: { type: 'running' }, ses_other_root: { type: 'running' } }),
      inbox: { list: async () => [] },
      form: { list: async ({ sessionID }) => sessionID === 'ses_grandchild' ? [{ id: 'frm_child', sessionID, title: 'Question', fields: [] }] : [] },
    },
    permission: { list: async () => [] },
    message: { list: async ({ sessionID, order, type, limit }) => {
      assert.equal(sessionID, 'ses_root'); assert.equal(order, 'desc'); assert.equal(type, 'assistant'); assert.equal(limit, 1);
      return { data: [snapshot().lastAssistant], cursor: {} };
    } },
    shell: { list: async () => ({ data: [], location: { directory: '/project' } }) },
  };
}

test('snapshot resolves ancestry, pagination, nested worktrees and excludes unrelated running roots', async () => {
  const input = await loadTask(clientFixture(), 'ses_child');
  assert.equal(input.root.id, 'ses_root');
  assert.deepEqual(input.sessions.map((s) => s.id).sort(), ['ses_child', 'ses_grandchild', 'ses_root']);
  assert.deepEqual(input.active, ['ses_grandchild']);
  const result = classifyTask(input);
  assert.equal(result.state, 'blocked');
  assert.deepEqual(result.questionIDs, ['frm_child']);
});

test('a child inspection reads only the latest root assistant, never a child or earlier final answer', async () => {
  const client = clientFixture();
  client.session.active = async () => ({});
  client.session.form.list = async () => [];
  const prior = { ...snapshot().lastAssistant, id: 'msg_prior', time: { completed: 40 }, content: [{ type: 'text', text: 'Earlier root answer.' }] };
  const interim = { ...prior, id: 'msg_interim', finish: 'tool-calls', time: { completed: 90 }, content: [{ type: 'text', text: 'Still working.' }] };
  const child = { ...prior, id: 'msg_child', time: { completed: 95 }, content: [{ type: 'text', text: 'Private child result.' }] };
  const rootMessages = [interim, prior];
  client.message.list = async ({ sessionID, type, order, limit }) => {
    assert.equal(type, 'assistant'); assert.equal(order, 'desc'); assert.equal(limit, 1);
    return { data: (sessionID === 'ses_root' ? rootMessages : [child]).slice(0, limit), cursor: { next: 'older-page' } };
  };
  const first = await loadTask(client, 'ses_child');
  assert.equal(first.lastAssistant.id, 'msg_interim');
  assert.equal(classifyTask(first).complete, false);
  rootMessages.unshift(snapshot().lastAssistant);
  const final = await loadTask(client, 'ses_child');
  assert.equal(final.lastAssistant.id, 'msg_final');
  assert.equal(classifyTask(final).complete, true);
});

test('a failed authoritative read cannot turn into an empty successful snapshot', async () => {
  const client = clientFixture();
  client.session.form.list = async () => { throw new Error('temporarily unavailable'); };
  await assert.rejects(loadTask(client, 'ses_root'), /temporarily unavailable/);
});

test('snapshot refetches a known shell after it disappears from the running-only list', async () => {
  const client = clientFixture();
  client.session.active = async () => ({});
  client.session.form.list = async () => [];
  const shell = { id: 'sh_build', status: 'running', metadata: { sessionID: 'ses_root' }, location: { directory: '/project' }, time: { started: 10 } };
  client.shell.get = async ({ id, location }) => {
    assert.equal(id, 'sh_build'); assert.deepEqual(location, { directory: '/project' });
    return { location, data: { ...shell, status: 'exited', time: { started: 10, completed: 120 } } };
  };
  const input = await loadTask(client, 'ses_root', { knownShells: [shell] });
  assert.equal(classifyTask(input).reason, 'awaiting-parent');
});

function retiredLocationFixture() {
  const client = clientFixture();
  const root = snapshot().root;
  const retired = { id: 'ses_retired', parentID: root.id, outcome: 'succeeded', time: { created: 15, idle: 120 },
    location: { directory: '/removed-temporary-worktree' } };
  const get = client.session.get;
  client.session.get = async (input) => input.sessionID === root.id ? root : input.sessionID === retired.id ? retired : get(input);
  const list = client.session.list;
  client.session.list = async (input) => {
    const page = await list(input);
    return input.parentID === root.id && !input.cursor ? { ...page, data: [...page.data, retired] } : page;
  };
  const unavailable = () => { throw new Error('FileSystem.realPath: NotFound: removed-temporary-worktree'); };
  client.session.form.list = async ({ sessionID }) => sessionID === retired.id ? unavailable() : [];
  client.permission.list = async ({ sessionID }) => sessionID === retired.id ? unavailable() : [];
  client.shell.list = async ({ location }) => location.directory === retired.location.directory ? unavailable() : { data: [], location };
  return { client, root, retired };
}

test('a retired worktree cannot hide live descendants or erase the parent continuation barrier', async () => {
  const { client, root } = retiredLocationFixture();
  const running = await loadTask(client, root.id);
  assert.equal(classifyTask(running).state, 'working');
  assert.equal(classifyTask(running).activeCount, 1);
  assert.ok(running.sessions.some((session) => session.id === 'ses_retired'));
  client.session.active = async () => ({});
  assert.equal(classifyTask(await loadTask(client, root.id)).reason, 'awaiting-parent');
  root.time.idle = 140;
  client.message.list = async () => ({ data: [{ ...snapshot().lastAssistant, time: { completed: 139 } }], cursor: {} });
  assert.equal(classifyTask(await loadTask(client, root.id)).complete, true);
});

test('queued input in an unloaded child is still pending work', async () => {
  const { client, root, retired } = retiredLocationFixture();
  client.session.active = async () => ({});
  root.time.idle = 140;
  client.message.list = async () => ({ data: [{ ...snapshot().lastAssistant, time: { completed: 139 } }], cursor: {} });
  client.session.inbox.list = async ({ sessionID }) => sessionID === retired.id ? [{ id: 'input_waiting', sessionID, type: 'synthetic' }] : [];
  const result = classifyTask(await loadTask(client, root.id));
  assert.equal(result.state, 'working');
  assert.equal(result.pendingCount, 1);
  assert.equal(result.complete, false);
});

test('an unavailable runtime-location registry is not evidence that no work remains', async () => {
  const client = clientFixture();
  client.debug.location.list = async () => { throw new Error('location registry unavailable'); };
  await assert.rejects(loadTask(client, 'ses_root'), /location registry unavailable/);
});

test('a live child cannot be treated as an empty unloaded runtime', async () => {
  const client = clientFixture();
  client.debug.location.list = async () => [{ directory: '/project' }];
  await assert.rejects(loadTask(client, 'ses_root'), /Active task location/);
});

test('a child runtime appearing during a snapshot is reread before completion', async () => {
  const { client, root, retired } = retiredLocationFixture();
  client.session.active = async () => ({});
  let reads = 0;
  client.debug.location.list = async () => [
    { directory: '/project' }, { directory: '/other' },
    ...(reads++ ? [{ directory: retired.location.directory }] : []),
  ];
  client.session.form.list = async ({ sessionID }) => sessionID === retired.id ? [{ id: 'frm_resumed', sessionID, title: 'Choose', fields: [] }] : [];
  client.permission.list = async () => [];
  client.shell.list = async ({ location }) => ({ data: [], location });
  await assert.rejects(loadTask(client, root.id), /Task runtime locations changed/);
  const result = classifyTask(await loadTask(client, root.id));
  assert.equal(result.state, 'blocked');
  assert.deepEqual(result.questionIDs, ['frm_resumed']);
});

function failingRuntimeFixture() {
  const fixture = retiredLocationFixture();
  const { client, root, retired } = fixture;
  const live = [{ directory: '/project' }, { directory: '/other' }];
  let reads = 0;
  client.debug.location.list = async () => reads++ === 0 ? [...live, retired.location] : live;
  root.time.idle = 140;
  client.message.list = async () => ({ data: [{ ...snapshot().lastAssistant, time: { completed: 139 } }], cursor: {} });
  return { ...fixture, live };
}

test('a listed but failed runtime is reconciled after all its reads settle', async () => {
  const { client, root, retired } = failingRuntimeFixture();
  const permission = client.permission.list;
  client.permission.list = async (input) => {
    if (input.sessionID === retired.id) await new Promise(setImmediate);
    return permission(input);
  };
  const input = await loadTask(client, root.id);
  const result = classifyTask(input);
  assert.equal(result.state, 'working');
  assert.equal(result.activeCount, 1);
  assert.ok(input.sessions.some((session) => session.id === retired.id));
});

test('reconciled runtime failure preserves queued input and parent settlement ordering', async () => {
  for (const queued of [false, true]) {
    const { client, root, retired } = failingRuntimeFixture();
    client.session.active = async () => ({});
    if (queued) client.session.inbox.list = async ({ sessionID }) => sessionID === retired.id ? [{ id: 'input_retired', sessionID }] : [];
    else root.time.idle = 100;
    const result = classifyTask(await loadTask(client, root.id));
    assert.equal(result.complete, false);
    assert.equal(result.reason, queued ? 'running' : 'awaiting-parent');
    assert.equal(result.pendingCount, queued ? 1 : 0);
  }
  const { client, root } = failingRuntimeFixture();
  client.session.active = async () => ({});
  assert.equal(classifyTask(await loadTask(client, root.id)).complete, true);
});

test('a failed runtime that remains registered cannot be treated as empty', async () => {
  const { client, root, retired, live } = failingRuntimeFixture();
  client.session.active = async () => ({});
  client.debug.location.list = async () => [...live, retired.location];
  await assert.rejects(loadTask(client, root.id), /removed-temporary-worktree/);
});

test('a failed runtime reappearing after reconciliation invalidates completion', async () => {
  const { client, root, retired, live } = failingRuntimeFixture();
  client.session.active = async () => ({});
  let reads = 0;
  client.debug.location.list = async () => reads++ === 1 ? live : [...live, retired.location];
  await assert.rejects(loadTask(client, root.id), /Task runtime locations changed/);
});

test('an active owner or successful runtime read contradicts failure reconciliation', async (t) => {
  for (const mode of ['active owner', 'successful form read', 'known running shell']) {
    await t.test(mode, async () => {
      const { client, root, retired } = failingRuntimeFixture();
      client.session.active = async () => ({});
      const knownShells = [];
      if (mode === 'active owner') {
        let reads = 0;
        client.session.active = async () => reads++ === 0 ? { [retired.id]: { type: 'running' } } : {};
      } else if (mode === 'successful form read') {
        client.session.form.list = async () => [];
      } else {
        knownShells.push({ id: 'sh_unresolved', status: 'running', metadata: { sessionID: retired.id }, location: retired.location });
        client.shell.get = async () => { throw new Error('Unresolved running shell'); };
      }
      await assert.rejects(loadTask(client, root.id, { knownShells }));
    });
  }
});

test('failed runtime reconciliation requires a successful registry read', async () => {
  const { client, root } = failingRuntimeFixture();
  const list = client.debug.location.list;
  let reads = 0;
  client.debug.location.list = async () => {
    if (reads++) throw new Error('registry unavailable during reconciliation');
    return list();
  };
  await assert.rejects(loadTask(client, root.id), /registry unavailable during reconciliation/);
});

test('cancellation cannot be mistaken for an absent runtime', async () => {
  const { client, root, retired } = failingRuntimeFixture();
  const controller = new AbortController();
  const forms = client.session.form.list;
  client.session.form.list = async (input) => {
    if (input.sessionID === retired.id) controller.abort();
    return forms(input);
  };
  await assert.rejects(loadTask(client, root.id, { signal: controller.signal }), { name: 'AbortError' });
});

test('an unrelated active root can retain this task\'s detached location runtime', async (t) => {
  for (const phase of ['initially absent', 'failed replacement']) {
    await t.test(phase, async () => {
      const { client, root, retired, live } = failingRuntimeFixture();
      // The unrelated root is active in /elsewhere. The retired child used the
      // same graph; that graph can still own its background commands after detach.
      retired.location.directory = '/elsewhere';
      client.session.active = async () => ({ ses_other_root: { type: 'running' } });
      if (phase === 'initially absent') client.debug.location.list = async () => live;
      await assert.rejects(loadTask(client, root.id));
    });
  }
});
