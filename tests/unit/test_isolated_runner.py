"""K4-004 offline draft: real fresh processes, not hostile-code sandbox proof."""

import importlib.util
import os
import time
from pathlib import Path

import pytest

from jarvis.isolated_runner import RunConfig
from jarvis.skill_manifest import ManifestRegistry, SkillCapability, SkillManifest


def emit_message(message: str) -> None:
    print(f"hello {message}")
    print(f"pid={os.getpid()}")


def registry() -> ManifestRegistry:
    manifests = ManifestRegistry()
    manifests.register(
        SkillManifest(
            id="skill-echo",
            name="echo",
            version="1.0.0",
            description="Offline fixture",
            trigger_patterns=[],
            capabilities={SkillCapability.READ_ONLY},
            dependencies=[],
            created_at=0,
            author="test",
        )
    )
    return manifests


def test_normal_handler_runs_in_fresh_process(tmp_path: Path) -> None:
    # Import in the test body so missing implementation is an assertion failure,
    # rather than a misleading collection error in the initial RED run.
    assert importlib.util.find_spec("jarvis.isolated_runner") is not None
    from jarvis.isolated_runner import IsolatedRunner, RunConfig, RunResult

    result = IsolatedRunner(registry(), draft_fixture=True).run(
        "echo",
        emit_message,
        ("world",),
        RunConfig(max_memory_mb=None, allowed_capabilities={SkillCapability.READ_ONLY}),
    )
    assert isinstance(result, RunResult)
    assert result.success, result.error
    assert result.return_code == 0
    assert result.error is None
    assert "hello world\n" in result.stdout
    assert f"pid={os.getpid()}\n" not in result.stdout
    assert result.stderr == ""
    assert result.wall_time_s > 0
    assert result.memory_mb is not None and result.memory_mb > 0


def slow_handler(marker: str) -> None:
    print("started", flush=True)
    # 3 秒远大于超时：子进程必须在中途被杀死，副作用永远写不出来。
    time.sleep(3.0)
    Path(marker).write_text("should never happen")


def test_timeout_really_stops_handler_before_late_side_effect(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner, RunConfig

    marker = tmp_path / "late"
    result = IsolatedRunner(registry(), draft_fixture=True).run(
        "echo",
        slow_handler,
        (str(marker),),
        RunConfig(
            # 0.15s 时子进程还在 import，stdout 为空（历史上因此偶发失败）；
            # 1.0s 足够看到 "started"，又远小于 handler 的 3s sleep。
            timeout_s=1.0,
            max_memory_mb=None,
            allowed_capabilities={SkillCapability.READ_ONLY},
        ),
    )
    assert not result.success
    assert "timeout" in (result.error or "").lower()
    assert "started" in result.stdout
    time.sleep(3.2)
    assert not marker.exists()


def test_resource_limit_installed_in_child_never_parent(tmp_path: Path) -> None:
    import resource

    from jarvis.isolated_runner import IsolatedRunner, RunConfig

    before = resource.getrlimit(resource.RLIMIT_AS)
    script = tmp_path / "limits.py"
    script.write_text(
        "import resource\nprint(resource.getrlimit(resource.RLIMIT_AS)[0], flush=True)\n"
    )
    result = IsolatedRunner(draft_fixture=True).run_subprocess(
        script, RunConfig(max_memory_mb=256, allowed_capabilities={SkillCapability.PROCESS_EXEC})
    )
    assert resource.getrlimit(resource.RLIMIT_AS) == before
    # Some macOS kernels reject ANY address-space limit. The draft must fail
    # closed BEFORE executing user code rather than silently drop the bound.
    if result.success:
        assert result.stdout == f"{256 * 1024**2}\n"
    else:
        assert "memory limit unavailable" in (result.error or "")
        assert result.stdout == ""


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "1"])
def test_invalid_timeout_cannot_start_process(timeout: object) -> None:
    from jarvis.isolated_runner import RunConfig

    with pytest.raises((TypeError, ValueError), match="timeout"):
        RunConfig(timeout_s=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("memory", [0, -1, True, 1.5, "256"])
def test_invalid_memory_cannot_start_process(memory: object) -> None:
    from jarvis.isolated_runner import RunConfig

    with pytest.raises((TypeError, ValueError), match="memory"):
        RunConfig(max_memory_mb=memory)  # type: ignore[arg-type]


def script_config(*, capture_output: bool = True, timeout_s: float = 10) -> RunConfig:
    return RunConfig(
        max_memory_mb=None,
        allowed_capabilities={SkillCapability.PROCESS_EXEC},
        capture_output=capture_output,
        timeout_s=timeout_s,
    )


def test_subprocess_captures_both_streams(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "output.py"
    script.write_text("import sys\nprint('输出')\nprint('diagnostic', file=sys.stderr)\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert result.success, result.error
    assert result.stdout == "输出\n"
    assert result.stderr == "diagnostic\n"
    assert result.return_code == 0


@pytest.mark.parametrize("code", [0, 7])
def test_subprocess_preserves_system_exit_code(tmp_path: Path, code: int) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "exit.py"
    script.write_text(f"raise SystemExit({code})\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert result.return_code == code
    assert result.success == (code == 0)


def test_output_is_bounded_and_run_fails_closed(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "flood.py"
    script.write_text("print('x' * (4 * 1024**2), flush=True)\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert not result.success
    assert "output limit" in (result.error or "")
    assert len(result.stdout.encode()) <= 1024**2


def marker_handler(marker: str) -> None:
    Path(marker).write_text("executed")


@pytest.mark.parametrize("name", ["echo", "missing"])
def test_capability_or_unknown_skill_denied_before_execution(tmp_path: Path, name: str) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    marker = tmp_path / "denied"
    result = IsolatedRunner(registry(), draft_fixture=True).run(
        name, marker_handler, (str(marker),), RunConfig(max_memory_mb=None)
    )
    assert not result.success
    assert result.return_code is None
    assert not marker.exists()
    assert "denied" in (result.error or "") or "unknown" in (result.error or "")


def explode() -> None:
    raise RuntimeError("fixture exception")


def test_handler_exception_cannot_propagate_to_caller() -> None:
    from jarvis.isolated_runner import IsolatedRunner

    result = IsolatedRunner(registry(), draft_fixture=True).run(
        "echo",
        explode,
        (),
        RunConfig(max_memory_mb=None, allowed_capabilities={SkillCapability.READ_ONLY}),
    )
    assert not result.success
    assert result.return_code == 1
    assert "RuntimeError: fixture exception" in (result.error or "")
    assert "RuntimeError: fixture exception" in result.stderr


def test_subprocess_capture_disabled_discards_both_streams(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "discard.py"
    script.write_text("import sys\nprint('private')\nprint('private', file=sys.stderr)\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(
        script, script_config(capture_output=False)
    )
    assert result.success, result.error
    assert result.stdout == result.stderr == ""
    assert capfd.readouterr() == ("", "")


def test_default_opt_in_and_raw_process_permission_are_required(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    with pytest.raises(ValueError, match="draft_fixture"):
        IsolatedRunner()
    marker = tmp_path / "raw-denied"
    script = tmp_path / "denied.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(
        script, RunConfig(max_memory_mb=None)
    )
    assert not result.success
    assert "PROCESS_EXEC" in (result.error or "")
    assert not marker.exists()


def test_child_environment_and_cwd_do_not_change_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    monkeypatch.setenv("JARVIS_TEST_SECRET", "synthetic-only")
    cwd = Path.cwd()
    script = tmp_path / "env.py"
    script.write_text(
        "import os\n"
        "print(os.environ.get('JARVIS_TEST_SECRET', 'absent'))\n"
        "print(os.getcwd())\n"
        "os.chdir('/')\n"
    )
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert result.success, result.error
    lines = result.stdout.splitlines()
    assert lines[0] == "absent"
    assert lines[1] != str(cwd)
    assert not Path(lines[1]).exists()  # private execution dir was removed
    assert os.environ["JARVIS_TEST_SECRET"] == "synthetic-only"
    assert Path.cwd() == cwd


async def asynchronous_handler() -> None:
    pass


def test_async_handler_is_rejected_not_silently_reported_successful() -> None:
    from jarvis.isolated_runner import IsolatedRunner

    result = IsolatedRunner(registry(), draft_fixture=True).run(
        "echo",
        asynchronous_handler,
        (),
        RunConfig(max_memory_mb=None, allowed_capabilities={SkillCapability.READ_ONLY}),
    )
    assert not result.success
    assert "synchronous" in (result.error or "")


def test_config_mutated_after_creation_fails_closed(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "config.py"
    script.write_text("print('must not execute')\n")
    config = script_config()
    config.allowed_capabilities.add("NETWORK")  # type: ignore[arg-type]
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, config)
    assert not result.success
    assert "allowed_capabilities" in (result.error or "")
    assert result.stdout == ""


def test_timeout_kills_same_group_grandchild(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    marker = tmp_path / "descendant"
    child = f"import time; from pathlib import Path; time.sleep(0.6); Path({str(marker)!r}).touch()"
    script = tmp_path / "children.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "print('descendant started', flush=True)\n"
        "time.sleep(10)\n"
    )
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config(timeout_s=0.2))
    assert not result.success
    assert "timeout" in (result.error or "")
    assert result.return_code == -9
    assert "descendant started" in result.stdout
    time.sleep(0.65)
    assert not marker.exists()


def test_crash_returns_failure_without_spoofed_success(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    script = tmp_path / "crash.py"
    script.write_text("import os\nos._exit(42)\n")
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert not result.success
    assert result.return_code == 42
    assert result.memory_mb is None
    assert result.error


def test_sqlite_skill_registry_can_supply_manifest(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner
    from jarvis.skill_registry import SkillRegistry

    manifest = registry().get("echo")
    assert manifest is not None
    with SkillRegistry(tmp_path / "registry.db", draft_fixture=True) as skills:
        skills.install(manifest)
        result = IsolatedRunner(skills, draft_fixture=True).run(
            "echo",
            emit_message,
            ("registry",),
            RunConfig(max_memory_mb=None, allowed_capabilities={SkillCapability.READ_ONLY}),
        )
    assert result.success, result.error
    assert "hello registry" in result.stdout


def test_oversized_worker_metadata_is_bounded(tmp_path: Path) -> None:
    from jarvis.isolated_runner import IsolatedRunner

    # A trusted-but-buggy fixture can overwrite its own status path. Parent must
    # reject huge records before JSON parsing; this is not authenticity proof.
    script = tmp_path / "metadata.py"
    script.write_text(
        "import json, pathlib, sys, os\n"
        "payload = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "pathlib.Path(payload['status']).write_text('x' * (2 * 1024**2))\n"
        "os._exit(0)\n"
    )
    result = IsolatedRunner(draft_fixture=True).run_subprocess(script, script_config())
    assert not result.success
    assert "metadata" in (result.error or "")
