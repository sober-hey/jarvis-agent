#!/usr/bin/env python3
"""JARVIS 黑盒探活与资源阈值检查。

判定的是"用户能不能用"，不是"进程在不在"：
- 语音网关端口是否可连（TCP 握手，不发协议帧）
- 网关最近是否真的接过设备（日志里近 N 分钟有 hello）
- 守护进程心跳文件是否新鲜
- 记忆库大小、WAL 体积、磁盘剩余
- 进程 RSS 是否超过阈值

用法：
  healthcheck.py            # 打印报告，异常时退出码 1
  healthcheck.py --json     # 机器可读
退出码：0 正常，1 有异常，2 无法判定（例如日志不存在）
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))
GATEWAY_PORT = int(os.environ.get("JARVIS_GATEWAY_PORT", "8765"))
MIND_DB = HOME / "loop-data" / "mind.db"
GATEWAY_LOG = HOME / "voice-gateway.log"
HEARTBEAT = HOME / "loop-data" / "heartbeat.json"

MAX_DB_GB = float(os.environ.get("JARVIS_MAX_DB_GB", "8"))
MAX_WAL_MB = float(os.environ.get("JARVIS_MAX_WAL_MB", "512"))
MIN_DISK_GB = float(os.environ.get("JARVIS_MIN_DISK_GB", "20"))
MAX_RSS_MB = float(os.environ.get("JARVIS_MAX_RSS_MB", "2048"))
HELLO_WINDOW_MIN = int(os.environ.get("JARVIS_HELLO_WINDOW_MIN", "30"))


def tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def recent_hello(minutes: int) -> bool | None:
    if not GATEWAY_LOG.exists():
        return None
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    try:
        # 只读尾部，避免整份日志进内存
        with GATEWAY_LOG.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 2_000_000))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if "hello device=" not in line:
            continue
        stamp = line[:19]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        return when >= cutoff
    return False


def heartbeat_age() -> float | None:
    if not HEARTBEAT.exists():
        return None
    return time.time() - HEARTBEAT.stat().st_mtime


def process_rss() -> dict[str, float]:
    """按命令行匹配（python 进程的 comm 只有 python3，认不出是谁）。"""
    out: dict[str, float] = {}
    for marker in ("jarvis_daemon.py", "voice_gateway.py", "JarvisMacApp"):
        try:
            pids = (
                subprocess.run(["pgrep", "-f", marker], capture_output=True, timeout=5)
                .stdout.decode()
                .split()
            )
        except (OSError, subprocess.SubprocessError):
            continue
        total = 0.0
        for pid in pids:
            try:
                rss = (
                    subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, timeout=5)
                    .stdout.decode()
                    .strip()
                )
                total += float(rss) / 1024
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
        if total:
            out[marker] = total
    return out


def check() -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    report: dict[str, Any] = {"at": datetime.now().astimezone().isoformat(timespec="seconds")}

    report["gateway_port"] = tcp_ok("127.0.0.1", GATEWAY_PORT)
    if not report["gateway_port"]:
        problems.append(f"语音网关 {GATEWAY_PORT} 端口连不上")

    hello = recent_hello(HELLO_WINDOW_MIN)
    report["device_hello_within_min"] = hello
    if hello is False:
        problems.append(f"近 {HELLO_WINDOW_MIN} 分钟没有设备连上网关")

    age = heartbeat_age()
    report["heartbeat_age_s"] = None if age is None else round(age, 1)
    if age is not None and age > 900:
        problems.append(f"守护进程心跳已停 {int(age)} 秒")

    if MIND_DB.exists():
        report["db_gb"] = round(MIND_DB.stat().st_size / 1e9, 2)
        wal = Path(str(MIND_DB) + "-wal")
        report["wal_mb"] = round(wal.stat().st_size / 1e6, 1) if wal.exists() else 0
        if report["db_gb"] > MAX_DB_GB:
            problems.append(f"记忆库 {report['db_gb']}GB 超过阈值 {MAX_DB_GB}GB")
        if report["wal_mb"] > MAX_WAL_MB:
            problems.append(f"WAL {report['wal_mb']}MB 超过阈值 {MAX_WAL_MB}MB（可能有长读事务）")

    try:
        free_gb = os.statvfs(HOME).f_bavail * os.statvfs(HOME).f_frsize / 1e9
        report["disk_free_gb"] = round(free_gb, 1)
        if free_gb < MIN_DISK_GB:
            problems.append(f"磁盘剩余 {report['disk_free_gb']}GB 低于阈值 {MIN_DISK_GB}GB")
    except OSError:
        report["disk_free_gb"] = None

    rss = process_rss()
    report["rss_mb"] = {k: round(v) for k, v in rss.items()}
    for name, value in rss.items():
        if value > MAX_RSS_MB:
            problems.append(f"{name} 常驻内存 {round(value)}MB 超过阈值 {MAX_RSS_MB}MB")

    return problems, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    problems, report = check()
    report["problems"] = problems
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for key, value in report.items():
            if key != "problems":
                print(f"{key}: {value}")
        if problems:
            print("\n异常：")
            for item in problems:
                print(f"- {item}")
        else:
            print("\n正常")
    if problems:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
