#!/usr/bin/env python3
"""Install and test actual release archives in isolated temporary homes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


def run(command, *, cwd, env):
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        raise RuntimeError("Package verification command failed: " + " ".join(map(str, command)))
    return result.stdout


def extract(archive_path, directory):
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            if not member.isfile() or not (directory / member.name).resolve().is_relative_to(directory.resolve()):
                raise ValueError("Unexpected release archive entry")
        archive.extractall(directory, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def verify(dist, version):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Expected a stable version")
    source_name = "herdr-agent-lifecycle-" + version + ".tar.gz"
    package_name = "opencode-herdr-lifecycle-" + version + ".tgz"
    hashes = dict((name, checksum) for checksum, name in
                  (line.split("  ") for line in (dist / "SHA256SUMS").read_text().splitlines()))
    if set(hashes) != {source_name, package_name}:
        raise ValueError("Unexpected release checksum entries")
    for name, checksum in hashes.items():
        if hashlib.sha256((dist / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("Release checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="lifecycle-packages-") as temporary:
        root = Path(temporary).resolve()
        extract(dist / source_name, root)
        source = root / ("herdr-agent-lifecycle-" + version)
        home = root / "home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith(("HERDR_", "CLAUDE_TASK_LIFECYCLE_"))}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
                   HERDR_LIFECYCLE_CONFIG=str(home / "absent-config.json"), PYTHONDONTWRITEBYTECODE="1")
        npm = ["--ignore-scripts", "--no-audit", "--no-fund"]
        run(["npm", "ci", "--prefix", str(source / "opencode"), *npm], cwd=root, env=env)
        packed = root / "npm-install"
        run(["npm", "install", "--prefix", str(packed), str(dist / package_name), *npm], cwd=root, env=env)
        run(["node", "--input-type=module", "-e",
             "import server from 'opencode-herdr-lifecycle'; import tui from 'opencode-herdr-lifecycle/tui';"
             "if(server.id !== 'local.task-lifecycle' || tui.id !== 'local.task-lifecycle.tui') process.exit(1);"],
            cwd=packed, env=env)
        config = home / ".config/opencode"
        config.mkdir(parents=True)
        original = '{"plugins":["existing-plugin"]}\n'
        (config / "opencode.json").write_text(original)
        command = [sys.executable, str(source / "scripts/install-opencode.py"), "--config-dir", str(config)]
        installed = json.loads(run([*command, "--apply"], cwd=root, env=env))
        assert json.loads(run([*command, "--apply"], cwd=root, env=env))["changedFiles"] == []
        server = json.loads((config / "opencode.json").read_text())
        terminal = json.loads((config / "cli.json").read_text())
        assert "existing-plugin" in server["plugins"] and str(source / "opencode") in server["plugins"]
        assert server["plugins"][-1] == "-herdr.opencode.agent-state"
        assert terminal["plugins"][-1] == "-herdr.opencode.session-selection"
        run([sys.executable, str(source / "scripts/install-opencode.py"), "--restore",
             installed["backupDirectory"], "--apply"], cwd=root, env=env)
        assert (config / "opencode.json").read_text() == original
        assert not (config / "cli.json").exists()
        command = [sys.executable, str(source / "claude/install.py"), "--home", str(home)]
        installed = json.loads(run([*command, "--apply"], cwd=root, env=env))
        assert json.loads(run([*command, "--apply"], cwd=root, env=env))["changed_files"] == 0
        staged = home / ".claude/hooks/task-lifecycle"
        run([sys.executable, "-B", "-c",
             "from runtime import Runtime; from configuration import load_config; "
             "assert load_config()['claude']['allowIdleTeammates'] is False"], cwd=staged, env=env)
        manifest = json.loads((staged / ".claude-plugin/plugin.json").read_text())
        assert manifest["version"] == version
        run([*command, "--restore", installed["backup_directory"], "--apply"], cwd=root, env=env)
        assert not (staged / "runtime.py").exists()
        output = run(["npm", "run", "test:runtime"], cwd=source, env=env)
        print(output)
    print(json.dumps({"version": version, "sourceRuntimeTests": "passed", "npmEntrypoints": "passed",
                      "cleanHomeInstallAndRestore": ["opencode", "claude"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    verify(args.dist.resolve(), args.version)
