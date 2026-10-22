"""Disposable packaging evidence; persistent output requires an explicit opt-in."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


def evidence_directory(name: str) -> Path:
    """Create a unique output directory, refusing historical runs even via symlinks."""
    override = os.environ.get("JARVIS_TEST_EVIDENCE_DIR")
    if override is not None:
        if not override.strip():
            raise ValueError("JARVIS_TEST_EVIDENCE_DIR must not be empty")
        supplied = Path(override).expanduser().absolute()
        base = supplied.resolve()
        if any(".runs_01" in path.parts for path in (supplied, base)):
            raise ValueError("JARVIS_TEST_EVIDENCE_DIR must not point into historical runs")
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"jarvis-{name}-", dir=base))
    # Product validators intentionally reject the host Hermes scratch tree.
    # Keep disposable fixture roots outside that tree without relaxing validators.
    folder = Path(tempfile.mkdtemp(prefix=f"jarvis-{name}-", dir=Path.home()))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    return folder


def self_evaluate() -> dict[str, object]:
    """Describe source capabilities and gaps without accessing private state."""
    from datetime import datetime, timezone

    return {
        "module": "evidence_support",
        "domain": "test-only disposable evidence output allocation and historical evidence "
        "preservation",
        "current_version": "unique mkdtemp test output directories with optional explicit base, "
        "resolved historical-path rejection and atexit cleanup for default "
        "allocations",
        "known_limitations": [
            "Only default allocations receive atexit cleanup; explicitly configured "
            "output directories persist until the test owner removes them.",
            "Historical path checks reject the named archive directory but are not "
            "an OS sandbox or a complete writable-path policy.",
            "Default temporary directories live under the user home to satisfy "
            "product root guards, so this utility is not a resident product "
            "capability or a mind/KG writer.",
        ],
        "improvement_directions": [
            "Let the test harness own allocation lifetime and cleanup "
            "reporting while retaining unique non-overwriting child "
            "directories.",
            "Represent test-only consumers as validation dependencies, "
            "separately from runtime imports, instead of inventing product "
            "knowledge graph or mind writes.",
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
