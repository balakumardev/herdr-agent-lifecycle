"""Claude replaces a plugin's cache directory on update; loaded sessions keep the old hooks.

These cases use the real audit, installer migration and a Claude cache layout:
plugins/cache/<marketplace>/<plugin>/<version>.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLUGIN = "security-guidance@claude-plugins-official"
REVIEW = {"type": "command", "asyncRewake": True, "rewakeMessage": "Review feedback",
          "command": 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/review.sh"'}
COMMIT_REVIEW = {**REVIEW, "if": "Bash(git*commit*)"}


class CaptureTransport:
    def __init__(self):
        self.reports = []

    def publish(self, **notice):
        return {"status": "sent"}

    def report(self, **report):
        self.reports.append(report)
        return True


class PluginUpdateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("task_lifecycle_runtime_updates", ROOT / "runtime.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-plugin-updates-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.claude = base / "home/.claude"
        self.state = base / "state"
        self.cache = self.claude / "plugins/cache/claude-plugins-official/security-guidance"
        self.registry = self.claude / "plugins/installed_plugins.json"
        self.manifest_path = self.claude / "hooks/task-lifecycle/install-manifest.json"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write(self.claude / "settings.json", {"enabledPlugins": {PLUGIN: True}})
        # The explicit installer instrumented 2.0.16; Claude then installs 2.0.17 beside it.
        self.install_version("2.0.16")
        self.instrument("2.0.16")
        self.sequence = 0
        self.session = "session-1"
        self.make_runtime(reinstrument=True, pid=None)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def hooks_file(self, version):
        return self.cache / version / "hooks/hooks.json"

    def install_version(self, version, hooks=None):
        self.write(self.cache / version / ".claude-plugin/plugin.json", {"name": "security-guidance", "version": version})
        self.write(self.hooks_file(version), {"hooks": hooks or {
            "Stop": [{"hooks": [dict(REVIEW)]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [dict(COMMIT_REVIEW)]}],
        }})
        self.write(self.registry, {"version": 2, "plugins": {PLUGIN: [
            {"scope": "user", "installPath": str(self.cache / version), "version": version}]}})

    def instrument(self, version):
        import install
        installation = install.Installation(self.claude.parent, ROOT)
        for plugin_root, path, definitions in install.plugin_files(
                json.loads((self.claude / "settings.json").read_text()), self.claude):
            installation.migrate_hooks(path, plugin_root, definitions=definitions)
        for change in installation.changes:
            change["path"].write_bytes(change["data"])
        self.write(self.manifest_path, {"version": 1, "wrappers": installation.wrappers})
        return {record["event"]: record["id"] for record in installation.wrappers}

    def make_runtime(self, reinstrument, started=None, pid=4242):
        """The runtime of one Claude process; `started` is when it registered its hooks."""
        hook = (lambda: self.module.reinstrument_plugins(self.claude)) if reinstrument else None
        self.transport = CaptureTransport()
        process = None if started is None and pid is None else [pid, started]
        self.runtime = self.module.Runtime(self.state, self.transport, env={"CLAUDE_CONFIG_DIR": str(self.claude)},
                                           reinstrument=hook, claude_process=lambda: process)

    def moment(self):
        """A time clearly between the file writes before and after it, even with coarse file clocks."""
        time.sleep(0.05)
        moment = time.time()
        time.sleep(0.05)
        return moment

    def accept(self, action, owner, **data):
        self.sequence += 1
        return self.runtime.accept({"session_id": self.session, "owner": owner,
                                    "seq": self.sequence, "action": action, **data})

    def load(self, owner, load_id, born_at):
        """session.start: bind, then the load-time configuration."""
        self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = owner
        self.accept("bind", owner, born_at=born_at)
        return self.configure(owner, load_id, "load")

    def configure(self, owner, load_id, phase, registered_at=None):
        settings = json.loads((self.claude / "settings.json").read_text())
        return self.accept("configure", owner, load_id=load_id, phase=phase, registered_at=registered_at,
                           hooks={"Stop": [], "SubagentStop": [], "PostToolUse": [], "UserPromptSubmit": []},
                           enabled_plugins=settings["enabledPlugins"])

    def stop_event(self, key):
        return {"session_id": self.session, "hook_event_name": "Stop", "prompt_id": key,
                "background_tasks": [], "session_crons": []}

    def run_review(self, owner, event, hook_id):
        runner = self.module.Runtime(self.state, self.transport,
                                     env={"CLAUDE_TASK_LIFECYCLE_OWNER": owner, "CLAUDE_CONFIG_DIR": str(self.claude)})
        run_id = runner.begin_hook(self.session, self.module.event_fingerprint(event), hook_id)
        self.assertTrue(run_id, "the loaded wrapper could not record its review")
        runner.finish_hook(self.session, run_id, 0)

    def finish_turn(self, owner, turn_id, review_ran, review_first=False):
        """Stop snapshot, the wrapped review when the session loaded one, then the native boundary."""
        event = self.stop_event(turn_id)
        if review_ran is not None and review_first:
            self.run_review(owner, event, review_ran)  # Concurrent hooks: the review can win the race.
        self.runtime.observe_hook(event)
        if review_ran is not None and not review_first:
            self.run_review(owner, event, review_ran)
        return self.accept("complete", owner, turn_id=turn_id, reason="answer", post_turn=True,
                           has_answer=True, answer_text="Final answer")

    def wrapped_ids(self, version):
        stored = json.loads(self.hooks_file(version).read_text())
        ids = {}
        for event, groups in stored["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    if hook.get("asyncRewake"):
                        words = shlex.split(hook["command"])
                        ids[event] = words[words.index("--id") + 1] if "--id" in words else None
        return ids

    def test_update_after_load_keeps_the_reviews_this_session_loaded(self):
        loaded = self.wrapped_ids("2.0.16")
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")  # Background update while the session runs.
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "step")
        result = self.finish_turn("owner-1", "turn-1", review_ran=loaded["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_update_is_reinstrumented_for_later_sessions(self):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "turn")
        current = self.wrapped_ids("2.0.17")
        self.assertTrue(all(current.values()), "the updated version was left uninstrumented")
        self.assertNotEqual(current["Stop"], self.wrapped_ids("2.0.16")["Stop"], "a new plugin root is a new native scope")
        records = json.loads(self.manifest_path.read_text())["wrappers"]
        self.assertTrue({current["Stop"], current["PostToolUse"]} <= {record["id"] for record in records})
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {"enabledPlugins": {PLUGIN: True}})
        self.session = "session-2"
        self.load("owner-2", "load-2", born_at=200)
        self.accept("start", "owner-2", turn_id="turn-1")
        result = self.finish_turn("owner-2", "turn-1", review_ran=current["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_a_session_that_loaded_unwrapped_reviews_is_told_to_restart(self):
        self.install_version("2.0.17")
        configured = self.load("owner-1", "load-1", born_at=100)
        self.assertIs(configured["restart_required"], True)
        self.accept("start", "owner-1", turn_id="turn-1")
        result = self.finish_turn("owner-1", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))
        # A restart (claude --resume) is a new process: a new owner and load.
        restarted = self.load("owner-2", "load-2", born_at=200)
        self.assertIs(restarted["restart_required"], False)
        self.accept("start", "owner-2", turn_id="turn-2")
        result = self.finish_turn("owner-2", "turn-2", review_ran=self.wrapped_ids("2.0.17")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_an_observer_reloaded_in_the_same_process_keeps_untracked_reviews_pending(self):
        self.make_runtime(reinstrument=True)  # One Claude process throughout.
        other = self.claude / "plugins/cache/community/background-review/1.0.0"
        self.write(other / ".claude-plugin/plugin.json", {"name": "background-review"})
        self.write(other / "hooks/hooks.json", {"hooks": {"Stop": [{"hooks": [dict(REVIEW)]}]}})
        registry = json.loads(self.registry.read_text())
        registry["plugins"]["background-review@community"] = [{"scope": "user", "installPath": str(other)}]
        self.write(self.registry, registry)
        self.write(self.claude / "settings.json", {"enabledPlugins": {PLUGIN: True, "background-review@community": True}})
        loaded = self.wrapped_ids("2.0.16")["Stop"]
        self.load("owner-1", "load-1", born_at=100)
        self.accept("start", "owner-1", turn_id="turn-1")
        result = self.finish_turn("owner-1", "turn-1", review_ran=loaded)
        self.assertEqual((result["state"], result["reason"]), ("working", "untracked_async_hook"))
        # The uninstrumented review may still run after the plugin is disabled and
        # /reload-plugins starts a changed observer: a new owner in the same process.
        self.write(self.claude / "settings.json", {"enabledPlugins": {PLUGIN: True}})
        self.assertIs(self.load("owner-2", "load-2", born_at=200)["restart_required"], True)
        self.accept("start", "owner-2", turn_id="turn-2")
        result = self.finish_turn("owner-2", "turn-2", review_ran=loaded)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))

    def test_reload_plugins_registers_the_rewrapped_update_as_a_new_load(self):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "turn")  # Re-wraps 2.0.17 for later loads.
        self.finish_turn("owner-1", "turn-1", review_ran=self.wrapped_ids("2.0.16")["Stop"])
        # /reload-plugins keeps the module and owner; the module reports a load timed before it.
        reloaded = self.configure("owner-1", "load-2", "reload", registered_at=int(self.moment() * 1000))
        self.assertIs(reloaded["restart_required"], False)
        self.accept("start", "owner-1", turn_id="turn-2")
        self.configure("owner-1", "load-2", "turn")
        result = self.finish_turn("owner-1", "turn-2", review_ran=self.wrapped_ids("2.0.17")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_a_reload_that_registered_an_unwrapped_update_is_told_to_restart(self):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        # The reload runs before any turn re-wrapped the update; its own configure re-wraps after.
        reloaded = self.configure("owner-1", "load-2", "reload", registered_at=int(self.moment() * 1000))
        self.assertIs(reloaded["restart_required"], True)
        self.accept("start", "owner-1", turn_id="turn-1")
        result = self.finish_turn("owner-1", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))

    def test_a_reload_before_another_session_rewrapped_the_update_is_told_to_restart(self):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        registered = self.moment()  # The reload registered 2.0.17 unwrapped.
        self.session, owner = "session-2", "owner-2"
        self.load(owner, "load-2", born_at=200)  # Another session's load re-wraps 2.0.17 meanwhile.
        self.session = "session-1"
        self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = "owner-1"
        reloaded = self.configure("owner-1", "load-3", "reload", registered_at=int(registered * 1000))
        self.assertIs(reloaded["restart_required"], True)
        self.accept("start", "owner-1", turn_id="turn-1")
        result = self.finish_turn("owner-1", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))

    def test_a_session_that_started_before_another_rewrapped_the_update_is_told_to_restart(self):
        self.install_version("2.0.17")
        started = self.moment()  # This process registered 2.0.17 unwrapped.
        self.load("owner-1", "load-1", born_at=100)  # Another session's load re-wraps 2.0.17.
        self.assertTrue(self.wrapped_ids("2.0.17")["Stop"])
        self.session = "session-2"
        self.make_runtime(reinstrument=True, started=started)
        configured = self.load("owner-2", "load-2", born_at=200)
        self.assertIs(configured["restart_required"], True)
        self.accept("start", "owner-2", turn_id="turn-1")
        result = self.finish_turn("owner-2", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))
        self.make_runtime(reinstrument=True, started=self.moment())
        self.assertIs(self.load("owner-3", "load-3", born_at=300)["restart_required"], False)
        self.accept("start", "owner-3", turn_id="turn-2")
        result = self.finish_turn("owner-3", "turn-2", review_ran=self.wrapped_ids("2.0.17")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def replaced_by_update(self, review_first):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "turn")
        # A reload the module never saw: Claude now runs the re-wrapped 2.0.17 review instead.
        return self.finish_turn("owner-1", "turn-1", review_ran=self.wrapped_ids("2.0.17")["Stop"],
                                review_first=review_first)

    def test_an_update_that_runs_in_place_of_an_expected_review_is_told_to_restart(self):
        result = self.replaced_by_update(review_first=False)
        self.assertEqual((result["state"], result["reason"], result["pending_reviews"]),
                         ("working", "restart_required", 0))

    def test_an_update_that_ran_before_the_snapshot_is_told_to_restart(self):
        result = self.replaced_by_update(review_first=True)
        self.assertEqual((result["state"], result["reason"], result["pending_reviews"]),
                         ("working", "restart_required", 0))

    def test_another_plugins_review_does_not_replace_an_expected_one(self):
        foreign = "async-" + "f" * 24
        other = self.claude / "plugins/cache/community/background-review/1.0.0/hooks/hooks.json"
        self.write(other, {"hooks": {}})
        manifest = json.loads(self.manifest_path.read_text())
        manifest["wrappers"].append({"id": foreign, "event": "Stop", "matcher": None, "file": str(other),
                                     "original": dict(REVIEW), "wrapped": dict(REVIEW)})
        self.write(self.manifest_path, manifest)
        self.load("owner-1", "load-1", born_at=100)
        self.accept("start", "owner-1", turn_id="turn-1")
        event = self.stop_event("turn-1")
        self.runtime.observe_hook(event)
        self.run_review("owner-1", event, foreign)
        result = self.accept("inspect", "owner-1")
        self.assertEqual((result["state"], result["reason"], result["pending_reviews"]),
                         ("working", "background_review", 1))

    def test_a_load_without_settings_rewraps_nothing_before_its_own_snapshot(self):
        self.install_version("2.0.17")
        before = self.hooks_file("2.0.17").read_bytes()
        self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = "owner-1"
        self.accept("bind", "owner-1", born_at=100)
        # $.settings.read() failed at session.start, so this load has no snapshot yet.
        self.accept("configure", "owner-1", load_id="load-1", phase="load", enabled_plugins={},
                    hooks={"Stop": None, "SubagentStop": None, "PostToolUse": None, "UserPromptSubmit": None})
        self.assertEqual(self.hooks_file("2.0.17").read_bytes(), before)
        self.accept("start", "owner-1", turn_id="turn-1")
        self.assertIs(self.configure("owner-1", "load-1", "turn")["restart_required"], True)
        result = self.finish_turn("owner-1", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "restart_required"))
        self.assertIs(self.load("owner-2", "load-2", born_at=200)["restart_required"], False)
        self.accept("start", "owner-2", turn_id="turn-2")
        result = self.finish_turn("owner-2", "turn-2", review_ran=self.wrapped_ids("2.0.17")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_an_unwrapped_review_still_suppresses_done_when_reinstrumentation_is_disabled(self):
        self.make_runtime(reinstrument=False)
        self.install_version("2.0.17")
        before = self.hooks_file("2.0.17").read_bytes()
        configured = self.load("owner-1", "load-1", born_at=100)
        self.assertIs(configured["restart_required"], False)
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "turn")
        result = self.finish_turn("owner-1", "turn-1", review_ran=None)
        self.assertEqual((result["state"], result["reason"]), ("working", "untracked_async_hook"))
        self.assertEqual(self.hooks_file("2.0.17").read_bytes(), before)

    def test_plugins_never_instrumented_by_the_installer_are_not_wrapped(self):
        other = self.claude / "plugins/cache/community/background-review/1.0.0"
        self.write(other / ".claude-plugin/plugin.json", {"name": "background-review"})
        self.write(other / "hooks/hooks.json", {"hooks": {"Stop": [{"hooks": [dict(REVIEW)]}]}})
        registry = json.loads(self.registry.read_text())
        registry["plugins"]["background-review@community"] = [{"scope": "user", "installPath": str(other)}]
        self.write(self.registry, registry)
        self.write(self.claude / "settings.json", {"enabledPlugins": {PLUGIN: True, "background-review@community": True}})
        before = (other / "hooks/hooks.json").read_bytes()
        configured = self.load("owner-1", "load-1", born_at=100)
        self.assertEqual((other / "hooks/hooks.json").read_bytes(), before)
        self.assertIs(configured["restart_required"], False)
        self.accept("start", "owner-1", turn_id="turn-1")
        result = self.finish_turn("owner-1", "turn-1", review_ran=self.wrapped_ids("2.0.16")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("working", "untracked_async_hook"))

    def test_a_plugin_enabled_after_load_is_audited_as_installed_now(self):
        self.load("owner-1", "load-1", born_at=100)
        other = self.claude / "plugins/cache/community/background-review/1.0.0"
        self.write(other / ".claude-plugin/plugin.json", {"name": "background-review"})
        self.write(other / "hooks/hooks.json", {"hooks": {"Stop": [{"hooks": [dict(REVIEW)]}]}})
        registry = json.loads(self.registry.read_text())
        registry["plugins"]["background-review@community"] = [{"scope": "user", "installPath": str(other)}]
        self.write(self.registry, registry)
        self.write(self.claude / "settings.json", {"enabledPlugins": {PLUGIN: True, "background-review@community": True}})
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "turn")
        result = self.finish_turn("owner-1", "turn-1", review_ran=self.wrapped_ids("2.0.16")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("working", "untracked_async_hook"))

    def test_clear_keeps_the_hooks_of_the_same_module_load(self):
        loaded = self.wrapped_ids("2.0.16")
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        # /clear binds a new session id without reloading plugins or the module.
        self.session = "session-after-clear"
        self.runtime.env["CLAUDE_TASK_LIFECYCLE_OWNER"] = "owner-2"
        self.accept("bind", "owner-2", born_at=200)
        self.configure("owner-2", "load-1", "turn")
        self.accept("start", "owner-2", turn_id="turn-1")
        result = self.finish_turn("owner-2", "turn-1", review_ran=loaded["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_conditional_reviews_use_the_loaded_conditions_after_the_manifest_moves_on(self):
        self.load("owner-1", "load-1", born_at=100)
        self.install_version("2.0.17")
        self.instrument("2.0.17")  # A later explicit installer run drops the 2.0.16 records.
        self.accept("start", "owner-1", turn_id="turn-1")
        self.configure("owner-1", "load-1", "step")
        # The loaded commit review matches Bash but its `if` excludes this command.
        self.runtime.observe_hook({"session_id": self.session, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                   "tool_use_id": "bash-1", "tool_input": {"command": "ls -la"}})
        result = self.finish_turn("owner-1", "turn-1", review_ran=self.wrapped_ids("2.0.16")["Stop"])
        self.assertEqual((result["state"], result["reason"]), ("idle", "task_complete"))

    def test_status_names_the_restart_remedy(self):
        self.install_version("2.0.17")
        self.runtime.env.update({"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1",
                                 "HERDR_SOCKET_PATH": str(self.state / "herdr.sock")})
        self.load("owner-1", "load-1", born_at=100)
        self.runtime.observe_hook(self.stop_event("turn-1"))
        self.assertEqual(self.transport.reports[-1]["reason"], "restart_required")
        self.assertEqual(self.accept("inspect", "owner-1")["reason"], "restart_required")


if __name__ == "__main__":
    unittest.main()
