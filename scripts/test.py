"""Run every portable suite; native Claude tests are a separate CLI gate."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
commands = [
    ["npm", "test", "--prefix", "opencode"],
    [sys.executable, "-B", "-m", "unittest", "discover", "-s", "claude/tests", "-p", "test_*.py"],
]
if "--runtime-only" not in sys.argv and list((root / "tests").glob("test_*.py")):
    commands.append([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])
if "--runtime-only" not in sys.argv and list((root / "tests").glob("*.test.mjs")):
    commands.append(["node", "--test", *[str(p) for p in sorted((root / "tests").glob("*.test.mjs"))]])
with tempfile.TemporaryDirectory(prefix="lifecycle-suites-") as temporary:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", XDG_STATE_HOME=temporary,
               HERDR_LIFECYCLE_CONFIG=str(Path(temporary) / "absent-config.json"))
    for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"):
        env.pop(key, None)
    for command in commands:
        result = subprocess.run(command, cwd=root, env=env)
        if result.returncode:
            raise SystemExit(result.returncode)
