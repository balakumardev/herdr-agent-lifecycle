"""Exercise real local sockets, HTTP delivery, and persistent deduplication."""
import concurrent.futures
from email.utils import formatdate
import errno
import http.server
import importlib.util
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class Receiver(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.messages.append((self.path, self.headers.get("Title"), body))
        callback = getattr(self.server, "on_post", None)
        if callback:
            callback()
        code = self.server.codes.pop(0) if self.server.codes else 200
        self.send_response(code)
        for key, value in getattr(self.server, "response_headers", {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class TransportTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "transport.py"
        self.assertTrue(path.exists(), "the lifecycle transport is not implemented yet")
        spec = importlib.util.spec_from_file_location("lifecycle_transport", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.topic_file = self.root / "ntfy-notify.sh"
        self.topic_file.write_text('TOPIC="private-test-topic"\n')
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.server.messages = []
        self.server.codes = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = dict(
            state_dir=self.root / "state",
            topic_file=self.topic_file,
            endpoint="http://127.0.0.1:%d" % self.server.server_port,
            timeout=0.3,
        )
        self.transport = module.Transport(**self.config)

    def publish(self, kind="done", key="turn-1", message="Fixture final answer"):
        return self.transport.publish(session_id="session-1", kind=kind, notice_id=key, message=message)

    def test_completed_turn_is_sent_once_across_process_restarts(self):
        self.assertEqual(self.publish()["status"], "sent")
        restarted = self.module.Transport(**self.config)
        receipt = restarted.publish(session_id="session-1", kind="done", notice_id="turn-1")
        self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(len(self.server.messages), 1)

    def test_question_and_completion_are_distinct_notifications(self):
        self.publish("question", "question-1")
        self.publish("done", "turn-1")
        self.assertEqual(len(self.server.messages), 2)
        self.assertIn("Input Needed", self.server.messages[0][1])
        self.assertIn("Task Complete", self.server.messages[1][1])

    def test_concurrent_publishers_share_the_delivery_ledger(self):
        def publish(_):
            return self.module.Transport(**self.config).publish(
                session_id="session-1", kind="done", notice_id="concurrent-turn", message="Fixture final answer")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(publish, range(12)))
        self.assertEqual(sum(r["status"] == "sent" for r in receipts), 1)
        self.assertEqual(len(self.server.messages), 1)

    def test_missing_topic_never_uses_shell_evaluation(self):
        marker = self.root / "should-not-exist"
        self.topic_file.write_text('TOPIC="$(touch %s)"\n' % marker)
        self.assertEqual(self.publish()["status"], "disabled")
        self.assertFalse(marker.exists())
        self.assertEqual(self.server.messages, [])

    def test_failure_is_one_attempt_and_restart_cannot_retry_until_expiry(self):
        self.server.codes = [503, 200]
        with mock.patch.object(self.module.time, "time", return_value=1700000000):
            receipt = self.publish()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["httpStatus"], 503)
        self.assertEqual(receipt["attempts"], 1)
        self.assertEqual(len(self.server.messages), 1)
        self.assertGreaterEqual(receipt["nextAttemptAt"], 1700000030)
        self.transport = self.module.Transport(**self.config)
        with mock.patch.object(self.module.time, "time", return_value=1700000029):
            self.assertEqual(self.publish()["status"], "deferred")
        self.assertEqual(len(self.server.messages), 1)
        with mock.patch.object(self.module.time, "time", return_value=receipt["nextAttemptAt"] + 1):
            recovered = self.publish()
        self.assertEqual(recovered["status"], "sent")
        self.assertEqual(recovered["httpStatus"], 200)
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(len(self.server.messages), 2)

    def test_permanent_http_rejection_remains_terminal_after_restart(self):
        self.server.codes = [403]
        receipt = self.publish()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["httpStatus"], 403)
        self.assertTrue(receipt["terminal"])
        self.transport = self.module.Transport(**self.config)
        with mock.patch.object(self.module.time, "time", return_value=time.time() + 3600):
            again = self.publish()
        self.assertEqual(again["status"], "failed")
        self.assertTrue(again["terminal"])
        self.assertEqual(again["attempts"], 1)
        self.assertEqual(len(self.server.messages), 1)
        self.assertEqual(self.publish(key="different-turn")["status"], "sent")

    def test_burst_during_cooldown_adds_no_receipts_or_per_check_files(self):
        self.server.codes = [429] * 100
        with mock.patch.object(self.module.time, "time", return_value=1700000000):
            receipt = self.publish()
            self.assertEqual(len(self.server.messages), 1)
            self.assertGreaterEqual(receipt["nextAttemptAt"], 1700000060)
            before = {str(p): (p.stat().st_mtime_ns, p.read_bytes())
                      for p in (self.root / "state").rglob("*") if p.is_file()}
            for _ in range(40):
                self.assertEqual(self.publish()["status"], "deferred")
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes())
                 for p in (self.root / "state").rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(len(self.server.messages), 1)
        self.assertEqual(len((self.root / "state/receipts.jsonl").read_text().splitlines()), 1)

    def test_retry_after_seconds_and_http_date_are_respected_across_sessions(self):
        for header, earliest in (("120", 1700000120),
                                 (formatdate(1700000180, usegmt=True), 1700000180),
                                 ("7200", 1700007200), ("invalid", 1700000060),
                                 (formatdate(1600000000, usegmt=True), 1700000060)):
            with self.subTest(header=header):
                config = {**self.config, "state_dir": self.root / ("case-" + str(earliest) + header[:3].replace("/", ""))}
                self.transport = self.module.Transport(**config)
                self.server.codes = [429, 200]
                self.server.response_headers = {"Retry-After": header}
                before = len(self.server.messages)
                with mock.patch.object(self.module.time, "time", return_value=1700000000):
                    failed = self.publish()
                self.assertEqual(len(self.server.messages) - before, 1)
                self.assertGreaterEqual(failed["nextAttemptAt"], earliest)
                restarted = self.module.Transport(**config)
                with mock.patch.object(self.module.time, "time", return_value=earliest - 1):
                    deferred = restarted.publish(session_id="another-session", kind="question", notice_id="new-question")
                self.assertEqual(deferred["status"], "deferred")
                self.assertEqual(len(self.server.messages) - before, 1)
                with mock.patch.object(self.module.time, "time", return_value=failed["nextAttemptAt"] + 1):
                    self.assertEqual(self.publish()["status"], "sent")

    def test_backoff_increases_to_fifteen_minutes_and_survives_restart(self):
        self.server.codes = [503] * 12
        now = 1700000000
        for number, delay in enumerate((30, 60, 120, 240, 480, 900, 900), 1):
            self.transport = self.module.Transport(**self.config)
            with mock.patch.object(self.module.time, "time", return_value=now):
                receipt = self.publish()
            self.assertEqual(receipt["attempts"], number)
            self.assertEqual(len(self.server.messages), number)
            self.assertEqual(receipt["nextAttemptAt"], now + delay)
            now = receipt["nextAttemptAt"] + 1

    def test_cross_process_endpoint_lock_prevents_different_sessions_storming_an_outage(self):
        self.server.codes = [503] * 20
        self.server.on_post = lambda: time.sleep(0.05)
        code = ("import json,sys; from transport import Transport; "
                "t=Transport(**json.loads(sys.argv[1])); "
                "print(json.dumps(t.publish(session_id=sys.argv[2], kind='question', notice_id='q')))" )
        config = {key: str(value) if isinstance(value, Path) else value for key, value in self.config.items()}
        processes = [subprocess.Popen([sys.executable, "-B", "-c", code, json.dumps(config), "session-" + str(i)],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}) for i in range(8)]
        replies = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual((process.returncode, stderr), (0, b""))
            replies.append(json.loads(stdout))
        self.assertEqual(len(self.server.messages), 1)
        self.assertEqual(sum(r["status"] == "failed" for r in replies), 1)
        self.assertEqual(sum(r["status"] == "deferred" for r in replies), 7)

    def publish_in_fresh_process(self, now):
        code = ("import json,sys,transport; "
                "original=transport.locked; "
                "transport.locked=lambda path: original(path, timeout=.1); "
                "transport.time.time=lambda: float(sys.argv[2]); "
                "t=transport.Transport(**json.loads(sys.argv[1])); "
                "print(json.dumps(t.publish(session_id='session-1', kind='done', notice_id='turn-1', message='Fixture final answer')))" )
        config = {key: str(value) if isinstance(value, Path) else value for key, value in self.config.items()}
        process = subprocess.run([sys.executable, "-B", "-c", code, json.dumps(config), str(now)],
                                 cwd=ROOT, text=True, capture_output=True, timeout=5)
        self.assertEqual((process.returncode, process.stderr), (0, ""))
        return json.loads(process.stdout)

    def assert_settlement_survives_enospc(self, status, headers, wait):
        now = 1700000000
        expected_deadline = now + wait if wait else 0
        self.server.codes = [status, 200]
        self.server.response_headers = headers
        failed, recovered = threading.Event(), threading.Event()
        sleeps = []
        save, sleep, lock = self.module.save_json, time.sleep, self.module.locked

        def persist(path, value):
            if (Path(path) == self.transport.endpoint_path and value.get("nextAttemptAt") == expected_deadline
                    and not recovered.is_set()):
                failed.set()
                raise OSError(errno.ENOSPC, "fixture disk full")
            return save(path, value)

        def backoff(seconds):
            if failed.is_set() and seconds >= 30:
                sleeps.append(seconds)
                recovered.wait(5)
            else:
                sleep(seconds)

        with mock.patch.object(self.module, "save_json", side_effect=persist), \
                mock.patch.object(self.module.time, "sleep", side_effect=backoff), \
                mock.patch.object(self.module, "locked", side_effect=lambda path: lock(path, timeout=.1)), \
                mock.patch.object(self.module.time, "time", return_value=now) as clock:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                publication = pool.submit(self.publish)
                try:
                    self.assertTrue(failed.wait(2), "post-response endpoint write was not exercised")
                    clock.return_value = now + 31  # The original reservation has expired.
                    peer = self.module.Transport(**self.config).publish(
                        session_id="another-session", kind="question", notice_id="another-question")
                    self.assertEqual(len(self.server.messages), 1, "storage failure shortened the observed endpoint cooldown")
                    self.assertEqual(peer["status"], "deferred")
                    self.assertEqual(self.publish_in_fresh_process(now + 31)["status"], "deferred")
                    self.assertEqual(len(self.server.messages), 1)
                    self.assertEqual(sleeps, [30], "metadata recovery must back off without another HTTP call")
                    if publication.done():
                        self.assertNotEqual(publication.result()["status"], "sent")
                finally:
                    recovered.set()
                    outcome = publication.result(timeout=3)

            # Once storage is writable, fresh instances/processes must observe the
            # server deadline or the durable success, not the provisional 30s gate.
            self.transport = self.module.Transport(**self.config)
            repeated = self.publish()
            expected_status = "deferred" if wait else "duplicate"
            self.assertEqual(repeated["status"], expected_status)
            self.assertEqual(self.publish_in_fresh_process(now + 31)["status"], expected_status)
            self.assertEqual(len(self.server.messages), 1)
            if wait:
                self.assertEqual(outcome["status"], "failed")
                self.assertGreaterEqual(outcome["nextAttemptAt"], expected_deadline)
                self.assertGreaterEqual(repeated["nextAttemptAt"], expected_deadline)
                clock.return_value = expected_deadline + 1
                self.assertEqual(self.publish()["status"], "sent")
                self.assertEqual(len(self.server.messages), 2)
            else:
                self.assertEqual(outcome["status"], "sent")

    def test_retry_after_survives_endpoint_settlement_enospc(self):
        self.assert_settlement_survives_enospc(429, {"Retry-After": "7200"}, 7200)

    def test_429_minimum_survives_endpoint_settlement_enospc(self):
        self.assert_settlement_survives_enospc(429, {}, 60)

    def test_success_waits_for_durable_settlement_without_resending(self):
        self.assert_settlement_survives_enospc(200, {}, 0)

    def test_failed_delivery_metadata_never_contains_message_topic_or_url(self):
        self.server.codes = [503]
        self.publish(message="PRIVATE-FINAL-REPLY")
        contents = "\n".join(p.read_text() for p in (self.root / "state").rglob("*") if p.is_file())
        for value in ("PRIVATE-FINAL-REPLY", "private-test-topic", "127.0.0.1", "http://", "session-1", "turn-1"):
            self.assertNotIn(value, contents)

    def test_receipts_do_not_contain_private_topic_or_message_content(self):
        self.publish("question", "private-question-token")
        contents = (self.root / "state" / "receipts.jsonl").read_text()
        for private in ("private-test-topic", "private-question-token", "session-1", "Input Needed"):
            self.assertNotIn(private, contents)
        receipt = json.loads(contents.splitlines()[-1])
        self.assertEqual(receipt["status"], "sent")
        self.assertEqual(len(receipt["keyHash"]), 64)

    def test_herdr_uses_the_calling_pane_and_checks_acknowledgement(self):
        sock = socket.socket(socket.AF_UNIX)
        path = str(self.root / "herdr.sock")
        sock.bind(path)
        sock.listen(1)
        self.addCleanup(sock.close)
        captured = []

        def receive():
            connection, _ = sock.accept()
            with connection:
                message = json.loads(connection.makefile("rb").readline())
                captured.append(message)
                reply = {"id": message["id"], "result": {"type": "ok"}}
                connection.sendall((json.dumps(reply) + "\n").encode())

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        ok = self.transport.report(
            session_id="root-session", state="working", reason="background_work",
            seq=7, env={"HERDR_ENV": "1", "HERDR_PANE_ID": "pane-local", "HERDR_SOCKET_PATH": path})
        thread.join(1)
        self.assertTrue(ok)
        params = captured[0]["params"]
        self.assertEqual(params["pane_id"], "pane-local")
        self.assertEqual(params["state"], "working")
        self.assertEqual(params["agent_session_id"], "root-session")
        self.assertEqual(params["resume_argv"], ["claude", "--resume", "root-session"])

    def test_absent_herdr_environment_is_a_noop(self):
        self.assertFalse(self.transport.report(session_id="s", state="working", reason="running", seq=1, env={}))

    def test_non_notification_states_cannot_publish(self):
        receipt = self.publish("idle_prompt", "reminder")
        self.assertEqual(receipt["status"], "disabled")
        self.assertEqual(self.server.messages, [])

    def test_a_resolved_question_is_not_retried_after_a_transient_failure(self):
        self.assertIn("is_current", inspect.signature(self.transport.publish).parameters,
                      "delivery cannot yet recheck whether the question is outstanding")
        resolved = threading.Event()
        self.server.codes = [503, 200]
        self.server.on_post = resolved.set
        receipt = self.transport.publish(session_id="s", kind="question", notice_id="q",
                                         is_current=lambda: not resolved.is_set())
        self.assertEqual(receipt["status"], "failed")
        with mock.patch.object(self.module.time, "time", return_value=receipt["nextAttemptAt"] + 1):
            receipt = self.transport.publish(session_id="s", kind="question", notice_id="q",
                                             is_current=lambda: not resolved.is_set())
        self.assertEqual(receipt["status"], "cancelled")
        self.assertEqual(len(self.server.messages), 1)

    def test_runtime_cancellation_does_not_wait_behind_notification_delivery(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from runtime import Runtime
        runtime = Runtime(self.root / "runtime", self.transport, env={}, hook_audit=lambda event: ([], False))
        common = {"session_id": "s", "owner": "owner"}
        runtime.accept({**common, "seq": 1, "action": "bind", "born_at": 100})
        runtime.accept({**common, "seq": 2, "action": "start", "turn_id": "turn"})
        publishing, release = threading.Event(), threading.Event()

        def delay_response():
            if len(self.server.messages) == 1:
                publishing.set()
                release.wait(1)

        self.server.on_post = delay_response
        self.server.codes = [503, 200]
        self.transport.timeout = 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(runtime.accept, {**common, "seq": 3, "action": "question_begin", "tool_id": "q"})
            self.assertTrue(publishing.wait(2))
            answer = pool.submit(runtime.accept, {**common, "seq": 4, "action": "question_end", "tool_id": "q"})
            try:
                cleared = answer.result(timeout=0.3)["state"] == "working"
            except concurrent.futures.TimeoutError:
                cleared = False
            finally:
                release.set()
            first.result(timeout=3)
            answer.result(timeout=3)
        self.assertTrue(cleared, "answer admission was blocked by outbound delivery")
        self.assertEqual(len(self.server.messages), 1)

    def test_completion_body_is_the_assistant_reply(self):
        reply = "Fixed the hooks.\nTests: 4 passed."
        self.publish(message=reply)
        self.assertEqual(self.server.messages[0][2].decode(), reply)

    def test_long_unicode_reply_is_bounded_without_splitting_a_character(self):
        self.publish(message="🚀" * 1500)
        body = self.server.messages[0][2]
        self.assertLessEqual(len(body), 4000)
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("🚀"))
        self.assertTrue(text.endswith("…"))
        self.assertNotIn("�", text)

    def test_missing_completion_text_never_uses_boilerplate(self):
        receipt = self.transport.publish(session_id="s", kind="done", notice_id="missing")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reason"], "no_message")
        self.assertEqual(self.server.messages, [])

    def test_reply_is_not_written_into_receipts_or_delivery_keys(self):
        self.publish(message="FINAL-REPLY-PRIVATE-SENTINEL")
        self.assertEqual(self.server.messages[0][2].decode(), "FINAL-REPLY-PRIVATE-SENTINEL")
        text = "\n".join(p.read_text() for p in (self.root / "state").rglob("*") if p.is_file())
        self.assertNotIn("FINAL-REPLY-PRIVATE-SENTINEL", text)


if __name__ == "__main__":
    unittest.main()
