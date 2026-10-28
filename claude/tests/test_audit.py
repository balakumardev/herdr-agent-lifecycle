import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime import audit_hooks, condition_matches


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "plugins").mkdir()
        self.plugin = self.root / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "review", "hooks": "./extra-hooks.json"}))
        (self.root / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {"review@local": [{"installPath": str(self.plugin)}]}}))
        self.event = {"hook_event_name": "Stop", "effective_hooks": [], "enabled_plugins": {"review@local": True}}

    def test_custom_plugin_hook_manifest_is_not_ignored(self):
        (self.plugin / "extra-hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "review", "asyncRewake": True}]}]}}))
        self.assertTrue(audit_hooks(self.event, self.root)[1])

    def test_unresolved_enabled_plugin_remains_unknown(self):
        self.event["enabled_plugins"] = {"unknown@local": True}
        self.assertTrue(audit_hooks(self.event, self.root)[1])

    def test_subagent_matchers_are_not_filtered_before_the_agent_type_is_known(self):
        wrapped = {"type": "command", "command": "wrapper", "asyncRewake": True}
        (self.plugin / "hooks").mkdir()
        (self.plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {"SubagentStop": [{"matcher": "Explore", "hooks": [wrapped]}]}}))
        (self.plugin / ".claude-plugin/plugin.json").write_text('{"name":"review"}')
        manifest = self.root / "hooks/task-lifecycle/install-manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"wrappers": [{"id": "explore-review", "event": "SubagentStop", "matcher": "Explore", "wrapped": wrapped}]}))
        event = {**self.event, "hook_event_name": "SubagentStop"}
        self.assertEqual(audit_hooks(event, self.root)[0], ["explore-review"])
        self.assertEqual(audit_hooks({**event, "agent_type": "general-purpose"}, self.root)[0], [])
        current = self.plugin / "hooks/hooks.json"
        current.write_text(current.read_text().replace('"Explore"', '"general-purpose"'))
        expected, unknown = audit_hooks({**event, "agent_type": "general-purpose"}, self.root)
        self.assertTrue(unknown, "a stale installation matcher was treated as current")
        self.assertEqual(expected, [])

    def test_conditional_bash_matching_follows_proven_native_literal_cases(self):
        cases = [
            ("Bash(git commit:*)", "git commit -m 'change'", True),
            ("Bash(git commit:*)", "git commitment", False),
            ("Bash(git commit:*)", "sleep 3", False),
            ("Bash(git -C * commit *)", "git -C /repo commit -m change", True),
            ("Bash(gt submit:*)", "gt submit", True),
            ("Bash(git push:*)", "git push && echo done", True),
            ("Bash(git push:*)", "echo $(date)", None),
            ("Bash(git push:*)", "VAR=x git push", True),
        ]
        for condition, command, expected in cases:
            with self.subTest(command=command):
                result = condition_matches(condition, {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command}})
                self.assertIs(result, expected)

    def test_unrelated_multiline_commands_do_not_leave_commit_reviews_unknown(self):
        conditions = ["Bash(git commit:*)", "Bash(git push:*)", "Bash(git -C * commit *)",
                      "Bash(git -C * push*)", "Bash(gt create:*)", "Bash(gt modify:*)", "Bash(gt submit:*)"]
        commands = [
            "echo 'Inspecting diagnostics'\npython3 - <<'PY'\nprint('status complete')\nPY",
            "cd /work\npython3 - <<'PY'\nprint('Local probe')\nPY",
            "printf '%s\\n' 'finished'",
        ]
        for command in commands:
            for condition in conditions:
                with self.subTest(condition=condition, command=command):
                    self.assertIs(condition_matches(condition, {"hook_event_name": "PostToolUse",
                        "tool_name": "Bash", "tool_input": {"command": command}}), False)


if __name__ == '__main__':
    unittest.main()
