"""Recover an interrupted tagged release against a stateful GitHub boundary."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseStore:
    def __init__(self):
        self.record = None
        self.assets = {}
        self.writes = 0
        self.interrupt_upload = False

    def release(self, tag):
        return {**self.record, "assets": [{"name": name} for name in self.assets]} if self.record else None

    def create_draft(self, tag, notes):
        if self.record:
            raise RuntimeError("duplicate release")
        self.record = {"tag_name": tag, "draft": True, "body": notes}
        self.writes += 1

    def upload(self, tag, files):
        for path in files:
            self.assets[path.name] = path.read_bytes()
            self.writes += 1
            if self.interrupt_upload:
                self.interrupt_upload = False
                raise RuntimeError("interrupted asset upload")

    def download(self, tag, directory, names):
        for name in names:
            (directory / name).write_bytes(self.assets[name])

    def publish(self, tag):
        self.record["draft"] = False
        self.writes += 1


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lifecycle-recover-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "checkout"
        self.root.mkdir()
        for name in ("package.json", "package-lock.json", "README.md", "LICENSE",
                     "opencode/package.json", "opencode/package-lock.json", "opencode/index.js", "opencode/LICENSE",
                     "claude/.claude-plugin/plugin.json", "claude/runtime.py", ".claude-plugin/marketplace.json",
                     "scripts/prepare-release.py"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (self.root / "CHANGELOG.md").write_text("# 1.0.0\n\nFirst public release.\n")
        for args in (("init", "-q"), ("add", ".")):
            self.git(*args)
        subprocess.run(["python3", "scripts/prepare-release.py", "1.0.0"], cwd=self.root,
                       capture_output=True, text=True, check=True)
        self.git("add", "-u")
        self.git("add", "CHANGELOG.md")
        self.git("-c", "user.name=Release Fixture", "-c", "user.email=release@example.test",
                 "commit", "-qm", "chore(release): 1.0.0 [skip ci]")
        self.git("tag", "v1.0.0")
        self.expected = {p.name: p.read_bytes() for p in (self.root / "dist").iterdir()}
        self.store = ReleaseStore()
        spec = importlib.util.spec_from_file_location("recover_release", ROOT / "scripts/recover-release.py")
        self.recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.recovery)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()

    def test_retry_finishes_a_partial_draft_at_the_existing_tag_without_a_new_version(self):
        self.store.interrupt_upload = True
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.recovery.recover(self.root, "HEAD", self.store)
        self.assertTrue(self.store.record["draft"])
        self.assertEqual(len(self.store.assets), 1)
        self.assertEqual(self.recovery.recover(self.root, "HEAD", self.store), "v1.0.0")
        self.assertFalse(self.store.record["draft"])
        self.assertEqual(self.store.assets, self.expected)
        for line in self.store.assets["SHA256SUMS"].decode().splitlines():
            checksum, name = line.split("  ")
            self.assertEqual(hashlib.sha256(self.store.assets[name]).hexdigest(), checksum)
        before = self.store.writes
        self.recovery.recover(self.root, "HEAD", self.store)
        self.assertEqual(self.store.writes, before, "retry rewrote a completed release")
        self.assertEqual(self.git("tag"), "v1.0.0")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_a_tag_without_any_github_release_is_published_from_tagged_not_working_tree_files(self):
        (self.root / "README.md").write_text("UNRELEASED CONTENT")
        self.recovery.recover(self.root, "HEAD", self.store)
        self.assertEqual(self.store.assets, self.expected)
        self.assertEqual((self.root / "README.md").read_text(), "UNRELEASED CONTENT")

    def test_corrupted_published_assets_fail_verification_without_being_overwritten(self):
        self.recovery.recover(self.root, "HEAD", self.store)
        name = "opencode-herdr-lifecycle-1.0.0.tgz"
        self.store.assets[name] = b"corrupted-public-asset"
        before = self.store.writes
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.recovery.recover(self.root, "HEAD", self.store)
        self.assertEqual(self.store.writes, before)

    def test_untagged_history_does_not_create_a_release(self):
        self.git("tag", "-d", "v1.0.0")
        self.assertIsNone(self.recovery.recover(self.root, "HEAD", self.store))
        self.assertEqual(self.store.writes, 0)
