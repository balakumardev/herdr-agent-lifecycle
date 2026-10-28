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
Herdr decides whether the UI labels that state Idle or Done.

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
npm test
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

Run the Claude installer again after upgrading another plugin that owns
`asyncRewake` reviews: upgrades can replace its wrapped hooks. Unrecognized
review state keeps completion pending until it is instrumented.

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
  "claude": { "allowIdleTeammates": false }
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

## Development

Requires Node 22.14+, Python 3.9+, and macOS or Linux.

```sh
npm ci --prefix opencode
npm test
claude plugin validate claude
claude plugin test claude
```

The portable tests use temporary homes and loopback HTTP/socket fixtures.
Native Claude tests require a compatible locally installed Claude CLI.

## Origins

Extracted from the author's existing task-lifecycle integrations: Claude 1.0.4
and OpenCode 1.0.2/revision 3. The imported source was independently compared
with its running installation and the checksum-verified recovery bundle.
This project is independent of Herdr, Anthropic, and OpenCode.

## License

Apache-2.0. See [LICENSE](LICENSE). Herdr's public protocol is documented by
[herdrdev/herdr](https://github.com/herdrdev/herdr).
