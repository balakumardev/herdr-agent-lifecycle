# Claude Code background-work state for Herdr

Herdr shows **Working** while Claude Code still has a background shell or
subagent running, instead of Done a few seconds after the turn ends.

Herdr's own detection reads the screen and already handles turns, prompts and
idle. It cannot see background work, so it reports idle once the prompt returns.
This plugin only corrects that case.

## How it works

Plain command hooks, no native module and no installer:

| Hook | Action |
| --- | --- |
| `Stop` | If the event's `background_tasks` has a running shell or subagent: report **working**. Otherwise release. |
| `UserPromptSubmit`, `SessionStart`, `SessionEnd` | Release (a `SessionStart` from `/compact` is ignored, since the work is still running). Herdr's own detection decides again. A finished background task arrives as a new prompt, so this is also how the pane returns to Done. |
| `PermissionRequest` | While holding: report **blocked**, so a pending prompt never shows as Working (Herdr 0.9.3 also re-detects it from the screen). |
| `PostToolUse` (async) | While holding and blocked: report **working** again once the prompt was answered. |

The plugin never reports idle, Done or unknown, so Herdr's detection stays in
charge of everything except "background work remains". Any failure leaves Herdr
on its own detection. Reports use the pane's own socket, with source
`custom:claude-background-work`. State per pane is a small file under
`$XDG_STATE_HOME/herdr-agent-lifecycle/claude/`.

Named teammates and session cron jobs are not counted. A teammate stays
`running` after its work ends, and a recurring `/loop` would hold Working until
cancelled.

## Install

```sh
claude plugin marketplace add balakumardev/herdr-agent-lifecycle
claude plugin install task-lifecycle@herdr-agent-lifecycle --scope user
```

Requires Python 3.9+ on `PATH`, macOS or Linux, and a Claude Code session started
inside a Herdr pane. Start a new Claude session to load the hooks.

## Limits

- **Killing a task from `/tasks` leaves the pane on Working** until your next
  prompt. Claude fires no hook for it, so nothing can tell the plugin.
- **Interrupting a turn with Esc leaves background work unheld.** Claude runs no
  `Stop` hook on a user interrupt, so the pane shows Done until the next prompt.
- **While two background agents run, one's tool finishing can flip `blocked` back
  to `working`** while the other still waits on a prompt. Herdr 0.9.3 also
  re-detects the prompt from the screen.
- **No resume or session reporting, no notifications.** Herdr accepts a resume
  command only from a source that holds the pane, and releasing clears it, so this
  overlay cannot provide it.
- **Do not combine it with Herdr's own Claude hook** (`herdr integration install
  claude`). After that hook reported a session on a pane, state reports from other
  sources were ignored (observed on Herdr 0.9.3).
- A crashed Claude needs no cleanup: Herdr drops the pane's agent state within a
  second.

## Diagnostics

```sh
herdr agent get "$HERDR_PANE_ID"
herdr agent explain "$HERDR_PANE_ID" --json
```

## Development

```sh
python3 -m unittest discover -s claude/tests -p 'test_*.py' -v
claude plugin validate claude
```

Tests use a fake Herdr socket and temporary state directories.

See the [root README](../README.md) for the OpenCode adapter and releases.
