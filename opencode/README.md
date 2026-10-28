# OpenCode V2 task lifecycle integration

Native V2 session-tree state for Herdr and optional ntfy on macOS and Linux.

## Behavior

- **Working:** the root or any descendant is running, an inbox has queued work, an owned shell is running, or a descendant has finished but the parent has not yet produced its final response.
- **Blocked:** a user form or permission is pending, or root execution failed/interrupted. Only pending user forms produce blocked **ntfy** alerts.
- **Done:** a successful root execution has a completed, final (`stop`) assistant response after its descendants settled, with no pending work. Herdr receives `idle`; ntfy receives one completion notification.
- **Unknown:** the first snapshot could not be read, or a failed refresh made an earlier Idle report untrustworthy. The custom reporter claims the pane and retries, so screen detection cannot mistake invisible background work for completion. A previously confirmed Working or Blocked report is retained during a failed refresh.
- The completion body is the text of that newest root assistant response, read by the existing root-only `message.list` query (`type: assistant`, descending, limit 1). Reasoning and tool parts are excluded; older answers and child messages are never substitutes. Multiple text parts are separated by blank lines. Bodies over 4000 UTF-8 bytes are truncated at a code-point boundary with `…` included in that budget.
- If the final body is missing or unreadable, completion stays unconfirmed and is retried through normal reconciliation. There is no boilerplate fallback. Message text goes only to the notifier, never to the public snapshot RPC, snapshot cache, or persistent receipts/deduplication records.
- Opening an already completed session does not replay historical completion alerts.
- Descendants can live in different directories/worktrees. The pane remains attributed to its root.
- Completed children remain part of the task after their temporary worktrees are removed. Persisted history and queued input are read for every child; forms, permissions and shells are read only from locations in OpenCode's live runtime registry. Registry failures, missing active-child runtimes and locations appearing during the read prevent completion and trigger a fresh snapshot.

### Delivery retries

Each admission makes at most one HTTP request. Failed transient requests back off exponentially (30 seconds up to 15 minutes, plus up to 20% jitter). HTTP 429 waits at least 60 seconds. `Retry-After` seconds and HTTP dates are honored when they require a longer wait. A persisted endpoint-wide cooldown covers all sessions in this integration, including concurrent publishers and newly created notifier instances. Non-retryable HTTP errors are recorded as terminal failures for that notice, never as successful deliveries.

The monitor re-reads lifecycle state before eligible retries; new work or an answered question revokes the old alert. Notification HTTP work runs independently of status reads. Cooldown checks create neither HTTP traffic nor receipt rows. Receipt rows for failed attempts include `retryAt` (Unix milliseconds) or `terminal:true`; backoff state contains metadata only. These rules correct the initial rapid retry loop that kept starting fresh three-request batches during an outage.

Claim-numbered retry records keep expired publishers from overwriting newer cooldowns. A received HTTP success remains acknowledged even if its claim expired while the process was suspended. If saving a server deadline fails, a process-wide guard retains it and repairs the durable checkpoint when storage becomes writable.

## Components

- `lifecycle.mjs`: bounded, paginated reads of the public V2 API; shared classification.
- `monitor.mjs`: event invalidation, snapshot reconciliation, and notification admission.
- `index.js`: one authenticated monitor per OpenCode service process, shared across location instances; exposes `local.task-lifecycle/snapshot` RPC.
- `tui.js` / `herdr.mjs`: selected-root reporting using the terminal's own Herdr pane environment.
- `config.mjs`: the shared public configuration schema.
- `notify.mjs`: bounded HTTP delivery, persistent concurrent deduplication, and sanitized receipts.

The service is discovered through `@opencode/client/service`. Discovery never launches a service. Its PID must match the plugin host, so a standalone server cannot accidentally report another shared server's sessions.

## Configuration

Effective server configuration: `~/.config/opencode/opencode.json` (or the
`opencode/` directory under `$XDG_CONFIG_HOME`). Use the [installer](../README.md#opencode)
to merge a dependency-complete release. Its resulting plugin entries are:

```json
{
  "plugins": [
    "-herdr.opencode.agent-state",
    "/absolute/release/path/opencode"
  ]
}
```

These entries are merged with existing plugins. If a recognized legacy Claude
notification bridge is present, the installer transfers its notification role
to this integration while retaining its audit/context/lint behavior.

Terminal configuration is `~/.config/opencode/cli.json`, with
`"-herdr.opencode.session-selection"` and the same absolute plugin directory in
`plugins`. The custom Herdr agent label is `opencode-v2`; the resume executable
defaults to `opencode` and is configurable through `opencode.command` in the
[shared settings file](../README.md#configuration).

## Diagnostics

```bash
opencode api post '/api/rpc/local.task-lifecycle/health' --data '{"input":{}}'
opencode api post '/api/rpc/local.task-lifecycle/snapshot' \
  --data '{"input":{"sessionID":"<sessionID>"}}'
herdr agent explain '<paneID>' --json
```

Notifications use the shared settings file and are disabled by default. Receipts
are at `$XDG_STATE_HOME/opencode/task-lifecycle/receipts.jsonl` (normally
`~/.local/state/opencode/task-lifecycle/receipts.jsonl`). They contain hashed event
keys, HTTP status and delivery outcome. HTTP acceptance verifies publication;
device alerts also depend on the subscribing app.

The `local.task-lifecycle/health` RPC returns the loaded version/build revision,
monitor readiness and location reference count. A receipt with `retryAt` means
delivery is waiting. Terminal failures (such as HTTP 401/403) require correcting
the endpoint/auth problem; future notices use new keys.

## Verification

Run `npm test` from the repository root. Tests use temporary Unix sockets and
local HTTP servers, including the packaged legacy-bridge fixture.

## Applying source updates on the shared service

[V2 plugin configuration](https://opencode.ai/v2/docs/plugins#reload) supports automatic reloads for watched configuration directories. This package keeps its monitor in a process-global, reference-counted singleton (`index.js`). Upgrades use a versioned handoff: the first setup from a newer build aborts the old event subscription, replaces the monitor and retains pending observation metadata and retry deadlines. Old in-flight admissions are invalidated. Other location references keep using the replacement.

After updating source, trigger a watched plugin-config reload and verify `local.task-lifecycle/health` reports the new version, then check a real diagnostic completion. Editing an unwatched imported file alone is not proof of activation. If a restart is used instead, schedule it after active work finishes. Startup hydration continues to suppress historical completion alerts.

The installer prints a private backup directory and supports guarded
`--restore BACKUP --apply`. See [upgrade and rollback](../README.md#upgrade-and-rollback).
