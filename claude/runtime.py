"""Persistent lifecycle reducer shared by native Claude hooks and review wrappers.

Only allowlisted metadata is stored. A Stop is a candidate, never a completion.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from transport import Transport, bounded_message, locked, save_json
from bash_conditions import match_bash_condition


AUDITED_EVENTS = ("Stop", "SubagentStop", "PostToolUse", "UserPromptSubmit")
LOAD_RETENTION_SECONDS = 30 * 86400


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def event_fingerprint(event):
    return digest([event.get("hook_event_name", ""), event.get("session_id", ""),
                   event.get("prompt_id", ""), event.get("agent_id", ""),
                   event.get("stop_hook_active") is True, event.get("last_assistant_message", ""),
                   event.get("tool_use_id", "")])


def condition_matches(condition, event):
    """A closed subset of Claude's documented `if` rules; None means unknown.

    Literal Bash commands use a native-verified closed matcher. Expansions and
    unsupported syntax remain unknown; positive review receipts can settle them.
    """
    if not condition:
        return True
    if event.get("hook_event_name") not in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"):
        return False
    match = re.fullmatch(r"([A-Za-z0-9_]+)(?:\((.*)\))?", condition)
    if not match:
        return None
    tool, pattern = match.groups()
    if tool != event.get("tool_name"):
        return False
    if pattern is None:
        return True
    if tool != "Bash":
        return None
    command = event.get("tool_input", {}).get("command")
    return match_bash_condition(pattern, command)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def classify(state):
    if state.get("questions"):
        return "blocked", "user_question"
    if state.get("permissions"):
        return "blocked", "permission_required"
    if state.get("failure"):
        return "blocked", state["failure"]
    if state.get("closed"):
        return "blocked", "session_ended"
    if any(item["status"] in ("expected", "running") and item.get("owner") != state.get("owner")
           for item in state["reviews"].values()):
        return "blocked", "unsettled_previous_run"
    if state.get("unknown_hook"):
        # The loaded hooks are uninstrumented, but their installed versions are not.
        return "working", "reload_plugins_required" if state.get("reload_required") else "untracked_async_hook"
    if any(state.get("unknown_dispatches", {}).values()):
        return "working", "review_applicability_unknown"
    if any(item["status"] in ("expected", "running") for item in state["reviews"].values()):
        return "working", "background_review"
    if state.get("continuation"):
        return "working", "pending_continuation"
    if state.get("children"):
        return "working", "subagents_running"
    if state.get("teammate_completion_unproven"):
        return "working", "teammate_completion_unproven"
    if state.get("running"):
        return "working", "parent_running"
    if state.get("candidate"):
        return "idle", "task_complete"
    return "working", "background_work"


def observed_idle_teammates(stop, event):
    count = stop.get("teammate_count", 0)
    agents = event.get("agents")
    if not count or event.get("post_turn") is not True or not isinstance(agents, list):
        return False
    identities, teammates = set(), set()
    idle_count = 0
    for agent in agents:
        if not isinstance(agent, dict):
            return False
        identity, teammate = agent.get("id"), agent.get("teammateId")
        status = agent.get("status")
        if (not isinstance(identity, str) or not identity.strip() or identity in identities
                or status not in ("idle", "completed", "failed", "aborted")):
            return False
        identities.add(identity)
        if teammate is not None:
            if (not isinstance(teammate, str) or not teammate.strip()
                    or teammate == identity or teammate in teammates):
                return False
            teammates.add(teammate)
        if status == "idle":
            if teammate is None:
                return False
            idle_count += 1
    # Count equality is a sanity guard, not an identity join or proof of an empty inbox.
    return idle_count == count


def audit_hooks(event, config_dir=None):
    """Detect overwritten/upgraded wrappers rather than silently trusting old ids.

    Effective settings come from the native API, so local and managed sources
    are observed too. Only the merge-only installer writes user-owned files.
    """
    config_dir = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    manifest_path = config_dir / "hooks/task-lifecycle/install-manifest.json"
    manifest = load_json(manifest_path, {})
    records = manifest.get("wrappers", [])
    if not isinstance(records, list):
        return [], True
    known = {record["id"]: record for record in records if isinstance(record, dict) and "id" in record}
    hook_event = event.get("hook_event_name", "Stop")
    effective = event.get("effective_hooks", [])
    groups = list(effective) if isinstance(effective, list) else []
    unknown = not isinstance(effective, list)
    enabled = event.get("enabled_plugins", {})
    registry = load_json(config_dir / "plugins/installed_plugins.json", {}).get("plugins", {})
    # Read installed plugin hooks afresh: a plugin upgrade may replace a wrapper.
    for name, flag in enabled.items() if isinstance(enabled, dict) else ():
        if not flag:
            continue
        entries = registry.get(name, [])
        if not entries:
            unknown = True
        for entry in entries:
            directory = entry.get("installPath")
            if not directory or not Path(directory).is_dir():
                unknown = True
                continue
            try:
                plugin_root = Path(directory)
                plugin = load_json(plugin_root / ".claude-plugin/plugin.json", {})
                sources = []
                default = plugin_root / "hooks/hooks.json"
                if default.exists():
                    sources.append(str(default))
                custom = plugin.get("hooks", [])
                sources += custom if isinstance(custom, list) else [custom]
                seen = set()
                for source in sources:
                    if isinstance(source, str):
                        path = (plugin_root / source).resolve()
                        if path in seen:
                            continue
                        seen.add(path)
                        if not path.is_file():
                            unknown = True
                            continue
                        definition = load_json(path, {})
                    elif isinstance(source, dict):
                        definition = source
                    else:
                        unknown = True
                        continue
                    hooks = definition.get("hooks", definition)
                    groups.extend(hooks.get(hook_event, []))
            except (ValueError, AttributeError, TypeError):
                unknown = True
    expected = []
    for group in groups:
        matcher = group.get("matcher", "")
        # Stop has no matcher. SubagentStop matches the agent type, not its id.
        match_field = "agent_type" if hook_event == "SubagentStop" else "tool_name" if hook_event == "PostToolUse" else None
        if match_field and matcher and match_field in event:
            try:
                if not re.search(matcher, event.get(match_field, "")):
                    continue
            except (re.error, TypeError):
                unknown = True
                continue
        for hook in group.get("hooks", []):
            if not hook.get("asyncRewake"):
                continue
            matches = [identifier for identifier, record in known.items()
                       if record.get("event") == hook_event and record.get("wrapped") == hook
                       and (record.get("matcher") or "") == (matcher or "")]
            if len(matches) == 1:
                expected.append(matches[0])
            else:
                unknown = True
    # The engine deduplicates identical command hooks; the installer assigns
    # stable ids. Repeated source entries should not invent another process.
    return list(dict.fromkeys(expected)), unknown


class Runtime:
    def __init__(self, state_dir, transport, *, env=None, hook_audit=audit_hooks, background_delivery=False,
                 allow_idle_teammates=False, reinstrument=None):
        self.root = Path(state_dir)
        self.transport = transport
        self.env = dict(os.environ if env is None else env)
        self.hook_audit = hook_audit
        self.background_delivery = background_delivery
        self.allow_idle_teammates = allow_idle_teammates is True
        self.reinstrument = reinstrument

    def _loaded_plugins(self, load_id, enabled, records, settings_available):
        """Plugin hooks as registered for this native module load.

        Claude keeps a session's plugin hooks until /reload-plugins, even after a
        background update installs another version; /clear keeps them too. This
        load re-wraps nothing before its snapshot exists.
        """
        if not isinstance(load_id, str) or not load_id or len(load_id) > 256:
            return None
        path = self.root / "loads" / (digest(load_id) + ".json")
        snapshot = load_json(path, None)
        if isinstance(snapshot, dict) and snapshot.get("schema") == 1:
            try:
                os.utime(path)
            except OSError:
                pass
            return snapshot
        if not settings_available or not isinstance(enabled, dict):
            return None  # The loaded plugin set is unknown: keep auditing live.
        names = sorted(name for name, flag in enabled.items() if flag)
        events = {}
        for name in AUDITED_EVENTS:
            expected, unknown = self.hook_audit({"hook_event_name": name, "effective_hooks": [],
                                                 "enabled_plugins": {plugin: True for plugin in names}})
            events[name] = {"expected": expected, "unknown": unknown}
        identifiers = {identifier for item in events.values() for identifier in item["expected"]}
        known = {record["id"]: record for record in records
                 if isinstance(record, dict) and record.get("id") in identifiers}
        # Keep the loaded matchers and conditions: a later installer run drops old records.
        snapshot = {"schema": 1, "enabled": names, "events": events,
                    "matchers": {key: record.get("matcher") or "" for key, record in known.items()},
                    "conditions": {key: (record.get("original") or {}).get("if") for key, record in known.items()}}
        save_json(path, snapshot)
        cutoff = time.time() - LOAD_RETENTION_SECONDS
        for old in path.parent.glob("*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        return snapshot

    def path(self, session_id):
        return self.root / "sessions" / (digest(session_id) + ".json")

    def payload_path(self, session_id):
        return self.root / "payloads" / (digest(session_id) + ".json")

    def _discard_payload(self, session_id, owner=None, turn_id=None):
        path = self.payload_path(session_id)
        try:
            if owner is not None:
                payload = load_json(path, {})
                if payload.get("owner") != owner or payload.get("turn_id") != turn_id:
                    return
            path.unlink(missing_ok=True)
        except OSError:
            # Cleanup cannot prevent newer work from invalidating delivery.
            pass

    def _load(self, session_id):
        return load_json(self.path(session_id), {
            "schema": 1, "session_id": session_id, "seq": 0, "revision": 0,
            "children": [], "reviews": {}, "questions": [], "permissions": [], "notified": [],
            "unknown_dispatches": {},
        })

    def _pane_path(self, env):
        if env.get("HERDR_ENV") == "1" and env.get("HERDR_PANE_ID") and env.get("HERDR_SOCKET_PATH"):
            return self.root / "panes" / (digest([env["HERDR_SOCKET_PATH"], env["HERDR_PANE_ID"]]) + ".json")
        return None

    def _emit(self, state):
        status, reason = classify(state)
        state["state"], state["reason"] = status, reason
        state["updated_at"] = time.time()
        # Save admission before I/O. Notification delivery happens outside this
        # lock so an answer/new turn can invalidate a retry immediately.
        save_json(self.path(state["session_id"]), state)
        report_env = state.get("herdr", {})
        pane_path = self._pane_path(report_env)
        if pane_path:
            with locked(pane_path.with_suffix(".lock")):
                pane = load_json(pane_path, {})
                if pane.get("owner") == state.get("owner") and pane.get("session_id") == state["session_id"]:
                    key = [state["session_id"], status, reason]
                    if pane.get("reported") != key:
                        seq = max(time.time_ns() // 1000, pane.get("report_seq", 0) + 1)
                        ok = self.transport.report(session_id=state["session_id"], state=status,
                                                   reason=reason, seq=seq, env=report_env)
                        pane["reported"] = key if ok else None
                        pane["report_seq"] = seq
                        save_json(pane_path, pane)
        return {"state": status, "reason": reason, "session_id": state["session_id"],
                "pending_reviews": sum(r["status"] in ("expected", "running") for r in state["reviews"].values())}

    @staticmethod
    def _notices(state):
        notices = [("question", tool) for tool in state["questions"]]
        if classify(state)[0] == "idle" and state.get("candidate"):
            notices.append(("done", state["candidate"]["turn_id"]))
        elif state.get("closed") and state.get("final_notice"):
            notices.append(("done", state["final_notice"]))
        settled = state["notified"] + state.get("delivery_failed", [])
        return [(kind, identity) for kind, identity in notices if digest([kind, identity]) not in settled]

    def _cancel_delivery(self, session_id):
        queue = self.root / "delivery-queue-v2.json"
        if not queue.exists():
            return
        try:
            with locked(self.root / "delivery-control-v2.lock"):
                jobs = load_json(queue, {})
                if jobs.pop(digest(session_id), None) is not None:
                    save_json(queue, jobs)
        except (OSError, TimeoutError, ValueError, TypeError, AttributeError):
            # Queue cleanup is best-effort; newer lifecycle state MUST still be
            # persisted so current() can reject the old job before any HTTP.
            pass

    def _start_delivery_worker(self, target):
        """Share workers only for the same admitted consent and destination."""
        workers = self.root / "delivery-workers-v2"
        workers.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(workers / (digest(target) + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            env = {**self.env, "CLAUDE_TASK_LIFECYCLE_STATE_DIR": str(self.root.resolve())}
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--delivery-worker", str(descriptor)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, pass_fds=(descriptor,), env=env)
            # Reap when the caller is long-lived, but never keep a command hook
            # alive waiting for HTTP recovery. The worker owns the inherited fd.
            threading.Thread(target=process.wait, daemon=True).start()
            # Do not re-read another caller's config in the detached process.
            # The destination crosses an anonymous pipe, never argv or disk.
            process.stdin.write(json.dumps(target).encode())
            process.stdin.close()
        finally:
            # Do not LOCK_UN here: flock ownership is shared with the child.
            os.close(descriptor)

    def _dispatch(self, session_id, result):
        if result.get("ignored") or not session_id:
            return
        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            notices = self._notices(state)
            if not notices:
                self._cancel_delivery(session_id)
                return
            owner = state.get("owner")
            target = self.transport.notification_target() if self.background_delivery else None
            if target and target["topic"] is not None:
                generation = digest([owner, state["revision"], notices])
                with locked(self.root / "delivery-control-v2.lock"):
                    queue = self.root / "delivery-queue-v2.json"
                    jobs = load_json(queue, {})
                    key = digest(session_id)
                    previous = jobs.get(key, {})
                    # Re-admission must fence a worker that already observed a
                    # closed review gate, even for the same owner/turn/generation.
                    jobs[key] = {"session_id": session_id, "owner": owner, "generation": generation,
                                  "admission": uuid.uuid4().hex, "route": digest(target),
                                  "nextAttemptAt": previous.get("nextAttemptAt", 0)
                                  if previous.get("generation") == generation and previous.get("route") == digest(target) else 0}
                    save_json(queue, jobs)
                    self._start_delivery_worker(target)
                return
            self._cancel_delivery(session_id)
        self.deliver_pending(session_id, owner)

    def delivery_worker(self, descriptor):
        """One detached worker per destination, only while its admitted work remains.

        Queue entries contain metadata, never notification text or transport
        configuration. Retry time does not grant admission: deliver_pending and
        Transport.current re-read all lifecycle/owner/review gates before HTTP.
        """
        target = self.transport.notification_target()
        route = digest(target)
        delivery_transport = Transport(self.root, **target)
        # A distinct queue also fences pre-publication workers that lack routing.
        queue = self.root / "delivery-queue-v2.json"
        try:
            while True:
                try:
                    with locked(self.root / "delivery-control-v2.lock"):
                        jobs = {key: job for key, job in load_json(queue, {}).items() if job.get("route") == route}
                        if not jobs:
                            # Serialize exit with enqueue + spawn. A newly queued job
                            # cannot mistake this exiting worker for a live owner.
                            os.close(descriptor)
                            descriptor = None
                            return
                    wake_at = time.time() + 1
                    for key, job in jobs.items():
                        if job["nextAttemptAt"] > time.time():
                            wake_at = min(wake_at, job["nextAttemptAt"])
                            continue
                        try:
                            due = self.deliver_pending(job["session_id"], job["owner"], job, delivery_transport)
                        except (OSError, ValueError, TypeError, TimeoutError):
                            due = time.time() + 30
                        with locked(self.root / "delivery-control-v2.lock"):
                            latest = load_json(queue, {})
                            if latest.get(key) != job:
                                continue  # Cancellation/new generation won the race.
                            if due is None:
                                latest.pop(key)
                            else:
                                latest[key] = {**job, "nextAttemptAt": due}
                                wake_at = min(wake_at, due)
                            if latest.get(key) != job:
                                save_json(queue, latest)
                    # This poll only reads the queue; it neither republishes nor
                    # rewrites deferred records. New sessions wake within one second.
                    time.sleep(max(0, min(1, wake_at - time.time())))
                except (OSError, TimeoutError):
                    # Keep singleton ownership when queue reads/writes fail.
                    # Retry storage after a bounded pause, outside queue locks.
                    time.sleep(30)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def deliver_pending(self, session_id, owner, admission=None, transport=None):
        def admitted():
            return admission is None or load_json(self.root / "delivery-queue-v2.json", {}).get(digest(session_id)) == admission

        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            if state.get("owner") != owner or not admitted():
                return
            notices = self._notices(state)
        next_attempt = None
        for kind, notice_id in notices:
            message = None
            if kind == "done":
                with locked(self.path(session_id).with_suffix(".lock")):
                    payload = load_json(self.payload_path(session_id), {})
                    if payload.get("owner") == owner and payload.get("turn_id") == notice_id:
                        message = payload.get("text")
            def current():
                with locked(self.path(session_id).with_suffix(".lock")):
                    latest = self._load(session_id)
                    pane = self._pane_path(latest.get("herdr", {}))
                    return (admitted() and latest.get("owner") == owner and (kind, notice_id) in self._notices(latest)
                            and (not pane or load_json(pane, {}).get("owner") == owner))
            receipt = (transport or self.transport).publish(session_id=session_id, kind=kind, notice_id=notice_id,
                                                           message=message, is_current=current)
            with locked(self.path(session_id).with_suffix(".lock")):
                latest = self._load(session_id)
                if not admitted() or latest.get("owner") != owner or (kind, notice_id) not in self._notices(latest):
                    continue
                if receipt.get("status") in ("sent", "duplicate", "disabled") or receipt.get("terminal"):
                    # A permanent HTTP rejection is settled, never called sent.
                    target = "delivery_failed" if receipt.get("terminal") else "notified"
                    latest[target] = (latest.get(target, []) + [digest([kind, notice_id])])[-256:]
                    save_json(self.path(session_id), latest)
                    if kind == "done":
                        self._discard_payload(session_id, owner, notice_id)
                elif receipt.get("nextAttemptAt"):
                    due = receipt["nextAttemptAt"]
                    next_attempt = due if next_attempt is None else min(next_attempt, due)
        return next_attempt

    def _invalidate(self, state, keep_payload=False):
        if not keep_payload:
            self._discard_payload(state["session_id"])
            # Activity invalidates a completion, not an unanswered question.
            # Answer/end/bind clear questions through their own admission paths.
            if not state["questions"]:
                self._cancel_delivery(state["session_id"])
        state["revision"] += 1
        state["candidate"] = None
        state["stop"] = None
        state["final_notice"] = None
        state["teammate_completion_unproven"] = False

    def _prepare(self, state, event, expected, unknown):
        state["unknown_hook"] = bool(unknown) or state.get("unknown_hook", False)
        for hook in expected:
            # Classic command hooks run concurrently. A review can start AND
            # finish before our snapshot command gets CPU. Claim that same run
            # once; an earlier, already-observed Stop is a different dispatch.
            unmatched = next((item for item in state["reviews"].values()
                              if item["hook_id"] == hook and item["event_key"] == event["event_key"]
                              and not item.get("snapshot_seen")), None)
            if unmatched:
                unmatched["snapshot_seen"] = True
            else:
                state["reviews"][uuid.uuid4().hex] = {
                    "hook_id": hook, "event_key": event["event_key"], "status": "expected",
                    "owner": state["owner"], "snapshot_seen": True}
        if event.get("hook_event_name") == "Stop" and not event.get("agent_id"):
            tasks = event.get("background_tasks")
            teammate_ids = [task["id"] for task in tasks
                            if isinstance(task, dict) and task.get("type") == "teammate"
                            and task.get("status") == "running" and isinstance(task.get("id"), str)
                            and task["id"].strip()] if isinstance(tasks, list) else []
            queue_clear = event.get("session_crons") == [] and event.get("queue_empty") is not False
            teammate_count = len(teammate_ids) if (queue_clear and isinstance(tasks, list)
                                                  and len(set(teammate_ids)) == len(tasks)) else 0
            state["stop"] = {
                "key": event["event_key"], "revision": state["revision"], "turn_id": state.get("turn_id"),
                "empty": tasks == [] and queue_clear, "teammate_count": teammate_count, "accepted": False}

    def observe_hook(self, event):
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return {"ignored": True}
        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            if not state.get("owner") or state["owner"] != self.env.get("CLAUDE_TASK_LIFECYCLE_OWNER"):
                return {"ignored": True, "reason": "unbound_hook"}
            name = event.get("hook_event_name")
            if name in ("Stop", "SubagentStop", "PostToolUse", "UserPromptSubmit"):
                inventory = state.get("hook_inventory", {}).get(name)
                conditions = {}
                if inventory is not None and self.hook_audit is audit_hooks:
                    expected = inventory["expected"]
                    unknown = inventory["unknown"]
                    conditions = inventory.get("conditions", {})
                    match_field = "agent_type" if name == "SubagentStop" else "tool_name" if name == "PostToolUse" else None
                    if match_field:
                        matches = []
                        for identifier in expected:
                            matcher = inventory.get("matchers", {}).get(identifier, "")
                            try:
                                if not matcher or re.search(matcher, event.get(match_field, "")):
                                    matches.append(identifier)
                            except (re.error, TypeError):
                                unknown = True
                        expected = matches
                else:
                    expected, unknown = self.hook_audit(event)
                key = event_fingerprint(event)
                config_dir = Path(self.env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                records = load_json(config_dir / "hooks/task-lifecycle/install-manifest.json", {}).get("wrappers", [])
                by_id = {record["id"]: record for record in records}
                applicable, uncertain = [], []
                for identifier in expected:
                    if identifier in conditions:
                        condition = conditions[identifier]
                    else:
                        condition = by_id.get(identifier, {}).get("original", {}).get("if")
                    answer = condition_matches(condition, event)
                    if answer is True:
                        applicable.append(identifier)
                    elif answer is None:
                        uncertain.append(identifier)
                seen = {item["hook_id"] for item in state["reviews"].values()
                        if item["event_key"] == key and not item.get("snapshot_seen")}
                state.setdefault("unknown_dispatches", {})[key] = sorted(set(uncertain) - seen)
                for item in state["reviews"].values():
                    if item["event_key"] == key and item["hook_id"] in uncertain:
                        item["snapshot_seen"] = True
                self._prepare(state, {**event, "event_key": key}, applicable, unknown)
            elif name == "SubagentStart" and event.get("agent_id"):
                self._invalidate(state)
                state["children"] = sorted(set(state["children"]) | {event["agent_id"]})
            elif name == "PermissionRequest" and event.get("tool_use_id"):
                state["permissions"] = sorted(set(state["permissions"]) | {event["tool_use_id"]})
            return self._emit(state)

    def accept(self, event):
        result = self._accept_locked(event)
        if event.get("action") in ("question_begin", "question_end", "complete", "end"):
            self._dispatch(event.get("session_id"), result)
        return result

    def _accept_locked(self, event):
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            return {"ignored": True, "reason": "invalid_session"}
        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            action = event.get("action")
            if action == "bind":
                born = event.get("born_at", 0)
                if born < state.get("born_at", 0):
                    return {"ignored": True, "reason": "old_owner"}
                herdr = {key: self.env[key] for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH") if key in self.env}
                pane_path = self._pane_path(herdr)
                if pane_path:
                    with locked(pane_path.with_suffix(".lock")):
                        pane = load_json(pane_path, {})
                        if born < pane.get("born_at", 0):
                            return {"ignored": True, "reason": "old_pane_owner"}
                        save_json(pane_path, {"owner": event["owner"], "session_id": session_id,
                                              "born_at": born, "report_seq": pane.get("report_seq", 0)})
                state.update(owner=event["owner"], born_at=born, seq=event["seq"],
                             running=False, closed=False, failure=None, questions=[], permissions=[], children=[],
                             # A new module load registers its own hooks; its configure re-audits them.
                             unknown_hook=False, reload_required=False)
                self._invalidate(state)
                state["herdr"] = herdr
                save_json(self.path(session_id), state)
                return {"bound": True}
            if event.get("owner") != state.get("owner") or event.get("seq", 0) <= state.get("seq", 0):
                return {"ignored": True, "reason": "stale_event"}
            pane_path = self._pane_path(state.get("herdr", {}))
            if pane_path and load_json(pane_path, {}).get("owner") != state["owner"]:
                return {"ignored": True, "reason": "old_pane_owner"}
            state["seq"] = event["seq"]
            if action == "configure":
                inventory = {}
                config_dir = Path(self.env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                wrappers = load_json(config_dir / "hooks/task-lifecycle/install-manifest.json", {}).get("wrappers", [])
                hooks = event.get("hooks", {})
                enabled = event.get("enabled_plugins", {})
                loaded = self._loaded_plugins(event.get("load_id"), enabled, wrappers,
                                              isinstance(hooks.get("Stop"), list))
                matchers = {record["id"]: record.get("matcher", "") for record in wrappers}
                conditions = {}
                live_plugins = enabled
                if loaded is not None:
                    matchers.update(loaded["matchers"])
                    conditions = loaded["conditions"]
                    # A plugin enabled after this load is audited as installed now.
                    live_plugins = ({plugin: flag for plugin, flag in enabled.items()
                                     if flag and plugin not in loaded["enabled"]} if isinstance(enabled, dict) else {})
                for name in AUDITED_EVENTS:
                    # Settings hooks are always read live from the native merged settings.
                    expected, unknown = self.hook_audit({
                        "hook_event_name": name, "effective_hooks": hooks.get(name, []),
                        "enabled_plugins": live_plugins})
                    if loaded is not None:
                        expected = list(dict.fromkeys(loaded["events"][name]["expected"] + expected))
                        unknown = unknown or loaded["events"][name]["unknown"]
                    inventory[name] = {"expected": expected, "unknown": unknown,
                                       "matchers": matchers, "conditions": conditions}
                state["hook_inventory"] = inventory
                # Never before this load has its own snapshot; later loads register the re-wrapped files.
                if loaded is not None and event.get("phase") in ("load", "turn") and self.reinstrument is not None:
                    self.reinstrument()
                reload_required = False
                if loaded is not None and any(loaded["events"][name]["unknown"] for name in AUDITED_EVENTS):
                    # /reload-plugins registers the currently installed versions instead.
                    current = {plugin: True for plugin in loaded["enabled"]}
                    reload_required = not any(
                        self.hook_audit({"hook_event_name": name, "effective_hooks": [], "enabled_plugins": current})[1]
                        for name in AUDITED_EVENTS if loaded["events"][name]["unknown"])
                state["reload_required"] = reload_required
                save_json(self.path(session_id), state)
                return {"configured": True, "reload_required": reload_required}
            elif action in ("start", "activity", "child_start", "child_complete"):
                self._invalidate(state)
                state["closed"] = False
                if action == "start":
                    state.update(turn_id=event.get("turn_id"), running=True, failure=None, continuation=False)
                elif action == "activity":
                    state["running"] = True
                    if event.get("feedback_delivered") and state.get("continuation"):
                        state["feedback_seen"] = True
                    if event.get("parent_step") and state.get("feedback_seen"):
                        state["continuation"] = False
                        state["feedback_seen"] = False
                else:
                    child = event.get("agent_id")
                    if child:
                        children = set(state["children"])
                        if action == "child_start":
                            children.add(child)
                        else:
                            children.discard(child)
                        state["children"] = sorted(children)
            elif action == "prepare_stop":
                expected, unknown = self.hook_audit(event)
                self._prepare(state, event, expected, unknown)
            elif action == "stop_verdict":
                stop = state.get("stop")
                if stop and stop["key"] == event.get("event_key"):
                    stop["accepted"] = event.get("accepted") is True
            elif action == "complete":
                if event.get("turn_id") != state.get("turn_id"):
                    return {"ignored": True, "reason": "old_turn"}
                state["running"] = False
                stop = state.get("stop") or {}
                state["teammate_completion_unproven"] = bool(
                    stop.get("teammate_count") and not stop.get("empty") and self.allow_idle_teammates is not True)
                if not stop.get("teammate_count"):
                    state["questions"], state["permissions"] = [], []
                # Native turn.complete is emitted after the engine's Stop-loop
                # decision. User classic.* mods are deliberately bypassed by
                # Claude's default security mod; a command hook supplies the
                # snapshot, and this native event supplies finality.
                if event.get("post_turn") is True:
                    stop["accepted"] = True
                if event.get("reason") != "answer":
                    state["failure"] = "turn_" + str(event.get("reason", "unknown"))
                    state["candidate"] = None
                elif (event.get("has_answer") is True and stop.get("accepted")
                      and (stop.get("empty") or (self.allow_idle_teammates is True and observed_idle_teammates(stop, event)))
                      and stop.get("revision") == state["revision"] and stop.get("turn_id") == event["turn_id"]
                      and not state["children"] and not state.get("continuation")
                      and not state["questions"] and not state["permissions"]):
                    state["candidate"] = {"turn_id": event["turn_id"]}
                    text = bounded_message(event.get("answer_text"))
                    if text is not None:
                        # A private, short-lived outbox, separate from status and
                        # receipts. Reviews may settle after the native turn ends.
                        save_json(self.payload_path(session_id), {
                            "owner": state["owner"], "turn_id": event["turn_id"], "text": text})
            elif action in ("question_begin", "question_end", "permission_begin", "permission_end"):
                collection = "questions" if action.startswith("question") else "permissions"
                tool_id = event.get("tool_id")
                if tool_id:
                    values = set(state[collection])
                    if action.endswith("begin"):
                        self._invalidate(state)
                        values.add(tool_id)
                    else:
                        values.discard(tool_id)
                    state[collection] = sorted(values)
            elif action == "end":
                final_notice = state["candidate"]["turn_id"] if classify(state)[0] == "idle" and state.get("candidate") else None
                self._invalidate(state, keep_payload=final_notice is not None)
                state.update(closed=True, questions=[], permissions=[], running=False, final_notice=final_notice)
            elif action != "inspect":
                return {"ignored": True, "reason": "unknown_event"}
            return self._emit(state)

    def begin_hook(self, session_id, event_key, hook_id):
        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            invoking_owner = self.env.get("CLAUDE_TASK_LIFECYCLE_OWNER")
            if not invoking_owner or invoking_owner != state.get("owner"):
                return ""
            for run_id, item in state["reviews"].items():
                if (item["event_key"] == event_key and item["hook_id"] == hook_id
                        and item["status"] == "expected" and item.get("owner") == invoking_owner):
                    item["status"] = "running"
                    save_json(self.path(session_id), state)
                    return run_id
            # The module missed this launch (or was not loaded). Account for it,
            # but never fabricate a completed turn from a wrapper alone.
            run_id = uuid.uuid4().hex
            uncertain = state.setdefault("unknown_dispatches", {}).get(event_key, [])
            was_expected = hook_id in uncertain
            if was_expected:
                uncertain.remove(hook_id)
            state["reviews"][run_id] = {"event_key": event_key, "hook_id": hook_id,
                                        "status": "running", "owner": state.get("owner"), "snapshot_seen": was_expected}
            if not was_expected:
                self._invalidate(state)
            self._emit(state)
            return run_id

    def finish_hook(self, session_id, run_id, exit_code):
        result = self._finish_locked(session_id, run_id, exit_code)
        self._dispatch(session_id, result)
        return result

    def _finish_locked(self, session_id, run_id, exit_code):
        with locked(self.path(session_id).with_suffix(".lock")):
            state = self._load(session_id)
            item = state["reviews"].get(run_id)
            if not item or item["status"] not in ("expected", "running"):
                return {"ignored": True, "reason": "settled_hook"}
            item["status"] = "ok" if exit_code == 0 else "continuation" if exit_code == 2 else "error"
            if item.get("owner") != state.get("owner"):
                save_json(self.path(session_id), state)
                return {"ignored": True, "reason": "old_hook_owner"}
            if exit_code:
                self._invalidate(state)
                if exit_code == 2:
                    state["continuation"] = True
                    state["feedback_seen"] = False
                else:
                    state["failure"] = "review_failed"
            return self._emit(state)


def reinstrument_plugins(config_dir=None):
    """Re-wrap updated versions of already instrumented plugins; never blocks a turn.

    A concurrent installer or session holding the installation lock wins; the
    next turn retries. Returns whether any file changed.
    """
    config_dir = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if config_dir.name != ".claude":
        return False  # The installer only manages <home>/.claude.
    try:
        import install
        installation = install.Installation(config_dir.parent, Path(__file__).resolve().parent)
        if not (installation.target / "install-manifest.json").is_file():
            return False
        with locked(installation.target / ".install.lock", timeout=0):
            installation.plan_reinstrument()
            installation.apply()
        return bool(installation.changes)
    except Exception:
        # Maintenance only: a failure leaves the update untracked, which already
        # keeps completion pending. It must not make the lifecycle unavailable.
        return False


def default_runtime():
    from configuration import load_config
    config = load_config()
    home = Path.home()
    root = Path(os.environ.get("CLAUDE_TASK_LIFECYCLE_STATE_DIR") or
                Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "claude/task-lifecycle")
    topic_file = os.environ.get("CLAUDE_TASK_LIFECYCLE_TOPIC_FILE")
    notification = config["notifications"]
    topic = notification["topic"] if notification["enabled"] else None
    endpoint = os.environ.get("CLAUDE_TASK_LIFECYCLE_ENDPOINT", notification["endpoint"])
    return Runtime(root, Transport(root, topic_file, endpoint=endpoint, topic=topic), background_delivery=True,
                   allow_idle_teammates=config["claude"]["allowIdleTeammates"],
                   reinstrument=reinstrument_plugins if config["claude"]["reinstrumentPluginUpdates"] else None)


def begin_hook(hook_input, hook_id):
    return default_runtime().begin_hook(hook_input["session_id"], event_fingerprint(hook_input), hook_id)


def finish_hook(hook_input, hook_id, run_id, exit_code):
    return default_runtime().finish_hook(hook_input["session_id"], run_id, exit_code)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--delivery-worker":
            root = Path(os.environ["CLAUDE_TASK_LIFECYCLE_STATE_DIR"])
            target = json.loads(sys.stdin.read(2_000_000))
            Runtime(root, Transport(root, **target)).delivery_worker(int(sys.argv[2]))
        elif len(sys.argv) == 4 and sys.argv[1] == "--deliver":
            default_runtime().deliver_pending(sys.argv[2], sys.argv[3])
        else:
            data = json.loads(sys.stdin.read(2_000_000))
            result = default_runtime().accept(data)
            print(json.dumps(result))
    except Exception:
        # Never include exception strings: file paths/URLs may be private.
        print('{"error":"lifecycle_unavailable"}')
        raise SystemExit(1)
