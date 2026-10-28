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
