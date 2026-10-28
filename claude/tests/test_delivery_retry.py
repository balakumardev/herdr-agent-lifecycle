"""Lifecycle admission with real loopback HTTP and detached command processes."""
import concurrent.futures
import errno
import fcntl
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime
from transport import Transport, locked, save_json
from test_transport import Receiver


class DeliveryRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-delivery-retry-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        topic = self.root / "topic.sh"
        topic.write_text('TOPIC="local-private-topic"\n')
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.server.messages, self.server.codes = [], []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH")}
        self.env.update(CLAUDE_TASK_LIFECYCLE_STATE_DIR=str(self.state_dir),
                        CLAUDE_TASK_LIFECYCLE_TOPIC_FILE=str(topic),
                        CLAUDE_TASK_LIFECYCLE_ENDPOINT="http://127.0.0.1:" + str(self.server.server_port),
                        CLAUDE_TASK_LIFECYCLE_OWNER="owner-one", PYTHONDONTWRITEBYTECODE="1")
        self.transport = Transport(self.state_dir, topic, endpoint=self.env["CLAUDE_TASK_LIFECYCLE_ENDPOINT"])
        self.expected = []
        self.runtime = runtime.Runtime(self.state_dir, self.transport, env=self.env,
                                       hook_audit=lambda event: (list(self.expected), False))
        self.seq = 0
        self.processes = []
        self.addCleanup(self.cleanup_workers)
        self.event("bind", born_at=100)
        self.event("start", turn_id="turn-one")

    def packet(self, action, **data):
        self.seq += 1
        return {"session_id": "session-one", "owner": "owner-one", "seq": self.seq, "action": action, **data}

    def event(self, action, **data):
        return self.runtime.accept(self.packet(action, **data))

    def stop(self):
        self.event("prepare_stop", hook_event_name="Stop", event_key="stop-one",
                   background_tasks=[], session_crons=[])
        self.event("stop_verdict", event_key="stop-one", accepted=True)

    def complete(self):
        self.stop()
        return self.event("complete", turn_id="turn-one", has_answer=True, reason="answer",
                          answer_text="PRIVATE-PARENT-FINAL-REPLY")

    def receipts(self):
        path = self.state_dir / "receipts.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, description, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for " + description)

    def worker_idle(self):
        path = self.state_dir / "delivery-worker.lock"
        if not path.exists():
            return True
        with path.open("r+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    def cleanup_workers(self):
        queue = self.state_dir / "delivery-queue.json"
        if queue.exists():
            with locked(self.state_dir / "delivery-control.lock"):
                save_json(queue, {})
        self.wait_for(self.worker_idle, "test-owned worker exit")
        for process in self.processes:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def expire_cooldown(self):
        # Simulate elapsed wall time without adding a test-only worker switch.
        # The transport and worker still read their real durable records.
        queue = self.state_dir / "delivery-queue.json"
        if queue.exists():
            self.wait_for(lambda: all(job["nextAttemptAt"] > 0 for job in json.loads(queue.read_text()).values()),
                          "worker scheduling the failed attempt")
        for directory in ("endpoints", "deliveries"):
            for path in (self.state_dir / directory).glob("*.json"):
                with locked(path.with_suffix(".lock")):
                    data = json.loads(path.read_text())
                    if data.get("nextAttemptAt"):
                        save_json(path, {**data, "nextAttemptAt": 0})
        if queue.exists():
            with locked(self.state_dir / "delivery-control.lock"):
                data = json.loads(queue.read_text())
                save_json(queue, {key: {**job, "nextAttemptAt": 0} for key, job in data.items()})

    def failed_in_background(self, question=False):
        self.runtime.background_delivery = True
        self.server.codes = [503, 200]
        if question:
            self.event("question_begin", tool_id="question-one")
        else:
            self.complete()
        self.wait_for(lambda: len(self.receipts()) == 1, "first failed HTTP receipt")
        self.assertEqual(self.receipts()[0]["status"], "failed")

    def start_worker_in_pool(self, pool):
        descriptor = os.open(self.state_dir / "delivery-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return pool.submit(self.runtime.delivery_worker, descriptor)

    def clear_queue(self):
        with locked(self.state_dir / "delivery-control.lock"):
            save_json(self.state_dir / "delivery-queue.json", {})

    def test_final_reply_recovers_after_the_admitting_command_has_exited(self):
        self.server.codes = [503, 200]
        self.stop()
        packet = self.packet("complete", turn_id="turn-one", has_answer=True, reason="answer",
                             answer_text="PRIVATE-PARENT-FINAL-REPLY")
        command = subprocess.run([sys.executable, "-B", str(ROOT / "runtime.py")],
                                 input=json.dumps(packet), text=True, capture_output=True,
                                 cwd=ROOT, env=self.env, timeout=5)
        self.assertEqual((command.returncode, command.stderr), (0, ""))
        self.assertEqual(json.loads(command.stdout)["state"], "idle")
        self.wait_for(lambda: len(self.receipts()) == 1, "first failed HTTP attempt")
        payload = self.runtime.payload_path("session-one")
        self.assertIn("PRIVATE-PARENT-FINAL-REPLY", payload.read_text())
        self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
        # No native event or second command is needed to retry this final turn.
        self.expire_cooldown()
        self.wait_for(lambda: len(self.receipts()) == 2 and not payload.exists(), "automatic retry and outbox cleanup")
        self.assertEqual([r["status"] for r in self.receipts()], ["failed", "sent"])
        self.assertEqual([body.decode() for _, _, body in self.server.messages],
                         ["PRIVATE-PARENT-FINAL-REPLY"] * 2)

    def test_burst_dispatches_share_one_detached_worker(self):
        spawn = subprocess.Popen

        def capture_spawn(*args, **kwargs):
            process = spawn(*args, **kwargs)
            self.processes.append(process)
            return process

        with mock.patch.object(runtime.subprocess, "Popen", side_effect=capture_spawn):
            self.failed_in_background()
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: self.runtime._dispatch("session-one", {"state": "idle"}), range(16)))
            self.assertEqual(len(self.processes), 1, "each notification admission spawned another sleeping process")
        self.assertEqual(len(self.server.messages), 1)

    def test_session_end_preserves_a_qualified_deferred_final_reply(self):
        self.server.codes = [503, 200]
        self.complete()
        self.assertTrue(self.runtime.payload_path("session-one").exists())
        self.event("end")
        self.expire_cooldown()
        self.runtime.deliver_pending("session-one", "owner-one")
        self.assertEqual([r["status"] for r in self.receipts()], ["failed", "sent"])
        self.assertEqual(self.server.messages[-1][2].decode(), "PRIVATE-PARENT-FINAL-REPLY")
        self.assertFalse(self.runtime.payload_path("session-one").exists())

    def test_new_work_revokes_deferred_reply_and_stops_the_worker(self):
        self.failed_in_background()
        self.event("activity")
        self.expire_cooldown()
        self.wait_for(self.worker_idle, "cancelled delivery worker exit")
        self.assertEqual(len(self.server.messages), 1)
        self.assertFalse(self.runtime.payload_path("session-one").exists())

    def test_answering_a_deferred_question_stops_the_worker_without_retry(self):
        self.failed_in_background(question=True)
        self.event("question_end", tool_id="question-one")
        self.expire_cooldown()
        self.wait_for(self.worker_idle, "resolved question worker exit")
        self.assertEqual(len(self.server.messages), 1)

    def test_child_completion_preserves_an_outstanding_question_retry(self):
        self.event("child_start", agent_id="background-child")
        self.failed_in_background(question=True)
        self.event("child_complete", agent_id="background-child")
        self.assertEqual(self.runtime._load("session-one")["questions"], ["question-one"])
        self.expire_cooldown()
        self.wait_for(lambda: len(self.receipts()) == 2, "question retry after child completion")
        self.assertEqual([r["status"] for r in self.receipts()], ["failed", "sent"])
        self.assertTrue(all("Input Needed" in title for _, title, _ in self.server.messages))

    def test_review_readmission_wins_over_an_inflight_queue_removal(self):
        self.runtime.background_delivery = True
        observed_blocked, release = threading.Event(), threading.Event()
        deliver = self.runtime.deliver_pending
        first = True

        def pause_after_review_gate(*args):
            nonlocal first
            due = deliver(*args)
            if first:
                first = False
                self.assertIsNone(due)
                observed_blocked.set()
                self.assertTrue(release.wait(5))
            return due

        with mock.patch.object(self.runtime, "_start_delivery_worker"), \
                mock.patch.object(self.runtime, "deliver_pending", side_effect=pause_after_review_gate):
            self.complete()
            self.expected = ["late-review"]
            self.event("prepare_stop", hook_event_name="Stop", event_key="review-stop",
                       background_tasks=[], session_crons=[])
            run_id = self.runtime.begin_hook("session-one", "review-stop", "late-review")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                worker = self.start_worker_in_pool(pool)
                try:
                    self.assertTrue(observed_blocked.wait(2), "worker never reached the review gate")
                    self.runtime.finish_hook("session-one", run_id, 0)
                    release.set()
                    worker.result(timeout=5)
                    self.assertEqual(len(self.server.messages), 1, "stale removal discarded the re-admitted completion")
                    self.assertFalse(self.runtime.payload_path("session-one").exists())
                finally:
                    release.set()
                    self.clear_queue()

    def test_worker_retries_queue_enospc_while_retaining_ownership(self):
        self.runtime.background_delivery = True
        self.server.codes = [503, 200]
        queue = self.state_dir / "delivery-queue.json"
        failed, parked, recovered = threading.Event(), threading.Event(), threading.Event()
        sleeps = []
        real_sleep = time.sleep

        def persist(path, value):
            if Path(path) == queue and any(job["nextAttemptAt"] > 0 for job in value.values()) and not recovered.is_set():
                failed.set()
                raise OSError(errno.ENOSPC, "fixture disk full")
            return save_json(path, value)

        def sleep(seconds):
            if failed.is_set() and seconds >= 30:
                sleeps.append(seconds)
                parked.set()
                recovered.wait(5)
            else:
                real_sleep(min(seconds, 0.01))

        with mock.patch.object(self.runtime, "_start_delivery_worker"), \
                mock.patch.object(runtime, "save_json", side_effect=persist), \
                mock.patch.object(runtime.time, "sleep", side_effect=sleep):
            self.complete()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                worker = self.start_worker_in_pool(pool)
                try:
                    self.assertTrue(failed.wait(2), "queue deadline write was not exercised")
                    self.assertTrue(parked.wait(1), "queue ENOSPC killed the worker instead of backing off")
                    self.assertFalse(worker.done())
                    self.assertFalse(self.worker_idle())
                    self.assertEqual(len(self.server.messages), 1)
                    self.assertEqual(sleeps, [30])
                    recovered.set()
                    self.wait_for(lambda: all(job["nextAttemptAt"] > 0 for job in json.loads(queue.read_text()).values()),
                                  "persisted deadline after storage recovery")
                    self.expire_cooldown()
                    worker.result(timeout=5)
                    self.assertEqual([r["status"] for r in self.receipts()], ["failed", "sent"])
                finally:
                    recovered.set()
                    self.clear_queue()

    def test_late_review_gate_is_checked_again_before_deferred_delivery(self):
        self.failed_in_background()
        self.expected = ["late-review"]
        self.event("prepare_stop", hook_event_name="Stop", event_key="review-stop",
                   background_tasks=[], session_crons=[])
        self.expire_cooldown()
        self.wait_for(self.worker_idle, "review-blocked worker exit")
        self.assertEqual(len(self.server.messages), 1)
        self.assertTrue(self.runtime.payload_path("session-one").exists())
        run_id = self.runtime.begin_hook("session-one", "review-stop", "late-review")
        self.runtime.finish_hook("session-one", run_id, 0)
        self.wait_for(lambda: len(self.receipts()) == 2, "delivery after review settlement")
        self.assertEqual(self.receipts()[-1]["status"], "sent")

    def test_expected_review_does_not_start_a_retry_worker_or_publish(self):
        self.runtime.background_delivery = True
        self.expected = ["review"]
        self.assertEqual(self.complete()["state"], "working")
        self.assertTrue(self.worker_idle())
        self.assertEqual(self.server.messages, [])
        self.assertTrue(self.runtime.payload_path("session-one").exists())
        run_id = self.runtime.begin_hook("session-one", "stop-one", "review")
        self.runtime.finish_hook("session-one", run_id, 2)
        self.assertEqual(self.server.messages, [])
        self.assertFalse(self.runtime.payload_path("session-one").exists())

    def test_permanent_rejection_is_not_marked_sent_and_releases_private_outbox(self):
        self.server.codes = [403]
        self.complete()
        self.assertEqual(self.receipts()[0]["status"], "failed")
        self.assertEqual(self.runtime._load("session-one")["notified"], [])
        self.assertFalse(self.runtime.payload_path("session-one").exists())
        self.runtime.deliver_pending("session-one", "owner-one")
        self.assertEqual(len(self.server.messages), 1)

    def test_old_delivery_response_cannot_settle_a_new_owner(self):
        def rebind_during_http():
            self.runtime.accept({"action": "bind", "session_id": "session-one", "owner": "new-owner",
                                 "seq": 1, "born_at": 200})

        self.server.on_post = rebind_during_http
        self.complete()
        state = self.runtime._load("session-one")
        self.assertEqual(state["owner"], "new-owner")
        self.assertEqual(state["notified"], [])

    def test_broken_retry_queue_cannot_prevent_new_work_revoking_a_reply(self):
        self.server.codes = [503]
        self.complete()
        (self.state_dir / "delivery-queue.json").write_text("not-json")
        self.assertEqual(self.event("activity")["state"], "working")
        self.assertIsNone(self.runtime._load("session-one")["candidate"])
        self.assertFalse(self.runtime.payload_path("session-one").exists())
        self.runtime.deliver_pending("session-one", "owner-one")
        self.assertEqual(len(self.server.messages), 1)

    def test_retry_records_and_queue_hold_metadata_only(self):
        self.failed_in_background()
        payload = self.runtime.payload_path("session-one")
        text = "\n".join(p.read_text() for p in self.state_dir.rglob("*") if p.is_file() and p != payload)
        for private in ("PRIVATE-PARENT-FINAL-REPLY", "local-private-topic", "127.0.0.1", "http://"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
