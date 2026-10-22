"""Mocked branch coverage for the environment gate script.

Platform and SQLite failures below exercise mocked gate branches only; they are
not Linux/ECS measurements.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from jarvis.environment import EnvironmentReport, SQLiteProbe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_environment.py"


def _healthy_sqlite() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="wal",
        foreign_keys=True,
        synchronous=2,
        busy_timeout_ms=5000,
        integrity="ok",
    )


def _report(
    *,
    python_baseline: bool = True,
    platform_name: str = "Darwin",
    uv_available: bool = True,
    sqlite: SQLiteProbe | None = None,
) -> EnvironmentReport:
    return EnvironmentReport(
        python="3.12.14",
        python_baseline=python_baseline,
        platform=platform_name,
        machine="arm64",
        uv_available=uv_available,
        directory_fd_available=True,
        nofollow_available=True,
        sqlite=_healthy_sqlite() if sqlite is None else sqlite,
        production_ready=False,
    )


def _script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_environment_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sqlite_with_journal_delete() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="delete",
        foreign_keys=True,
        synchronous=2,
        busy_timeout_ms=5000,
        integrity="ok",
    )


def _sqlite_with_foreign_keys_off() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="wal",
        foreign_keys=False,
        synchronous=2,
        busy_timeout_ms=5000,
        integrity="ok",
    )


def _sqlite_with_zero_busy_timeout() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="wal",
        foreign_keys=True,
        synchronous=2,
        busy_timeout_ms=0,
        integrity="ok",
    )


def _sqlite_with_normal_synchronous() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="wal",
        foreign_keys=True,
        synchronous=1,
        busy_timeout_ms=5000,
        integrity="ok",
    )


def _sqlite_with_failed_integrity() -> SQLiteProbe:
    return SQLiteProbe(
        version="3.46.1",
        journal_mode="wal",
        foreign_keys=True,
        synchronous=2,
        busy_timeout_ms=5000,
        integrity="not ok",
    )


def _mock_inspect(report: EnvironmentReport) -> Callable[[Path], EnvironmentReport]:
    def inspect(_: Path) -> EnvironmentReport:
        return report

    return inspect


def test_main_healthy_darwin_returns_zero_and_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _script_module()
    report = _report()
    monkeypatch.setattr(module, "inspect_environment", _mock_inspect(report))

    assert module.main() == 0

    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize(
    "report_factory",
    [
        pytest.param(lambda: _report(python_baseline=False), id="python_baseline_false"),
        pytest.param(lambda: _report(uv_available=False), id="uv_false"),
        pytest.param(lambda: _report(platform_name="Linux"), id="linux"),
        pytest.param(lambda: _report(platform_name="Windows"), id="windows"),
        pytest.param(
            lambda: _report(sqlite=_sqlite_with_journal_delete()), id="journal_mode_delete"
        ),
        pytest.param(
            lambda: _report(sqlite=_sqlite_with_foreign_keys_off()), id="foreign_keys_false"
        ),
        pytest.param(
            lambda: _report(sqlite=_sqlite_with_zero_busy_timeout()), id="busy_timeout_zero"
        ),
        pytest.param(
            lambda: _report(sqlite=_sqlite_with_normal_synchronous()), id="synchronous_one"
        ),
        pytest.param(
            lambda: _report(sqlite=_sqlite_with_failed_integrity()), id="integrity_not_ok"
        ),
    ],
)
def test_main_unhealthy_mocked_gate_branches_return_one_and_print_report(
    report_factory: Callable[[], EnvironmentReport],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _script_module()
    report = report_factory()
    monkeypatch.setattr(module, "inspect_environment", _mock_inspect(report))

    assert module.main() == 1

    assert json.loads(capsys.readouterr().out) == report
