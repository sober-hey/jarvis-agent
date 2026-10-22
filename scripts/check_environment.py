"""Print measured development environment; no production readiness claim."""

import json
from pathlib import Path

from jarvis.environment import inspect_environment


def main() -> int:
    report = inspect_environment(Path(__file__).resolve().parents[1])
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    sqlite = report["sqlite"]
    valid = (
        report["python_baseline"]
        and report["uv_available"]
        and report["platform"] == "Darwin"
        and sqlite["journal_mode"] == "wal"
        and sqlite["foreign_keys"]
        and sqlite["busy_timeout_ms"] == 5000
        and sqlite["synchronous"] == 2
        and sqlite["integrity"] == "ok"
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
