"""故障注入：模型调用结果未知后，同一数据根不得再派生新进程。

对应整改项 A5（取消/崩溃后不重复办事）。这里注入的是最脏的一种失败：
子进程根本没起来（Popen 直接抛），但占位记录已写成 started/unknown。
产品必须拒绝继续发送，直到人工核对并 resolve。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packaging"))

import chat_launcher as launcher  # noqa: E402

FAKE_KEY = "FAKE-NONSECRET-KEY"


PORT = 8000


def config() -> dict:
    # base_url 的端口必须与 test_port 一致，否则绑定校验会先拒（与产品行为一致）。
    return {
        "model": "fake-test-model",
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{PORT}/v1",
        "api_mode": "chat_completions",
        "context_length": 256000,
        "reasoning_effort": "high",
    }


@pytest.fixture()
def root():
    base = Path(tempfile.mkdtemp(prefix="fi-"))
    return launcher.prepare_root(base / "data")


def test_unknown_outcome_blocks_new_launch_until_resolved(root):
    boom = RuntimeError("child never started")
    with patch.object(launcher.subprocess, "Popen", side_effect=boom):
        with pytest.raises(launcher.LauncherError) as first:
            launcher.launch(config(), FAKE_KEY, "hello", root, None, test_port=PORT)
    assert "结果未知" in str(first.value)

    pending = launcher.unresolved_attempts(root)
    assert len(pending) == 1, pending

    # 未核对前：新的发送必须被拒绝（不是靠调用方自觉）。
    with patch.object(launcher.subprocess, "Popen") as spawned:
        with pytest.raises(launcher.LauncherError) as second:
            launcher.launch(config(), FAKE_KEY, "another", root, None, test_port=PORT)
        spawned.assert_not_called()
    assert "结果未知" in str(second.value)

    # 人工核对后解除阻塞，且记录留痕。
    path = launcher.resolve_attempt(root, pending[0]["attempt_id"], "确认子进程未启动")
    record = path.read_text("utf-8")
    assert '"resolved"' in record
    assert launcher.unresolved_attempts(root) == []


def test_explicit_continue_still_logs_and_proceeds(root):
    boom = RuntimeError("child never started")
    with patch.object(launcher.subprocess, "Popen", side_effect=boom):
        with pytest.raises(launcher.LauncherError):
            launcher.launch(config(), FAKE_KEY, "hello", root, None, test_port=PORT)

    with patch.object(launcher.subprocess, "Popen", side_effect=RuntimeError("still down")):
        with pytest.raises(launcher.LauncherError):
            launcher.launch(
                config(), FAKE_KEY, "next", root, None, test_port=PORT, after_unknown="continue"
            )
    # 第二次确实尝试了发送：未结案数量变成 2（说明 continue 是显式放行，不是静默忽略）。
    assert len(launcher.unresolved_attempts(root)) == 2
