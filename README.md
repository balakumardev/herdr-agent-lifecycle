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
