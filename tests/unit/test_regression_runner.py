"""The explicit regression runner must not inherit unsafe fixture roots."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts" / "run_regression.py"


def test_runner_canonicalizes_fixture_root_and_covers_all_owned_suites(tmp_path: Path) -> None:
    assert SCRIPT.is_file(), "explicit full regression runner is missing"
    spec = importlib.util.spec_from_file_location("regression_runner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    runs = module.invocations(
        PROJECT, alias, ("--collect-only",), {**os.environ, "TMPDIR": "unsafe-inherited-alias"}
    )
    assert [name for name, _, _ in runs] == ["kernel", "product", "isolated-engine"]
    kernel, product, engine = [command for _, command, _ in runs]
    assert kernel[:6] == [sys.executable, "-B", "-m", "pytest", "tests", "scripts"]
    assert product[:5] == [sys.executable, "-B", "-m", "pytest", "packaging"]
    assert "--ignore=packaging/test_engine_entry.py" in product
    assert kernel[-1] == product[-1] == "--collect-only"
    assert engine == [
        str(PROJECT / "packaging/runtime/engine-venv/bin/python3"),
        "-I",
        "-S",
        "-B",
        "packaging/test_engine_entry.py",
    ]
    for _, _, env in runs:
        assert env["TMPDIR"] == str(target.resolve())
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["JARVIS_TEST_EVIDENCE_DIR"] == str(target.resolve() / "evidence")
    assert runs[0][2]["PYTHONPATH"].split(os.pathsep) == [
        str(PROJECT / "src"),
        str(PROJECT / "packaging"),
        str(PROJECT),
    ]
    assert runs[1][2]["PYTHONPATH"].split(os.pathsep) == [
        str(PROJECT / "packaging"),
        str(PROJECT / "src"),
        str(PROJECT),
    ]
    assert all("no:cacheprovider" in command for command in (kernel, product))


@pytest.mark.parametrize("return_codes", [(0, 0, 0), (1, 0, 0), (0, 2, 0), (0, 0, 1)])
def test_runner_propagates_every_suite_failure_and_cleans_temporary_root(
    monkeypatch: pytest.MonkeyPatch, return_codes: tuple[int, int, int]
) -> None:
    spec = importlib.util.spec_from_file_location("regression_runner_main", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    roots: list[Path] = []
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        temporary = Path(kwargs["env"]["TMPDIR"])
        assert temporary.is_dir()
        assert temporary == temporary.resolve()
        roots.append(temporary)
        commands.append(command)
        return SimpleNamespace(returncode=return_codes[len(commands) - 1])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert module.main(()) == int(any(return_codes))
    assert len(commands) == 3
    assert len(set(roots)) == 1
    assert not roots[0].exists()
