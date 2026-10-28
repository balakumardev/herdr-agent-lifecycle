"""Portable Herdr/ntfy transport; lifecycle decisions belong to the observer."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request
import uuid


@contextmanager
def locked(path, timeout=12):
    """Keep the lock inode stable: unlinking a lock races another publisher."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("lifecycle lock") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bounded_message(text, limit=4000):
    """Keep text below ntfy's default message limit, on a UTF-8 boundary."""
    if not isinstance(text, str) or not text.strip():
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text
    suffix = "…"
    return encoded[:limit - len(suffix.encode())].decode("utf-8", errors="ignore") + suffix


def load_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def retry_after(value, now):
    """Return only a numeric deadline; never retain the response header itself."""
    if not value:
        return 0
    try:
        if value.strip().isdigit():
            return now + int(value.strip())
        date = parsedate_to_datetime(value)
        return max(now, date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp())
    except (ValueError, TypeError, OverflowError):
        return 0


class Transport:
    def __init__(self, state_dir, topic_file, endpoint="https://ntfy.sh", timeout=2,
                 retry_delay=30, max_retry_delay=900):
        self.state_dir = Path(state_dir)
        self.topic_file = Path(topic_file)
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        endpoint_key = hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.endpoint_path = self.state_dir / "endpoints" / (endpoint_key + ".json")

    def _delay(self, attempts):
        # Cap the exponent as well as the delay for a long-running outage.
        return min(self.max_retry_delay, self.retry_delay * 2 ** min(attempts - 1, 16))

    def _receipt(self, receipt):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state_dir / "receipts.jsonl"
        data = (json.dumps(receipt, separators=(",", ":")) + "\n").encode()
        with os.fdopen(os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600), "ab") as handle:
            handle.write(data)
        return receipt

    def _send(self, topic, title, message):
        request = urllib.request.Request(
            self.endpoint + "/" + topic, data=message.encode(), method="POST",
            headers={"Title": title, "Priority": "3", "Content-Type": "text/plain; charset=utf-8"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                return {"httpStatus": response.status}
        except urllib.error.HTTPError as error:
            status = error.code
            deadline = retry_after(error.headers.get("Retry-After"), time.time())
            error.close()
            return {"reason": "http", "httpStatus": status, "retryAfter": deadline}
        except (TimeoutError, socket.timeout):
            return {"reason": "timeout"}
        except (urllib.error.URLError, OSError, ValueError):
            return {"reason": "network"}

    def publish(self, *, session_id, kind, notice_id, message=None, is_current=lambda: True):
        # Keys are opaque ids, never prompt, question, tool, or response bodies.
        key = hashlib.sha256(json.dumps([session_id, kind, notice_id]).encode()).hexdigest()
        receipt = {"keyHash": key, "time": datetime.now(timezone.utc).isoformat(), "event": kind}
        if kind not in ("question", "done") or not session_id or not notice_id:
            return {**receipt, "status": "disabled", "reason": "invalid_event"}
        try:
            source = self.topic_file.read_text()
        except OSError:
            source = ""
        match = re.search(r"^\s*TOPIC=(?:\"([A-Za-z0-9_-]+)\"|'([A-Za-z0-9_-]+)'|([A-Za-z0-9_-]+))\s*(?:#.*)?$", source, re.M)
        if not match:
            return {**receipt, "status": "disabled", "reason": "no_topic"}
        topic = next(value for value in match.groups() if value)
        sent = self.state_dir / "deliveries" / (key + ".json")
        try:
            # Serialize the entire admission + HTTP exchange across sessions.
            # The per-key lock also fences a concurrent endpoint/config change.
            with locked(self.endpoint_path.with_suffix(".lock")), locked(sent.with_suffix(".lock")):
                previous = load_json(sent)
                if previous.get("status") == "sent":
                    return {**previous, "status": "duplicate"}
                if previous.get("terminal"):
                    return previous
                if not is_current():
                    return {**receipt, "status": "cancelled", "reason": "state_changed"}
                endpoint = load_json(self.endpoint_path)
                now = time.time()
                next_attempt = max(previous.get("nextAttemptAt", 0), endpoint.get("nextAttemptAt", 0))
                if now < next_attempt:
                    # Suppression is read-only: heartbeats cannot grow receipts
                    # or rewrite a retry record indefinitely.
                    return {**receipt, "status": "deferred", "attempts": previous.get("attempts", 0),
                            "nextAttemptAt": next_attempt}
                title = "Claude Code - " + ("Input Needed" if kind == "question" else "Task Complete")
                if kind == "question":
                    message = "A user question is waiting for your answer.\nClaude session: " + str(session_id)[:80]
                else:
                    message = bounded_message(message)
                    if message is None:
                        return {**receipt, "status": "failed", "reason": "no_message", "terminal": True}

                attempt = previous.get("attempts", 0) + 1
                failures = endpoint.get("failures", 0) + 1
                delay = max(self._delay(attempt), self._delay(failures))
                current = {**receipt, "attempts": attempt, "status": "pending", "terminal": False,
                           "nextAttemptAt": now + delay}
                # Reserve a cooldown durably before I/O. A process dying during
                # an ambiguous HTTP exchange cannot cause an immediate replay.
                save_json(self.endpoint_path, {"failures": failures, "nextAttemptAt": now + delay})
                save_json(sent, current)
                # Recheck after durable writes, immediately before HTTP; lifecycle
                # admission never holds this transport lock.
                if not is_current():
                    return {**receipt, "status": "cancelled", "reason": "state_changed"}
                result = self._send(topic, title, message)
                retry_at = result.pop("retryAfter", 0)
                current.update(result)
                current["status"] = "failed" if result.get("reason") else "sent"
                status = result.get("httpStatus", 0)
                transient = bool(result.get("reason")) and (not status or status in (408, 429) or status >= 500)
                if transient:
                    delay = max(delay, 60 if status == 429 else 0)
                    current["nextAttemptAt"] = max(time.time() + delay, retry_at)
                    endpoint = {"failures": failures, "nextAttemptAt": current["nextAttemptAt"]}
                else:
                    current.update(terminal=current["status"] == "failed", nextAttemptAt=0)
                    endpoint = {"failures": 0, "nextAttemptAt": 0}
                while True:
                    try:
                        save_json(self.endpoint_path, endpoint)
                        save_json(sent, current)
                        return self._receipt(current)
                    except OSError:
                        # The detached worker retains the observed response AND
                        # endpoint/key locks until settlement is durable. A fresh
                        # publisher cannot outwait the provisional 30s reservation
                        # and violate a longer Retry-After (or resend a success).
                        # No lifecycle lock is held, and no HTTP is retried here.
                        time.sleep(30)
        except (OSError, TimeoutError, ValueError, TypeError):
            # No HTTP on unreadable state, and no append-per-poll failure log.
            return {**receipt, "status": "deferred", "reason": "storage", "nextAttemptAt": time.time() + 30}

    def report(self, *, session_id, state, reason, seq, env):
        pane, socket_path = env.get("HERDR_PANE_ID"), env.get("HERDR_SOCKET_PATH")
        if env.get("HERDR_ENV") != "1" or not pane or not socket_path:
            return False
        if state not in ("working", "blocked", "idle") or not session_id:
            return False
        request = {
            "id": str(uuid.uuid4()), "method": "pane.report_agent",
            "params": {
                "pane_id": pane, "source": "custom:claude-task-lifecycle", "agent": "claude",
                "seq": seq, "state": state, "agent_session_id": session_id,
                "resume_argv": ["claude", "--resume", session_id], "message": str(reason)[:120],
            },
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(min(self.timeout, 0.5))
                client.connect(socket_path)
                client.sendall((json.dumps(request) + "\n").encode())
                data = b""
                deadline = time.monotonic() + min(self.timeout, 0.5)
                while b"\n" not in data and len(data) <= 65536:
                    client.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = client.recv(4096)
                    if not chunk or time.monotonic() > deadline:
                        return False
                    data += chunk
                ack = json.loads(data.split(b"\n", 1)[0])
                return (ack.get("id") == request["id"] and not ack.get("error")
                        and ack.get("result", {}).get("type") == "ok")
        except (OSError, ValueError, TypeError):
            return False
