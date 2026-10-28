"""Run every portable suite; native Claude tests are a separate CLI gate."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
commands = [
    ["npm", "test", "--prefix", "opencode"],
    [sys.executable, "-B", "-m", "unittest", "discover", "-s", "claude/tests", "-p", "test_*.py"],
]
if list((root / "tests").glob("test_*.py")):
    commands.append([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])
if list((root / "tests").glob("*.test.mjs")):
    commands.append(["node", "--test", *[str(p) for p in sorted((root / "tests").glob("*.test.mjs"))]])
for command in commands:
    result = subprocess.run(command, cwd=root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if result.returncode:
        raise SystemExit(result.returncode)
