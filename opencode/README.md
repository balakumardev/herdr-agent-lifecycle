# OpenCode background-work overlay for Herdr

Herdr reads the screen to decide whether an agent is working, and says **Done**
while OpenCode still has subagents or background shells running. This TUI
plugin corrects exactly that and nothing else.

## Behavior

Herdr's own detection stays in charge of everything it gets right (a turn
running, a question or permission on screen, idle). The plugin takes over the
pane only while the selected session's family (the root plus every descendant)
has background work:

| Situation | Reported to Herdr |
| --- | --- |
| A descendant session is running, or a running shell is owned by any session in the family | `working` |
| ...and a family session has a pending question or permission | `blocked` |
| Anything else, a read failure, the route leaving a session, or the plugin being disposed | authority cleared, so Herdr's detection decides |

It never reports `idle` or `unknown`, never sends resume or session data, and
never notifies. A hold is confirmed by a live `session.active()` /
`shell.list()` read before it is reported, so a missed event cannot leave the
pane stuck on Working; any failed or timed-out read clears it instead.

Shells are per directory, so every directory used by a family member is read.

Not supported on purpose: notifications (ntfy), resume or session reporting, and
any Done/Idle authority. If you want those, they are not in this package.

## How it is registered

The package exposes only `./tui`. Register the directory in `cli.json`
(`~/.config/opencode/cli.json`), and keep Herdr's own OpenCode reporters off
because they conflict with it:

```json
{ "plugins": ["/absolute/path/to/opencode", "-herdr.opencode.session-selection"] }
```

and in `opencode.json`: `{ "plugins": ["-herdr.opencode.agent-state"] }`.

`scripts/install-opencode.py --config-dir ~/.config/opencode` does this merge
(dry run by default, `--apply` to write, `--restore BACKUP --apply` to undo with
later-edit protection). It also removes an older server-side registration of
this plugin, which no longer has a server entry point. No `npm ci` is needed:
the package has no dependencies.

Only Herdr panes are affected: it does nothing unless `HERDR_ENV=1`,
`HERDR_PANE_ID` and `HERDR_SOCKET_PATH` are set.

## Diagnostics

```bash
herdr agent get <paneID>        # status as Herdr currently reports it
herdr agent explain <paneID>    # what the screen detector sees
```

While a hold is active the pane's status is `working` (or `blocked`) even if the
screen looks idle. The reports use source `custom:opencode-background-work` and
agent `opencode`, matching the label Herdr detects.

The pane is handed back with `pane.clear_agent_authority`, not
`pane.release_agent`: on Herdr 0.9.3 the latter drops the agent identity and
leaves the pane `unknown` for about 3 seconds while it is re-detected. A Herdr
that does not know the method answers `invalid_request` (checked on 0.9.3, whose
error lists the supported methods), and the plugin then switches to
`pane.release_agent` for the rest of that process.

## Verification

`npm test` in this directory runs the unit tests against a fake Herdr socket and
fake OpenCode objects. Run it outside a Herdr pane, or with `HERDR_ENV`,
`HERDR_PANE_ID` and `HERDR_SOCKET_PATH` unset.
