#!/usr/bin/env python3
"""Show Claude as working in Herdr while it still has background shells or subagents.

Herdr's own detection already handles turns, prompts and idle. This hook only
overrides it after a turn ends with background work still running, and hands
the pane back (release) as soon as that work is gone or the user acts.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

SOURCE = "custom:claude-background-work"
AGENT = "claude"
# A named teammate stays "running" after its work ends, so it can never prove work is left.
IGNORED_TASK_TYPES = {"teammate"}
TIMEOUT_SECONDS = 1.0


def running_tasks(event):
    tasks = event.get("background_tasks")
    return [task for task in tasks if isinstance(task, dict) and task.get("status") == "running"
            and task.get("type") not in IGNORED_TASK_TYPES] if isinstance(tasks, list) else []


def request(socket_path, method, params):
    """Return (acknowledged, Herdr's error code or None)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(TIMEOUT_SECONDS)
            client.connect(socket_path)
            client.sendall((json.dumps({"id": SOURCE, "method": method, "params": params}) + "\n").encode())
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                reply += chunk
        reply = json.loads(reply)
        return reply.get("result", {}).get("type") == "ok", (reply.get("error") or {}).get("code")
    except (OSError, ValueError, AttributeError):
        return False, None


def overlay(event, env=os.environ):
    """Apply one hook event to the pane's overlay; never raises."""
    pane, socket_path = env.get("HERDR_PANE_ID"), env.get("HERDR_SOCKET_PATH")
    if env.get("HERDR_ENV") != "1" or not pane or not socket_path:
        return
    state_home = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    path = state_home / "herdr-agent-lifecycle/claude" / (hashlib.sha256(f"{socket_path}\0{pane}".encode()).hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    # One lock per pane: Stop, prompt and tool hooks of a pane can run at the same time.
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        held, seq = saved.get("state"), saved.get("seq", 0)

        def send(method, **params):
            nonlocal seq
            seq = max(seq + 1, time.time_ns() // 1000)  # Herdr drops reports that do not increase.
            return request(socket_path, method, {"pane_id": pane, "source": SOURCE, "agent": AGENT, "seq": seq, **params})

        def hold(state, message):
            nonlocal held
            # Recorded before sending: a report Herdr applied but whose reply was lost must still be released later.
            held = state
            send("pane.report_agent", state=state, message=message)

        def release():
            nonlocal held
            if not held:
                return
            # Clearing keeps the pane's agent; release_agent drops it and Herdr then shows unknown for seconds.
            acknowledged, code = send("pane.clear_agent_authority")
            if not acknowledged and code == "invalid_request":  # only a Herdr without clear_agent_authority says this
                acknowledged = send("pane.release_agent")[0]
            if acknowledged:
                held = None

        name = event.get("hook_event_name")
        tasks = running_tasks(event)
        if name == "Stop":
            # The turn is over, so Herdr would show Done: keep Working while work remains.
            if tasks:
                hold("working", f"{len(tasks)} background task(s) running")
            else:
                release()
        elif name == "SessionStart" and event.get("source") == "compact":
            pass  # /compact is not a new session, and the background work it left running still counts.
        elif name in ("UserPromptSubmit", "SessionStart", "SessionEnd"):
            release()  # New input or a new/ended session: Herdr's own detection decides again.
        elif name == "PermissionRequest" and held:
            # Herdr 0.9.3 also re-detects the prompt from the screen; this keeps it right on any version.
            hold("blocked", "background work needs permission")
        elif name == "PostToolUse" and held == "blocked":
            hold("working", "background work continues")  # The prompt was answered and the tool ran.
        if (held, seq) != (saved.get("state"), saved.get("seq", 0)):
            temporary = path.with_suffix(".tmp")  # Replace atomically so a crash never leaves unreadable state.
            temporary.write_text(json.dumps({"state": held, "seq": seq}))
            os.replace(temporary, path)


def main():
    try:
        overlay(json.load(sys.stdin))
    except Exception:
        pass  # A hook must never change Claude's behavior or print.


if __name__ == "__main__":
    main()
