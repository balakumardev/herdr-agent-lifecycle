#!/usr/bin/env python3
"""Synchronize versions and produce portable, checksummed release archives."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


def prepare(root, version):
    root = Path(root).resolve()
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Expected a stable semantic version without the v prefix")
    updates = {}
    for relative in ("package.json", "package-lock.json", "opencode/package.json", "opencode/package-lock.json",
                     "claude/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        path = root / relative
        value = json.loads(path.read_text())
        if relative.endswith("marketplace.json"):
            for plugin in value["plugins"]:
                if plugin["name"] == "task-lifecycle":
                    plugin["version"] = version
        else:
            value["version"] = version
            if relative.endswith("package-lock.json"):
                value["packages"][""]["version"] = version
        updates[path] = (json.dumps(value, indent=2) + "\n").encode()
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    files = {path.decode() for path in tracked.split(b"\0") if path}
    if not {"LICENSE", "README.md", "opencode/index.js", "claude/runtime.py"}.issubset(files):
        raise ValueError("Prepare releases from a tracked repository checkout")
    if (root / "CHANGELOG.md").is_file():
        files.add("CHANGELOG.md")
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Release source must contain ordinary repository files")
    for path, data in updates.items():
        path.write_bytes(data)
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    result = subprocess.run(["npm", "pack", "--ignore-scripts", "--json", "--pack-destination", str(dist)],
                            cwd=root / "opencode", capture_output=True, text=True, check=True)
    package = dist / json.loads(result.stdout)[0]["filename"]
    source = dist / ("herdr-agent-lifecycle-" + version + ".tar.gz")
    with source.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name in sorted(files):
                path = root / name
                info = archive.gettarinfo(str(path), arcname="herdr-agent-lifecycle-" + version + "/" + name)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                info.pax_headers = {}
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    sums = "".join(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
                   for path in sorted((source, package)))
    (dist / "SHA256SUMS").write_text(sums)
    return [source, package, dist / "SHA256SUMS"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    artifacts = prepare(Path(__file__).resolve().parents[1], args.version)
    print(json.dumps({"version": args.version, "artifacts": [path.name for path in artifacts]}))
