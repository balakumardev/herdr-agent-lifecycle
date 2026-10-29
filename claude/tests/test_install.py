"""Installer integration tests; every mutation is confined to a temporary home."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "credential-sentinel-do-not-migrate"
TOPIC = "private-fixture-topic"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None,
                                      stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
            for p in root.rglob("*")}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "install.py").is_file(), "installer is not implemented yet")
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-install-")
        self.addCleanup(self.tmp.cleanup)
        # The installer canonicalizes --home; macOS /var is a symlink.
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home with spaces"
        self.claude = self.home / ".claude"
        self.source = self.root / "source with spaces"
        self.target = self.claude / "hooks/task-lifecycle"
        self.market = self.claude / "local-marketplaces/task-lifecycle"
        self.settings_path = self.claude / "settings.json"
        self.registry_path = self.claude / "plugins/installed_plugins.json"
        self.plugin = self.claude / "plugins/cache/reviewer/v1"
        self.plugin_path = self.plugin / "hooks/hooks.json"
        self.disabled_path = self.claude / "plugins/cache/disabled/hooks/hooks.json"
        self.project_path = self.claude / "plugins/cache/project/hooks/hooks.json"
        self.managed_path = self.root / "managed/hooks/hooks.json"
        self.shim = self.claude / "hooks/ntfy-notify.sh"
        for name in ("install.py", "hook_runner.py", "event_hook.py", "runtime.py", "transport.py", "configuration.py", "bash_conditions.py"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Portable fixture module.\n")
        write_json(self.source / ".claude-plugin/plugin.json", {"name": "task-lifecycle", "version": "1.0.0"})
        write_json(self.source / "hooks/hooks.json", {"modules": ["./register.js"], "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": 'python3 "${CLAUDE_PLUGIN_ROOT}/event_hook.py"'}]}]}})
        (self.source / "hooks/register.js").write_text("export default function register() {}\n")
        for name in (".credentials.json", ".env", "config.json", "runtime-config.json",
                     "state/events.jsonl", "backups/old.json", "node_modules/private/index.js",
                     "__pycache__/runtime.pyc", ".claude-plugin/types/hooks.d.ts"):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PRIVATE)
        self.async_shell = {"type": "command", "command": "printf '%s' 'user payload'",
                            "asyncRewake": True, "timeout": 43, "rewakeMessage": "Review ready",
                            "rewakeSummary": "Summary", "if": "true", "futureProperty": {"keep": True}}
        self.async_exec = {"type": "command", "command": "/usr/bin/printf",
                           "args": ["%s", "literal $HOME; with spaces"], "asyncRewake": True,
                           "timeout": 19}
        self.audit = {"type": "command", "command": "audit-logger", "timeout": 5}
        self.auth = {"type": "command", "command": "auth-refresh", "timeout": 9}
        self.memory = {"type": "command", "command": "memory-save", "async": True}
        self.worktree = {"type": "command", "command": "worktree-sync"}
        self.settings = {
            "env": {"API_KEY": PRIVATE}, "permissions": {"allow": ["Read"]},
            "enabledPlugins": {"reviewer@local": True, "disabled@local": False,
                               "project@local": True, "managed@local": True},
            "hooks": {
                "Notification": [{"matcher": "idle_prompt", "groupOption": True, "hooks": [
                    {"type": "command", "command": shlex.quote(str(self.shim))}, self.audit]}],
                "SessionStart": [{"matcher": "startup|resume", "hooks": [
                    {"type": "command", "command": "bash " + shlex.quote(str(self.claude / "hooks/herdr-agent-state.sh"))},
                    self.auth, {"type": "http", "url": "https://local.invalid/hook"}]}],
                "Stop": [{"hooks": [
                    {"type": "command", "command": shlex.quote(str(self.shim))},
                    self.async_shell, self.memory,
                    {"type": "command", "command": "audit-logger ntfy-notify.sh"}]}],
                "SubagentStop": [{"matcher": "*", "hooks": [self.async_exec]}],
                "PostToolUse": [{"matcher": "Edit|Write", "hooks": [self.async_shell, self.worktree]}],
                "UserPromptSubmit": [{"hooks": [self.async_exec]}],
            }, "custom": {"opaque": [1, 2, 3]},
        }
        self.plugin_hook = {"type": "command", "asyncRewake": True, "timeout": 67,
                            "command": 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/review.sh"',
                            "if": "true", "rewakeMessage": "Review completed"}
        self.plugin_config = {"description": "Existing plugin", "env": {"PRIVATE": PRIVATE},
                              "hooks": {event: [{"matcher": "*", "hooks": [self.plugin_hook]}]
                                        for event in ("Stop", "SubagentStop", "PostToolUse", "UserPromptSubmit", "Notification")}}
        write_json(self.settings_path, self.settings)
        for path in (self.plugin_path, self.disabled_path, self.project_path, self.managed_path):
            write_json(path, self.plugin_config)
        write_json(self.registry_path, {"version": 2, "plugins": {
            "reviewer@local": [{"scope": "user", "installPath": str(self.plugin), "version": "1"}],
            "disabled@local": [{"scope": "user", "installPath": str(self.disabled_path.parent.parent)}],
            "project@local": [{"scope": "project", "installPath": str(self.project_path.parent.parent)}],
            "managed@local": [{"scope": "user", "installPath": str(self.managed_path.parent.parent)}],
        }})
        self.original_shim = '#!/bin/sh\nTOPIC="' + TOPIC + '"\n' + 'touch "' + str(self.root / "legacy-ran") + '"\n'
        self.shim.parent.mkdir(parents=True, exist_ok=True)
        self.shim.write_text(self.original_shim)

    def run_installer(self, *flags, success=True):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "install.py"),
                                 "--home", str(self.home), "--source-dir", str(self.source), *flags],
                                capture_output=True, text=True, timeout=15)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertNotIn(TOPIC, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0 if success else 1, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def manifest(self):
        return json.loads((self.target / "install-manifest.json").read_text())

    def test_apply_removes_only_named_registrations_and_keeps_unrelated_settings(self):
        report = self.run_installer("--apply")
        current = json.loads(self.settings_path.read_text())
        for key in ("env", "permissions", "enabledPlugins", "custom"):
            self.assertEqual(current[key], self.settings[key])
        self.assertEqual(current["hooks"]["Notification"], [
            {"matcher": "idle_prompt", "groupOption": True, "hooks": [self.audit]}])
        self.assertEqual(current["hooks"]["SessionStart"][0]["hooks"], [
            self.auth, {"type": "http", "url": "https://local.invalid/hook"}])
        self.assertIn(self.memory, current["hooks"]["Stop"][0]["hooks"])
        self.assertIn(self.worktree, current["hooks"]["PostToolUse"][0]["hooks"])
        self.assertIn({"type": "command", "command": "audit-logger ntfy-notify.sh"},
                      current["hooks"]["Stop"][0]["hooks"])
        self.assertEqual(report["removed_hooks"], 3)
        self.assertEqual(report["wrapped_hooks"], 8)

    def test_wraps_enabled_user_plugins_and_preserves_hook_properties(self):
        untouched = {p: p.read_bytes() for p in (self.registry_path, self.disabled_path,
                                                self.project_path, self.managed_path)}
        self.run_installer("--apply")
        for path, content in untouched.items():
            self.assertEqual(path.read_bytes(), content)
        plugin = json.loads(self.plugin_path.read_text())
        self.assertEqual(plugin["env"], self.plugin_config["env"])
        self.assertEqual(plugin["hooks"]["Notification"], self.plugin_config["hooks"]["Notification"])
        records = self.manifest()["wrappers"]
        self.assertEqual(len(records), 8)
        self.assertEqual(len({record["id"] for record in records}), 8)
        for record in records:
            self.assertEqual({k: v for k, v in record["original"].items() if k not in ("command", "args")},
                             {k: v for k, v in record["wrapped"].items() if k not in ("command", "args")})
            stored = json.loads(Path(record["file"]).read_text())
            matching = [hook for group in stored["hooks"][record["event"]]
                        if group.get("matcher") == record["matcher"] for hook in group["hooks"]]
            self.assertIn(record["wrapped"], matching)
            wrapped = record["wrapped"]
            invocation = ([wrapped["command"], *wrapped["args"]] if "args" in wrapped
                          else shlex.split(wrapped["command"]))
            self.assertEqual(invocation[:2], [str(Path(sys.executable).resolve()), str(self.target / "hook_runner.py")])
            self.assertEqual(invocation[2:4], ["--id", record["id"]])
            if "args" in record["original"]:
                self.assertEqual(invocation[4:], ["--", record["original"]["command"], *record["original"]["args"]])
            else:
                self.assertEqual(invocation[4:], ["--shell", record["original"]["command"]])

    def test_private_backups_do_not_copy_unrelated_credentials(self):
        self.run_installer("--apply")
        backups = self.target / "backups"
        self.assertTrue(backups.is_dir())
        files = [p for p in backups.rglob("*") if p.is_file()]
        self.assertTrue(files)
        self.assertIn(self.original_shim.encode(), [p.read_bytes() for p in files])
        for path in [backups, *backups.rglob("*"), self.target / "install-manifest.json"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, str(path))
            if path.is_file():
                self.assertNotIn(PRIVATE.encode(), path.read_bytes())
        metadata = [json.loads(p.read_text()) for p in files if p.suffix == ".json"]
        self.assertTrue(any(str(self.settings_path) in json.dumps(value) for value in metadata))
        self.assertTrue(any(self.async_shell["command"] in json.dumps(value) for value in metadata))

    def test_stages_local_marketplace_without_migrating_private_files(self):
        self.run_installer("--apply")
        marketplace = json.loads((self.market / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(marketplace["name"], "herdr-agent-lifecycle")
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "task-lifecycle")
        stage = self.market / entry["source"]
        for relative in ("runtime.py", "transport.py", "configuration.py", "bash_conditions.py", "hook_runner.py", "event_hook.py", "install.py",
                         ".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.js"):
            self.assertTrue((stage / relative).is_file(), "required plugin entrypoint was not staged: " + relative)
            self.assertEqual((stage / relative).read_bytes(), (self.source / relative).read_bytes())
            self.assertEqual((self.target / relative).read_bytes(), (self.source / relative).read_bytes())
        for path in stage.rglob("*"):
            if path.is_file():
                self.assertNotIn(PRIVATE.encode(), path.read_bytes())
        for relative in ("node_modules", "state", "backups", "config.json", "runtime-config.json",
                         ".credentials.json", ".env", "__pycache__", ".claude-plugin/types"):
            self.assertFalse((stage / relative).exists())
        report = self.run_installer("--dry-run")
        self.assertEqual(report["commands"], [
            "claude plugin marketplace add " + shlex.quote(str(self.market)),
            "claude plugin install task-lifecycle@herdr-agent-lifecycle --scope user"])
        self.assertEqual(report["minimum_claude_version"], "2.1.292")

    def test_restore_recovers_owned_hooks_and_files_without_copying_private_settings(self):
        self.settings["hooks"]["SessionEnd"] = [{"matcher": "exit", "custom": True, "hooks": [
            {"type": "command", "command": "bash " + shlex.quote(str(self.claude / "hooks/herdr-agent-state.sh"))}]}]
        write_json(self.settings_path, self.settings)
        self.settings_path.chmod(0o640)
        report = self.run_installer("--apply")
        backup = report["backup_directory"]
        self.assertIsInstance(backup, str)
        self.run_installer("--restore", backup, "--apply")
        self.assertEqual(json.loads(self.settings_path.read_text()), self.settings)
        self.assertEqual(json.loads(self.plugin_path.read_text()), self.plugin_config)
        self.assertEqual(self.settings_path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.shim.read_text(), self.original_shim)
        self.assertFalse((self.target / "runtime.py").exists())
        self.assertFalse((self.market / ".claude-plugin/marketplace.json").exists())
        for path in Path(backup).rglob("*"):
            if path.is_file():
                self.assertNotIn(PRIVATE.encode(), path.read_bytes())

    def test_restore_refuses_every_change_if_one_hook_file_was_edited_later(self):
        report = self.run_installer("--apply")
        with self.plugin_path.open("a") as output:
            output.write("\n")
        before = snapshot(self.root)
        self.run_installer("--restore", report["backup_directory"], "--apply", success=False)
        self.assertEqual(snapshot(self.root), before)

    def test_restore_is_dry_run_by_default_and_idempotent_after_applying(self):
        report = self.run_installer("--apply")
        backup = report["backup_directory"]
        before = snapshot(self.root)
        self.run_installer("--restore", backup)
        self.assertEqual(snapshot(self.root), before)
        self.run_installer("--restore", backup, "--apply")
        restored = snapshot(self.root)
        self.assertEqual(self.run_installer("--restore", backup, "--apply")["changed_files"], 0)
        self.assertEqual(snapshot(self.root), restored)

    def test_restore_reconstructs_removed_and_partial_groups_in_original_order(self):
        legacy = {"type": "command", "command": shlex.quote(str(self.shim))}
        self.settings["hooks"]["Stop"] = [
            {"matcher": "a", "custom": 1, "hooks": [legacy, legacy]},
            {"matcher": "b", "hooks": [legacy, self.async_shell, legacy, self.audit]},
            {"matcher": "c", "hooks": [legacy]},
            {"matcher": "d", "hooks": [self.async_exec]},
        ]
        write_json(self.settings_path, self.settings)
        report = self.run_installer("--apply")
        self.run_installer("--restore", report["backup_directory"], "--apply")
        self.assertEqual(json.loads(self.settings_path.read_text()), self.settings)

    def test_restore_reverses_custom_and_inline_plugin_hook_patches(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        custom_path = self.plugin / "custom.json"
        custom = {"Stop": [{"hooks": [self.plugin_hook]}]}
        metadata = {"name": "reviewer", "env": {"TOKEN": PRIVATE}, "hooks": [
            "./custom.json", {"description": "Inline", "hooks": custom}]}
        write_json(custom_path, custom)
        write_json(manifest_path, metadata)
        report = self.run_installer("--apply")
        self.run_installer("--restore", report["backup_directory"], "--apply")
        self.assertEqual(json.loads(custom_path.read_text()), custom)
        self.assertEqual(json.loads(manifest_path.read_text()), metadata)

    def test_optional_public_verification_artifacts_are_runnable_after_staging(self):
        public = {
            "README.md": "# Verify\nRun the tests from this directory.\n",
            "tests/test_staged.py": (
                "import json\nfrom pathlib import Path\nimport unittest\n"
                "class StagedTest(unittest.TestCase):\n"
                "    def test_plugin_is_present(self):\n"
                "        root = Path(__file__).resolve().parents[1]\n"
                "        self.assertEqual(json.loads((root / '.claude-plugin/plugin.json').read_text())['name'], 'task-lifecycle')\n"),
            "tests/observer.test.ts": "import { test } from 'claude-code/testing';\n",
            "tests/other.test.js": "// Native regression fixture.\n",
            "tests/verify_live_cli.py": (
                "import json\nfrom pathlib import Path\n"
                "fixture = Path(__file__).parent / 'live-fixture'\n"
                "assert (fixture / 'hooks/hooks.json').is_file()\n"
                "assert (fixture / 'hooks/register.js').is_file()\n"
                "assert (fixture / 'stop-control.py').is_file()\n"
                "print(json.loads((fixture / '.claude-plugin/plugin.json').read_text())['name'])\n"),
            "tests/live-fixture/.claude-plugin/plugin.json": '{"name":"live-fixture"}\n',
            "tests/live-fixture/hooks/hooks.json": '{"modules":["./register.js"]}\n',
            "tests/live-fixture/hooks/register.js": "export function register(on) {}\n",
            "tests/live-fixture/stop-control.py": "# Public fixture control.\n",
        }
        private = ("tests/__pycache__/test_staged.pyc", "tests/state/private.json",
                   "tests/live-fixture/.claude-plugin/types/test_private.py",
                   "tests/live-fixture/.credentials.json", "tests/live-fixture/config.json",
                   "tests/live-fixture/install-manifest.json", "tests/live-fixture/topic.sh",
                   "tests/live-fixture/backups/old.py", "tests/live-fixture/node_modules/extra.js")
        for relative, content in {**public, **dict.fromkeys(private, PRIVATE)}.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        before = snapshot(self.root)
        self.run_installer("--dry-run")
        self.assertEqual(snapshot(self.root), before)
        self.run_installer("--apply")
        for destination in (self.target, self.market / "plugins/task-lifecycle"):
            for relative, content in public.items():
                self.assertTrue((destination / relative).is_file(), "missing public artifact: " + relative)
                self.assertEqual((destination / relative).read_text(), content)
            for relative in private:
                self.assertFalse((destination / relative).exists(), relative)
            result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                                    cwd=destination, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Ran 1 test", result.stderr)
            fixture = subprocess.run([sys.executable, "-B", "tests/verify_live_cli.py"], cwd=destination,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual((fixture.returncode, fixture.stdout, fixture.stderr), (0, "live-fixture\n", ""))
        before = snapshot(self.root)
        self.assertEqual(self.run_installer("--apply")["changed_files"], 0)
        self.assertEqual(snapshot(self.root), before)

    def test_shim_retains_literal_topic_but_cannot_send_or_execute_old_body(self):
        self.run_installer("--apply")
        self.assertIn('TOPIC="' + TOPIC + '"\n', self.shim.read_text())
        result = subprocess.run(["sh", str(self.shim)], input=b"private hook input", capture_output=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertFalse((self.root / "legacy-ran").exists())
        self.assertNotIn(TOPIC, (self.target / "install-manifest.json").read_text())

    def test_missing_or_dynamic_topic_is_not_invented_or_evaluated(self):
        marker = self.root / "must-not-run"
        self.shim.write_text('TOPIC="$(touch ' + str(marker) + ')"\n')
        self.run_installer("--apply")
        self.assertFalse(marker.exists())
        self.assertFalse(any(line.lstrip().startswith("TOPIC=") for line in self.shim.read_text().splitlines()))
        self.shim.unlink()
        self.run_installer("--apply")
        self.assertFalse(any(line.lstrip().startswith("TOPIC=") for line in self.shim.read_text().splitlines()))

    def test_dry_run_and_default_mode_leave_all_files_and_directories_unchanged(self):
        before = snapshot(self.root)
        for flags in ((), ("--dry-run",)):
            report = self.run_installer(*flags)
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["wrapped_hooks"], 8)
            self.assertEqual(snapshot(self.root), before)

    def test_reapply_is_byte_and_mtime_idempotent(self):
        self.run_installer("--apply")
        before = snapshot(self.root)
        report = self.run_installer("--apply")
        self.assertEqual(report["changed_files"], 0)
        self.assertEqual(report["wrapped_hooks"], 0)
        self.assertEqual(report["removed_hooks"], 0)
        self.assertEqual(report["tracked_hooks"], 8)
        self.assertEqual(snapshot(self.root), before)

    def test_plugin_upgrade_updates_manifest_location_without_nested_wrappers(self):
        self.run_installer("--apply")
        old_manifest = self.manifest()
        old_content = self.plugin_path.read_bytes()
        upgraded = self.plugin.parent / "v2"
        write_json(upgraded / "hooks/hooks.json", self.plugin_config)
        registry = json.loads(self.registry_path.read_text())
        registry["plugins"]["reviewer@local"][0]["installPath"] = str(upgraded)
        write_json(self.registry_path, registry)
        report = self.run_installer("--apply")
        self.assertEqual(report["wrapped_hooks"], 4)
        current = self.manifest()["wrappers"]
        old_user = {r["id"] for r in old_manifest["wrappers"] if r["file"] == str(self.settings_path)}
        new_user = {r["id"] for r in current if r["file"] == str(self.settings_path)}
        old_plugin = {r["id"] for r in old_manifest["wrappers"] if r["file"] == str(self.plugin_path)}
        new_plugin = {r["id"] for r in current if r["file"] == str(upgraded / "hooks/hooks.json")}
        self.assertEqual(new_user, old_user)
        self.assertEqual(len(new_plugin), 4)
        self.assertTrue(old_plugin.isdisjoint(new_plugin), "a new plugin root is a new native scope")
        self.assertFalse(any(r["file"] == str(self.plugin_path) for r in current))
        self.assertEqual(self.plugin_path.read_bytes(), old_content)

    def install_cached_plugin(self, version, enable=False):
        """Claude's own layout: plugins/cache/<marketplace>/<plugin>/<version>."""
        root = self.claude / "plugins/cache/market/guard" / version
        write_json(root / ".claude-plugin/plugin.json", {"name": "guard", "version": version})
        write_json(root / "hooks/hooks.json", self.plugin_config)
        registry = json.loads(self.registry_path.read_text())
        registry["plugins"]["guard@market"] = [{"scope": "user", "installPath": str(root), "version": version}]
        write_json(self.registry_path, registry)
        if enable:
            self.settings["enabledPlugins"]["guard@market"] = True
            write_json(self.settings_path, self.settings)
        return root

    def test_reinstrument_wraps_the_updated_version_of_an_instrumented_plugin(self):
        self.install_cached_plugin("1.0.0", enable=True)
        self.run_installer("--apply")
        installed = {r["id"] for r in self.manifest()["wrappers"]}
        updated = self.install_cached_plugin("1.1.0")
        untouched = {p: p.read_bytes() for p in (self.settings_path, self.plugin_path, self.registry_path,
                                                self.shim, self.target / "runtime.py")}
        staged = snapshot(self.market)
        self.assertEqual(self.run_installer("--reinstrument")["wrapped_hooks"], 4)
        self.assertEqual(json.loads((updated / "hooks/hooks.json").read_text()), self.plugin_config)
        report = self.run_installer("--reinstrument", "--apply")
        self.assertEqual(report["wrapped_hooks"], 4)
        self.assertIsInstance(report["backup_directory"], str)
        self.assertEqual(report["commands"], [], "reinstrumentation activates nothing")
        for path, content in untouched.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(snapshot(self.market), staged)
        current = self.manifest()["wrappers"]
        new = [r for r in current if r["file"] == str(updated / "hooks/hooks.json")]
        self.assertEqual(len(new), 4)
        self.assertTrue(installed <= {r["id"] for r in current}, "already-loaded sessions lost their records")
        stored = json.loads((updated / "hooks/hooks.json").read_text())
        for record in new:
            hooks = [hook for group in stored["hooks"][record["event"]]
                     if group.get("matcher") == record["matcher"] for hook in group["hooks"]]
            self.assertIn(record["wrapped"], hooks)
        self.assertEqual(stored["hooks"]["Notification"], self.plugin_config["hooks"]["Notification"])
        self.assertEqual(self.run_installer("--reinstrument", "--apply")["changed_files"], 0)

    def test_reinstrument_leaves_plugins_the_installer_never_instrumented(self):
        self.run_installer("--apply")
        later = self.install_cached_plugin("1.0.0", enable=True)
        before = (later / "hooks/hooks.json").read_bytes()
        report = self.run_installer("--reinstrument", "--apply")
        self.assertEqual(report["changed_files"], 0)
        self.assertIsNone(report["backup_directory"])
        self.assertEqual((later / "hooks/hooks.json").read_bytes(), before)

    def test_reinstrument_backup_restores_the_updated_plugin_and_manifest(self):
        self.install_cached_plugin("1.0.0", enable=True)
        self.run_installer("--apply")
        manifest = (self.target / "install-manifest.json").read_bytes()
        updated = self.install_cached_plugin("1.1.0")
        report = self.run_installer("--reinstrument", "--apply")
        self.run_installer("--restore", report["backup_directory"], "--apply")
        self.assertEqual(json.loads((updated / "hooks/hooks.json").read_text()), self.plugin_config)
        self.assertEqual((self.target / "install-manifest.json").read_bytes(), manifest)

    def reinstrument_in_process(self, python):
        """Reinstrumentation as run by an observer whose `python3` resolves elsewhere."""
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        import install
        with mock.patch.object(sys, "executable", str(python)):
            installation = install.Installation(self.home, self.source)
            installation.plan_reinstrument()
            installation.apply()
        return installation

    def test_reinstrument_keeps_wrappers_whatever_interpreter_runs_the_observer(self):
        self.install_cached_plugin("1.0.0", enable=True)
        self.run_installer("--apply")
        updated = self.install_cached_plugin("1.1.0") / "hooks/hooks.json"
        projects = [self.root / name / "bin/python3" for name in ("project-a", "project-b")]
        for path in projects:
            path.parent.mkdir(parents=True)
            path.write_text("")
        self.assertEqual(self.reinstrument_in_process(projects[0]).wrapped, 4)
        wrapped = updated.read_bytes()
        stored = json.loads(wrapped)["hooks"]["Stop"][0]["hooks"][0]["command"]
        # The installation's interpreter, never one project's environment baked into a shared plugin.
        self.assertEqual(shlex.split(stored)[0], str(Path(sys.executable).resolve()))
        manifest = (self.target / "install-manifest.json").read_bytes()
        self.assertEqual(self.reinstrument_in_process(projects[1]).changes, [])
        self.assertEqual(updated.read_bytes(), wrapped)
        self.assertEqual((self.target / "install-manifest.json").read_bytes(), manifest)

    def test_reinstrument_records_the_wrappers_of_an_interrupted_run(self):
        self.install_cached_plugin("1.0.0", enable=True)
        self.run_installer("--apply")
        updated = self.install_cached_plugin("1.1.0") / "hooks/hooks.json"
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        import install
        installation = install.Installation(self.home, self.source)
        installation.plan_reinstrument()
        # Interrupted after the plugin file and before the manifest.
        updated.write_bytes(next(change["data"] for change in installation.changes if change["path"] == updated))
        report = self.run_installer("--reinstrument", "--apply")
        self.assertEqual((report["wrapped_hooks"], report["changed_files"]), (0, 1))
        self.assertEqual(len([r for r in self.manifest()["wrappers"] if r["file"] == str(updated)]), 4)

    def test_restoring_the_installation_names_the_later_reinstrumentation_backup(self):
        self.install_cached_plugin("1.0.0", enable=True)
        installed = self.run_installer("--apply")["backup_directory"]
        updated = self.install_cached_plugin("1.1.0") / "hooks/hooks.json"
        later = self.run_installer("--reinstrument", "--apply")["backup_directory"]
        refused = self.run_installer("--restore", installed, "--apply", success=False)
        self.assertIn("restore the later backup " + later + " first", refused["error"])
        self.run_installer("--restore", later, "--apply")
        self.run_installer("--restore", installed, "--apply")
        self.assertEqual(json.loads(updated.read_text()), self.plugin_config)
        self.assertFalse((self.target / "install-manifest.json").exists())

    def test_reinstrument_without_an_installation_changes_nothing(self):
        self.install_cached_plugin("1.0.0", enable=True)
        before = snapshot(self.root)
        report = self.run_installer("--reinstrument", "--apply")
        self.assertEqual(report["changed_files"], 0)
        self.assertEqual(snapshot(self.root), before)

    def test_identical_hook_registrations_remain_identical_for_engine_deduplication(self):
        self.settings["hooks"]["Stop"][0]["hooks"].append(self.async_shell)
        self.plugin_config["hooks"]["Stop"][0]["hooks"].append(self.async_shell)
        write_json(self.settings_path, self.settings)
        write_json(self.plugin_path, self.plugin_config)
        self.run_installer("--apply")
        records = [record for record in self.manifest()["wrappers"]
                   if record["event"] == "Stop" and record["original"] == self.async_shell]
        self.assertEqual(len(records), 3)
        user_records = [r for r in records if r["file"] == str(self.settings_path)]
        plugin_record = next(r for r in records if r["file"] == str(self.plugin_path))
        self.assertEqual(user_records[0]["wrapped"], user_records[1]["wrapped"],
                         "duplicates within one native scope must keep the same invocation")
        self.assertNotEqual(user_records[0]["id"], plugin_record["id"])

    def test_native_duplicates_ignore_timeout_async_and_status_but_keep_last_properties(self):
        first = {**self.async_shell, "timeout": 4, "async": True, "statusMessage": "First status",
                 "rewakeMessage": "First result", "rewakeSummary": "First summary"}
        last = {**self.async_shell, "timeout": 91, "async": False, "statusMessage": "Last status",
                "rewakeMessage": "Last result", "rewakeSummary": "Last summary"}
        self.settings["hooks"]["Stop"] = [{"hooks": [first, last]}]
        write_json(self.settings_path, self.settings)
        self.run_installer("--apply")
        hooks = json.loads(self.settings_path.read_text())["hooks"]["Stop"][0]["hooks"]
        self.assertEqual(hooks[0]["command"], hooks[1]["command"])
        self.assertEqual(hooks[0].get("args"), hooks[1].get("args"))
        records = [r for r in self.manifest()["wrappers"]
                   if r["file"] == str(self.settings_path) and r["event"] == "Stop"]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r["id"] for r in records}), 1)
        for original, wrapped in zip((first, last), hooks):
            self.assertEqual({k: v for k, v in wrapped.items() if k not in ("command", "args")},
                             {k: v for k, v in original.items() if k not in ("command", "args")})
        before = snapshot(self.root)
        self.assertEqual(self.run_installer("--apply")["changed_files"], 0)
        self.assertEqual(snapshot(self.root), before)

    def test_same_absolute_command_keeps_distinct_user_and_plugin_scopes(self):
        hook = {"type": "command", "command": "/bin/echo absolute-review", "asyncRewake": True}
        self.settings["enabledPlugins"]["disabled@local"] = True
        self.settings["hooks"]["Stop"] = [{"hooks": [hook]}]
        write_json(self.settings_path, self.settings)
        for path in (self.plugin_path, self.disabled_path):
            write_json(path, {"hooks": {"Stop": [{"hooks": [hook]}]}})
        self.run_installer("--apply")
        records = [r for r in self.manifest()["wrappers"] if r["original"] == hook]
        self.assertEqual(len(records), 3)
        self.assertEqual(len({r["id"] for r in records}), 3)

    def test_native_identity_keeps_absent_args_distinct_from_empty_and_defaults_shell(self):
        base = {"type": "command", "command": "/bin/echo", "asyncRewake": True}
        originals = [base, {**base, "shell": "bash", "if": ""},
                     {**base, "if": None}, {**base, "args": []}, {**base, "if": "true"}]
        self.settings["hooks"]["Stop"] = [{"hooks": originals}]
        write_json(self.settings_path, self.settings)
        self.run_installer("--apply")
        records = [r for r in self.manifest()["wrappers"]
                   if r["file"] == str(self.settings_path) and r["event"] == "Stop"]
        ids = [next(r["id"] for r in records if r["original"] == hook) for hook in originals]
        self.assertEqual(ids[:3], [ids[0]] * 3)
        self.assertEqual(len(set(ids)), 3)
        hooks = json.loads(self.settings_path.read_text())["hooks"]["Stop"][0]["hooks"]
        self.assertTrue(all(hook["asyncRewake"] is True for hook in hooks))
        self.assertEqual([hook.get("if") for hook in hooks], [None, "", None, None, "true"])

    def test_seven_condition_variants_remain_seven_background_reviews(self):
        originals = [{**self.plugin_hook, "async": True, "if": "condition-" + str(i)} for i in range(7)]
        self.plugin_config["hooks"]["PostToolUse"] = [{"matcher": "Edit|Write", "hooks": originals}]
        write_json(self.plugin_path, self.plugin_config)
        self.run_installer("--apply")
        records = [r for r in self.manifest()["wrappers"]
                   if r["file"] == str(self.plugin_path) and r["event"] == "PostToolUse"]
        self.assertEqual(len(records), 7)
        self.assertEqual(len({r["id"] for r in records}), 7)
        hooks = json.loads(self.plugin_path.read_text())["hooks"]["PostToolUse"][0]["hooks"]
        for original, wrapped in zip(originals, hooks):
            self.assertEqual({k: v for k, v in wrapped.items() if k not in ("command", "args")},
                             {k: v for k, v in original.items() if k not in ("command", "args")})

    def test_conflicting_async_rewake_flags_fail_before_migration_in_either_order(self):
        for explicit_false in (True, False):
            disabled = {**self.async_shell, "timeout": 99}
            if explicit_false:
                disabled["asyncRewake"] = False
            else:
                disabled.pop("asyncRewake")
            for hooks in ([self.async_shell, disabled], [disabled, self.async_shell]):
                with self.subTest(explicit_false=explicit_false, rewake_first=hooks[0] == self.async_shell):
                    self.settings["hooks"]["Stop"] = [{"hooks": hooks}]
                    write_json(self.settings_path, self.settings)
                    before = snapshot(self.root)
                    self.run_installer("--apply", success=False)
                    after = snapshot(self.root)
                    for path, value in before.items():
                        if value[0] is not None:
                            self.assertEqual(after.get(path), value)
                    self.assertFalse(self.market.exists())
                    self.assertFalse((self.target / "install-manifest.json").exists())

    def test_conflicting_plugin_identity_is_detected_across_default_and_custom_files(self):
        write_json(self.plugin / ".claude-plugin/plugin.json", {"name": "reviewer", "hooks": "./custom.json"})
        write_json(self.plugin / "custom.json", {"hooks": {"Stop": [{"hooks": [
            {**self.plugin_hook, "asyncRewake": False, "timeout": 2}]}]}})
        before = snapshot(self.root)
        self.run_installer("--apply", success=False)
        after = snapshot(self.root)
        for path, value in before.items():
            if value[0] is not None:
                self.assertEqual(after.get(path), value)
        self.assertFalse(self.market.exists())

    def test_plugin_relative_commands_keep_distinct_ids_for_distinct_plugin_roots(self):
        self.settings["enabledPlugins"]["disabled@local"] = True
        write_json(self.settings_path, self.settings)
        self.run_installer("--apply")
        records = [record for record in self.manifest()["wrappers"]
                   if record["event"] == "Stop" and record["original"] == self.plugin_hook]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)

    def test_custom_hook_paths_support_strings_lists_and_default_path_deduplication(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        first = self.plugin / "extra/review.json"
        second = self.plugin / "extra/more.json"
        hook = {**self.async_shell, "command": "printf custom-review"}
        write_json(first, {"Stop": [{"hooks": [hook]}]})
        write_json(manifest_path, {"name": "reviewer", "hooks": "./extra/review.json", "secret": PRIVATE})
        report = self.run_installer("--apply")
        self.assertEqual(report["wrapped_hooks"], 9)
        records = [r for r in self.manifest()["wrappers"] if r["file"] == str(first)]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["original"], hook)
        self.assertEqual(json.loads(first.read_text())["Stop"][0]["hooks"], [records[0]["wrapped"]])
        write_json(second, {"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [hook]}]}})
        write_json(manifest_path, {"name": "reviewer", "hooks": [
            "./hooks/hooks.json", "./extra/review.json", "./extra/../extra/review.json", "./extra/more.json"],
            "secret": PRIVATE})
        report = self.run_installer("--apply")
        self.assertEqual((report["wrapped_hooks"], report["tracked_hooks"]), (1, 10))
        before = snapshot(self.root)
        self.assertEqual(self.run_installer("--apply")["changed_files"], 0)
        self.assertEqual(snapshot(self.root), before)

    def test_inline_plugin_hooks_preserve_manifest_fields_and_private_backups(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        custom_path = self.plugin / "extra-hooks.json"
        hook = {**self.async_shell, "command": "printf inline-review"}
        write_json(custom_path, {"hooks": {"SubagentStop": [{"hooks": [hook]}]}})
        metadata = {"name": "reviewer", "env": {"TOKEN": PRIVATE}, "hooks": [
            {"Stop": [{"hooks": [hook]}]}, "./extra-hooks.json",
            {"description": "Inline definition", "env": {"TOKEN": PRIVATE},
             "hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}]}
        write_json(manifest_path, metadata)
        report = self.run_installer("--apply")
        self.assertEqual((report["wrapped_hooks"], report["tracked_hooks"]), (11, 11))
        current = json.loads(manifest_path.read_text())
        self.assertEqual(current["env"], metadata["env"])
        self.assertEqual(current["hooks"][1], "./extra-hooks.json")
        self.assertEqual(current["hooks"][2]["env"], metadata["hooks"][2]["env"])
        self.assertEqual(current["hooks"][2]["description"], "Inline definition")
        records = [r for r in self.manifest()["wrappers"] if r["file"] == str(manifest_path)]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["original"], hook)
            groups = (current["hooks"][0]["Stop"] if record["event"] == "Stop"
                      else current["hooks"][2]["hooks"]["UserPromptSubmit"])
            self.assertEqual(groups[0]["hooks"], [record["wrapped"]])
        for backup in (self.target / "backups").rglob("*"):
            if backup.is_file():
                self.assertNotIn(PRIVATE.encode(), backup.read_bytes())
        before = snapshot(self.root)
        self.assertEqual(self.run_installer("--apply")["changed_files"], 0)
        self.assertEqual(snapshot(self.root), before)

    def test_single_inline_hook_definition_supports_bare_and_wrapped_event_maps(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        hooks = {"Stop": [{"hooks": [{**self.async_exec, "args": ["%s", "inline"]}]}]}
        for definition in (hooks, {"hooks": hooks}):
            with self.subTest(wrapped="hooks" in definition):
                write_json(manifest_path, {"name": "reviewer", "hooks": definition})
                self.run_installer("--apply")
                records = [r for r in self.manifest()["wrappers"] if r["file"] == str(manifest_path)]
                self.assertEqual(len(records), 1)
                current = json.loads(manifest_path.read_text())["hooks"]
                self.assertEqual(current.get("hooks", current)["Stop"][0]["hooks"], [records[0]["wrapped"]])

    def test_custom_hook_references_cannot_escape_to_project_or_managed_files(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        linked = self.plugin / "linked-hooks.json"
        linked.symlink_to(self.project_path)
        for reference in (os.path.relpath(self.project_path, self.plugin), str(self.managed_path), "./linked-hooks.json"):
            with self.subTest(reference=reference):
                write_json(manifest_path, {"name": "reviewer", "hooks": reference})
                before = snapshot(self.root)
                self.run_installer("--apply", success=False)
                after = snapshot(self.root)
                for path, value in before.items():
                    if value[0] is not None:
                        self.assertEqual(after.get(path), value)
                self.assertFalse(self.market.exists())

    def test_custom_declarations_on_disabled_project_and_managed_plugins_are_untouched(self):
        untouched = {}
        for default in (self.disabled_path, self.project_path, self.managed_path):
            root = default.parent.parent
            paths = {root / ".claude-plugin/plugin.json": {"name": "other", "hooks": [
                "./custom.json", {"Stop": [{"hooks": [self.plugin_hook]}]}]},
                root / "custom.json": {"hooks": {"PostToolUse": [{"hooks": [self.plugin_hook]}]}}}
            for path, data in paths.items():
                write_json(path, data)
                untouched[path] = path.read_bytes()
        self.run_installer("--apply")
        for path, content in untouched.items():
            self.assertEqual(path.read_bytes(), content)

    def test_unsupported_custom_declaration_shapes_fail_before_migration(self):
        manifest_path = self.plugin / ".claude-plugin/plugin.json"
        for declaration in (None, 17, [["./hooks/hooks.json"]], {"hooks": "./hooks/hooks.json"}):
            with self.subTest(declaration=declaration):
                write_json(manifest_path, {"name": "reviewer", "hooks": declaration})
                before = snapshot(self.root)
                self.run_installer("--apply", success=False)
                after = snapshot(self.root)
                for path, value in before.items():
                    if value[0] is not None:
                        self.assertEqual(after.get(path), value)
                self.assertFalse(self.market.exists())

    def test_invalid_settings_fail_before_any_migration(self):
        self.settings_path.write_text('{"env":{"secret":"' + PRIVATE + '"}, "hooks": "invalid"}')
        before = snapshot(self.root)
        self.run_installer("--apply", success=False)
        # The installation lock is permitted; no settings, shim, or artifact changes are.
        after = snapshot(self.root)
        for path, value in before.items():
            if value[0] is not None:
                self.assertEqual(after.get(path), value)
        self.assertFalse((self.target / "install-manifest.json").exists())
        self.assertFalse(self.market.exists())


if __name__ == "__main__":
    unittest.main()
