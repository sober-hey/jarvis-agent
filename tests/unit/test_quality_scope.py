"""Quality checks must cover formal Python trees and exclude archival copies."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
QUALITY_SCRIPT: Final = PROJECT_ROOT / "scripts" / "run_quality_checks.sh"
PYPROJECT: Final = PROJECT_ROOT / "pyproject.toml"


def _ruff_command(kind: str) -> list[str]:
    lines = QUALITY_SCRIPT.read_text(encoding="utf-8").splitlines()
    matches = [
        shlex.split(match.group(1))
        for line in lines
        if (match := re.fullmatch(r"\s*uv run --frozen ((?:ruff )?\S+ .*)", line))
        and match.group(1).startswith(f"ruff {kind} ")
    ]
    assert len(matches) == 1, f"expected one ruff {kind} declaration, got {matches!r}"
    return matches[0][1:]


def _ruff_executable() -> list[str]:
    candidate = PROJECT_ROOT / ".venv" / "bin" / "ruff"
    return [str(candidate)] if candidate.is_file() else [sys.executable, "-m", "ruff"]


def _run_ruff(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_ruff_executable(), *args, "--config", str(tmp_path / "pyproject.toml")],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _make_project(tmp_path: Path, *, bad_tree: str | None = None) -> None:
    for tree in ("src", "tests", "scripts"):
        path = (
            tmp_path / tree / ("evidence_status.py" if tree == "scripts" else f"{tree}_module.py")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "class EvidenceStatus:\n    pass\n" if tree == "scripts" else "VALUE = 1\n"
        if tree == bad_tree:
            body += "UNDEFINED = missing_name\n"
        path.write_text(body, encoding="utf-8")
    for directory in ("docs/archive", "docs/audits", "项目代码/src"):
        path = tmp_path / directory / "historical_copy.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("UNDEFINED = missing_name\n", encoding="utf-8")
    (tmp_path / "standalone.py").write_text("UNDEFINED = missing_name\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        PYPROJECT.read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_quality_scope_is_declared_once_and_excludes_nonformal_assets(tmp_path: Path) -> None:
    _make_project(tmp_path)
    check_args = _ruff_command("check")
    assert check_args[-3:] == ["src", "tests", "scripts"]
    result = _run_ruff(tmp_path, [*check_args, "--show-files"])
    assert result.returncode == 0, result.stderr
    shown = {str(Path(path).relative_to(tmp_path)) for path in result.stdout.splitlines()}
    assert {"src/src_module.py", "tests/tests_module.py", "scripts/evidence_status.py"} <= shown
    assert (
        not {
            "standalone.py",
            "docs/archive/historical_copy.py",
            "docs/audits/historical_copy.py",
            "项目代码/src/historical_copy.py",
        }
        & shown
    )
    lint = _run_ruff(tmp_path, check_args)
    assert lint.returncode == 0, lint.stdout + lint.stderr


@pytest.mark.parametrize("tree", ("src", "tests", "scripts"))
def test_each_declared_tree_catches_undefined_names(tmp_path: Path, tree: str) -> None:
    _make_project(tmp_path, bad_tree=tree)
    args = _ruff_command("check")
    result = _run_ruff(tmp_path, args)
    assert result.returncode == 1
    assert f"{tree}/" in result.stdout
    assert "F821" in result.stdout


def test_format_scope_matches_check_scope_and_passes(tmp_path: Path) -> None:
    _make_project(tmp_path)
    check_targets = _ruff_command("check")[-3:]
    format_args = _ruff_command("format")
    assert format_args[:2] == ["format", "--check"]
    assert format_args[2:] == check_targets
    result = _run_ruff(tmp_path, format_args)
    assert result.returncode == 0, result.stdout + result.stderr
