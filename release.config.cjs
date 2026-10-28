const maintenance = ['docs', 'refactor', 'perf', 'test', 'build', 'ci', 'chore', 'style', 'revert'];

module.exports = {
  branches: ['main', 'master'],
  tagFormat: 'v${version}',
  plugins: [
    ['@semantic-release/commit-analyzer', {
      preset: 'conventionalcommits',
      releaseRules: [{ breaking: true, release: 'major' }, ...maintenance.map(type => ({ type, release: 'patch' }))],
    }],
    ['@semantic-release/release-notes-generator', {
      preset: 'conventionalcommits',
      presetConfig: { types: [
        { type: 'feat', section: 'Features' },
        { type: 'fix', section: 'Fixes' },
        ...maintenance.map(type => ({ type, section: 'Maintenance', hidden: false })),
      ] },
    }],
    ['@semantic-release/changelog', { changelogFile: 'CHANGELOG.md' }],
    ['@semantic-release/exec', { prepareCmd: 'python3 scripts/prepare-release.py ${nextRelease.version}' }],
    ['@semantic-release/git', {
      assets: ['CHANGELOG.md', 'package.json', 'package-lock.json', 'opencode/package.json',
        'opencode/package-lock.json', 'claude/.claude-plugin/plugin.json', '.claude-plugin/marketplace.json'],
      message: 'chore(release): ${nextRelease.version} [skip ci]\n\n${nextRelease.notes}',
    }],
    ['@semantic-release/github', {
      assets: ['dist/*.tar.gz', 'dist/*.tgz', 'dist/SHA256SUMS'],
      successComment: false,
      failComment: false,
      releasedLabels: false,
    }],
  ],
};
