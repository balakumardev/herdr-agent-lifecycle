#!/usr/bin/env python3
"""Run an asyncRewake command transparently, with bounded lifecycle bookkeeping."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


BOOKKEEPING_TIMEOUT = 2
CANCEL_GRACE = 1


def record_worker(operation):
    """Isolate runtime failures, hangs and diagnostics from the original hook."""
    response = os.dup(sys.stdout.fileno())
    try:
        with open(os.devnull, "w") as quiet:
            os.dup2(quiet.fileno(), 1)
            os.dup2(quiet.fileno(), 2)
            request = json.loads(sys.stdin.buffer.read())
            from runtime import begin_hook, finish_hook
            if operation == "begin":
                run_id = begin_hook(request["input"], request["id"])
                if isinstance(run_id, str) and 0 < len(run_id) <= 4096:
                    os.write(response, json.dumps(run_id).encode())
            else:
                finish_hook(request["input"], request["id"], request["run_id"], request["exit_code"])
        return 0
    except BaseException:
        return 1
    finally:
        os.close(response)


def record(operation, hook_input, hook_id, run_id="", exit_code=0):
    request = {"input": hook_input, "id": hook_id, "run_id": run_id, "exit_code": exit_code}
    try:
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--_record", operation],
                                input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=BOOKKEEPING_TIMEOUT,
                                start_new_session=True, check=False)
        if operation == "begin" and result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        pass
    return ""


def finish_cancellation(process_group, deadline):
    # A shell may exit before a descendant does. Keep the entire cancelled group bounded.
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_command(command, raw, hook_input, hook_id):
    child = None
    cancelled = None
    cancel_deadline = None

    def forward(signum, _frame):
        nonlocal cancelled, cancel_deadline
        if cancelled is None:
            cancelled = signum
            cancel_deadline = time.monotonic() + CANCEL_GRACE
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    run_id = ""
    exit_code = 126
    try:
        run_id = record("begin", hook_input, hook_id)
        if cancelled is not None:
            exit_code = -cancelled
        else:
            try:
                # Inherit stdout/stderr directly: no buffering, decoding, prefixing, or log copies.
                child = subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True)
                if cancelled is not None:
                    forward(cancelled, None)
                first = True
                while True:
                    try:
                        child.communicate(input=raw if first else None, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        first = False
                        if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                exit_code = child.returncode
                if cancelled is not None:
                    finish_cancellation(child.pid, cancel_deadline)
                if cancelled is not None and exit_code == 0:
                    exit_code = -cancelled
            except FileNotFoundError:
                exit_code = 127
            except OSError:
                exit_code = 126
    finally:
        if run_id:
            record("finish", hook_input, hook_id, run_id, exit_code)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return exit_code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--_record" and argv[1] in ("begin", "finish"):
        return record_worker(argv[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument("--shell")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if (args.shell is None) == (not command):
        parser.error("supply either --shell COMMAND or -- EXECUTABLE [ARG ...]")
    if args.shell is not None:
        command = [shutil.which("bash") or "/bin/sh", "-c", args.shell]
    raw = sys.stdin.buffer.read()
    try:
        hook_input = json.loads(raw)
        if not isinstance(hook_input, dict):
            hook_input = {}
    except (ValueError, UnicodeError, RecursionError):
        hook_input = {}
    return run_command(command, raw, hook_input, args.id)


if __name__ == "__main__":
    status = main()
    if status < 0:
        # Preserve POSIX signal termination as well as ordinary numeric exit statuses.
        if -status != signal.SIGKILL:
            signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
        status = 128 - status
    raise SystemExit(status)
