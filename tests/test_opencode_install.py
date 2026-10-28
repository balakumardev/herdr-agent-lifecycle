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
        self.cli = {"theme": "existing-theme", "plugins": ["other-tui"]}
        self.server_path.write_text(json.dumps(self.server))
        self.cli_path.write_text(json.dumps(self.cli))

    def ready_plugin(self, name):
        path = self.root / name
        path.mkdir()
        (path / "package.json").write_text(json.dumps({"name": "opencode-herdr-lifecycle", "version": "1.0.0",
            "exports": {".": "./index.js", "./tui": "./tui.js"}}))
        for entry in ("index.js", "tui.js"):
            (path / entry).write_text("export default {};\n")
        client = path / "node_modules/@opencode/client"
        client.mkdir(parents=True)
        (client / "package.json").write_text('{"version":"2.0.18"}')
        return path

    def run_installer(self, *flags, ok=True, restore=None):
        args = (["--restore", str(restore)] if restore else
                ["--config-dir", str(self.config), "--plugin-dir", str(self.plugin)])
        result = subprocess.run([sys.executable, str(SCRIPT), *args, *flags], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stdout + result.stderr)
        self.assertNotIn("private-fixture-model", result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_registration_excludes_both_native_reporters_and_preserves_other_options(self):
        before = self.server_path.read_bytes()
        self.run_installer()
        self.assertEqual(self.server_path.read_bytes(), before)
        result = self.run_installer("--apply")
        server, cli = json.loads(self.server_path.read_text()), json.loads(self.cli_path.read_text())
        self.assertEqual(server["model"], self.server["model"])
        self.assertIn(self.server["plugins"][0], server["plugins"])
        self.assertIn({"package": str(self.plugin), "options": {"refreshIntervalMs": 5000}}, server["plugins"])
        self.assertIn("-herdr.opencode.agent-state", server["plugins"])
        self.assertIn("-herdr.opencode.session-selection", cli["plugins"])
        self.assertIn(str(self.plugin), cli["plugins"])
        self.assertEqual(cli["theme"], "existing-theme")
        self.assertTrue(Path(result["backupDirectory"]).is_dir())

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
        plugins = json.loads(self.server_path.read_text())["plugins"]
        self.assertIn({"package": str(self.plugin), "options": {"refreshIntervalMs": 5000}}, plugins)
        self.assertFalse(any((p.get("package") if isinstance(p, dict) else p) == str(old) for p in plugins))

    def test_missing_dependencies_cannot_expose_an_unloadable_plugin(self):
        (self.plugin / "node_modules/@opencode/client/package.json").unlink()
        before = self.server_path.read_bytes()
        self.run_installer("--apply", ok=False)
        self.assertEqual(self.server_path.read_bytes(), before)
        self.assertFalse((self.config / "task-lifecycle-backups").exists())

    def test_unknown_bridge_fails_before_configuration_changes(self):
        bridge = self.config / "plugins/claude-code-hooks.js"
        bridge.parent.mkdir()
        bridge.write_text("// Unfamiliar user work\nexport default {};\n")
        before = {p: p.read_bytes() for p in (self.server_path, self.cli_path, bridge)}
        self.run_installer("--apply", ok=False)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_known_bridge_is_patched_once_and_other_content_survives(self):
        bridge = self.config / "plugins/claude-code-hooks.js"
        bridge.parent.mkdir()
        bridge.write_text((ROOT / "tests/fixtures/legacy-bridge.js").read_text() + "\n// preserved user addition\n")
        self.run_installer("--apply")
        first = bridge.read_bytes()
        second = self.run_installer("--apply")
        self.assertEqual(bridge.read_bytes(), first)
        self.assertEqual(second["changedFiles"], [])
        self.assertTrue(first.endswith(b"// preserved user addition\n"))
        entry = next(p for p in json.loads(self.server_path.read_text())["plugins"] if isinstance(p, dict) and p["package"].endswith("claude-code-hooks.js"))
        self.assertIs(entry["options"]["notifications"], False)

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
