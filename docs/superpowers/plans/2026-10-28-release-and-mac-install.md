# Herdr Agent Lifecycle Implementation Plan

> **For agentic workers:** Use `executing-plans` for inline implementation. Track
> each task here and obtain one independent whole-change review before publishing.
> The user explicitly waived separate spec/plan review checkpoints.

**Goal:** Publish both lifecycle adapters with automatic semantic releases and
install the tested release on the Mac.

**Architecture:** Preserve the existing two host adapters, add shared-schema
configuration and a strict teammate default, and distribute them from one repo.
A release-only root Node package coordinates synchronized versions and assets.

**Tech Stack:** JavaScript/Node, Python standard library, Claude native mods,
OpenCode 2.0.18, GitHub Actions, semantic-release.

**Spec:** `docs/superpowers/specs/2026-10-28-public-release-design.md`.

## Global Constraints

- Apache-2.0; repository `balakumardev/herdr-agent-lifecycle`.
- Node 22.14+, Python 3.9+, OpenCode 2.0.18, Claude native mods 2.1.292+,
  Herdr 0.9.3+; Linux and macOS; release tooling runs on Node 24.
- Strict teammate default; best-effort requires explicit boolean true.
- Keep existing tests/assertions and all unrelated installed configuration.
- No live-service restart, private payload in Git, or external test notification.

## Review Focus

1. Invalid/truthy-string config must not enable notifications or idle completion.
2. Package installation from a release must work without the source checkout.
3. OpenCode native reporters must both be disabled without losing other plugins.
4. Restoring a backup must not overwrite later user edits.
5. Release retries/concurrent pushes must not create duplicate versions or assets.

## Task 1: reproducible public baseline

**Files:** `claude/`, `opencode/`, `scripts/install-opencode.py`, root package,
license, README, test fixtures, `.gitignore`.

**Interface:** adapters retain existing module and RPC interfaces; root
`npm test` runs Node and Python suites without access to live configuration.

- [ ] Copy only checksum-verified recovery source; initialize the new Git repo.
- [ ] Reproduce the inherited missing-temp-directory/external-bridge failures.
- [ ] Use `tmpdir()`/`tempfile` fixtures and a committed representative bridge
  fixture; preserve the bridge notification assertions and test the installer
  against both legacy and already-patched bridge layouts.
- [ ] Run `npm ci --prefix opencode`, `npm test --prefix opencode`, and
  `python3 -m unittest discover -s claude/tests -p 'test_*.py'`.
- [ ] Commit the portable baseline with provenance and Apache-2.0 metadata.

## Task 2: public configuration and completion policy

**Files:** `opencode/config.mjs`, `opencode/notify.mjs`, `opencode/herdr.mjs`,
`claude/configuration.py`, `claude/runtime.py`, `claude/transport.py`, matching tests.

**Interface:** `loadConfig()` / `load_config()` return the schema in the spec;
`Runtime(..., allow_idle_teammates=False)` owns the Claude completion policy;
explicit transport topic-file arguments remain supported.

- [ ] Add failing config tests for defaults, custom path, malformed JSON,
  invalid endpoint/topic/boolean, explicit opt-in, and configured launch command.
- [ ] Add a runtime test with nonempty Stop snapshot plus idle roster that must
  remain pending by default; keep the opt-in fallback's positive/negative cases.
- [ ] Implement configuration readers, direct topic inputs, configurable resume
  command, and strict-default gate. Read actual status after each transition.
- [ ] Run both complete suites plus `claude plugin validate claude` and
  `claude plugin test claude`; commit the verified feature.

## Task 3: installation and rollback

**Files:** `scripts/install-opencode.py`, `claude/install.py`,
`.claude-plugin/marketplace.json`, installer tests and README.

**Interface:** OpenCode `--plugin-dir PATH --config-dir PATH [--apply]` registers
a preinstalled immutable release; `--restore BACKUP` restores owned changes only.
Claude continues supporting `--home`, `--source-dir`, `--apply` and local staging.

- [ ] Write tests for repeat installation, both reporter exclusions, preserved
  plugin options, unfamiliar bridge refusal, and modified-after-install restore.
- [ ] Back up and atomically update only owned registration/bridge files.
- [ ] Keep the Claude async wrapper behavior and update its public marketplace
  identity and runtime-artifact allowlist for the configuration module.
- [ ] Exercise clean-home installs and guarded rollback with real subprocesses.
- [ ] Commit installers and installation docs after the suites pass.

## Task 4: automated publication

**Files:** root `package.json`, `package-lock.json`, `release.config.cjs`,
`scripts/prepare-release.py`, `.github/workflows/ci-release.yml`, release tests.

**Interface:** `python3 scripts/prepare-release.py VERSION` updates both manifests
and lockfiles, writes `dist/` source/package artifacts and SHA256SUMS; the
semantic-release prepare hook calls it before the release commit/tag/publication.

- [ ] Verify current upstream semantic-release APIs and pin its dependencies.
- [ ] Test manifest synchronization, malformed versions, and an independently
  extracted release bundle; pack the OpenCode package and inspect its file list.
- [ ] Configure two-platform CI, full Git history, release serialization,
  Conventional Commit analysis, release-note generation, asset upload, and
  main/master branch triggers. Ensure PRs have read-only permissions.
- [ ] Run local suites and package verification, then a fresh independent review;
  fix evidenced defects and rerun affected gates.
- [ ] Create the public repository, push main, and verify the first CI release.
- [ ] Make a substantive follow-up Conventional Commit through a PR, merge it,
  and verify the automatic next version/tag/release and assets.

## Task 5: install and verify the released build on the Mac

**Files:** machine-local config, immutable release directory and private backups.

**Interface:** released CLI/installer paths only; installation does not restart
the shared OpenCode service or reload unrelated running Claude sessions.

- [ ] Download/checksum the exact GitHub release and install its isolated deps.
- [ ] Snapshot Mac plugin registrations, relevant hooks, active sessions, and PID.
- [ ] Preserve the private topic locally, register OpenCode server/TUI and the
  Claude marketplace plugin, and verify idempotent reapplication.
- [ ] Run released suites and native fixtures using loopback receivers; inspect
  active OpenCode RPC health and selected-pane reporting with a fresh client.
- [ ] Confirm existing service identity/session activity and unrelated config.
- [ ] Record final repository, release, backup, version, and runtime evidence here.

## Evidence / resume checkpoint

- Approved scope and autonomous execution: 2026-10-28.
- Recovery archive: SHA256
  `5a70cc6d8795756a89bc3199f89eee3c6bd428209e24c749d6daecdcff5efad9`;
  all 43 manifest hashes verified before source import.
- Prior intbox audit: 156 Python, 15 native Claude, 113 OpenCode tests passed.
- Current Mac observed: OpenCode 2.0.18, Claude 2.1.311, Herdr 0.9.3.
- Next: copy baseline and implement Task 1.
