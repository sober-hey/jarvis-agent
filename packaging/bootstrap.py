#!/usr/bin/env python3
"""工程本机 bootstrap；仅转交 JARVIS 的标准库检查入口，不安装依赖。"""

import runpy
from pathlib import Path


def self_evaluate() -> dict[str, object]:
    """Observe only the fixed forwarding target; never run the CLI or checks."""
    from datetime import datetime, timezone

    target = Path(__file__).resolve().with_name("jarvis.py")
    checks = {
        "entrypoint_present": target.is_file(),
        "fixed_python_target": target.name == "jarvis.py" and target.suffix == ".py",
    }
    return {
        "module": "bootstrap",
        "domain": "Python application bootstrap fixed entrypoint forwarding",
        "current_version": "1.0.0",
        "known_limitations": [
            "runpy forwards arguments to jarvis.py; it does not build or install dependencies.",
            "Target presence does not establish distribution readiness or runtime acceptance.",
        ],
        "improvement_directions": [
            "Keep forwarding and control-interpreter requirements covered by entrypoint tests."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "entrypoint": target.name,
        "checks": checks,
        "ok": all(checks.values()),
    }


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().with_name("jarvis.py")), run_name="__main__")
