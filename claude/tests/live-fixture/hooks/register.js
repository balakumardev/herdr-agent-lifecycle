const calls = new Map();
const trace = [];
let childFinished = false;
export function register(on) {
  on('model.complete', () => ({ deny: 'The local fixture must not make model requests.' }));
  on('model.classify', () => ({ deny: 'The local fixture must not make model requests.' }));
  on('model.fork', () => ({ deny: 'The local fixture must not make model requests.' }));
  on('turn.complete', async ($, e, next) => {
    const result = await next(e);
    if (e.agentId) childFinished = true;
    return result;
  });
  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const result = await next(e);
    trace.push({ action: 'fixture_agent_result', result });
    const path = await $.env.get('LIFECYCLE_PROBE_TRACE');
    if (path) await $.fs.write(path, JSON.stringify(trace));
    return result;
  });
  // Supply a local model stream. No model request is made by this fixture.
  on('turn.step', async function* ($, e) {
    const mode = await $.env.get('LIFECYCLE_PROBE_MODE');
    const actor = e.agentId || 'root';
    const count = calls.get(actor) || 0;
    calls.set(actor, count + 1);
    let uses = [];
    if (count === 0 && mode === 'background-shell') {
      uses = [{ name: 'Bash', input: { command: 'sleep 3', run_in_background: true, description: 'Local lifecycle check' } }];
    } else if (count === 0 && (mode === 'background-agent' || mode === 'named-teammate')) {
      uses = e.agentId
        ? [{ name: 'Bash', input: { command: 'sleep 2', description: 'Local child lifecycle check' } }]
        : [{ name: 'Agent', input: { prompt: 'Run the local lifecycle fixture.', description: 'Local lifecycle child check', subagent_type: 'general-purpose', run_in_background: true, ...(mode === 'named-teammate' ? { name: 'lifecycle-fixture-helper' } : {}) } }];
    }
    const interim = !e.agentId && ((mode === 'named-teammate' && !childFinished)
      || (mode.startsWith('background-') && count === 1) || (mode === 'stop-veto' && count === 0));
    const answer = uses.length ? '' : interim ? 'Interim reply: waiting for the remaining work.'
      : e.agentId ? 'Child reply: my delegated work is complete.' : 'Lifecycle fixture complete.';
    if (uses.length) {
      yield { kind: 'tool', index: 0, id: `tool_lifecycle_${actor}`, name: uses[0].name };
      yield { kind: 'input', index: 0, json: JSON.stringify(uses[0].input) };
    } else {
      yield { kind: 'text', index: 0, text: answer };
    }
    const stopReason = uses.length ? 'tool_use' : 'end_turn';
    yield { kind: 'stop', stopReason, usage: null };
    return { turnId: e.turnId, index: e.index, answer, toolUses: uses, stopReason, usage: null };
  }).catch(async function* ($, e) {
    const answer = 'Local fixture setup failed; no model request was made.';
    yield { kind: 'text', index: 0, text: answer };
    yield { kind: 'stop', stopReason: 'end_turn', usage: null };
    return { turnId: e.turnId, index: e.index, answer, toolUses: [], stopReason: 'end_turn', usage: null };
  });
  on('process.run', async ($, e, next) => {
    const result = await next(e);
    if (e.argv?.some(arg => arg.endsWith('/runtime.py'))) {
      const packet = JSON.parse(e.init.stdin);
      trace.push({ action: packet.action, result: JSON.parse(result.value.stdout) });
      const path = await $.env.get('LIFECYCLE_PROBE_TRACE');
      if (path) await $.fs.write(path, JSON.stringify(trace));
    }
    return result;
  });
}
