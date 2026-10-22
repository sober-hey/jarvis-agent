#!/usr/bin/env python3
"""One-shot, idempotent Sir-belief migration; never modifies mind.db.

Run from packaging:
  PYTHONPATH=. ./runtime/engine-venv/bin/python3 migrate_beliefs.py \
      --data-root /Users/sober/.jarvis/loop-data

Use --mind-db to read a different source and --data-root to choose an isolated
destination. With no arguments, the existing loop-data root is used. Running
again reports unchanged facts rather than creating duplicates. Import is inert.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from sir_profile import SirProfile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path.home() / ".jarvis" / "loop-data")
    parser.add_argument("--mind-db", type=Path, default=None)
    args = parser.parse_args(argv)
    mind_db = args.mind_db if args.mind_db is not None else args.data_root / "mind.db"
    # Do not create an empty destination for a misspelled or missing source.
    if not mind_db.is_file():
        parser.error(f"Source mind database does not exist: {mind_db}")
    profile = SirProfile(args.data_root)
    report = profile.migrate_from_beliefs(mind_db)
    print(
        json.dumps(
            {
                "mind_db": str(mind_db),
                "profile_db": str(profile.path),
                **report,
                "total_facts": len(profile.get()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
