"""Run owned suites separately: kernel, product CLI, and isolated engine.

Use .venv/bin/python scripts/run_regression.py [pytest options].
The bare pytest default remains the kernel's tests/ suite. No validators or
outcomes are patched. Product CLI `jarvis.py` and kernel package `jarvis` must
never share a pytest interpreter. The engine has its own mandatory interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

Invocation = tuple[str, list[str], dict[str, str]]


def invocations(
    project: Path, temporary: Path, options: tuple[str, ...], inherited: dict[str, str]
) -> list[Invocation]:
    """Build all required suites without inheriting a TMPDIR alias."""
    temporary = temporary.resolve(strict=True)
    environment = {
        **inherited,
        "TMPDIR": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "JARVIS_TEST_EVIDENCE_DIR": str(temporary / "evidence"),
    }
    pytest = [sys.executable, "-B", "-m", "pytest"]
    flags = ["--tb=short", "-q", "-p", "no:cacheprovider", *options]
    kernel_env = {
        **environment,
        "PYTHONPATH": os.pathsep.join(
            (str(project / "src"), str(project / "packaging"), str(project))
        ),
    }
    product_env = {
        **environment,
        "PYTHONPATH": os.pathsep.join(
            (str(project / "packaging"), str(project / "src"), str(project))
        ),
    }
    return [
        ("kernel", [*pytest, "tests", "scripts", *flags], kernel_env),
        (
            "product",
            [*pytest, "packaging", "--ignore=packaging/test_engine_entry.py", *flags],
            product_env,
        ),
        (
            "isolated-engine",
            [
                str(project / "packaging/runtime/engine-venv/bin/python3"),
                "-I",
                "-S",
                "-B",
                "packaging/test_engine_entry.py",
            ],
            environment,
        ),
    ]


def main(options: tuple[str, ...] | None = None) -> int:
    project = Path(__file__).resolve().parents[1]
    # Dedicated sibling roots are outside both source and protected Hermes.
    with tempfile.TemporaryDirectory(prefix="jarvis-regression-", dir=project.parent) as folder:
        runs = invocations(
            project,
            Path(folder),
            tuple(sys.argv[1:]) if options is None else options,
            dict(os.environ),
        )
        failed = False
        for name, command, environment in runs:
            print(f"=== {name} ===", flush=True)
            result = subprocess.run(command, cwd=project, env=environment, check=False)
            failed = failed or result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
