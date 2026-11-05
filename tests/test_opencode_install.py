import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install-opencode.py"


class OpenCodeInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.plugin = self.ready_plugin("release-one")
        self.server_path = self.config / "opencode.json"
        self.cli_path = self.config / "cli.json"
        self.server = {"model": "private-fixture-model", "plugins": [
            {"package": "other-plugin", "options": {"enabled": True}},
            {"package": "./integrations/task-lifecycle", "options": {"refreshIntervalMs": 5000}},
        ]}
        self.cli = {"theme": "existing-theme", "plugins": [
            "other-tui", {"package": "./integrations/task-lifecycle", "options": {"refreshIntervalMs": 5000}}]}
        self.server_path.write_text(json.dumps(self.server))
        self.cli_path.write_text(json.dumps(self.cli))

    def ready_plugin(self, name):
        path = self.root / name
        path.mkdir()
        (path / "package.json").write_text(json.dumps({"name": "opencode-herdr-lifecycle", "version": "1.0.0",
            "exports": {"./tui": "./tui.js"}}))
        for entry in ("tui.js", "herdr.mjs"):
            (path / entry).write_text("export default {};\n")
        return path

    def run_installer(self, *flags, ok=True, restore=None):
        args = (["--restore", str(restore)] if restore else
                ["--config-dir", str(self.config), "--plugin-dir", str(self.plugin)])
        result = subprocess.run([sys.executable, str(SCRIPT), *args, *flags], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        self.assertNotIn("private-fixture-model", result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_registers_the_tui_plugin_only_and_excludes_both_native_reporters(self):
        before = self.server_path.read_bytes()
        self.run_installer()
        self.assertEqual(self.server_path.read_bytes(), before)
        result = self.run_installer("--apply")
        server, cli = json.loads(self.server_path.read_text()), json.loads(self.cli_path.read_text())
        self.assertEqual(server["model"], self.server["model"])
        self.assertIn(self.server["plugins"][0], server["plugins"])
        self.assertEqual(server["plugins"][-1], "-herdr.opencode.agent-state")
        self.assertFalse(any(str(self.plugin) in json.dumps(p) or "task-lifecycle" in json.dumps(p) for p in server["plugins"]),
                         "the TUI-only plugin must not stay registered with the server")
        self.assertEqual(cli["plugins"][-1], "-herdr.opencode.session-selection")
        self.assertIn("other-tui", cli["plugins"])
        self.assertIn({"package": str(self.plugin), "options": {"refreshIntervalMs": 5000}}, cli["plugins"])
        self.assertEqual(cli["theme"], "existing-theme")
        self.assertTrue(Path(result["backupDirectory"]).is_dir())

    def test_a_fresh_configuration_gets_only_the_two_directives_and_the_plugin(self):
        self.server_path.unlink()
        self.cli_path.unlink()
        self.run_installer("--apply")
        self.assertEqual(json.loads(self.server_path.read_text())["plugins"], ["-herdr.opencode.agent-state"])
        self.assertEqual(json.loads(self.cli_path.read_text())["plugins"], [str(self.plugin), "-herdr.opencode.session-selection"])

    def test_a_stale_server_registration_by_package_name_is_removed(self):
        stale = self.ready_plugin("old-release")
        self.server["plugins"].append({"package": str(stale), "options": {"x": 1}})
        self.server_path.write_text(json.dumps(self.server))
        self.run_installer("--apply")
        server = json.loads(self.server_path.read_text())
        self.assertFalse(any(str(stale) in json.dumps(p) for p in server["plugins"]))
        self.assertIn(self.server["plugins"][0], server["plugins"])

    def test_reinstallation_is_byte_identical_and_creates_no_new_backup(self):
        self.run_installer("--apply")
        before = {p: p.read_bytes() for p in self.config.rglob("*") if p.is_file()}
        result = self.run_installer("--apply")
        self.assertEqual(result["changedFiles"], [])
        self.assertEqual({p: p.read_bytes() for p in self.config.rglob("*") if p.is_file()}, before)

    def test_upgrade_replaces_owned_release_registration_and_retains_options(self):
        self.run_installer("--apply")
        old = self.plugin
        self.plugin = self.ready_plugin("release-two")
        self.run_installer("--apply")
        plugins = json.loads(self.cli_path.read_text())["plugins"]
        self.assertIn({"package": str(self.plugin), "options": {"refreshIntervalMs": 5000}}, plugins)
        self.assertFalse(any((p.get("package") if isinstance(p, dict) else p) == str(old) for p in plugins))

    def test_upgrade_replaces_an_escaped_file_url_and_preserves_its_options(self):
        old = self.ready_plugin("release with spaces")
        self.cli["plugins"][1]["package"] = old.as_uri()
        self.cli_path.write_text(json.dumps(self.cli))
        self.run_installer("--apply")
        plugins = json.loads(self.cli_path.read_text())["plugins"]
        self.assertIn({"package": str(self.plugin), "options": {"refreshIntervalMs": 5000}}, plugins)
        self.assertFalse(any((p.get("package") if isinstance(p, dict) else p) == old.as_uri() for p in plugins))

    def test_reporter_exclusion_wins_over_a_later_explicit_reenable(self):
        for path, reporter in ((self.server_path, "herdr.opencode.agent-state"),
                               (self.cli_path, "herdr.opencode.session-selection")):
            config = json.loads(path.read_text())
            config["plugins"].extend(["-" + reporter, reporter])
            path.write_text(json.dumps(config))
        self.run_installer("--apply")
        for path, reporter in ((self.server_path, "herdr.opencode.agent-state"),
                               (self.cli_path, "herdr.opencode.session-selection")):
            directives = [p for p in json.loads(path.read_text())["plugins"]
                          if isinstance(p, str) and p in (reporter, "-" + reporter)]
            self.assertEqual(directives[-1], "-" + reporter)
        self.assertEqual(self.run_installer("--apply")["changedFiles"], [])

    def test_an_incomplete_plugin_directory_cannot_be_registered(self):
        (self.plugin / "herdr.mjs").unlink()
        before = self.server_path.read_bytes()
        self.run_installer("--apply", ok=False)
        self.assertEqual(self.server_path.read_bytes(), before)
        self.assertFalse((self.config / "task-lifecycle-backups").exists())

    def test_unrelated_plugin_files_are_never_touched(self):
        bridge = self.config / "plugins/claude-code-hooks.js"
        bridge.parent.mkdir()
        bridge.write_text("// Unfamiliar user work\nexport default {};\n")
        before = bridge.read_bytes()
        self.run_installer("--apply")
        self.assertEqual(bridge.read_bytes(), before)

    def test_restore_recovers_original_bytes_and_modes(self):
        self.server_path.chmod(0o640)
        before = {p: p.read_bytes() for p in (self.server_path, self.cli_path)}
        result = self.run_installer("--apply")
        self.run_installer("--apply", restore=result["backupDirectory"])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(self.server_path.stat().st_mode & 0o777, 0o640)

    def test_restore_refuses_to_overwrite_a_later_user_edit(self):
        result = self.run_installer("--apply")
        self.cli_path.write_text('{"theme":"my-new-theme"}')
        before = {p: p.read_bytes() for p in (self.server_path, self.cli_path)}
        self.run_installer("--apply", restore=result["backupDirectory"], ok=False)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_restore_refuses_to_undo_a_later_permission_tightening(self):
        self.server_path.chmod(0o644)
        result = self.run_installer("--apply")
        self.server_path.chmod(0o600)
        before = {p: (p.read_bytes(), p.stat().st_mode) for p in (self.server_path, self.cli_path)}
        self.run_installer("--apply", restore=result["backupDirectory"], ok=False)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mode) for p in before}, before)

    def test_symlink_configuration_is_not_replaced(self):
        real = self.root / "real-config.json"
        self.server_path.rename(real)
        self.server_path.symlink_to(real)
        self.run_installer("--apply", ok=False)
        self.assertTrue(self.server_path.is_symlink())
        self.assertEqual(json.loads(real.read_text()), self.server)

    def test_jsonc_refusal_preserves_the_original_document(self):
        path = self.config / "opencode.jsonc"
        path.write_text('{\n// user comment\n"plugins": []\n}')
        before = path.read_bytes()
        self.run_installer("--apply", ok=False)
        self.assertEqual(path.read_bytes(), before)
