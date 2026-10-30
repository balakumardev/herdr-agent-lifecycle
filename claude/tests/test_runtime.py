import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CaptureTransport:
    def __init__(self):
        self.notices = []
        self.reports = []

    def publish(self, **notice):
        self.notices.append(notice)
        return {"status": "sent"}

    def report(self, **report):
        self.reports.append(report)
        return True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime.py"
        self.assertTrue(path.exists(), "the lifecycle reducer is not implemented yet")
        spec = importlib.util.spec_from_file_location("task_lifecycle_runtime", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-runtime-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = CaptureTransport()
        self.expected = []
        self.unknown = False
        self.runtime = module.Runtime(self.root, self.transport, env={},
                                      hook_audit=lambda event: (list(self.expected), self.unknown))
        self.sequence = 0
        self.owner = "process-one"
        self.event("bind", born_at=100)
        self.event("start", turn_id="turn-1")

    def event(self, action, **data):
        self.sequence += 1
        if action == "bind":
            self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = self.owner
        return self.runtime.accept({"session_id": "session-1", "owner": self.owner,
                                    "seq": self.sequence, "action": action, **data})

    def stop(self, background=None, crons=None, accepted=True, key="stop-1"):
        result = self.event("prepare_stop", event_key=key, hook_event_name="Stop",
                            background_tasks=[] if background is None else background,
                            session_crons=[] if crons is None else crons)
        self.event("stop_verdict", event_key=key, accepted=accepted)
        return result

    def complete(self, reason="answer", turn_id="turn-1", answer_text="Fixture final answer"):
        return self.event("complete", turn_id=turn_id, reason=reason, has_answer=True, answer_text=answer_text)

    def test_stop_alone_is_not_completion(self):
        self.stop()
        self.assertEqual(self.transport.notices, [])

    def test_final_parent_response_after_empty_accepted_stop_completes(self):
        self.stop()
        result = self.complete()
        self.assertEqual(result["state"], "idle")
        self.assertEqual([n["kind"] for n in self.transport.notices], ["done"])

    def teammate_stop(self, allow_idle_teammates=True, **changes):
        # Existing roster coverage exercises the explicitly opted-in policy.
        if allow_idle_teammates is not None:
            self.runtime.allow_idle_teammates = allow_idle_teammates
        # Native task IDs are not the public agent loop IDs.
        return self.runtime.observe_hook({
            "hook_event_name": "Stop", "session_id": "session-1",
            "background_tasks": [
                {"id": "9e6417d2", "type": "teammate", "status": "running"},
                {"id": "b281c504", "type": "teammate", "status": "running"},
            ], "session_crons": [], **changes})

    def complete_with_idle_agents(self, **changes):
        return self.event("complete", **{
            "turn_id": "turn-1", "reason": "answer", "has_answer": True,
            "post_turn": True, "answer_text": "Both delegated checks are complete.",
            "agents": [
                {"id": "a370ca72-2f18", "teammateId": "helper-one@fixture", "status": "idle"},
                {"id": "ade285c9-8064", "teammateId": "helper-two@fixture", "status": "idle"},
            ], **changes})

    def test_observed_idle_teammates_allow_final_parent_completion(self):
        self.teammate_stop()
        result = self.complete_with_idle_agents()
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))
        self.assertEqual([notice["kind"] for notice in self.transport.notices], ["done"])
        self.assertEqual(self.transport.notices[0]["message"], "Both delegated checks are complete.")

    def test_default_policy_cannot_infer_empty_inboxes_from_idle_teammates(self):
        self.teammate_stop(allow_idle_teammates=None)
        result = self.complete_with_idle_agents()
        self.assertEqual((result["state"], result["reason"]), ("working", "teammate_completion_unproven"))
        self.assertEqual(self.transport.notices, [])
        self.event("start", turn_id="turn-2")
        self.stop()
        self.assertEqual(self.complete(turn_id="turn-2")["state"], "idle")
        self.assertEqual(len(self.transport.notices), 1)

    def test_truthy_string_is_not_idle_teammate_opt_in(self):
        self.teammate_stop(allow_idle_teammates="true")
        self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_with_terminal_agents_notify_once(self):
        self.teammate_stop()
        agents = [{"id": "loop-1", "teammateId": "mate-1", "status": "idle"},
                  {"id": "loop-2", "teammateId": "mate-2", "status": "idle"}]
        agents.extend({"id": status, "status": status} for status in ("completed", "failed", "aborted"))
        self.assertEqual(self.complete_with_idle_agents(agents=agents)["state"], "idle")
        self.complete_with_idle_agents(agents=agents)
        self.assertEqual(len(self.transport.notices), 1)

    def test_idle_teammate_exception_rejects_active_and_unknown_agents(self):
        for status in ("pending", "running", "waiting", "queued", "unknown", None):
            with self.subTest(status=status):
                self.teammate_stop()
                agents = [{"id": "loop-1", "teammateId": "mate-1", "status": "idle"},
                          {"id": "loop-2", "teammateId": "mate-2", "status": status}]
                self.assertEqual(self.complete_with_idle_agents(agents=agents)["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammate_exception_rejects_missing_or_malformed_roster(self):
        for agents in (None, [], {}, "unavailable", [None], [{"status": "idle"}]):
            with self.subTest(agents=agents):
                self.teammate_stop()
                self.assertEqual(self.complete_with_idle_agents(agents=agents)["state"], "working")
        self.teammate_stop()
        result = self.event("complete", turn_id="turn-1", reason="answer", has_answer=True, post_turn=True)
        self.assertEqual(result["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammate_exception_rejects_ambiguous_identities_and_counts(self):
        first = {"id": "loop-1", "teammateId": "mate-1", "status": "idle"}
        second = {"id": "loop-2", "teammateId": "mate-2", "status": "idle"}
        rosters = ([first], [first, second, {"id": "loop-3", "teammateId": "mate-3", "status": "idle"}],
                   [first, {**second, "id": first["id"]}], [first, {**second, "teammateId": first["teammateId"]}],
                   [first, {**second, "id": second["teammateId"]}], [first, {**second, "teammateId": None}],
                   [first, {**second, "teammateId": ""}], [first, {**second, "id": " "}],
                   [{"id": "finished-1", "status": "completed"}, {"id": "finished-2", "status": "completed"}])
        for agents in rosters:
            with self.subTest(agents=agents):
                self.teammate_stop()
                self.assertEqual(self.complete_with_idle_agents(agents=agents)["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammate_exception_rejects_invalid_native_task_records(self):
        task = {"id": "native-1", "type": "teammate", "status": "running"}
        snapshots = (None, {}, [None], [task, task], [{**task, "id": ""}],
                     [{**task, "status": "completed"}], [{**task, "id": 7}],
                     [{"id": "native-1", "type": "teammate"}])
        for tasks in snapshots:
            with self.subTest(tasks=tasks):
                self.teammate_stop(background_tasks=tasks)
                self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_hide_shell_workflow_or_unknown_background_work(self):
        for kind in ("shell", "workflow", "future-task-type"):
            with self.subTest(kind=kind):
                self.teammate_stop(background_tasks=[
                    {"id": "native-1", "type": "teammate", "status": "running"},
                    {"id": "native-2", "type": kind, "status": "running"}])
                self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_hide_crons_or_known_queued_work(self):
        for changes in ({"session_crons": [{"id": "cron-1"}]}, {"session_crons": None}, {"queue_empty": False}):
            with self.subTest(changes=changes):
                self.teammate_stop(**changes)
                self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_hide_running_children(self):
        self.event("child_start", agent_id="active-child")
        self.teammate_stop()
        self.assertEqual(self.complete_with_idle_agents()["reason"], "subagents_running")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammate_completion_requires_an_uninvalidated_stop(self):
        self.teammate_stop()
        self.event("activity")
        self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.teammate_stop()
        self.event("child_start", agent_id="resumed-child")
        self.event("child_complete", agent_id="resumed-child")
        self.assertEqual(self.complete_with_idle_agents()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_wait_for_the_actual_security_review_to_settle(self):
        self.expected = ["security-review"]
        self.teammate_stop()
        result = self.complete_with_idle_agents()
        self.assertEqual(result["reason"], "background_review")
        self.assertEqual(self.transport.notices, [])
        key = self.module.event_fingerprint({"hook_event_name": "Stop", "session_id": "session-1"})
        run = self.runtime.begin_hook("session-1", key, "security-review")
        self.assertEqual(self.runtime.finish_hook("session-1", run, 0)["state"], "idle")
        self.assertEqual([notice["message"] for notice in self.transport.notices], ["Both delegated checks are complete."])

    def test_idle_teammates_do_not_hide_review_continuations(self):
        self.expected = ["security-review"]
        self.teammate_stop()
        key = self.module.event_fingerprint({"hook_event_name": "Stop", "session_id": "session-1"})
        run = self.runtime.begin_hook("session-1", key, "security-review")
        self.runtime.finish_hook("session-1", run, 2)
        self.expected = []
        self.teammate_stop()
        self.assertEqual(self.complete_with_idle_agents()["reason"], "pending_continuation")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_hide_unknown_reviews(self):
        self.unknown = True
        self.teammate_stop()
        self.assertEqual(self.complete_with_idle_agents()["reason"], "untracked_async_hook")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_clear_a_pending_permission(self):
        self.teammate_stop()
        self.runtime.observe_hook({"hook_event_name": "PermissionRequest", "session_id": "session-1",
                                   "tool_use_id": "approval-1"})
        self.assertEqual(self.complete_with_idle_agents()["reason"], "permission_required")
        self.assertEqual(self.transport.notices, [])

    def test_idle_teammates_do_not_clear_an_unanswered_question(self):
        self.event("question_begin", tool_id="question-1")
        self.teammate_stop()
        self.assertEqual(self.complete_with_idle_agents()["reason"], "user_question")
        self.assertEqual([notice["kind"] for notice in self.transport.notices], ["question"])

    def test_idle_teammate_exception_requires_the_native_completion_boundary(self):
        self.teammate_stop()
        key = self.module.event_fingerprint({"hook_event_name": "Stop", "session_id": "session-1"})
        self.event("stop_verdict", event_key=key, accepted=True)
        self.assertEqual(self.complete_with_idle_agents(post_turn=False)["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_native_background_work_prevents_completion(self):
        self.stop(background=[{"id": "shell-1", "type": "new-unrecognized-task-type", "status": "running"}])
        self.assertEqual(self.complete()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_missing_task_registry_is_unknown(self):
        self.event("prepare_stop", hook_event_name="Stop", event_key="stop-1")
        self.event("stop_verdict", event_key="stop-1", accepted=True)
        self.assertEqual(self.complete()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_recurring_cron_keeps_the_task_working(self):
        self.stop(crons=[{"id": "loop-1", "recurring": True}])
        self.assertEqual(self.complete()["state"], "working")

    def test_other_stop_hooks_continuing_the_turn_prevent_done(self):
        self.stop(accepted=False)
        self.complete()
        self.assertEqual(self.transport.notices, [])

    def test_background_child_completion_requires_another_parent_response(self):
        self.event("child_start", agent_id="child-1")
        self.stop(background=[{"id": "child-1"}])
        self.complete()
        self.event("child_complete", agent_id="child-1")
        self.assertEqual(self.transport.notices, [])
        self.event("start", turn_id="turn-2")
        self.stop(key="stop-2")
        self.assertEqual(self.complete(turn_id="turn-2")["state"], "idle")

    def test_child_activity_after_stop_snapshot_invalidates_that_snapshot(self):
        self.stop()
        self.event("child_start", agent_id="child-1")
        self.event("child_complete", agent_id="child-1")
        self.assertEqual(self.complete()["state"], "working")
        self.assertEqual(self.transport.notices, [])

    def test_resumed_child_id_is_new_running_work(self):
        self.event("child_start", agent_id="child-1")
        self.event("child_complete", agent_id="child-1")
        self.event("child_start", agent_id="child-1")
        self.stop()
        self.assertEqual(self.complete()["state"], "working")

    def test_review_is_expected_before_the_wrapper_process_starts(self):
        self.expected = ["security-review"]
        self.stop()
        self.assertEqual(self.complete()["state"], "working")
        self.assertEqual(self.transport.notices, [])
        run_id = self.runtime.begin_hook("session-1", "stop-1", "security-review")
        result = self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(result["state"], "idle")
        self.assertEqual([n["kind"] for n in self.transport.notices], ["done"])

    def test_all_overlapping_reviews_must_settle(self):
        self.expected = ["review-1", "review-2"]
        self.stop()
        self.complete()
        first = self.runtime.begin_hook("session-1", "stop-1", "review-1")
        second = self.runtime.begin_hook("session-1", "stop-1", "review-2")
        self.runtime.finish_hook("session-1", first, 0)
        self.assertEqual(self.transport.notices, [])
        self.runtime.finish_hook("session-1", second, 0)
        self.assertEqual(len(self.transport.notices), 1)

    def test_exit_two_records_continuation_before_claude_receives_feedback(self):
        self.expected = ["review"]
        self.stop()
        self.complete()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.assertEqual(self.runtime.finish_hook("session-1", run_id, 2)["state"], "working")
        self.assertEqual(self.transport.notices, [])
        self.expected = []
        self.event("start", turn_id="turn-2")
        self.stop(key="stop-2")
        self.complete(turn_id="turn-2")
        self.assertEqual(len(self.transport.notices), 1)

    def test_new_prompt_invalidates_a_pending_review_completion(self):
        self.expected = ["review"]
        self.stop()
        self.complete()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.event("activity")
        self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(self.transport.notices, [])

    def test_uninstrumented_async_hook_is_never_assumed_finished(self):
        self.unknown = True
        self.stop()
        result = self.complete()
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["reason"], "untracked_async_hook")

    def test_resuming_does_not_publish_a_historical_completion(self):
        self.expected = ["review"]
        self.stop()
        self.complete()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.owner = "process-two"
        self.event("bind", born_at=200)
        self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(self.transport.notices, [])

    def test_old_owner_cannot_change_a_resumed_session(self):
        self.owner = "process-two"
        self.event("bind", born_at=200)
        result = self.runtime.accept({"session_id": "session-1", "owner": "process-one", "seq": 900,
                                      "action": "question_begin", "tool_id": "old-question"})
        self.assertTrue(result["ignored"])
        self.assertEqual(self.transport.notices, [])

    def test_pending_question_notifies_once_and_answer_returns_to_working(self):
        result = self.event("question_begin", tool_id="question-1")
        self.assertEqual(result["state"], "blocked")
        self.event("question_begin", tool_id="question-1")
        self.assertEqual([n["kind"] for n in self.transport.notices], ["question"])
        self.assertEqual(self.event("question_end", tool_id="question-1")["state"], "working")

    def test_permission_requests_do_not_generate_question_notifications(self):
        self.assertEqual(self.event("permission_begin", tool_id="tool-1")["state"], "blocked")
        self.assertEqual(self.transport.notices, [])
        self.assertEqual(self.event("permission_end", tool_id="tool-1")["state"], "working")

    def test_failed_or_interrupted_turns_are_not_done(self):
        for reason in ("error", "aborted", "refusal"):
            self.event("start", turn_id="turn-1")
            self.stop(key="stop-" + reason)
            self.assertEqual(self.complete(reason=reason)["state"], "blocked")
        self.assertEqual(self.transport.notices, [])

    def test_session_exit_invalidates_pending_completion(self):
        self.expected = ["review"]
        self.stop()
        self.complete()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.event("end")
        self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(self.transport.notices, [])

    def test_review_infrastructure_failure_does_not_claim_success(self):
        self.expected = ["review"]
        self.stop()
        self.complete()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.assertEqual(self.runtime.finish_hook("session-1", run_id, 1)["state"], "blocked")
        self.assertEqual(self.transport.notices, [])

    def test_state_does_not_persist_extra_payload_fields(self):
        self.event("activity", prompt="SECRET-PROMPT", tool_input={"command": "SECRET-COMMAND"})
        text = "\n".join(path.read_text() for path in self.root.rglob("*.json"))
        self.assertNotIn("SECRET-PROMPT", text)
        self.assertNotIn("SECRET-COMMAND", text)

    def test_command_stop_snapshot_can_be_confirmed_by_native_turn_end(self):
        self.assertTrue(hasattr(self.runtime, "observe_hook"), "standard command hook capture is missing")
        self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = self.owner
        self.runtime.observe_hook({"hook_event_name": "Stop", "session_id": "session-1",
                                   "background_tasks": [], "session_crons": []})
        self.assertEqual(self.transport.notices, [])
        result = self.event("complete", turn_id="turn-1", reason="answer", has_answer=True, post_turn=True)
        self.assertEqual(result["state"], "idle")

    def test_review_starting_before_command_snapshot_is_not_expected_twice(self):
        self.expected = ["review"]
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.stop()
        self.complete()
        result = self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(result["pending_reviews"], 0)
        self.assertEqual(result["state"], "idle")

    def test_a_delayed_different_session_cannot_take_back_the_pane(self):
        self.runtime.env.update(HERDR_ENV="1", HERDR_PANE_ID="pane-1", HERDR_SOCKET_PATH="/test/socket")
        self.event("bind", born_at=100)
        other = {"session_id": "session-2", "owner": "new-pane-owner", "seq": 1,
                 "action": "bind", "born_at": 200}
        self.runtime.accept(other)
        self.runtime.accept({**other, "seq": 2, "action": "start", "turn_id": "new-turn"})
        late = self.event("bind", born_at=150)
        self.assertTrue(late.get("ignored"), "an old session stole a newer pane binding")
        self.assertEqual(self.transport.reports[-1]["session_id"], "session-2")

    def test_old_review_exit_two_does_not_invalidate_the_new_owner(self):
        self.expected = ["review"]
        self.stop()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.owner = "new-owner"
        self.event("bind", born_at=200)
        self.event("start", turn_id="turn-2")
        self.runtime.finish_hook("session-1", run_id, 2)
        self.expected = []
        self.stop(key="stop-2")
        self.assertEqual(self.complete(turn_id="turn-2")["state"], "idle")

    def test_unreserved_late_review_immediately_reports_working(self):
        self.runtime.env.update(HERDR_ENV="1", HERDR_PANE_ID="pane-1", HERDR_SOCKET_PATH="/test/socket")
        self.event("bind", born_at=100)
        self.event("start", turn_id="turn-1")
        self.stop()
        self.complete()
        self.runtime.begin_hook("session-1", "late-review", "review")
        self.assertEqual(self.transport.reports[-1]["state"], "working")

    def test_review_feedback_consumed_in_the_current_turn_can_finish(self):
        self.expected = ["review"]
        self.stop()
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.runtime.finish_hook("session-1", run_id, 2)
        self.event("activity", feedback_delivered=True)
        self.event("activity", parent_step=True)
        self.expected = []
        self.stop(key="stop-2")
        self.assertEqual(self.complete()["state"], "idle")

    def test_an_untracked_child_review_is_not_cleared_by_a_clean_root_stop(self):
        self.unknown = True
        self.event("prepare_stop", event_key="child-stop", hook_event_name="SubagentStop", agent_id="child")
        self.unknown = False
        self.stop()
        self.assertEqual(self.complete()["reason"], "untracked_async_hook")

    def test_rebinding_exposes_unsettled_old_reviews_as_a_recoverable_unknown(self):
        self.expected = ["review"]
        self.stop()
        self.owner = "new-owner"
        self.event("bind", born_at=200)
        self.event("start", turn_id="turn-2")
        self.expected = []
        self.stop(key="stop-2")
        result = self.complete(turn_id="turn-2")
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["reason"], "unsettled_previous_run")
        self.assertEqual(result["pending_reviews"], 1)
        self.assertEqual(self.transport.notices, [])

    def test_running_resumed_session_with_old_reviews_reports_working_to_herdr(self):
        self.runtime.env.update(HERDR_ENV="1", HERDR_PANE_ID="pane-1", HERDR_SOCKET_PATH="/test/socket")
        self.event("bind", born_at=100)
        self.expected = ["review"]
        self.stop()
        self.owner = "new-owner"
        self.event("bind", born_at=200)
        result = self.event("start", turn_id="turn-2")
        self.assertEqual((result["state"], result["reason"]), ("working", "unsettled_previous_run"))
        self.assertEqual(self.transport.reports[-1]["state"], "working")
        self.assertEqual(result["pending_reviews"], 1)
        self.assertEqual(self.transport.notices, [])

    def test_old_reviews_do_not_hide_genuine_input_requests(self):
        self.expected = ["review"]
        self.stop()
        self.owner = "new-owner"
        self.event("bind", born_at=200)
        self.event("start", turn_id="turn-2")
        for kind, reason in (("question", "user_question"), ("permission", "permission_required")):
            with self.subTest(kind=kind):
                result = self.event(kind + "_begin", tool_id="input-1")
                self.assertEqual((result["state"], result["reason"]), ("blocked", reason))
                result = self.event(kind + "_end", tool_id="input-1")
                self.assertEqual((result["state"], result["reason"]), ("working", "unsettled_previous_run"))

    def test_session_exit_keeps_an_already_qualified_notification_deliverable(self):
        self.runtime._dispatch = lambda *args: None  # Pause the transport boundary only.
        self.stop()
        self.complete()
        self.event("end")
        self.runtime.deliver_pending("session-1", self.owner)
        self.assertEqual([n["kind"] for n in self.transport.notices], ["done"])

    def test_late_old_owner_cannot_admit_a_review_for_the_new_owner(self):
        self.owner = "new-owner"
        self.event("bind", born_at=200)
        self.event("start", turn_id="turn-2")
        old_runtime = self.module.Runtime(self.root, self.transport,
                                         env={"CLAUDE_TASK_LIFECYCLE_OWNER": "process-one"})
        self.assertEqual(old_runtime.begin_hook("session-1", "old-event", "review"), "")
        self.stop(key="stop-2")
        self.assertEqual(self.complete(turn_id="turn-2")["state"], "idle")

    def conditional_event(self, command):
        config = self.root / "config"
        self.runtime.env["CLAUDE_CONFIG_DIR"] = str(config)
        manifest = config / "hooks/task-lifecycle/install-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"wrappers": [
            {"id": "commit-review", "original": {"if": "Bash(git commit:*)"}},
            {"id": "push-review", "original": {"if": "Bash(git push:*)"}},
        ]}))
        self.expected = ["commit-review", "push-review"]
        event = {"session_id": "session-1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                 "tool_use_id": "git-tool", "tool_input": {"command": command}}
        self.runtime.observe_hook(event)
        self.expected = []
        return event

    def test_known_applicable_conditional_review_is_reserved(self):
        event = self.conditional_event('git commit -m "change"')
        self.stop()
        self.assertEqual(self.complete()["state"], "working")
        run_id = self.runtime.begin_hook("session-1", self.module.event_fingerprint(event), "commit-review")
        self.assertEqual(self.runtime.finish_hook("session-1", run_id, 0)["state"], "idle")

    def test_unproven_conditional_applicability_suppresses_done(self):
        event = self.conditional_event('echo $(date)')
        self.stop()
        self.assertEqual(self.complete()["reason"], "review_applicability_unknown")
        run_id = self.runtime.begin_hook("session-1", self.module.event_fingerprint(event), "commit-review")
        self.assertEqual(self.runtime.finish_hook("session-1", run_id, 0)["reason"], "review_applicability_unknown")
        self.assertEqual(self.transport.notices, [])

    def test_positive_receipts_for_every_uncertain_hook_can_resolve_the_gate(self):
        event = self.conditional_event('echo $(date)')
        self.stop()
        self.complete()
        for hook in ("commit-review", "push-review"):
            run_id = self.runtime.begin_hook("session-1", self.module.event_fingerprint(event), hook)
            result = self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(result["state"], "idle")
        self.assertEqual([n["kind"] for n in self.transport.notices], ["done"])

    def test_tool_hook_fingerprints_distinguish_parallel_calls(self):
        common = {"session_id": "session-1", "hook_event_name": "PostToolUse", "prompt_id": "prompt"}
        self.assertNotEqual(self.module.event_fingerprint({**common, "tool_use_id": "tool-a"}),
                            self.module.event_fingerprint({**common, "tool_use_id": "tool-b"}))

    def test_notification_contains_the_final_parent_reply(self):
        self.stop()
        self.complete(answer_text="Fixed the notification body.\nAll checks passed.")
        self.assertEqual(self.transport.notices[-1].get("message"),
                         "Fixed the notification body.\nAll checks passed.")

    def test_reply_survives_review_delay_without_entering_status_and_is_removed_after_delivery(self):
        self.expected = ["review"]
        self.stop()
        result = self.complete(answer_text="FINAL-REPLY-FOR-DELIVERY")
        self.assertEqual(self.transport.notices, [])
        self.assertNotIn("FINAL-REPLY-FOR-DELIVERY", json.dumps(result))
        self.assertNotIn("FINAL-REPLY-FOR-DELIVERY", self.runtime.path("session-1").read_text())
        payloads = [p for p in self.root.rglob("*.json") if "FINAL-REPLY-FOR-DELIVERY" in p.read_text()]
        self.assertEqual(len(payloads), 1, "reply must wait privately for the review to settle")
        self.assertEqual(payloads[0].stat().st_mode & 0o777, 0o600)
        run_id = self.runtime.begin_hook("session-1", "stop-1", "review")
        self.runtime.finish_hook("session-1", run_id, 0)
        self.assertEqual(self.transport.notices[-1].get("message"), "FINAL-REPLY-FOR-DELIVERY")
        self.assertFalse(payloads[0].exists())

    def test_new_activity_discards_the_waiting_reply_payload(self):
        self.expected = ["review"]
        self.stop()
        self.complete(answer_text="OLD-REPLY-THAT-MUST-NOT-SEND")
        self.event("activity")
        text = "\n".join(p.read_text() for p in self.root.rglob("*.json"))
        self.assertNotIn("OLD-REPLY-THAT-MUST-NOT-SEND", text)
        self.assertEqual(self.transport.notices, [])

    def test_waiting_response_is_not_used_in_place_of_the_final_parent_reply(self):
        self.stop(background=[{"id": "child", "status": "running"}])
        self.complete(answer_text="Interim: waiting for the child.")
        self.event("child_complete", agent_id="child")
        self.event("start", turn_id="turn-2")
        self.stop(key="stop-2")
        self.complete(turn_id="turn-2", answer_text="Final: implemented and checked.")
        self.assertEqual([n.get("message") for n in self.transport.notices], ["Final: implemented and checked."])

    def test_unrelated_multiline_tool_call_settles_after_the_actual_stop_review(self):
        config = self.root / "config"
        manifest = config / "hooks/task-lifecycle/install-manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"wrappers": [
            {"id": "push-review", "original": {"if": "Bash(git push:*)"}},
            {"id": "commit-review", "original": {"if": "Bash(git commit:*)"}},
        ]}))
        self.runtime.env["CLAUDE_CONFIG_DIR"] = str(config)
        self.expected = ["push-review", "commit-review"]
        self.runtime.observe_hook({"hook_event_name": "PostToolUse", "session_id": "session-1",
            "tool_name": "Bash", "tool_use_id": "tool-multiline", "tool_input": {
                "command": "cd /work\npython3 - <<'PY'\nprint('read-only probe')\nPY"}})
        self.expected = ["stop-review"]
        self.stop()
        self.complete(answer_text="The diagnostic work is complete.")
        self.assertEqual(self.transport.notices, [])
        run = self.runtime.begin_hook("session-1", "stop-1", "stop-review")
        result = self.runtime.finish_hook("session-1", run, 0)
        self.assertEqual(result["state"], "idle")
        self.assertEqual([notice["message"] for notice in self.transport.notices], ["The diagnostic work is complete."])

    def test_a_git_command_later_in_a_compound_still_requires_its_review(self):
        config = self.root / "config"
        manifest = config / "hooks/task-lifecycle/install-manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"wrappers": [{"id": "push-review", "original": {"if": "Bash(git push:*)"}}]}))
        self.runtime.env["CLAUDE_CONFIG_DIR"] = str(config)
        self.expected = ["push-review"]
        tool = {"hook_event_name": "PostToolUse", "session_id": "session-1", "tool_name": "Bash",
                "tool_use_id": "tool-compound", "tool_input": {"command": "pwd && git push"}}
        self.runtime.observe_hook(tool)
        self.expected = []
        self.stop()
        result = self.complete()
        self.assertEqual(result["reason"], "background_review")
        self.assertEqual(result["pending_reviews"], 1)
        self.assertEqual(self.transport.notices, [])
        run = self.runtime.begin_hook("session-1", self.module.event_fingerprint(tool), "push-review")
        self.runtime.finish_hook("session-1", run, 0)
        self.assertEqual(len(self.transport.notices), 1)


if __name__ == "__main__":
    unittest.main()
