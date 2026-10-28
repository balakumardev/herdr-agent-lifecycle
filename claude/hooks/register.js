// Claude Code 2.1.292 native hooks. The final root reply is handed to the private
// notification outbox; status and receipts stay metadata-only. Preserve results.
let sessionID;
let owner;
let sequence = 0;
let revision = 0;
let healthy = false;
let interactive = false;
let rootRunning = false;
let queuedPrompts = 0;
let python = 'python3';
let chain = Promise.resolve();
let completionTimer;
const questions = new Map();

function invalidate() {
  revision += 1;
  completionTimer?.cancel();
  completionTimer = undefined;
}

async function send($, data, valid = () => true) {
  if (!sessionID || !owner) return { error: 'not_bound' };
  const packet = { ...data, session_id: sessionID, owner, seq: ++sequence };
  const operation = chain.then(async () => {
    if (!valid() || packet.owner !== owner) return { ignored: true };
    try {
      const result = await $.process.run([python, `${$.plugin.root}/runtime.py`], {
        stdin: JSON.stringify(packet), timeoutMs: 12000,
      });
      const response = JSON.parse(result.stdout);
      if (result.exitCode !== 0 || response.error || response.ignored) healthy = false;
      return response;
    } catch {
      healthy = false;
      return { error: 'transport_unavailable' };
    }
  });
  chain = operation.then(() => undefined, () => undefined);
  return operation;
}

async function admitCompletion($, packet, valid) {
  if (!valid()) return;
  let agents = null;
  if (packet.reason === 'answer') {
    try {
      const roster = await $.agent.list();
      if (Array.isArray(roster)) agents = roster.map(({ id, teammateId, status }) => ({ id, teammateId, status }));
    } catch {
      // An unavailable roster cannot release a nonempty native Stop snapshot.
    }
  }
  await send($, { ...packet, agents }, valid);
}

async function bind($, id) {
  invalidate();
  for (const question of questions.values()) question.timer?.cancel();
  questions.clear();
  sessionID = id || await $.session.id();
  owner = crypto.randomUUID();
  sequence = 0;
  rootRunning = false;
  queuedPrompts = 0;
  python = await $.env.get('CLAUDE_TASK_LIFECYCLE_PYTHON') || 'python3';
  await $.env.set('CLAUDE_TASK_LIFECYCLE_OWNER', owner);
  const reply = await send($, { action: 'bind', born_at: await $.clock.now() });
  healthy = !reply.error && !reply.ignored;
}

async function configure($) {
  let settings;
  try { settings = await $.settings.read(); } catch { settings = null; }
  return await send($, {
    action: 'configure',
    // Configuration is inspected locally, never logged or copied into state.
    hooks: { Stop: settings ? settings.hooks?.Stop || [] : null,
      SubagentStop: settings ? settings.hooks?.SubagentStop || [] : null,
      PostToolUse: settings ? settings.hooks?.PostToolUse || [] : null,
      UserPromptSubmit: settings ? settings.hooks?.UserPromptSubmit || [] : null },
    enabled_plugins: settings?.enabledPlugins || {},
  });
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    interactive = e.isInteractive;
    await bind($);
    await configure($);
    await $.command.register({ name: 'task-lifecycle-status', description: 'Show sanitized Herdr/notification lifecycle status' });
    return next(e);
  });

  on('command.run', { command: 'task-lifecycle-status' }, async ($) => {
    const result = await send($, { action: 'inspect' });
    return { text: JSON.stringify({ ...result, observerHealthy: healthy }) };
  });

  on('prompt.submit', async ($, e, next) => {
    invalidate();
    const queued = rootRunning && (e.wait || ['composer', 'bridge', 'sdk', 'plugin'].includes(e.origin?.kind));
    if (queued) queuedPrompts += 1;
    await send($, { action: 'activity', feedback_delivered: e.origin?.kind === 'task-notification' });
    const result = await next(e);
    if (queued && result.drop) queuedPrompts = Math.max(0, queuedPrompts - 1);
    return result;
  });

  on('turn.start', async ($, e, next) => {
    invalidate();
    const current = await $.session.id();
    if (current !== sessionID) await bind($, current);
    const configuration = await configure($);
    rootRunning = true;
    queuedPrompts = Math.max(0, queuedPrompts - 1);
    const result = await send($, { action: 'start', turn_id: e.turnId });
    healthy = !configuration?.error && !configuration?.ignored && !result.error && !result.ignored;
    return next(e);
  });

  on('turn.step', async function* ($, e, next) {
    invalidate();
    if (!e.agentId) await configure($);
    await send($, e.agentId ? { action: 'child_start', agent_id: e.agentId } : { action: 'activity', parent_step: true });
    return yield* next(e);
  });

  on('turn.complete', async ($, e, next) => {
    const result = await next(e);
    if (e.agentId) {
      invalidate();
      await send($, { action: 'child_complete', agent_id: e.agentId });
      return result;
    }
    rootRunning = false;
    const packet = { action: 'complete', turn_id: e.turnId, reason: e.isAborted ? 'aborted' : e.reason,
      post_turn: true, has_answer: healthy && queuedPrompts === 0 && typeof e.answer === 'string' && e.answer.trim().length > 0,
      answer_text: e.reason === 'answer' && !e.isAborted ? e.answer : undefined };
    for (const question of questions.values()) question.timer?.cancel();
    questions.clear();
    const generation = revision;
    const heldOwner = owner;
    const valid = () => revision === generation && owner === heldOwner;
    if (!interactive || packet.reason !== 'answer') {
      await admitCompletion($, packet, valid);
    } else {
      completionTimer?.cancel();
      completionTimer = $.clock.after(500, async () => {
        await admitCompletion($, packet, valid);
      });
    }
    return result;
  });

  on('tool.call', async ($, e, next) => {
    invalidate();
    await send($, { action: 'activity' });
    const id = e.tool_use_id;
    if (e.tool === 'AskUserQuestion') questions.set(id, { pending: true });
    try {
      return await next(e);
    } finally {
      const question = questions.get(id);
      if (question) {
        question.pending = false;
        question.timer?.cancel();
        questions.delete(id);
        await send($, { action: 'question_end', tool_id: id });
      }
      await send($, { action: 'permission_end', tool_id: id });
    }
  });

  on('ui.render', { component: 'AskUserQuestion' }, async ($, e, next) => {
    const drawing = await next(e);
    const question = questions.get(e.requestId);
    if (question && !question.timer) {
      const heldOwner = owner;
      question.timer = $.clock.after(750, async () => {
        await send($, { action: 'question_begin', tool_id: e.requestId },
          () => question.pending && owner === heldOwner && questions.get(e.requestId) === question);
      });
    }
    return drawing;
  });

  on('session.end', async ($, e, next) => {
    invalidate();
    for (const question of questions.values()) question.timer?.cancel();
    questions.clear();
    await send($, { action: 'end' });
    return next(e);
  });
}
