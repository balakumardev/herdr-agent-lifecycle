# OpenCode V2 task lifecycle integration

Native V2 session-tree state for Herdr and ntfy on this Linux shared-service setup.

## Behavior

- **Working:** the root or any descendant is running, an inbox has queued work, an owned shell is running, or a descendant has finished but the parent has not yet produced its final response.
- **Blocked:** a user form or permission is pending, or root execution failed/interrupted. Only pending user forms produce blocked **ntfy** alerts.
- **Done:** a successful root execution has a completed, final (`stop`) assistant response after its descendants settled, with no pending work. Herdr receives `idle`; ntfy receives one completion notification.
- The completion body is the text of that newest root assistant response, read by the existing root-only `message.list` query (`type: assistant`, descending, limit 1). Reasoning and tool parts are excluded; older answers and child messages are never substitutes. Multiple text parts are separated by blank lines. Bodies over 4000 UTF-8 bytes are truncated at a code-point boundary with `…` included in that budget.
- If the final body is missing or unreadable, completion stays unconfirmed and is retried through normal reconciliation. There is no boilerplate fallback. Message text goes only to the notifier, never to the public snapshot RPC, snapshot cache, or persistent receipts/deduplication records.
- Opening an already completed session does not replay historical completion alerts.
- Descendants can live in different directories/worktrees. The pane remains attributed to its root.

### Delivery retries (1.0.2)

Each admission makes at most one HTTP request. Failed transient requests back off exponentially (30 seconds up to 15 minutes, plus up to 20% jitter). HTTP 429 waits at least 60 seconds. `Retry-After` seconds and HTTP dates are honored when they require a longer wait. A persisted endpoint-wide cooldown covers all sessions in this integration, including concurrent publishers and newly created notifier instances. Non-retryable HTTP errors are recorded as terminal failures for that notice, never as successful deliveries.

The monitor re-reads lifecycle state before eligible retries; new work or an answered question revokes the old alert. Notification HTTP work runs independently of status reads. Cooldown checks create neither HTTP traffic nor receipt rows. Receipt rows for failed attempts include `retryAt` (Unix milliseconds) or `terminal:true`; backoff state contains metadata only. These rules correct the initial rapid retry loop that kept starting fresh three-request batches during an outage.

Claim-numbered retry records keep expired publishers from overwriting newer cooldowns. A received HTTP success remains acknowledged even if its claim expired while the process was suspended. If saving a server deadline fails, a process-wide guard retains it and repairs the durable checkpoint when storage becomes writable.

## Components

- `lifecycle.mjs`: bounded, paginated reads of the public V2 API; shared classification.
- `monitor.mjs`: event invalidation, snapshot reconciliation, and notification admission.
- `index.js`: one authenticated monitor per OpenCode service process, shared across location instances; exposes `local.task-lifecycle/snapshot` RPC.
- `tui.js` / `herdr.mjs`: selected-root reporting using the terminal's own Herdr pane environment.
- `notify.mjs`: literal topic lookup, bounded HTTP delivery, persistent concurrent deduplication, and sanitized receipts.

The service is discovered through `@opencode/client/service`. Discovery never launches a service. Its PID must match the plugin host, so a standalone server cannot accidentally report another shared server's sessions.

## Configuration

Effective server configuration: `~/.config/opencode2/opencode/opencode.json`.

```json
{
  "plugins": [
    "-herdr.opencode.agent-state",
    { "package": "./plugins/claude-code-hooks.js", "options": { "notifications": false } },
    "./integrations/task-lifecycle"
  ]
}
```

These entries are merged with the existing plugins. The portable Claude hooks retain their audit/context/lint functionality; this integration owns V2 attention. The compatibility bridge also defaults its V2 notifier to off, covering automatically discovered instances that have no explicit options. Legacy notification behavior is available only through an explicit `notifications:true` override.

Effective terminal configuration: `~/.config/opencode2/opencode/cli.json`, with `./integrations/task-lifecycle` in `plugins`. The custom Herdr agent label is `opencode-v2`, with a resumable `opencode2 --session <rootID>` command.

## Diagnostics

```bash
opencode2 api post '/api/rpc/local.task-lifecycle/snapshot?location[directory]=/home/coder/workspace/command-center' \
  --data '{"input":{"sessionID":"<sessionID>"}}'
herdr agent explain '<paneID>' --json
```

ntfy reads `TOPIC` from `~/.claude/hooks/ntfy-notify.sh` without executing that script. Receipts are at `$XDG_STATE_HOME/opencode/task-lifecycle/receipts.jsonl` (normally `~/.local/state/opencode2/opencode/task-lifecycle/receipts.jsonl`). They contain hashed event keys, HTTP status and delivery outcome, without the topic or message contents. HTTP acceptance verifies publication; device alerts also depend on the subscribing app.

The `local.task-lifecycle/health` RPC returns the loaded integration version/build revision, monitor readiness and location reference count. This repair is **version 1.0.2, revision 3**. A receipt with `retryAt` means delivery is waiting, not acknowledged. Terminal failures (such as HTTP 401/403) require correcting the endpoint/auth problem; future notices use new keys.

## Verification

Run `npm test` in this directory. Tests use temporary Unix sockets and local HTTP servers; they do not send external notifications. The existing question-timeout tests remain at `../../tests/question-timeout-effect.test.mjs`.

## Applying source updates on the shared service

[V2 plugin configuration](https://opencode.ai/v2/docs/plugins#reload) supports automatic reloads for watched configuration directories. This package keeps its monitor in a process-global, reference-counted singleton (`index.js`). Version 1.0.2 adds a versioned handoff: the first setup from a newer build aborts the old event subscription, replaces the monitor and retains pending observation metadata and retry deadlines. Old in-flight admissions are invalidated. Other location references keep using the replacement.

After updating source, trigger a watched plugin-config reload and verify `local.task-lifecycle/health` reports the new version, then check a real diagnostic completion. Editing an unwatched imported file alone is not proof of activation. If a restart is used instead, schedule it after active work finishes. Startup hydration continues to suppress historical completion alerts.

Original configuration and compatibility-hook backups are under `~/.local/state/opencode2/opencode/task-lifecycle/backups/20261006T113044Z/`. For rollback, remove this package from the two configuration files, remove the old Herdr disable directive, and remove the `notifications:false` override. Restore only the affected entries so unrelated later config changes are retained.
