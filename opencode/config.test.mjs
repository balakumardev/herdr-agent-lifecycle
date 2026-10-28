import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from './config.mjs';
import { createNotifier } from './notify.mjs';

function fixture(t, value) {
  const home = mkdtempSync(join(tmpdir(), 'lifecycle-config-'));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const path = join(home, 'settings.json');
  if (value !== undefined) writeFileSync(path, typeof value === 'string' ? value : JSON.stringify(value));
  return { home, env: { HERDR_LIFECYCLE_CONFIG: path }, path };
}

test('an unconfigured install is strict and cannot publish notifications', async (t) => {
  const config = loadConfig(fixture(t));
  assert.equal(config.claude.allowIdleTeammates, false);
  assert.equal(config.opencode.command, 'opencode');
  let requests = 0;
  const notifier = createNotifier({ config, fetchImpl: async () => { requests++; return new Response(null); } });
  assert.equal((await notifier.publish({ key: 'root:done', title: 'Done', message: 'Final' })).status, 'disabled');
  assert.equal(requests, 0);
});

test('explicit public configuration supplies the endpoint, topic and launch command', async (t) => {
  const f = fixture(t, { notifications: { enabled: true, topic: 'fixture-topic', endpoint: 'https://notify.example.test/base' },
    opencode: { command: '/Applications/OpenCode CLI/opencode' }, claude: { allowIdleTeammates: true } });
  const config = loadConfig(f);
  assert.equal(config.opencode.command, '/Applications/OpenCode CLI/opencode');
  assert.equal(config.claude.allowIdleTeammates, true);
  const requests = [];
  const notifier = createNotifier({ config, stateDirectory: join(f.home, 'state'), fetchImpl: async (url, init) => {
    requests.push([String(url), init.body]); return new Response(null, { status: 200 });
  } });
  assert.equal((await notifier.publish({ key: 'root:done', title: 'Done', message: 'Final reply' })).status, 'sent');
  assert.deepEqual(requests, [['https://notify.example.test/base/fixture-topic', 'Final reply']]);
});

test('XDG configuration is honored without requiring a Claude dotfile', (t) => {
  const f = fixture(t);
  const directory = join(f.home, 'config', 'herdr-agent-lifecycle');
  mkdirSync(directory, { recursive: true });
  writeFileSync(join(directory, 'config.json'), '{"opencode":{"command":"opencode2"}}');
  assert.equal(loadConfig({ home: f.home, env: { XDG_CONFIG_HOME: join(f.home, 'config') } }).opencode.command, 'opencode2');
});

for (const invalid of ['{not-json', [], { claude: { allowIdleTeammates: 'true' } },
  { notifications: { enabled: 'false' } }, { notifications: { enabled: true } },
  { notifications: { enabled: true, topic: '../private' } },
  { notifications: { endpoint: 'https://user:PRIVATE@notify.example.test' } },
  { notifications: { endpoint: 'file:///private' } }, { opencode: { command: '' } }]) {
  test(`invalid configuration is rejected without leaking values: ${JSON.stringify(invalid).slice(0, 35)}`, (t) => {
    assert.throws(() => loadConfig(fixture(t, invalid)), { message: 'Invalid lifecycle configuration' });
  });
}
