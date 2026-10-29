# Claude Code task lifecycle

Child-aware Herdr status and optional ntfy attention for Claude Code. Native validation and tests target Claude Code **2.1.292+**, with fresh validation on **2.1.311**. The Python helpers use only the standard library and POSIX file locks on Linux and macOS with Python 3.9+.

## Behavior

- **Working:** the parent, subagents, native background tasks, recurring session jobs, background reviews, or an expected continuation remain.
- **Blocked:** an actual user question, a permission request, or an interrupted/failed turn. Only a displayed, still-pending **AskUserQuestion** sends an ntfy input-needed alert.
- **Done:** a final successful native parent turn after all observed work and security reviews settle. Its native Stop work snapshot must be empty. The best-effort observed-idle rule below requires an explicit opt-in. One completion notification is admitted per turn when notifications are enabled.
- The completion notification body is that final **parent assistant reply**, UTF-8-truncated to 4000 bytes when necessary, with `…` marking truncation.
- A child finishing, an idle reminder, a session exit, and reopening an old conversation do not independently qualify as completion.
- An unavailable task snapshot or uninstrumented asynchronous review keeps completion pending.

Question alerts wait 750 ms and recheck the matching live tool call. Quickly answered or programmatically satisfied questions do not alert. The 500 ms interactive completion quiet period only cancels stale candidates; elapsed time alone never proves completion.

## Components

- `hooks/register.js`: supported native `turn.*`, `prompt.submit`, `tool.call`, and actual question-render observations.
- `hooks/hooks.json` + `event_hook.py`: ordinary command hooks capture Stop/SubagentStop snapshots, child starts, and permission waits.
- `runtime.py`: serialized persistent lifecycle state, generation fencing, expected review tracking, notification admission, and destination-scoped detached delivery workers.
- `hook_runner.py`: transparent wrapper around asynchronous rewake producers; preserves their input, output, exit code, and configuration. It records a pending continuation **before** returning exit 2 to Claude.
- `transport.py`: pane-specific Unix-socket Herdr reporting, durable ntfy backoff, endpoint-wide serialization, persistent concurrent deduplication, and sanitized receipts.
- `bash_conditions.py`: conservative native-compatible matching for literal Bash hook conditions.
- `configuration.py`: shared public configuration, with notifications disabled and strict teammate handling by default.
- `install.py`: scoped installation, private backups, and guarded restoration.

### Why both kinds of hooks?

Claude's built-in `sec-default` policy deliberately skips user-tier native `classic.*` handlers. The ordinary command hooks still run, so they capture snapshots; native `turn.complete` supplies the post-turn boundary. Claude's normal synchronous Stop continuation loop finishes before that native boundary.

Native `background_tasks` **does not include `asyncRewake` command hooks**. Those producers require separate acknowledgements. The installer wraps the enabled user-owned producers and the runtime reserves expected reviews before admitting completion. Wrappers that start before the snapshot are reconciled into the same invocation.

### Plugin updates

Claude installs a plugin update in a new cache directory, without the wrappers, while running sessions keep the hooks they loaded. Each native module load therefore records its plugin hook inventory once, before it re-wraps anything, and audits against that inventory for its lifetime. `/clear` keeps it; `/reload-plugins` starts a new load. Settings hooks are still read live, and a plugin enabled after the load is audited as currently installed.

Once a load has its inventory, at module load and at each turn start, the runtime wraps updated versions of plugins that the installer instrumented before (`plugins/cache/<marketplace>/<plugin>/<version>`), using the installer's own migration and private backups. Existing wrappers are left as they are, and new ones use the interpreter recorded by the explicit installation. A busy installation lock skips one attempt. Other plugins, user settings and staged files are untouched. Earlier manifest records remain for sessions that still run the older version. A session whose loaded hooks were uninstrumented reports `reload_plugins_required` and pins a `/reload-plugins` status line; the reload registers the wrapped version. A review that already ran untracked before the reload stays untracked: if it later returns feedback, Claude starts a new turn. `claude.reinstrumentPluginUpdates: false` disables this step, leaving `untracked_async_hook` until a manual `install.py --reinstrument --apply` or a full installer run.

One narrow window remains. If another session wraps a plugin between Claude reading its hooks and this load recording its inventory, the session waits on a review that never starts (`background_review`); start a new session. Each automatic run keeps its own private backup, so restore backups newest first; `install.py --restore` names a later backup that has to be restored first.

### Reusable named teammates: strict by default

With the default `claude.allowIdleTeammates: false`, a nonempty teammate Stop
snapshot remains Working (`teammate_completion_unproven`). Finish the team's
background work and let the parent reconcile in a fresh turn. The following
fallback applies only when `allowIdleTeammates` is explicitly the boolean `true`.

Claude's native Stop snapshot can retain a reusable named teammate with `type=teammate` and lifetime `status=running` after its work finishes. At root completion admission, after the existing 500 ms interactive quiet period, the native observer reads a fresh `$.agent.list()` roster and forwards only `id`, `teammateId`, and `status`. New activity or an owner change during that read cancels admission.

The original empty-Stop path is unchanged. A nonempty Stop qualifies only when every record is a valid running teammate with a distinct task ID, session crons are explicitly empty, and the observed queue is not explicitly nonempty. The fresh roster must contain only `idle`, `completed`, `failed`, or `aborted` agents, including locally resumable idle teammates with distinct identities and `id != teammateId`. Their count must match the native teammate count. Missing or failed roster reads, pending/running/waiting/unknown statuses, ambiguous identities/counts, and nonlocal fallback identities keep completion pending.

**This is not proof that teammate queues are empty.** Public `idle` can coexist with unseen queued teammate messages, so this opt-in rule can produce premature Herdr Idle or a Done notification. Count equality is only a conservative sanity check: native task IDs are not joined to public agent IDs, names, or descriptions.

Observed child work, shell/workflow tasks, crons, questions, permissions, continuations, and asynchronous security reviews still block completion. The exception does not settle or bypass any review.

### Background reviews remain asynchronous

Conditional commit/push reviews keep their original `if` and `asyncRewake` configuration.

The matcher handles a closed literal subset: command lists/chains, pipelines, shell quoting, literal assignments, redirections and quoted heredocs. Git commands later in a chain still require their configured review; literal Git text inside a quoted heredoc does not create one.

Native raw-text refusal checks run before comments or heredoc bodies are discarded. Equals-expansion forms, brace/quote ambiguity, certain backslash-newline sequences and invisible whitespace retain the unknown gate, including when such text occurs inside a comment or heredoc. These checks were validated against both native binaries; they must not be replaced with a blanket first-word check.

Expansions and other unsupported syntax remain `review_applicability_unknown` unless positive wrapper receipts account for every uncertain review. This preserves the selected suppression when completion cannot be established. Native Claude can deliberately run all conditional hooks for opaque inputs, so a non-Git first word is not proof that no review applies. `/task-lifecycle-status` exposes the reason. The native matcher behavior was independently checked against Claude **2.1.291 and 2.1.292** with the same 27 synthetic cases.

### Delivery and automatic recovery

Each transport admission makes **at most one HTTP attempt**. Transient network failures, HTTP 408/429, and 5xx persist the attempt count and `nextAttemptAt` before another attempt is allowed. Exponential delays start at **30 seconds**, double to **15 minutes**, and remain capped there. HTTP 429 waits at least **60 seconds**. `Retry-After` accepts delta-seconds and HTTP dates; a server-requested wait may exceed the exponential cap. No immediate retry batch runs inside a publish call.

An endpoint-wide cooldown and POSIX file lock serialize HTTP across sessions sharing the state directory, including separate command processes. A new session/notice cannot evade an ongoing endpoint outage. Other 4xx responses (and refused redirects) are terminal for that notice: they remain **failed**, never sent, and a later different notice can still publish. Sent, terminal, cancelled, and cooldown-suppressed checks do not append extra receipts. Only actual HTTP attempts append a receipt; dedup/backoff records contain hashes, counts, timestamps, status codes, and outcome metadata.

Sessions with the same admitted destination share one detached worker. Different topics or endpoints have separate workers; disabled notifications enqueue nothing. The destination is captured at admission and passed through an anonymous pipe, while the queue stores only its hash. A worker drains its own admitted jobs and exits when those jobs settle. A file lock is inherited across process launch, so repeated admissions do not spawn a sleeper per notice. The worker survives the short-lived native command that admitted a final turn; recovery needs no later user activity. Every attempt rechecks the exact admission, lifecycle, owner/pane, pending-question, and asynchronous-review state. Retry time never establishes completion or settles a review.

Each queue admission has a fresh token: review settlement cannot be erased by a worker removing an earlier blocked admission for the same turn. Parent/child activity revokes a deferred completion but preserves the retry for a still-unanswered question. Answering it, ending the session, or replacing its owner revokes that question's admission.

Recoverable queue I/O failures keep the worker alive and retry after **30 seconds**. If persistence fails after an HTTP response, the worker retains that response and the endpoint/key locks while retrying metadata writes at the same bounded interval. Other instances/processes cannot use an expired provisional reservation to bypass an observed `Retry-After` or resend a success. Lifecycle changes remain admissible during this storage wait.

The mode-0600 final-reply outbox survives deferred attempts and an already-qualified `SessionEnd` final notice. Delivery, permanent rejection, or newer invalidating activity removes it; answering a pending question cancels its queued retry. A killed worker releases its lock; its durable queue can be resumed by the next eligible admission. There is no system service or machine-reboot scheduler. Use an isolated state directory when testing or changing transport configuration.

## Verification

From this directory:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
claude plugin validate .
claude plugin test .
python3 tests/verify_live_cli.py
```

The Python tests use local HTTP/Unix sockets and temporary files, including cross-process outage bursts, restart/backoff suppression, Retry-After, terminal failures, detached final-turn recovery, cancellation, review/owner fencing, deterministic re-admission races, ENOSPC recovery, and metadata privacy. Native mod tests use Claude's test engine, with no model requests. A live CLI check also exercises actual engine callbacks with a deterministic local model-stream fixture and a local notification receiver.

Native tests run outside the default security guard, so passing those tests alone does not establish that a native `classic.*` handler works in a real session. The live verification covers this distinction. `--case named-teammate` selects the opted-in reusable-teammate case; `--case named-teammate-strict` verifies the strict default. Both require a real running teammate record in native Stop after child work finishes and run interactively in a pseudo-terminal. Current Claude cannot spawn teammates in `-p` mode. A managed-settings approval or policy/authentication denial is an explicit setup failure; the fixture never bypasses it.

## Diagnostics

In a fresh Claude session:

```text
/task-lifecycle-status
```

It prints the observed state/reason, pending-review count, and observer health. `reload_plugins_required` means this session loaded review hooks that an update left uninstrumented; run `/reload-plugins`. `untracked_async_hook` means an asynchronous review is not instrumented: a plugin or settings hook the installer has not wrapped, or settings that could not be read. Inspect the calling pane with:

```sh
herdr agent explain "$HERDR_PANE_ID" --json
```

The authority is `custom:claude-task-lifecycle`. Receipts live under `$XDG_STATE_HOME/claude/task-lifecycle/receipts.jsonl`, defaulting to `~/.local/state/claude/task-lifecycle/receipts.jsonl`. Receipt fields contain a hashed key, event kind, timestamp, numeric HTTP status, attempt count, retry deadline, terminal flag, and delivery outcome. Prompt text, questions, commands, response bodies, URLs, and the private ntfy topic are excluded. `deliveries/` holds the per-notice delivery/backoff records; `endpoints/` holds hashed endpoint cooldowns. `delivery-queue-v2.json` contains pending session/owner/generation/admission identifiers, destination hashes and retry times only. Its versioned filename keeps older unscoped workers from consuming newly admitted jobs. `loads/` holds each module load's plugin hook inventory: wrapper IDs, enabled plugin names, matchers and `if` conditions, removed after 30 days without use.

Both integrations read the shared configuration described in the [root README](../README.md#configuration). Notifications are opt-in. The installed compatibility shim retains any old literal topic assignment but does not send notifications; it is not the new configuration source.

Optional environment overrides:

- `CLAUDE_TASK_LIFECYCLE_PYTHON`: Python executable available to the actual Claude/Herdr launcher.
- `CLAUDE_TASK_LIFECYCLE_STATE_DIR`: isolated lifecycle state directory.
- `CLAUDE_TASK_LIFECYCLE_TOPIC_FILE`: alternate literal topic file.
- `CLAUDE_TASK_LIFECYCLE_ENDPOINT`: ntfy base URL; local tests use a loopback HTTP receiver.

`CLAUDE_TASK_LIFECYCLE_OWNER` is internal: the native observer sets it for its own hook processes.

Claude temporarily holds the bounded final reply in a mode-0600 `payloads/` outbox while reviews or delivery are pending; it removes that payload after delivery or when new activity invalidates it. Public status, receipts and deduplication records remain metadata-only.

## Operational boundaries

- This plugin requires Claude 2.1.292+ and its native mods API. Missing background-work fields remain unknown.
- Editing the source does not update the installed plugin cache. After the approved plugin update, active JavaScript observers require `/reload-plugins` or a new session; already-loaded observers do not pick up an edited file automatically.
- An indefinitely recurring `/loop` intentionally prevents completion until cancelled and followed by a final parent response.
- Abrupt process death or a lost review acknowledgement never becomes a synthetic successful completion.
- `unsettled_previous_run` explicitly identifies a review left by an older observer generation. Let its original wrapper settle; if that process was lost, use a fresh session for new work rather than reusing an unproven completion candidate.
- The observed task is Claude-managed work plus instrumented rewake producers. Arbitrary detached programs and future unsolicited messages do not have an authoritative inventory in the interactive CLI.
- HTTP acceptance verifies publication. Delivery to a phone depends on its ntfy subscription. Persistent deduplication prevents routine replays/concurrent duplicates; a crash or timeout around remote acceptance can still make delivery ambiguous.
- Notification delivery runs outside lifecycle admission. Every HTTP retry rechecks that the question or completion is still current, so answering a question or starting new work can cancel a pending retry.
- When Claude requires organization settings but cannot load them, authenticate normally; a policy failure is not a completed lifecycle test.

See the [root installation guide](../README.md#install-a-release) for release installation, upgrades and rollback.
