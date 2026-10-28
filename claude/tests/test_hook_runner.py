"""Run real child processes; only the runtime bookkeeping boundary is stubbed."""
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


RUNTIME_STUB = '''
import json
import os
from pathlib import Path
import sys
import time

def record(value):
    with Path(os.environ["RUNTIME_RECORDS"]).open("a") as stream:
        stream.write(json.dumps(value) + "\\n")

def begin_hook(hook_input, hook_id):
    print("private-runtime-output")
    print("private-runtime-error", file=sys.stderr)
    if os.environ.get("RUNTIME_BREAK") == "begin":
        raise RuntimeError("private-exception")
    if os.environ.get("RUNTIME_BREAK") == "hang":
        time.sleep(60)
    record({"operation": "begin", "input": hook_input, "id": hook_id})
    return "test-run-id"

def finish_hook(hook_input, hook_id, run_id, exit_code):
    record({"operation": "finish", "input": hook_input, "id": hook_id,
            "run_id": run_id, "exit_code": exit_code})
    if os.environ.get("RUNTIME_BREAK") == "finish":
        raise RuntimeError("private-exception")
'''


class HookRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "hook_runner.py").is_file(), "hook runner is not implemented yet")
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-runner-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runner = self.root / "hook_runner.py"
        shutil.copyfile(ROOT / "hook_runner.py", self.runner)
        (self.root / "runtime.py").write_text(RUNTIME_STUB)
        self.records_path = self.root / "records.jsonl"
        self.env = {**os.environ, "RUNTIME_RECORDS": str(self.records_path),
                    "PYTHONDONTWRITEBYTECODE": "1", "CLAUDE_PLUGIN_ROOT": str(self.root / "plugin with spaces")}
        self.payload = b' { "session_id": "session-1", "hook_event_name": "Stop", "private": "unchanged" } \n'

    def records(self):
        return [json.loads(line) for line in self.records_path.read_text().splitlines()] if self.records_path.exists() else []

    def invocation(self, *args):
        return [sys.executable, "-B", str(self.runner), "--id", "hook-test", *args]

    def run_hook(self, *args, payload=None):
        return subprocess.run(self.invocation(*args), input=self.payload if payload is None else payload,
                              capture_output=True, env=self.env, cwd=self.root, timeout=10)

    def test_exec_preserves_exact_input_argv_output_error_and_nonzero_status(self):
        code = ("import os,sys; raw=sys.stdin.buffer.read(); sys.stdout.buffer.write(raw); "
                "sys.stderr.buffer.write(sys.argv[1].encode()); sys.exit(7)")
        argument = "literal $HOME; 'quotes' and spaces"
        result = self.run_hook("--", sys.executable, "-c", code, argument)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, self.payload, argument.encode()))
        self.assertEqual(self.records(), [
            {"operation": "begin", "input": json.loads(self.payload), "id": "hook-test"},
            {"operation": "finish", "input": json.loads(self.payload), "id": "hook-test",
             "run_id": "test-run-id", "exit_code": 7}])

    def test_shell_expands_plugin_root_at_execution_and_preserves_bash_semantics(self):
        command = '[[ -n "$CLAUDE_PLUGIN_ROOT" ]] && printf "%s" "$CLAUDE_PLUGIN_ROOT"; cat; printf error >&2; exit 9'
        result = self.run_hook("--shell", command)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout, self.env["CLAUDE_PLUGIN_ROOT"].encode() + self.payload)
        self.assertEqual(result.stderr, b"error")
        self.assertEqual(self.records()[-1]["exit_code"], 9)

    def test_non_json_and_non_object_input_reaches_original_unchanged(self):
        for raw in (b"\xff\x00not-json\n", b"[1, 2]\n", b""):
            with self.subTest(raw=repr(raw)):
                result = self.run_hook("--", sys.executable, "-c",
                                       "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", payload=raw)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, raw, b""))
                self.assertEqual(self.records()[-1]["input"], {})

    def test_unparseably_nested_input_does_not_prevent_original_execution(self):
        raw = b"[" * 2000 + b"0" + b"]" * 2000
        result = self.run_hook("--", sys.executable, "-c",
                               "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", payload=raw)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, raw, b""))
        self.assertEqual(self.records()[-1]["input"], {})

    def test_runtime_import_or_callback_failure_cannot_change_original_result(self):
        for failure in ("begin", "finish", "import"):
            with self.subTest(failure=failure):
                self.env["RUNTIME_BREAK"] = failure
                if failure == "import":
                    (self.root / "runtime.py").write_text('raise RuntimeError("private-import-error")\n')
                result = self.run_hook("--shell", "printf original; printf stderr >&2; exit 23")
                self.assertEqual((result.returncode, result.stdout, result.stderr), (23, b"original", b"stderr"))

    def test_stuck_bookkeeping_is_bounded_and_does_not_block_original_hook(self):
        self.env["RUNTIME_BREAK"] = "hang"
        started = time.monotonic()
        result = self.run_hook("--shell", "printf finished; exit 4")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (4, b"finished", b""))

    def test_child_stdout_and_stderr_are_forwarded_while_it_is_still_running(self):
        child = self.root / "stream.py"
        child.write_text("import sys\nsys.stdout.write('ready\\n'); sys.stdout.flush()\n"
                         "sys.stderr.write('live\\n'); sys.stderr.flush()\n"
                         "from pathlib import Path\nimport time\n"
                         "while not Path('release').exists(): time.sleep(.01)\n")
        process = subprocess.Popen(self.invocation("--", sys.executable, str(child)), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.root)
        self.addCleanup(self.cleanup_process, process)
        process.stdin.write(self.payload)
        process.stdin.close()
        process.stdin = None
        ready, _, _ = select.select([process.stdout], [], [], 4)
        self.assertTrue(ready, "stdout was buffered until the child exited")
        self.assertEqual(process.stdout.readline(), b"ready\n")
        ready, _, _ = select.select([process.stderr], [], [], 1)
        self.assertTrue(ready, "stderr was buffered until the child exited")
        self.assertEqual(process.stderr.readline(), b"live\n")
        self.assertIsNone(process.poll())
        (self.root / "release").touch()
        stdout, stderr = process.communicate(timeout=4)
        self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def test_cancellation_reaches_shell_child_and_settles_nonzero(self):
        child = self.root / "cancel.py"
        child.write_text("import signal,sys,time\nfrom pathlib import Path\n"
                         "def cancel(sig, frame):\n    Path('signalled').write_text(str(sig))\n    sys.exit(0)\n"
                         "signal.signal(signal.SIGTERM, cancel)\nprint('ready', flush=True)\n"
                         "while True: time.sleep(.05)\n")
        command = shlex.join([sys.executable, str(child)]) + "; :"
        process = subprocess.Popen(self.invocation("--shell", command), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.root)
        self.addCleanup(self.cleanup_process, process)
        process.stdin.write(self.payload)
        process.stdin.close()
        process.stdin = None
        ready, _, _ = select.select([process.stdout], [], [], 4)
        self.assertTrue(ready)
        self.assertEqual(process.stdout.readline(), b"ready\n")
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((self.root / "signalled").read_text(), str(signal.SIGTERM))
        self.assertEqual(self.records()[-1]["operation"], "finish")
        self.assertNotEqual(self.records()[-1]["exit_code"], 0)

    def test_signal_death_and_missing_executable_are_not_reported_as_success(self):
        result = self.run_hook("--", sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
        self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertNotEqual(self.records()[-1]["exit_code"], 0)
        result = self.run_hook("--", str(self.root / "missing-executable"))
        self.assertEqual(result.returncode, 127)
        self.assertEqual(self.records()[-1]["exit_code"], 127)

    def test_sigkill_death_preserves_original_signal_without_a_traceback(self):
        result = self.run_hook("--", sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGKILL)")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (-signal.SIGKILL, b"", b""))
        self.assertNotEqual(self.records()[-1]["exit_code"], 0)

    def test_cancellation_is_bounded_even_when_shell_descendant_ignores_term(self):
        child = self.root / "ignore.py"
        child.write_text("import os,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                         "print(os.getpgrp(), flush=True)\nwhile True: time.sleep(.05)\n")
        command = shlex.join([sys.executable, str(child)]) + "; :"
        process = subprocess.Popen(self.invocation("--shell", command), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.root)
        self.addCleanup(self.cleanup_process, process)
        process.stdin.write(self.payload)
        process.stdin.close()
        process.stdin = None
        ready, _, _ = select.select([process.stdout], [], [], 4)
        self.assertTrue(ready)
        group = int(process.stdout.readline())
        self.addCleanup(self.cleanup_group, group)
        process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            self.fail("a cancelled shell left its descendant running with hook output pipes open")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((stdout, stderr), (b"", b""))
        self.assertNotEqual(self.records()[-1]["exit_code"], 0)

    @staticmethod
    def cleanup_group(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    unittest.main()
