"""Public shared configuration; never put secret values in error messages."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def load_config(*, env=None, home=None):
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    path = Path(env.get("HERDR_LIFECYCLE_CONFIG") or
                Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "herdr-agent-lifecycle/config.json")
    try:
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            value = {}
        if not isinstance(value, dict):
            raise ValueError()
        notifications = value.get("notifications", {})
        opencode = value.get("opencode", {})
        claude = value.get("claude", {})
        if not all(isinstance(section, dict) for section in (notifications, opencode, claude)):
            raise ValueError()
        enabled = notifications.get("enabled", False)
        topic = notifications.get("topic", "")
        endpoint = notifications.get("endpoint", "https://ntfy.sh")
        command = opencode.get("command", "opencode")
        allow_idle = claude.get("allowIdleTeammates", False)
        reinstrument = claude.get("reinstrumentPluginUpdates", True)
        if (type(enabled) is not bool or type(allow_idle) is not bool or type(reinstrument) is not bool
                or not isinstance(topic, str)
                or (topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", topic)) or (enabled and not topic)
                or not isinstance(endpoint, str) or not isinstance(command, str) or not command.strip()
                or len(command) > 4096 or re.search(r"[\r\n\x00]", command)):
            raise ValueError()
        url = urlsplit(endpoint)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError()
        _ = url.port  # Invalid ports raise ValueError without echoing the URL.
        return {"notifications": {"enabled": enabled, "topic": topic, "endpoint": endpoint.rstrip("/")},
                "opencode": {"command": command},
                "claude": {"allowIdleTeammates": allow_idle, "reinstrumentPluginUpdates": reinstrument}}
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid lifecycle configuration") from None
