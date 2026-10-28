import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { analyzeCommits } from '@semantic-release/commit-analyzer';
import { generateNotes } from '@semantic-release/release-notes-generator';

const require = createRequire(import.meta.url);
const config = require('../release.config.cjs');
const options = config.plugins.find(([name]) => name === '@semantic-release/commit-analyzer')[1];
const cwd = fileURLToPath(new URL('..', import.meta.url));

for (const [message, release] of [
  ['feat: observe additional background work', 'minor'],
  ['fix: preserve a pending parent response', 'patch'],
  ['feat!: require a newer host API', 'major'],
  ['refactor!: replace the RPC contract', 'major'],
  ['perf!: replace notification format', 'major'],
  ['chore: update the host requirement\n\nBREAKING CHANGE: older clients are no longer supported', 'major'],
  ['docs: explain verified installation and rollback', 'patch'],
  ['ci: verify packaged installations before releasing', 'patch'],
  ['chore: refresh pinned dependencies', 'patch'],
  ['refactor: share the notification parser', 'patch'],
]) {
  test(`a real conventional commit selects the ${release} release: ${message}`, async () => {
    assert.equal(await analyzeCommits(options, { cwd, commits: [{ message, hash: 'abcdef0123' }], logger: { log() {} } }), release);
  });
}

test('an already released branch with no new commits produces no duplicate release', async () => {
  assert.equal(await analyzeCommits(options, { cwd, commits: [], logger: { log() {} } }), null);
});

test('the pinned changelog preset renders release notes with the actual writer', async () => {
  const preset = config.plugins.find(([name]) => name === '@semantic-release/release-notes-generator')[1];
  const notes = await generateNotes(preset, {
    cwd, options: { repositoryUrl: 'https://github.com/balakumardev/herdr-agent-lifecycle.git' },
    lastRelease: { gitTag: 'v1.0.0' }, nextRelease: { version: '1.0.1', gitTag: 'v1.0.1' },
    commits: [
      { hash: 'abc123456789', message: 'fix: preserve pending work' },
      { hash: 'def123456789', message: 'ci: verify packaged installation' },
    ],
  });
  assert.match(notes, /preserve pending work/);
  assert.match(notes, /verify packaged installation/);
  assert.match(notes, /compare\/v1\.0\.0\.\.\.v1\.0\.1/);
});
