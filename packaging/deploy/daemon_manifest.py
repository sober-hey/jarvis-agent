"""Print supervisor manifest replacing the placeholder with the resident daemon."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon_wiring import component  # noqa: E402
from supervisor_components import default_components  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    definitions = [
        c
        for c in default_components(args.data_root, args.config, args.key_file, args.port)
        if c.name != "inner-placeholder"
    ]
    definitions.append(component(args.data_root, config=args.config, key_file=args.key_file))
    print(json.dumps({"components": [asdict(c) for c in definitions]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
