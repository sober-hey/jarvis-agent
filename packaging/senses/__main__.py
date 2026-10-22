"""Print counts only. Never serialize events, summaries, paths or payloads."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .api import poll_all, statistics
from .base import parse_time


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS sensor event counts only")
    parser.add_argument("--since", help="ISO 8601 event-time cutoff")
    parser.add_argument("--dry-run", action="store_true", help="do not persist cursor changes")
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    try:
        if args.since:
            parse_time(args.since)
        events = poll_all(data_root=args.data_root, since=args.since, dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"error": "poll_unavailable_or_invalid_arguments"}))
        return 2
    print(json.dumps(statistics(events), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
