# Herdr Agent Lifecycle

Task-aware **Working / Blocked / Done** reporting for **Claude Code** and
**OpenCode V2**, with optional ntfy notifications.

```text
parent + descendants + background work + pending input
                         |
                  lifecycle checks
                         |
              Herdr state + optional ntfy
```

The OpenCode adapter waits for the root's successful final reply after its
descendants and owned shells settle. The Claude adapter observes native turns,
displayed questions, background tasks, and asynchronous review completion.
Both use Herdr's normal socket API. A completed task is reported as `idle`;
Herdr shows **Done** for an unread completion and **Idle** after it has been
viewed. Refreshing an unchanged OpenCode task does not create a new completion
or reset that read/unread state.

## Install a release

Requirements: **macOS or Linux, Herdr 0.9.3+, Node 22.14+, Python 3.9+**.
The adapters target **OpenCode 2.0.18** and **Claude Code 2.1.292+**. Download
the [latest release](https://github.com/balakumardev/herdr-agent-lifecycle/releases/latest)
into a permanent directory; OpenCode will load the plugin from there.

```sh
VERSION=$(GH_HOST=github.com gh release view --repo balakumardev/herdr-agent-lifecycle --json tagName --jq .tagName)
GH_HOST=github.com gh release download "$VERSION" --repo balakumardev/herdr-agent-lifecycle --dir "lifecycle-$VERSION"
cd "lifecycle-$VERSION"
shasum -a 256 -c SHA256SUMS
tar -xzf "herdr-agent-lifecycle-${VERSION#v}.tar.gz"
cd "herdr-agent-lifecycle-${VERSION#v}"
npm ci --prefix opencode --ignore-scripts
npm run test:runtime
```

### OpenCode

Preview the registration changes, then apply them:

```sh
python3 scripts/install-opencode.py --config-dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
python3 scripts/install-opencode.py --config-dir "${XDG_CONFIG_HOME:-$HOME/.config}/opencode" --apply
```

The installer registers this prepared release in both `opencode.json` and
`cli.json`, preserves other plugins/options, and disables the two competing
native Herdr reporters. It migrates a recognized legacy Claude notification
bridge if present. JSONC with comments or an unfamiliar bridge requires a
manual merge; the installer refuses it before changing any configuration.

For a manual merge, add these entries to the existing `plugins` arrays:

| File | Entries |
| --- | --- |
| `opencode.json(c)` | `"-herdr.opencode.agent-state"`, `"/absolute/release/path/opencode"` |
| `cli.json` | `"-herdr.opencode.session-selection"`, `"/absolute/release/path/opencode"` |

Watched configuration changes normally hot-reload. Verify the health RPC from
[OpenCode diagnostics](opencode/README.md#diagnostics); open a fresh TUI for pane
reporting. The installer never restarts the shared service.

### Claude Code

Preview and apply the hook migration, then run the printed activation commands:

```sh
python3 claude/install.py
python3 claude/install.py --apply
claude plugin marketplace add "$HOME/.claude/local-marketplaces/task-lifecycle"
claude plugin install task-lifecycle@herdr-agent-lifecycle --scope user
```

The installer stages an allowlisted local marketplace, replaces the legacy
notification hook with a passive shim, and wraps enabled user-owned asynchronous
review hooks. Original conditions, arguments, exit status and rewake behavior
remain intact. Start a fresh Claude session to load the native observer; run
`/task-lifecycle-status` to inspect it.

The repository also exposes the public `herdr-agent-lifecycle` marketplace at
`balakumardev/herdr-agent-lifecycle`. The local-release installation above pins
the exact verified code and includes review-hook migration.

### Upgrade and rollback

Download a new release into a new directory and rerun its installers. For Claude,
refresh the staged marketplace and installed cache after staging:

```sh
claude plugin marketplace update herdr-agent-lifecycle
claude plugin update task-lifecycle@herdr-agent-lifecycle --scope user
```

Sessions that were already open keep the previous observer, which audits review
hooks as currently installed. Run `/reload-plugins` in each before its next
prompt, or start a new session. A session that runs a turn first may report
`unsettled_previous_run` after the reload; `/clear` it.

Updates of another plugin that owns `asyncRewake` reviews are re-instrumented
automatically; see [Claude plugin updates](#claude-plugin-updates). Run the
Claude installer again after installing a *new* plugin that owns such reviews.
Unrecognized review state keeps completion pending until it is instrumented.

Each installer prints its private backup directory. Preview a restoration with
`python3 scripts/install-opencode.py --restore BACKUP` or
`python3 claude/install.py --restore BACKUP`; add `--apply` to restore. Later edits
are refused before any restoration. For hook JSON, Claude backs up only migrated
records and restores their original structure, preserving configuration secrets
in their original file. If a later plugin activation or user edit changed a
backed-up file, merge the recorded hook changes manually rather than replacing
that file. Stop using the plugin in active clients before removing its files.

## Configuration

Both adapters read `~/.config/herdr-agent-lifecycle/config.json` (or
`$XDG_CONFIG_HOME/herdr-agent-lifecycle/config.json`). Set
`HERDR_LIFECYCLE_CONFIG` to use another file. The defaults are:

```json
{
  "notifications": { "enabled": false, "endpoint": "https://ntfy.sh", "topic": "" },
  "opencode": { "command": "opencode" },
  "claude": { "allowIdleTeammates": false, "reinstrumentPluginUpdates": true }
}
```

To enable ntfy, set `enabled` to `true` and supply your own private `topic`.
Keep this file private (`chmod 600`); it does not belong in your repository.
The completion notification contains the final root reply, truncated to 4000
UTF-8 bytes. Questions send a generic input-needed message. Receipts contain
only metadata. A self-hosted HTTP(S) endpoint can include a base path.

`opencode.command` is one executable name or path, not a shell command line.
Use `opencode2` here if that is your V2 launcher. Restart/reopen the relevant
client after changing configuration; existing process-owned monitors keep their
configuration until reloaded.

### Claude teammate policy

**Strict is the default.** An idle teammate may still have queued messages, so
an idle roster does not prove completion. With a nonempty native Stop snapshot,
the plugin remains Working with reason `teammate_completion_unproven`. End the
team's background work and let the parent reconcile in a new turn to complete.

Set `claude.allowIdleTeammates` to the boolean `true` to opt into the previous
best-effort idle-roster fallback. This can report Done prematurely. Strings such
as `"true"` are rejected. Use `/task-lifecycle-status` to inspect the current
reason. Review, continuation, question, and permission gates apply in both modes.

### Claude plugin updates

Claude installs each plugin version in a new cache directory. An update of a
plugin whose reviews the installer wrapped, such as `security-guidance`, arrives
with plain `asyncRewake` hooks. Their completion cannot be observed, so the
task would stay Working.

```text
installer wraps 2.0.16 reviews ──> Claude auto-updates to 2.0.17 (unwrapped)
                                              │
              next session start or turn re-wraps 2.0.17, as the installer would
                                              │
        later sessions: tracked reviews ──> Done after they settle
```

- A running session keeps the hooks it loaded. Its completion follows those
  hooks, not the newly installed version.
- Only plugins whose earlier versions the installer instrumented are re-wrapped,
  with the same private backups. Existing wrappers are left as they are, and new
  ones use the interpreter the installation recorded. A newly installed plugin
  still needs the installer. Run `python3 claude/install.py --reinstrument` to
  preview this step, or add `--apply` to run it yourself.
- Restore backups newest first. `--restore` names a later automatic backup that
  has to be restored before an earlier one.
- A session that loaded the unwrapped version stays Working with reason
  `reload_plugins_required`, and a status line asks you to run
  `/reload-plugins`. The reload registers the instrumented hooks.

Set `claude.reinstrumentPluginUpdates` to the boolean `false` to turn this off.
Updated reviews then stay `untracked_async_hook` until you rerun the installer.

## Development

Requires Node 22.14+, Python 3.9+, and macOS or Linux.

```sh
npm ci
npm ci --prefix opencode
npm test
claude plugin validate claude
claude plugin test claude
```

The portable tests use temporary homes and loopback HTTP/socket fixtures.
Native Claude tests require a compatible locally installed Claude CLI.
CI also builds the two release archives, installs the OpenCode package with its
own dependencies, applies/restores both installers in a temporary home, and runs
the runtime suites from the extracted source archive. To verify already prepared
assets, run `python3 scripts/verify-packages.py --dist dist --version X.Y.Z`.

The live host checks use a local model-stream fixture and loopback notifications:

```sh
python3 claude/tests/verify_live_cli.py
```

Named-teammate cases use a pseudo-terminal because current Claude does not
create teammates in `-p` mode. They require an attended Claude session to have
already approved any organization-managed settings. The fixture stops on that
approval prompt. It verifies both strict-pending and opted-in completion behavior;
an unavailable teammate, denied Agent tool or missing snapshot fails the check.

## Automatic releases

```text
PR merged into main/master
          ↓
Linux + macOS tests
          ↓
Conventional Commit → next shared version
          ↓
versioned manifests + CHANGELOG + vX.Y.Z tag
          ↓
GitHub release: source archive + OpenCode package + SHA256SUMS
```

Use a Conventional Commit title when squash-merging:

| Commit | Release |
| --- | --- |
| `fix: preserve pending work` | Patch |
| `feat: observe a new work type` | Minor |
| `feat!: require a newer host API` | Major |
| `docs:`, `ci:`, `test:`, `refactor:`, `chore:`, other configured maintenance types | Patch |

The workflow uses the job-scoped `GITHUB_TOKEN`; no npm publishing credential is
needed. Only the release job has write access. Pipelines are serialized and the
release uses its tested commit. Generated release commits use `[skip ci]`.
Re-running a completed release with no new commits creates no new version.
Use the workflow's manual dispatch to retry a failed run after fixing its cause.
Before version analysis, recovery checks the latest reachable release tag. If
GitHub publication was interrupted after tagging, it rebuilds that exact tag in
a disposable clone, completes its draft assets, verifies downloaded checksums,
and publishes that same version. A completed release is verified without being
rewritten. The release job also verifies downloaded assets after publication.

If you later protect `main`/`master` against release commits, configure an
appropriately scoped GitHub App for the release job. Do not substitute a broad
personal token. Ordinary non-Conventional Commit messages do not trigger a
release; use squash-merge titles to keep the version policy predictable.

## Origins

Extracted from the author's existing task-lifecycle integrations: Claude 1.0.4
and OpenCode 1.0.2/revision 3. The imported source was independently compared
with its running installation and the checksum-verified recovery bundle.
This project is independent of Herdr, Anthropic, and OpenCode.

## License

Apache-2.0. See [LICENSE](LICENSE). Herdr's public protocol is documented by
[herdrdev/herdr](https://github.com/herdrdev/herdr).
