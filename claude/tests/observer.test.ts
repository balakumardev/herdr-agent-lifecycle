import { expect, mock, test } from 'claude-code/testing';

function setup(on, options = {}) {
  const packets = [];
  const statuses = [];
  const clock = mock.clock(on, { now: 1000 });
  mock.env(on, {});
  on('env.set', () => ({ value: undefined }));
  on('session.id', () => ({ value: 'session-one' }));
  on('session.start', () => ({ cwd: '/work' }));
  on('command.register', () => ({ value: undefined }));
  on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined }; });
  on('settings.read', () => options.settingsFail ? { deny: 'unavailable' } : { value: { hooks: {}, enabledPlugins: {} } });
  on('agent.list', () => options.readAgents ? options.readAgents() : { value: options.agents || [] });
  on('process.run', ($, e) => {
    const packet = JSON.parse(e.init.stdin);
    packets.push(packet);
    if (options.configureFails && packet.action === 'configure') {
      return { value: { exitCode: 1, stdout: '{"error":"unavailable"}', stderr: '' } };
    }
    if (options.configureReply && packet.action === 'configure') {
      return { value: { exitCode: 0, stdout: JSON.stringify(options.configureReply(packet)), stderr: '' } };
    }
    return { value: { exitCode: 0, stdout: '{"state":"working"}', stderr: '' } };
  });
  on('turn.start', ($, e) => ({ turnId: e.turnId }));
  on('turn.complete', ($, e) => ({ text: e.answer }));
  on('classic.Stop', () => options.stopResult || {});
  on('classic.SubagentStart', () => ({}));
  on('classic.SubagentStop', () => ({}));
  on('classic.Notification', () => ({}));
  on('classic.PermissionRequest', () => ({}));
  on('prompt.submit', ($, e) => ({ text: e.text }));
  on('session.end', () => ({ sessionId: 'session-one' }));
  on('ui.render', () => ({ type: 'engine', ref: 0 }));
  return { packets, clock, statuses };
}

const START = { cwd: '/work', surface: 'terminal', isInteractive: true };
const STOP = {
  hook_event_name: 'Stop', session_id: 'session-one', prompt_id: 'prompt-1',
  transcript_path: '/work/transcript.jsonl', cwd: '/work', stop_hook_active: false,
  last_assistant_message: 'Done', background_tasks: [], session_crons: [],
};
const COMPLETE = { turnId: 'turn-1', answer: 'Done', durationMs: 5, reason: 'answer', isAborted: false };

test('completion requires the native post-turn event independently of classic mod dispatch', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.classic.Stop(STOP);
  expect(packets.map(p => p.action).includes('complete')).toBe(false);
  await $.turn.complete(COMPLETE);
  await clock.advance(600);
  expect(packets.map(p => p.action)).toContain('configure');
  expect(packets.filter(p => p.action === 'complete').length).toBe(1);
  expect(packets.find(p => p.action === 'complete').post_turn).toBe(true);
});

test('unavailable effective settings remain unknown to the runtime audit', async ($, on) => {
  const { packets } = setup(on, { settingsFail: true });
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  expect(packets.find(p => p.action === 'configure').hooks.Stop).toBe(null);
});

test('post-turn notification delivery receives the final parent reply', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete({ ...COMPLETE, answer: 'Final assistant reply for the notification.' });
  await clock.advance(600);
  expect(packets.find(p => p.action === 'complete').has_answer).toBe(true);
  expect(packets.find(p => p.action === 'complete').answer_text).toBe('Final assistant reply for the notification.');
});

test('completion reads a fresh sanitized roster after the quiet period', async ($, on) => {
  const options = { agents: [{ id: 'local-one', teammateId: 'mate-one', status: 'running' }] };
  const { packets, clock } = setup(on, options);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  await clock.advance(499);
  expect(packets.filter(p => p.action === 'complete').length).toBe(0);
  options.agents = [{ id: 'local-one', teammateId: 'mate-one', status: 'idle', name: 'SECRET-NAME', prompt: 'SECRET-PROMPT' }];
  await clock.advance(1);
  const packet = packets.find(p => p.action === 'complete');
  expect(packet.agents).toEqual([{ id: 'local-one', teammateId: 'mate-one', status: 'idle' }]);
  expect(JSON.stringify(packet).includes('SECRET')).toBe(false);
});

test('a headless completion also supplies the current roster', async ($, on) => {
  const { packets } = setup(on, { agents: [{ id: 'local-one', teammateId: 'mate-one', status: 'idle' }] });
  await $.session.start({ ...START, isInteractive: false });
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  expect(packets.find(p => p.action === 'complete').agents).toEqual([
    { id: 'local-one', teammateId: 'mate-one', status: 'idle' },
  ]);
});

test('an unavailable roster stays explicitly unknown', async ($, on) => {
  const { packets, clock } = setup(on, { readAgents: () => ({ deny: 'unavailable' }) });
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  await clock.advance(600);
  expect(packets.find(p => p.action === 'complete').agents).toBe(null);
});

test('activity during the roster read cancels completion admission', async ($, on) => {
  let finishRead;
  let beginRead;
  const reading = new Promise(resolve => { beginRead = resolve; });
  const { packets, clock } = setup(on, { readAgents: () => new Promise(resolve => {
    finishRead = resolve;
    beginRead();
  }) });
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  const admission = clock.advance(600);
  await reading;
  await $.prompt.submit({ text: 'Continue', wait: false, origin: { kind: 'task-notification' } });
  finishRead({ value: [{ id: 'local-one', teammateId: 'mate-one', status: 'idle' }] });
  await admission;
  expect(packets.filter(p => p.action === 'complete').length).toBe(0);
});

test('rebinding during the roster read cancels the old completion', async ($, on) => {
  let finishRead;
  let beginRead;
  const reading = new Promise(resolve => { beginRead = resolve; });
  const { packets, clock } = setup(on, { readAgents: () => new Promise(resolve => {
    finishRead = resolve;
    beginRead();
  }) });
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  const admission = clock.advance(600);
  await reading;
  await $.session.start(START);
  finishRead({ value: [{ id: 'local-one', teammateId: 'mate-one', status: 'idle' }] });
  await admission;
  expect(packets.filter(p => p.action === 'complete').length).toBe(0);
});

test('idle reminders are not pending questions', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.classic.Notification({ ...STOP, hook_event_name: 'Notification', notification_type: 'idle_prompt', message: 'Idle' });
  await clock.advance(1000);
  expect(packets.filter(p => p.action === 'question_begin').length).toBe(0);
});

test('an immediately answered question without a displayed dialog never notifies', async ($, on) => {
  const { packets, clock } = setup(on);
  on('tool.call', () => ({ result: { answers: { Continue: 'Yes' } } }));
  await $.session.start(START);
  await $.tool.call({ tool: 'AskUserQuestion', questions: [{ question: 'Continue?', header: 'Next', options: [{ label: 'Yes', description: 'Proceed' }, { label: 'No', description: 'Stop' }], multiSelect: false }] });
  await clock.advance(1000);
  expect(packets.filter(p => p.action === 'question_begin').length).toBe(0);
});

test('a displayed outstanding question notifies once and completion clears it', async ($, on) => {
  const { packets, clock } = setup(on);
  let finish;
  let started;
  const opened = new Promise(resolve => { started = resolve; });
  on('tool.call', ($, e) => new Promise(resolve => { finish = resolve; started(e.tool_use_id); }));
  await $.session.start(START);
  const pending = $.tool.call({ tool: 'AskUserQuestion', questions: [{ question: 'Continue?', header: 'Next', options: [{ label: 'Yes', description: 'Proceed' }, { label: 'No', description: 'Stop' }], multiSelect: false }] });
  const toolId = await opened;
  const ui = await $.ui.mount({ plugin: 'task-lifecycle', component: 'AskUserQuestion', requestId: toolId, surface: 'terminal', props: { tool: 'AskUserQuestion', questions: [] } });
  await clock.advance(800);
  expect(packets.filter(p => p.action === 'question_begin').length).toBe(1);
  await clock.advance(1000);
  expect(packets.filter(p => p.action === 'question_begin').length).toBe(1);
  finish({ result: { answers: { Continue: 'Yes' } } });
  await pending;
  expect(packets.filter(p => p.action === 'question_end').length).toBe(1);
  await ui.unmount();
});

test('child completion cannot be sent as root completion', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.turn.complete({ ...COMPLETE, agentId: 'child-1' });
  await clock.advance(1000);
  expect(packets.filter(p => p.action === 'complete').length).toBe(0);
  expect(packets.filter(p => p.action === 'child_complete').length).toBe(1);
});

test('new activity cancels a queued final response notification', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.classic.Stop(STOP);
  await $.turn.complete(COMPLETE);
  await $.prompt.submit({ text: 'Continue', wait: false, origin: { kind: 'task-notification' } });
  await clock.advance(1000);
  expect(packets.filter(p => p.action === 'complete').length).toBe(0);
});

test('an interrupted turn is explicitly non-successful', async ($, on) => {
  const { packets, clock } = setup(on);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete({ ...COMPLETE, reason: 'aborted', isAborted: true });
  await clock.advance(1000);
  expect(packets.find(p => p.action === 'complete').reason).toBe('aborted');
});

test('a failed configuration audit is not healed by the following start receipt', async ($, on) => {
  const { packets, clock } = setup(on, { configureFails: true });
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  await $.turn.complete(COMPLETE);
  await clock.advance(600);
  expect(packets.find(p => p.action === 'complete').has_answer).toBe(false);
});

test('turns share their module load and a plugin reload starts a new one', async ($, on) => {
  const { packets } = setup(on);
  await $.session.start(START);
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  const [load, turn] = packets.filter(p => p.action === 'configure');
  expect(load.phase).toBe('load');
  expect(turn.phase).toBe('turn');
  expect(typeof load.load_id).toBe('string');
  expect(turn.load_id).toBe(load.load_id);
  await $.session.start(START);
  const reloaded = packets.filter(p => p.action === 'configure').at(-1);
  expect(reloaded.phase).toBe('load');
  expect(reloaded.load_id).not.toBe(load.load_id);
});

test('untracked loaded reviews pin the reload advice until a reload clears it', async ($, on) => {
  const options = { configureReply: () => ({ configured: true, reload_required: true }) };
  const { statuses } = setup(on, options);
  await $.session.start(START);
  expect(statuses.length).toBe(1);
  expect(statuses[0]).toContain('/reload-plugins');
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  expect(statuses.length).toBe(1);
  options.configureReply = () => ({ configured: true, reload_required: false });
  await $.session.start(START);
  expect(statuses.length).toBe(2);
  expect(statuses[1]).toBe(undefined);
});

test('a failed configuration keeps the current reload advice', async ($, on) => {
  const options = { configureReply: () => ({ configured: true, reload_required: true }) };
  const { statuses } = setup(on, options);
  await $.session.start(START);
  options.configureReply = () => ({ error: 'lifecycle_unavailable' });
  await $.turn.start({ turnId: 'turn-1', text: 'Work' });
  expect(statuses.length).toBe(1);
  expect(statuses[0]).toContain('/reload-plugins');
});
