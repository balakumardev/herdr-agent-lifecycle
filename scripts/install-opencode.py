#!/usr/bin/env python3
"""Merge the existing V2 lifecycle repair into an explicitly selected config dir."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys


def read_config(path):
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text())
    except ValueError:
        raise ValueError(str(path) + ": JSONC/comments require the manual merge in README.md") from None
    if not isinstance(result, dict) or not isinstance(result.get("plugins", []), list):
        raise ValueError(str(path) + ": unexpected configuration shape")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).parent / "opencode-task-lifecycle")
    parser.add_argument("--apply", action="store_true", help="Otherwise show a dry run")
    args = parser.parse_args()
    destination = args.config_dir.expanduser().resolve()
    source = args.source_dir.expanduser().resolve()
    if not (source / "package.json").is_file():
        raise ValueError("Source integration is missing package.json")
    config = destination / ("opencode.jsonc" if (destination / "opencode.jsonc").exists() else "opencode.json")
    cli = destination / "cli.json"
    settings, terminal = read_config(config), read_config(cli)
    settings.setdefault("$schema", "https://opencode.ai/config.json")
    terminal.setdefault("$schema", "https://opencode.ai/v2/cli.json")
    plugins = settings.setdefault("plugins", [])
    for entry in ("-herdr.opencode.agent-state", "./integrations/task-lifecycle"):
        if not any((item.get("package") if isinstance(item, dict) else item) == entry for item in plugins):
            plugins.append(entry)
    patched = {}
    bridge_path = destination / "plugins/claude-code-hooks.js"
    if bridge_path.exists():
        patched[bridge_path] = patch_bridge(bridge_path.read_text())
        found = False
        for i, item in enumerate(plugins):
            package = item.get("package", "") if isinstance(item, dict) else item
            if isinstance(package, str) and package.endswith("/claude-code-hooks.js"):
                updated = dict(item) if isinstance(item, dict) else {"package": item}
                updated["options"] = {**updated.get("options", {}), "notifications": False}
                plugins[i] = updated
                found = True
        if not found:
            plugins.append({"package": "./plugins/claude-code-hooks.js", "options": {"notifications": False}})
    terminal_plugins = terminal.setdefault("plugins", [])
    if not any((item.get("package") if isinstance(item, dict) else item) == "./integrations/task-lifecycle" for item in terminal_plugins):
        terminal_plugins.append("./integrations/task-lifecycle")
    patched[config] = json.dumps(settings, indent=2) + "\n"
    patched[cli] = json.dumps(terminal, indent=2) + "\n"
    for path in source.iterdir():
        if path.is_file() and (path.suffix in (".mjs", ".js") or path.name in ("README.md", "package.json", "package-lock.json")):
            patched[destination / "integrations/task-lifecycle" / path.name] = path.read_text()
    changes = {path: text for path, text in patched.items() if not path.exists() or path.read_text() != text}
    backup = destination / "task-lifecycle-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if args.apply:
        for path, text in changes.items():
            if path.exists():
                saved = backup / path.relative_to(destination)
                saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(path, saved)
                saved.chmod(0o600)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".task-lifecycle.tmp")
            temporary.write_text(text)
            temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
            temporary.replace(path)
    print(json.dumps({"mode": "applied" if args.apply else "dry-run", "configDirectory": str(destination),
                      "changedFiles": [str(path.relative_to(destination)) for path in changes],
                      "backupDirectory": str(backup) if args.apply and changes else None}, indent=2))
    print("Next: cd " + str(destination / "integrations/task-lifecycle") + " && npm ci && npm test")
    print("Restart the OpenCode V2 service and reopen its TUI after its active work finishes.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
