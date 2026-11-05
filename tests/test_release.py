"""Exercise version preparation and the actual source/npm release archives."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prepare_release", ROOT / "scripts/prepare-release.py")
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-release-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "source"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(
            ".git", "node_modules", ".superpowers", ".codebase-memory", "__pycache__", "dist", "*.pyc"))
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        (self.root / ".env").write_text("PRIVATE-SENTINEL")
        (self.root / "opencode/node_modules/private").mkdir(parents=True)
        (self.root / "opencode/node_modules/private/secret.json").write_text("PRIVATE-SENTINEL")

    def test_versions_lockfiles_and_both_archives_agree_and_import_independently(self):
        RELEASE.prepare(self.root, "2.3.4")
        for file in ("package.json", "package-lock.json", "opencode/package.json", "opencode/package-lock.json",
                     "claude/.claude-plugin/plugin.json"):
            data = json.loads((self.root / file).read_text())
            self.assertEqual(data["version"], "2.3.4", file)
            if file.endswith("package-lock.json"):
                self.assertEqual(data["packages"][""]["version"], "2.3.4", file)
        marketplace = json.loads((self.root / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(marketplace["plugins"][0]["version"], "2.3.4")
        dist = self.root / "dist"
        expected = {"herdr-agent-lifecycle-2.3.4.tar.gz", "opencode-herdr-lifecycle-2.3.4.tgz"}
        hashes = {}
        for line in (dist / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            hashes[name] = digest
            self.assertEqual(hashlib.sha256((dist / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(set(hashes), expected)
        unpack = self.root.parent / "unpacked"
        with tarfile.open(dist / "herdr-agent-lifecycle-2.3.4.tar.gz") as archive:
            names = archive.getnames()
            self.assertTrue(all(name.startswith("herdr-agent-lifecycle-2.3.4/") for name in names))
            self.assertFalse(any("node_modules" in name or name.endswith("/.env") for name in names))
            self.assertIn("herdr-agent-lifecycle-2.3.4/claude/background_work.py", names)
            self.assertIn("herdr-agent-lifecycle-2.3.4/claude/hooks/hooks.json", names)
            archive.extractall(unpack)
        extracted = unpack / "herdr-agent-lifecycle-2.3.4"
        # Outside a Herdr pane the extracted hook must exit 0 and print nothing.
        outside = subprocess.run([sys.executable, "-B", "claude/background_work.py"], cwd=extracted, check=True,
                                 input='{"hook_event_name":"Stop"}', text=True, capture_output=True,
                                 env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((outside.stdout, outside.stderr), ("", ""))
        with tarfile.open(dist / "opencode-herdr-lifecycle-2.3.4.tgz") as archive:
            names = set(archive.getnames())
            self.assertEqual({"package/tui.js", "package/herdr.mjs", "package/LICENSE", "package/README.md",
                              "package/package.json"}, names)
            self.assertFalse(any("node_modules" in name or ".test." in name for name in names))
            package = json.loads(archive.extractfile("package/package.json").read())
            self.assertEqual(package["version"], "2.3.4")
            archive.extractall(unpack / "npm")
        subprocess.run(["node", "--input-type=module", "-e",
            "import tui from './tui.js'; import {createHerdr} from './herdr.mjs'; "
            "if(tui.id !== 'local.task-lifecycle.tui' || createHerdr({}) !== undefined) process.exit(1)"],
            cwd=unpack / "npm/package", check=True)

    def test_invalid_version_fails_before_any_manifest_changes(self):
        before = (self.root / "package.json").read_bytes()
        for version in ("../escape", "1.2", "01.2.3", "1.2.3;touch bad", "v1.2.3"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                RELEASE.prepare(self.root, version)
        self.assertEqual((self.root / "package.json").read_bytes(), before)
        self.assertFalse((self.root / "dist").exists())

    def test_repreparing_the_same_version_keeps_checksums_stable(self):
        RELEASE.prepare(self.root, "2.3.4")
        before = {p.name: p.read_bytes() for p in (self.root / "dist").iterdir()}
        RELEASE.prepare(self.root, "2.3.4")
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "dist").iterdir()}, before)
