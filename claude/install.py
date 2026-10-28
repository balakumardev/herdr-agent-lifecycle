#!/usr/bin/env python3
"""Stage the local plugin and migrate only user-owned lifecycle hook records.

No Claude registry is written and no plugin CLI is invoked. Applying is explicit;
without --apply this prints a count-only plan and the supported activation commands.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import uuid

from transport import locked


EVENTS = frozenset(("Stop", "SubagentStop", "UserPromptSubmit", "PostToolUse"))
ARTIFACTS = (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.js",
              "runtime.py", "transport.py", "configuration.py", "bash_conditions.py", "event_hook.py", "hook_runner.py", "install.py")
OPTIONAL_ARTIFACTS = ("README.md", "tests/verify_live_cli.py",
                      "tests/live-fixture/.claude-plugin/plugin.json",
                      "tests/live-fixture/hooks/hooks.json")
PUBLIC_TEST_PATTERNS = ("tests/test_*.py", "tests/*.test.ts", "tests/*.test.js",
                        "tests/live-fixture/*.py", "tests/live-fixture/hooks/*.js")
MAX_FILE_BYTES = 16 * 1024 * 1024
MINIMUM_CLAUDE_VERSION = "2.1.287"


class InstallError(Exception):
    """Messages must be static: configuration values can contain credentials."""


def read_bytes(path):
    if path.is_symlink():
        raise InstallError("refusing to replace a symbolic-link file")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_FILE_BYTES:
        raise InstallError("input exceeds the installation file-size limit")
    return data


def read_json(path):
    raw = read_bytes(path)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InstallError("invalid JSON configuration; nothing was migrated") from None
    if not isinstance(value, dict):
        raise InstallError("configuration must be a JSON object")
    return value


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def confined(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise InstallError("installation target is outside the selected user configuration")
    return path


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def hook_groups(config):
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("invalid hook configuration")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise InstallError("invalid hook matcher list")
        for group in groups:
            if (not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                    or any(not isinstance(hook, dict) for hook in group["hooks"])):
                raise InstallError("invalid hook record")
        yield event, groups


def legacy_registration(hook, event, home):
    """Match a sole script invocation, never a substring in an unrelated command."""
    if hook.get("type") != "command" or not isinstance(hook.get("command"), str):
        return False
    names = []
    if event in ("Notification", "Stop"):
        names.append("ntfy-notify.sh")
    if event in ("SessionStart", "SessionEnd"):
        names.append("herdr-agent-state.sh")
    try:
        tokens = ([hook["command"], *hook["args"]] if isinstance(hook.get("args"), list)
                  else shlex.split(hook["command"]))
    except ValueError:
        return False
    if not tokens or any(not isinstance(t, str) or re.search(r"[;|&<>`\n]", t) for t in tokens):
        return False
    if Path(tokens[0]).name in ("bash", "sh", "zsh"):
        tokens = tokens[1:]
        while tokens and tokens[0] in ("--", "-e", "-u", "-eu"):
            tokens = tokens[1:]
    if not tokens:
        return False
    for name in names:
        suffix = "/.claude/hooks/" + name
        if tokens[0] in (str(home) + suffix, "~" + suffix, "$HOME" + suffix, "${HOME}" + suffix):
            return True
    return False


def original_hook(hook, runner):
    """Recover our own invocation on reapply, including after a property edit."""
    original = copy.deepcopy(hook)
    try:
        args = ([hook["command"], *hook["args"]] if "args" in hook
                else shlex.split(hook.get("command", "")))
    except (ValueError, TypeError, KeyError):
        raise InstallError("invalid asyncRewake command or arguments") from None
    if (len(args) >= 6 and args[1:3] == [str(runner), "--id"]
            and isinstance(args[3], str) and re.fullmatch(r"async-[0-9a-f]{24}", args[3])):
        if args[4] == "--shell" and len(args) == 6:
            original["command"] = args[5]
            original.pop("args", None)
        elif args[4] == "--":
            original["command"] = args[5]
            original["args"] = args[6:]
        else:
            raise InstallError("unrecognized lifecycle wrapper invocation")
    if not isinstance(original.get("command"), str) or not original["command"]:
        raise InstallError("asyncRewake command must be a nonempty string")
    if "args" in original and (not isinstance(original["args"], list)
                               or any(not isinstance(a, str) for a in original["args"])):
        raise InstallError("asyncRewake exec arguments must be a string array")
    return original


def wrap_hook(original, hook_id, runner):
    hook = copy.deepcopy(original)
    invocation = [str(Path(sys.executable).resolve()), str(runner), "--id", hook_id]
    if "args" in original:
        hook["command"] = invocation[0]
        hook["args"] = invocation[1:] + ["--", original["command"], *original["args"]]
    else:
        hook["command"] = shlex.join(invocation + ["--shell", original["command"]])
    return hook


def native_hook_identity(hook, event, plugin_root=""):
    """Claude 2.1.291 command identity, namespaced by the already-filtered event.

    Timeout, async/rewake flags and status/result messages belong to the last
    matching definition, not its identity. Absent args serialize as null, not [].
    """
    scope = plugin_root or hook.get("pluginRoot") or hook.get("skillRoot") or ""
    shell = hook.get("shell")
    condition = hook.get("if")
    arguments = json.dumps(hook.get("args"), separators=(",", ":"), ensure_ascii=False)
    return json_bytes([event, scope, hook["type"], "bash" if shell is None else shell,
                       hook["command"], arguments, "" if condition is None else condition])


def public_artifacts(source):
    yield from ARTIFACTS
    for relative in OPTIONAL_ARTIFACTS:
        if (source / relative).is_file():
            yield relative
    for pattern in PUBLIC_TEST_PATTERNS:
        for path in sorted(source.glob(pattern)):
            if path.is_file():
                yield str(path.relative_to(source))


def plugin_files(settings, claude):
    enabled = settings.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise InstallError("invalid enabledPlugins configuration")
    registry = read_json(confined(claude / "plugins/installed_plugins.json", claude))
    if not registry:
        return
    if registry.get("version") != 2 or not isinstance(registry.get("plugins"), dict):
        raise InstallError("unsupported installed plugin registry; use Claude to update it")
    seen = set()
    for name in sorted(enabled):
        if enabled[name] is not True:
            continue
        records = registry["plugins"].get(name, [])
        if not isinstance(records, list):
            raise InstallError("invalid installed plugin records")
        for record in records:
            if not isinstance(record, dict) or record.get("scope") != "user":
                continue
            install_path = record.get("installPath")
            if not isinstance(install_path, str):
                raise InstallError("missing user plugin installPath")
            # Managed policy and project locations cannot be brought into scope by a registry entry.
            root = Path(install_path)
            if not root.is_absolute() or not root.resolve().is_relative_to(claude.resolve()):
                continue
            default = root / "hooks/hooks.json"
            paths = [default] if default.is_file() else []
            manifest_path = confined(root / ".claude-plugin/plugin.json", claude)
            confined(manifest_path, root)
            metadata = read_json(manifest_path)
            custom = metadata.get("hooks", [])
            if not isinstance(custom, (str, list, dict)):
                raise InstallError("unsupported plugin hooks declaration")
            sources = custom if isinstance(custom, list) else [custom]
            inline = []
            for index, source in enumerate(sources):
                if isinstance(source, str) and source:
                    path = confined(root / source, claude)
                    # A plugin cannot redirect the migration into a sibling/project or policy file.
                    confined(path, root)
                    if not path.is_file():
                        raise InstallError("declared plugin hook file is missing")
                    paths.append(path)
                elif isinstance(source, dict):
                    inline.append(("hooks", index) if isinstance(custom, list) else ("hooks",))
                else:
                    raise InstallError("unsupported plugin hooks declaration")
            for path in paths:
                confined(path, claude)
                confined(path, root)
                if path.resolve() not in seen:
                    seen.add(path.resolve())
                    yield str(root), path, ((),)
            if inline and manifest_path.resolve() not in seen:
                seen.add(manifest_path.resolve())
                yield str(root), manifest_path, tuple(inline)


class Installation:
    def __init__(self, home, source):
        self.home = home.resolve()
        self.claude = self.home / ".claude"
        self.target = confined(self.claude / "hooks/task-lifecycle", self.claude)
        self.market = confined(self.claude / "local-marketplaces/task-lifecycle", self.claude)
        self.source = source.resolve()
        self.changes = []
        self.wrappers = []
        self.wrapped = 0
        self.removed = 0
        self.native_rewake = {}

    def change(self, path, data, mode=None, patches=None):
        confined(path, self.claude)
        old = read_bytes(path)
        old_mode = stat.S_IMODE(path.stat().st_mode) if old is not None else None
        mode = mode if mode is not None else (old_mode if old_mode is not None else 0o600)
        if old != data or old_mode != mode:
            self.changes.append({"path": path, "data": data, "mode": mode,
                                 "old": old, "old_mode": old_mode, "patches": patches})

    def migrate_hooks(self, path, plugin_root="", user=False, definitions=((),)):
        config = read_json(path)
        migrated = copy.deepcopy(config)
        patches = []
        for location in definitions:
            definition = migrated
            for part in location:
                definition = definition[part]
            if not isinstance(definition, dict):
                raise InstallError("invalid inline hook definition")
            if user or "hooks" in definition:
                hook_map = definition.get("hooks", {})
                view = definition
            else:
                # Custom definitions may be bare event maps. Metadata/native modules stay untouched.
                hook_map = definition
                view = {"hooks": {event: groups for event, groups in definition.items() if event in EVENTS}}
            for event, groups in hook_groups(view):
                retained = []
                for group_index, group in enumerate(groups):
                    kept = []
                    for hook_index, hook in enumerate(group["hooks"]):
                        after = hook
                        if user and legacy_registration(hook, event, self.home):
                            after = None
                            self.removed += 1
                        elif event in EVENTS and hook.get("type") == "command":
                            original = original_hook(hook, self.target / "hook_runner.py")
                            key = native_hook_identity(original, event, plugin_root)
                            rewake = original.get("asyncRewake") is True
                            if key in self.native_rewake and self.native_rewake[key] != rewake:
                                raise InstallError("mixed asyncRewake values for one native hook identity; nothing was migrated")
                            self.native_rewake[key] = rewake
                            if rewake:
                                hook_id = "async-" + hashlib.sha256(key).hexdigest()[:24]
                                after = wrap_hook(original, hook_id, self.target / "hook_runner.py")
                                self.wrappers.append({"id": hook_id, "event": event, "matcher": group.get("matcher"),
                                                      "file": str(path), "original": original, "wrapped": after})
                                self.wrapped += after != hook
                        if after != hook:
                            patch = {"event": event, "matcher": group.get("matcher"),
                                     "group_index": group_index, "hook_index": hook_index,
                                     "original": hook, "wrapped": after}
                            if location:
                                patch["definition_path"] = list(location)
                            patches.append(patch)
                        if after is not None:
                            kept.append(after)
                    # Preserve pre-existing empty groups, but remove groups emptied by this migration.
                    if kept or not group["hooks"]:
                        group["hooks"] = kept
                        retained.append(group)
                hook_map[event] = retained
        if migrated != config:
            self.change(path, json_bytes(migrated), patches=patches)
        return config

    def plan(self):
        metadata = read_json(self.source / ".claude-plugin/plugin.json")
        if metadata.get("name") != "task-lifecycle":
            raise InstallError("source must contain the task-lifecycle plugin manifest")
        # An allowlist is intentional: state, configs, credentials, types, backups and dependencies never migrate.
        for relative in public_artifacts(self.source):
            data = read_bytes(confined(self.source / relative, self.source))
            if data is None:
                raise InstallError("source plugin is incomplete")
            mode = 0o700 if relative.endswith(".py") else 0o600
            self.change(self.target / relative, data, mode)
            self.change(self.market / "plugins/task-lifecycle" / relative, data, mode)
        marketplace = {"name": "local-task-lifecycle", "owner": {"name": "Local user"},
                       "plugins": [{"name": "task-lifecycle", "source": "./plugins/task-lifecycle"}]}
        self.change(self.market / ".claude-plugin/marketplace.json", json_bytes(marketplace), 0o600)

        settings_path = confined(self.claude / "settings.json", self.claude)
        settings = self.migrate_hooks(settings_path, user=True)
        for plugin_root, path, definitions in plugin_files(settings, self.claude):
            self.migrate_hooks(path, plugin_root, definitions=definitions)

        shim = confined(self.claude / "hooks/ntfy-notify.sh", self.claude)
        old = read_bytes(shim) or b""
        pattern = rb"^[ \t]*TOPIC=(?:\"[A-Za-z0-9_-]+\"|'[A-Za-z0-9_-]+'|[A-Za-z0-9_-]+)[ \t]*(?:#[^\r\n]*)?$"
        assignments = re.findall(pattern, old, re.M)
        topic_line = assignments[-1] if assignments else b"# TOPIC is unset; add a literal assignment here when configured."
        passive = (b"#!/bin/sh\n# Passive compatibility shim; lifecycle notifications are handled by task-lifecycle.\n"
                   + topic_line + b"\nexit 0\n")
        self.change(shim, passive, 0o700)
        manifest = {"version": 1, "minimum_claude_version": MINIMUM_CLAUDE_VERSION,
                    "plugin_root": str(self.target), "marketplace": str(self.market),
                    "wrappers": sorted(self.wrappers, key=lambda record: record["id"])}
        self.change(self.target / "install-manifest.json", json_bytes(manifest), 0o600)

    def apply(self):
        if not self.changes:
            return
        backups = confined(self.target / "backups", self.claude)
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backups.chmod(0o700)
        backup = backups / uuid.uuid4().hex
        backup.mkdir(mode=0o700)
        records = []
        for index, change in enumerate(self.changes):
            record = {"file": str(change["path"]), "existed": change["old"] is not None,
                      "mode": change["old_mode"]}
            if change["patches"] is not None:
                # Never copy env/API keys or unrelated hook registrations out of settings.
                record["changes"] = change["patches"]
            elif change["old"] is not None:
                name = str(index) + ".original"
                atomic_write(backup / name, change["old"], 0o600)
                record["backup"] = name
            records.append(record)
        atomic_write(backup / "backup.json", json_bytes({"version": 1, "files": records}), 0o600)
        # Complete all private backups before the first replacement; reject a concurrent config edit.
        if any(read_bytes(c["path"]) != c["old"] for c in self.changes):
            raise InstallError("configuration changed during installation; rerun to replan")
        for change in self.changes:
            atomic_write(change["path"], change["data"], change["mode"])

    def report(self, applied):
        return {"mode": "apply" if applied else "dry-run", "changed_files": len(self.changes),
                "wrapped_hooks": self.wrapped, "tracked_hooks": len(self.wrappers),
                "removed_hooks": self.removed, "minimum_claude_version": MINIMUM_CLAUDE_VERSION,
                "commands": ["claude plugin marketplace add " + shlex.quote(str(self.market)),
                             "claude plugin install task-lifecycle@local-task-lifecycle --scope user"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parent)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        installation = Installation(args.home, args.source_dir)
        if args.apply:
            with locked(installation.target / ".install.lock"):
                installation.plan()
                installation.apply()
        else:
            installation.plan()
        print(json.dumps(installation.report(args.apply)))
        return 0
    except InstallError as error:
        print(json.dumps({"error": str(error)}))
    except (OSError, ValueError, TypeError, TimeoutError):
        print(json.dumps({"error": "installation failed; inspect file access and configuration locally"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
