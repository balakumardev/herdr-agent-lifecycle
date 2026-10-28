#!/usr/bin/env python3
"""Finish interrupted GitHub publication at an existing, verified release tag."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def run(command, cwd=None):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True, timeout=120).stdout.strip()


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.repository = repository

    def release(self, tag):
        result = subprocess.run(["gh", "api", "repos/" + self.repository + "/releases/tags/" + tag],
                                capture_output=True, text=True, timeout=60)
        if result.returncode and "HTTP 404" in result.stderr:
            return None
        result.check_returncode()
        return json.loads(result.stdout)

    def create_draft(self, tag, notes):
        with tempfile.TemporaryDirectory(prefix="lifecycle-notes-") as directory:
            path = Path(directory) / "notes.txt"
            path.write_text(notes)
            run(["gh", "release", "create", tag, "--repo", self.repository, "--verify-tag",
                 "--draft", "--title", tag, "--notes-file", str(path)])

    def upload(self, tag, files):
        run(["gh", "release", "upload", tag, "--repo", self.repository, "--clobber", *map(str, files)])

    def download(self, tag, directory, names):
        patterns = [arg for name in names for arg in ("--pattern", name)]
        run(["gh", "release", "download", tag, "--repo", self.repository, "--dir", str(directory), *patterns])

    def publish(self, tag):
        run(["gh", "release", "edit", tag, "--repo", self.repository, "--draft=false"])


def verify(store, tag, directory, names):
    release = store.release(tag)
    if not release or not set(names).issubset({asset["name"] for asset in release["assets"]}):
        raise ValueError("Release assets are incomplete")
    store.download(tag, directory, names)
    sums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split("  ", 1)
        if name in sums or name not in names or name == "SHA256SUMS" or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid release checksum manifest")
        sums[name] = checksum
    if set(sums) != set(names) - {"SHA256SUMS"}:
        raise ValueError("Incomplete release checksum manifest")
    for name, checksum in sums.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("Release asset checksum mismatch")


def recover(root, ref, store):
    root = Path(root).resolve()
    tags = run(["git", "tag", "--merged", ref, "--list", "v*", "--sort=-version:refname"], cwd=root).splitlines()
    tag = next((tag for tag in tags if re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag)), None)
    if tag is None:
        return None
    version = tag[1:]
    manifest = json.loads(run(["git", "show", tag + ":package.json"], cwd=root))
    if manifest.get("version") != version:
        raise ValueError("Release tag and package version disagree")
    names = ["herdr-agent-lifecycle-" + version + ".tar.gz", "opencode-herdr-lifecycle-" + version + ".tgz", "SHA256SUMS"]
    release = store.release(tag)
    with tempfile.TemporaryDirectory(prefix="lifecycle-recovery-") as temporary:
        base = Path(temporary)
        download = base / "download"
        download.mkdir()
        if release and not release["draft"]:
            verify(store, tag, download, names)
            return tag
        # Rebuild only the tagged source in a disposable clone. The caller's
        # worktree may contain the next version or unrelated local edits.
        checkout = base / "checkout"
        run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(root), str(checkout)])
        run(["git", "checkout", "--quiet", "--detach", tag], cwd=checkout)
        run([sys.executable, "scripts/prepare-release.py", version], cwd=checkout)
        if release is None:
            notes = (checkout / "CHANGELOG.md").read_text()
            store.create_draft(tag, notes)
        store.upload(tag, [checkout / "dist" / name for name in names])
        verify(store, tag, download, names)
        store.publish(tag)
        if store.release(tag)["draft"]:
            raise ValueError("GitHub release is still a draft")
    return tag


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Verified branch whose latest release tag should be checked")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "balakumardev/herdr-agent-lifecycle"))
    args = parser.parse_args()
    os.environ["GH_HOST"] = "github.com"
    tag = recover(Path(__file__).resolve().parents[1], args.ref, GitHub(args.repo))
    print(json.dumps({"verifiedRelease": tag}))
