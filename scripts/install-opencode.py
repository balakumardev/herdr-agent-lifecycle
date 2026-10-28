#!/usr/bin/env python3
"""Register a prepared OpenCode lifecycle release without restarting the service."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid


def read_bytes(path):
    if path.is_symlink():
        raise ValueError("Refusing to replace a symbolic-link configuration file")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def encoded(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest() if value is not None else None


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode), "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path):
    raw = read_bytes(path)
    if raw is None:
        return {}
    try:
        result = json.loads(raw)
    except ValueError:
        raise ValueError("JSONC/comments require the manual plugin merge in README.md") from None
    if not isinstance(result, dict) or not isinstance(result.get("plugins", []), list):
        raise ValueError("Unexpected OpenCode configuration shape")
    for item in result.get("plugins", []):
        package = item.get("package") if isinstance(item, dict) else item
        if not isinstance(package, str) or (isinstance(item, dict) and not isinstance(item.get("options", {}), dict)):
            raise ValueError("Unexpected OpenCode plugin registration")
    return result


def patch_bridge(text):
    replacements = [
        ("async function v1({ client, directory }) {", "async function v1({ client, directory, notifications = true }) {"),
        ("  async function attention(event, sessionID) {\n", "  async function attention(event, sessionID) {\n    if (!notifications) return;\n"),
        ("const bridge = await v1({ client: v1Client, directory });",
         "const bridge = await v1({ client: v1Client, directory, notifications: ctx.options?.notifications === true });"),
    ]
    for old, new in replacements:
        if new in text:
            continue
        if text.count(old) != 1:
            raise ValueError("The existing claude-code-hooks.js has a different layout; apply the three README edits manually")
        text = text.replace(old, new, 1)
    return text


def is_owned(value, destination, plugin):
    if value in (str(plugin), "./integrations/task-lifecycle", "opencode-herdr-lifecycle"):
        return True
    if value.startswith("-"):
        return False
    path = Path(value.removeprefix("file://"))
    if not path.is_absolute():
        path = destination / path
    if path.resolve() == destination / "integrations/task-lifecycle":
        return True
    try:
        return json.loads((path / "package.json").read_text()).get("name") in (
            "local-opencode-task-lifecycle", "opencode-herdr-lifecycle")
    except (OSError, ValueError, AttributeError):
        return False


def register(entries, destination, plugin, disabled):
    result = []
    owned = False
    for item in entries:
        package = item.get("package") if isinstance(item, dict) else item
        if is_owned(package, destination, plugin):
            if owned:
                raise ValueError("Multiple lifecycle registrations need a manual merge")
            owned = True
            result.append({**item, "package": str(plugin)} if isinstance(item, dict) else str(plugin))
        else:
            result.append(item)
    if disabled not in result:
        result.append(disabled)
    if not owned:
        result.append(str(plugin))
    return result


def installation_plan(destination, plugin):
    for relative in ("package.json", "index.js", "tui.js", "node_modules/@opencode/client/package.json"):
        if not (plugin / relative).is_file():
            raise ValueError("Plugin release is not ready; run npm ci in its opencode directory first")
    config = destination / ("opencode.jsonc" if (destination / "opencode.jsonc").exists() else "opencode.json")
    cli = destination / "cli.json"
    settings, terminal = read_config(config), read_config(cli)
    settings.setdefault("$schema", "https://opencode.ai/config.json")
    terminal.setdefault("$schema", "https://opencode.ai/v2/cli.json")
    settings["plugins"] = register(settings.get("plugins", []), destination, plugin, "-herdr.opencode.agent-state")
    terminal["plugins"] = register(terminal.get("plugins", []), destination, plugin, "-herdr.opencode.session-selection")
    patched = {}
    bridge_path = destination / "plugins/claude-code-hooks.js"
    bridge = read_bytes(bridge_path)
    if bridge is not None:
        patched[bridge_path] = patch_bridge(bridge.decode()).encode()
        for i, item in enumerate(settings["plugins"]):
            package = item.get("package") if isinstance(item, dict) else item
            if package.endswith("/claude-code-hooks.js"):
                updated = dict(item) if isinstance(item, dict) else {"package": item}
                updated["options"] = {**updated.get("options", {}), "notifications": False}
                settings["plugins"][i] = updated
                break
        else:
            settings["plugins"].append({"package": "./plugins/claude-code-hooks.js", "options": {"notifications": False}})
    patched[config], patched[cli] = encoded(settings), encoded(terminal)
    changes = []
    for path, data in patched.items():
        if not path.resolve().is_relative_to(destination):
            raise ValueError("Configuration path escapes the selected directory")
        old = read_bytes(path)
        if old != data:
            changes.append({"path": path, "old": old, "data": data,
                            "mode": stat.S_IMODE(path.stat().st_mode) if old is not None else 0o600})
    return changes


def apply_plan(destination, changes):
    if not changes:
        return None
    backup = destination / "task-lifecycle-backups" / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    records = []
    for index, change in enumerate(changes):
        name = str(index) + ".original"
        if change["old"] is not None:
            atomic_write(backup / name, change["old"])
        records.append({"path": str(change["path"].relative_to(destination)), "mode": change["mode"],
                        "backup": name if change["old"] is not None else None,
                        "beforeHash": digest(change["old"]), "afterHash": digest(change["data"])})
    atomic_write(backup / "manifest.json", encoded({"version": 1, "configDirectory": str(destination), "files": records}))
    if any(read_bytes(c["path"]) != c["old"] for c in changes):
        raise ValueError("Configuration changed during installation; replan before applying")
    for change in changes:
        atomic_write(change["path"], change["data"], change["mode"])
    return backup


def restore_plan(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    destination = Path(manifest["configDirectory"]).resolve()
    if manifest.get("version") != 1 or not backup.is_relative_to(destination / "task-lifecycle-backups"):
        raise ValueError("Invalid installation backup")
    changes = []
    allowed = {"opencode.json", "opencode.jsonc", "cli.json", "plugins/claude-code-hooks.js"}
    for record in manifest["files"]:
        if record["path"] not in allowed:
            raise ValueError("Invalid backup target")
        path = destination / record["path"]
        if not path.resolve().is_relative_to(destination):
            raise ValueError("Backup target escapes the configuration directory")
        name = record["backup"]
        if name is not None and (Path(name).name != name or not name.endswith(".original")):
            raise ValueError("Invalid backup source")
        original = read_bytes(backup / name) if name is not None else None
        if digest(original) != record["beforeHash"]:
            raise ValueError("Backup checksum does not match")
        current = read_bytes(path)
        if current == original:
            continue
        if digest(current) != record["afterHash"]:
            raise ValueError("A configuration file changed after installation; refusing to overwrite it")
        changes.append({"path": path, "old": current, "data": original, "mode": record["mode"]})
    return destination, changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--plugin-dir", "--source-dir", dest="plugin_dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "opencode")
    parser.add_argument("--restore", type=Path, help="Restore an owned installation backup, with later-edit protection")
    parser.add_argument("--apply", action="store_true", help="Otherwise show a dry run")
    args = parser.parse_args(argv)
    backup = None
    if args.restore:
        destination, changes = restore_plan(args.restore.expanduser().resolve())
        if args.apply:
            if any(read_bytes(c["path"]) != c["old"] for c in changes):
                raise ValueError("Configuration changed during restore; no files restored")
            for change in changes:
                if change["data"] is None:
                    change["path"].unlink()
                else:
                    atomic_write(change["path"], change["data"], change["mode"])
    else:
        if args.config_dir is None:
            raise ValueError("Select --config-dir, or use --restore with an installation backup")
        destination = args.config_dir.expanduser().resolve()
        changes = installation_plan(destination, args.plugin_dir.expanduser().resolve())
        if args.apply:
            backup = apply_plan(destination, changes)
    print(json.dumps({"mode": "applied" if args.apply else "dry-run", "configDirectory": str(destination),
                      "changedFiles": [str(c["path"].relative_to(destination)) for c in changes],
                      "backupDirectory": str(backup) if backup else None}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(1)
    except (OSError, KeyError, TypeError):
        print(json.dumps({"error": "Installation failed; check configuration and file access locally"}))
        raise SystemExit(1)
