#!/usr/bin/env python3
"""Read native settings-hook snapshots without altering any hook decision."""
import json
import sys

from runtime import default_runtime


def main():
    try:
        event = json.load(sys.stdin)
        default_runtime().observe_hook(event)
    except Exception:
        # A missed snapshot cannot qualify for completion. Never return hook
        # decision JSON or error text that could change Claude's execution.
        pass


if __name__ == "__main__":
    main()
