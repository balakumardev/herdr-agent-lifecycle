import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { EventEmitter } from 'node:events';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('the V2 Claude compatibility bridge leaves notifications to the lifecycle integration', async () => {
  const legacy = await readFile(new URL('../tests/fixtures/legacy-bridge.js', import.meta.url), 'utf8');
  const source = execFileSync('python3', ['-c',
    'import runpy,sys; print(runpy.run_path(sys.argv[1])["patch_bridge"](sys.stdin.read()), end="")',
    fileURLToPath(new URL('../scripts/install-opencode.py', import.meta.url)),
  ], { input: legacy, encoding: 'utf8' });
  const spawned = [];
  const oldSpawn = globalThis.__lifecycleTestSpawn;
  globalThis.__lifecycleTestSpawn = (command) => {
    spawned.push(command);
    const child = new EventEmitter();
    child.unref = () => {};
    return child;
  };
  const oldWorktree = process.env.WORKTREE_MGR_TERMINAL_ID;
  delete process.env.WORKTREE_MGR_TERMINAL_ID;
  let stop;
  try {
    // Only the process boundary is replaced; the existing event adapter, session
    // lookup, completed-message selection, and attention gate execute for real.
    const instrumented = source.replace('import { spawn } from "node:child_process";', 'const spawn = globalThis.__lifecycleTestSpawn;');
    const plugin = (await import(`data:text/javascript;base64,${Buffer.from(instrumented).toString('base64')}`)).default;
    stop = await plugin.setup({
      options: {}, location: { directory: '/project' },
      session: {
        hook: async () => {},
        get: async () => ({ id: 'ses_test', title: 'Diagnostic task' }),
        context: async () => [{ id: 'msg_test', type: 'assistant', time: { completed: 10 }, content: [{ type: 'text', text: 'Completed.' }] }],
      },
      tool: { hook: async () => {} }, shell: { hook: async () => {} },
      event: { subscribe: async function* () { yield { type: 'session.execution.succeeded', data: { sessionID: 'ses_test' } }; } },
    });
    await new Promise(setImmediate);
    assert.deepEqual(spawned, [], 'the compatibility bridge must not launch its own curl or desktop notifier');
  } finally {
    stop?.();
    globalThis.__lifecycleTestSpawn = oldSpawn;
    if (oldWorktree === undefined) delete process.env.WORKTREE_MGR_TERMINAL_ID;
    else process.env.WORKTREE_MGR_TERMINAL_ID = oldWorktree;
  }
});
