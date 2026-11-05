import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import background_work  # noqa: E402

SCRIPT = Path(background_work.__file__)
SHELL = {"id": "bg1", "type": "shell", "status": "running", "command": "sleep 70"}
SUBAGENT = {"id": "a1", "type": "subagent", "status": "running", "agent_type": "general-purpose"}


class FakeHerdr:
    """Unix-socket stand-in for Herdr's API that records every request."""

    def __init__(self, path, reply=b'{"result":{"type":"ok"}}\n'):
        self.requests, self.reply, self.method_replies = [], reply, {}
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    self.requests.append(json.loads(data))
                    client.sendall(self.method_replies.get(self.requests[-1]["method"], self.reply))

    def close(self):
        self.server.close()


class BackgroundWorkTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="bw"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.herdr = FakeHerdr(self.directory / "h.sock")
        self.addCleanup(self.herdr.close)
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_SOCKET_PATH": str(self.directory / "h.sock"),
                    "XDG_STATE_HOME": str(self.directory / "state")}

    def fire(self, name, tasks=None, env=None, **extra):
        event = {"hook_event_name": name, **extra}
        if tasks is not None:
            event["background_tasks"] = tasks
        background_work.overlay(event, env or self.env)

    def sent(self):
        return [(r["method"], r["params"].get("state")) for r in self.herdr.requests]

    def test_stop_with_a_running_shell_reports_working(self):
        self.fire("Stop", [SHELL])
        self.assertEqual(self.sent(), [("pane.report_agent", "working")])
        params = self.herdr.requests[0]["params"]
        self.assertEqual((params["pane_id"], params["source"], params["agent"]),
                         ("w1:p1", "custom:claude-background-work", "claude"))

    def test_stop_with_a_running_subagent_reports_working(self):
        self.fire("Stop", [SUBAGENT])
        self.assertEqual(self.sent(), [("pane.report_agent", "working")])

    def test_stop_without_background_work_sends_nothing_when_not_holding(self):
        self.fire("Stop", [])
        self.fire("Stop")
        self.assertEqual(self.sent(), [])

    def test_stop_without_background_work_releases_a_hold(self):
        self.fire("Stop", [SHELL])
        self.fire("Stop", [])
        self.assertEqual(self.sent(), [("pane.report_agent", "working"), ("pane.clear_agent_authority", None)])

    def test_finished_and_teammate_tasks_do_not_count(self):
        done = {**SHELL, "status": "completed"}
        teammate = {"id": "t1", "type": "teammate", "status": "running"}
        self.fire("Stop", [done, teammate, "junk", {"type": "shell"}])
        self.assertEqual(self.sent(), [])

    def test_a_new_prompt_releases_the_hold(self):
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit")
        self.assertEqual(self.sent()[-1], ("pane.clear_agent_authority", None))

    def test_session_boundaries_release_a_stale_hold(self):
        for name in ("SessionStart", "SessionEnd"):
            with self.subTest(name):
                self.herdr.requests.clear()
                self.fire("Stop", [SHELL])
                self.fire(name)
                self.assertEqual(self.sent(), [("pane.report_agent", "working"), ("pane.clear_agent_authority", None)])

    def test_prompt_without_a_hold_sends_nothing(self):
        self.fire("UserPromptSubmit")
        self.assertEqual(self.sent(), [])

    def test_permission_prompt_while_holding_reports_blocked_then_working_again(self):
        self.fire("Stop", [SHELL])
        self.fire("PermissionRequest")
        self.fire("PostToolUse")
        self.fire("PostToolUse")
        self.assertEqual(self.sent(), [("pane.report_agent", "working"), ("pane.report_agent", "blocked"),
                                       ("pane.report_agent", "working")])

    def test_permission_prompt_and_tools_are_ignored_when_not_holding(self):
        self.fire("PermissionRequest")
        self.fire("PostToolUse")
        self.assertEqual(self.sent(), [])

    def test_sequence_numbers_always_increase(self):
        self.fire("Stop", [SHELL])
        self.fire("Stop", [SHELL])
        self.fire("PermissionRequest")
        self.fire("UserPromptSubmit")
        seqs = [r["params"]["seq"] for r in self.herdr.requests]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(len(seqs), 4)

    def test_panes_are_tracked_separately(self):
        other = {**self.env, "HERDR_PANE_ID": "w1:p2"}
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit", env=other)
        self.assertEqual(self.sent(), [("pane.report_agent", "working")])

    def test_outside_herdr_nothing_is_sent_or_stored(self):
        for missing in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"):
            env = {key: value for key, value in self.env.items() if key != missing}
            self.fire("Stop", [SHELL], env=env)
        self.assertEqual(self.sent(), [])
        self.assertFalse((self.directory / "state").exists())

    def test_unreachable_herdr_never_raises(self):
        env = {**self.env, "HERDR_SOCKET_PATH": str(self.directory / "gone.sock")}
        self.fire("Stop", [SHELL], env=env)
        self.fire("UserPromptSubmit", env=env)
        self.assertEqual(self.sent(), [])

    def test_a_hold_whose_acknowledgement_is_lost_is_still_released_later(self):
        # Herdr may have applied the report even though no ok reply came back (slow or dropped reply).
        self.herdr.method_replies["pane.report_agent"] = b'{"error":{"code":"busy"}}\n'
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit")
        self.assertEqual([method for method, _ in self.sent()], ["pane.report_agent", "pane.clear_agent_authority"])

    def test_compact_keeps_the_hold_but_other_session_starts_release_it(self):
        self.fire("Stop", [SHELL])
        self.fire("SessionStart", source="compact")
        self.assertEqual(self.sent(), [("pane.report_agent", "working")])
        for source in ("startup", "resume", "clear"):
            with self.subTest(source):
                self.herdr.requests.clear()
                self.fire("Stop", [SHELL])
                self.fire("SessionStart", source=source)
                self.assertEqual(self.sent(), [("pane.report_agent", "working"), ("pane.clear_agent_authority", None)])

    def test_state_is_replaced_atomically_and_leaves_no_temporary_file(self):
        self.fire("Stop", [SHELL])
        names = sorted(path.suffix for path in (self.directory / "state/herdr-agent-lifecycle/claude").iterdir())
        self.assertEqual(names, [".json", ".lock"])

    def test_a_failed_release_keeps_the_hold_so_a_later_event_retries(self):
        self.fire("Stop", [SHELL])
        self.herdr.reply = b'{"error":{"code":"nope"}}\n'
        self.fire("UserPromptSubmit")
        self.herdr.reply = b'{"result":{"type":"ok"}}\n'
        self.fire("UserPromptSubmit")
        methods = [method for method, _ in self.sent()]
        # A refused clear is retried on the next event; it never falls back to dropping the agent.
        self.assertEqual(methods, ["pane.report_agent", "pane.clear_agent_authority", "pane.clear_agent_authority"])

    def test_release_falls_back_to_release_agent_when_clear_is_unsupported(self):
        self.herdr.method_replies["pane.clear_agent_authority"] = b'{"error":{"code":"invalid_request"}}\n'
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit")
        self.fire("UserPromptSubmit")
        self.assertEqual([method for method, _ in self.sent()],
                         ["pane.report_agent", "pane.clear_agent_authority", "pane.release_agent"])

    def test_release_agent_is_not_used_when_clear_fails_for_another_reason(self):
        self.herdr.method_replies["pane.clear_agent_authority"] = b'{"error":{"code":"pane_not_found"}}\n'
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit")
        self.assertNotIn("pane.release_agent", [method for method, _ in self.sent()])

    def test_release_never_drops_the_agent_when_clear_works(self):
        self.fire("Stop", [SHELL])
        self.fire("UserPromptSubmit")
        self.assertNotIn("pane.release_agent", [method for method, _ in self.sent()])

    def test_main_ignores_malformed_input_and_stays_silent(self):
        for stdin in ("", "not json", "[]", '{"hook_event_name": 5}'):
            result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, text=True, capture_output=True, env=self.env)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), stdin)

    def test_script_reports_through_a_real_hook_process(self):
        stdin = json.dumps({"hook_event_name": "Stop", "background_tasks": [SHELL], "session_crons": []})
        result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, text=True, capture_output=True, env=self.env)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.sent(), [("pane.report_agent", "working")])


if __name__ == "__main__":
    unittest.main()
