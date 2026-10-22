#!/usr/bin/env python3
"""JARVIS product CLI: distribution checks, explicit configuration and one-shot chat."""

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

VERSION = "0.1.0-b01"
COMMIT = "d97007c32d6340f7ba79ac1dfb5773cc00846448"
PROJECT = Path(__file__).resolve().parents[1]
VENDOR_REL = Path("components/agent_engine/upstream")
VENV_REL = Path("packaging/runtime/engine-venv")
RESOURCES = (
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "hermes_bootstrap.py",
    "hermes_cli/main.py",
    "hermes_constants.py",
    "tools",
    "agent",
    "skills",
    "web",
    "pm",
)


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def forbidden_roots(env: Mapping[str, str]) -> list[Path]:
    native = Path.home().resolve()
    roots = [native / ".hermes", native / "hermes-agent", native / ".config/hermes"]
    for key in ("HERMES_HOME", "HERMES_DIR", "HERMES_ROOT", "HERMES_REPO_ROOT"):
        if env.get(key):
            roots.append(Path(env[key]).expanduser().resolve())
    # Explicit read-only host boundary: never permit the active development installation.
    roots.append(Path("/Volumes/扩展硬盘/hermes").resolve())
    return roots


def resolve_home(value: str | None, env: Mapping[str, str], project: Path) -> Path:
    selected = value if value is not None else env.get("JARVIS_HOME")
    if selected is not None and not selected.strip():
        raise ValueError("JARVIS home 不能为空")
    raw = Path(selected).expanduser() if selected else Path.home() / ".jarvis"
    if not raw.is_absolute():
        raise ValueError("JARVIS home 必须是绝对路径")
    home = raw.resolve()
    if any(overlaps(home, root.resolve()) for root in forbidden_roots(env) + [project]):
        raise ValueError("拒绝 JARVIS home 指向或覆盖宿主 Hermes / 项目源码（含符号链接）")
    return home


def child_environment(home: Path, venv: Path, env: Mapping[str, str]) -> dict[str, str]:
    # No provider secrets, proxy paths, PYTHON*, host PATH or Hermes settings are inherited.
    child = {key: env[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ") if key in env}
    child.update(
        {
            "HOME": str(home),
            "JARVIS_HOME": str(home),
            "HERMES_HOME": str(home),
            "PATH": str(venv / "bin") + ":/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    return child


def tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("固定引擎源码不能含符号链接: " + str(path))
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            digest.update(
                relative.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest()
            )
            count += 1
    return digest.hexdigest(), count


PROBE = r"""
import sys, sysconfig, json, platform, os
from pathlib import Path
from importlib import metadata
from email.parser import Parser
venv = Path(sys.argv[1]).resolve()
forbidden = [Path(p).resolve() for p in json.loads(sys.argv[2])]

def safe(path):
    target = Path(path).resolve()
    return (target == venv or venv in target.parents) and not any(
        target == root or root in target.parents or target in root.parents for root in forbidden)

purelib = sysconfig.get_path('purelib')
packages, paths, errors = {}, [], []
# Check before enumeration or reads; never traverse a borrowed host directory.
if not safe(purelib):
    errors.append('purelib 超出独立 venv 边界')
else:
    # Do not follow directory links, including cycles. Resolve targets without reading them.
    for directory, dirs, files in os.walk(purelib, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not safe(path):
                errors.append('site-packages 链接超出独立 venv 边界')
    if not errors:
        for dist in metadata.distributions(path=[purelib]):
            directory = Path(dist._path)
            if not safe(directory):
                errors.append('发行元数据目录超出独立 venv 边界')
                continue
            # Match PathDistribution's metadata fallback, recording the actual file read.
            for filename in ('METADATA', 'PKG-INFO'):
                path = directory / filename
                if not safe(path):
                    errors.append('发行元数据文件超出独立 venv 边界')
                    break
                if not path.is_file():
                    continue
                text = path.read_text(encoding='utf-8')
                paths.append({'directory': str(directory), 'file': str(path)})
                if text:
                    fields = Parser().parsestr(text)
                    name = fields.get('Name')
                    if name:
                        packages[name.lower().replace('_', '-')] = fields.get('Version')
                    break
print(json.dumps({
    'version': list(sys.version_info[:3]), 'prefix': sys.prefix,
    'base_prefix': sys.base_prefix, 'executable': sys.executable,
    'purelib': purelib, 'packages': packages, 'metadata_paths': paths,
    'metadata_errors': errors, 'platform': sys.platform, 'machine': platform.machine()
}))
"""


def check(
    project: Path = PROJECT, env: Mapping[str, str] | None = None, home_value: str | None = None
) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    report: dict[str, Any] = {
        "product": "JARVIS",
        "version": VERSION,
        "upstream_commit": COMMIT,
        "scope": "发行资源与解释器检查；不启动对话、控制服务或上游入口",
        "launch_ready": False,
        "checks": [],
        "ok": False,
    }

    def record(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})

    record(
        "control_python",
        sys.version_info[:2] == (3, 12),
        "入口控制端要求 Python 3.12；实际 " + ".".join(map(str, sys.version_info[:3])),
    )
    try:
        home = resolve_home(home_value, env, project)
        report["home"] = str(home)
        record("home", True, "独立产品目录（检查不创建）: " + str(home))
    except (ValueError, OSError, RuntimeError) as exc:
        record("home", False, str(exc))
        return report
    vendor = project / VENDOR_REL
    venv = project / VENV_REL
    python = venv / "bin/python3"
    for name, path in (("vendor_path", vendor), ("engine_venv", venv)):
        safe = path.resolve() == path and not any(
            overlaps(path.resolve(), r) for r in forbidden_roots(env)
        )
        record(
            name,
            safe and path.is_dir(),
            ("固定目录: " if safe else "拒绝目录回退/符号链接: ") + str(path),
        )
    if not report["checks"][-2]["ok"]:
        return report
    for relative in RESOURCES:
        path = vendor / relative
        record("resource:" + relative, path.exists() and not path.is_symlink(), str(path))
    expected: dict[str, Any] = {}
    try:
        expected = json.loads(
            (project / "packaging/VENDOR_MANIFEST.json").read_text(encoding="utf-8")
        )
        if (
            not isinstance(expected, dict)  # pyright: ignore[reportUnnecessaryIsInstance]
            or not isinstance(expected.get("required_metadata"), list)
            or not all(isinstance(name, str) for name in expected["required_metadata"])
        ):
            raise ValueError("固定源码清单格式无效")
        digest, count = tree_digest(vendor)
        record(
            "vendor_integrity",
            expected["commit"] == COMMIT
            and expected["tree_sha256"] == digest
            and expected["file_count"] == count,
            "固定源码 SHA-256=" + digest + "；文件数=" + str(count),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        record("vendor_integrity", False, "固定源码清单缺失或无效: " + str(exc))
    if not report["checks"][3]["ok"]:
        record("engine_python", False, "独立引擎环境未构建；不调用安装器，不使用宿主解释器")
        return report
    cfg = venv / "pyvenv.cfg"
    try:
        values = dict(line.split("=", 1) for line in cfg.read_text().splitlines() if "=" in line)
        isolated = {k.strip(): v.strip().lower() for k, v in values.items()}.get(
            "include-system-site-packages"
        ) == "false"
        record("venv_isolation", isolated, str(cfg))
    except OSError as exc:
        record("venv_isolation", False, str(exc))
    safe_python = (
        python.is_file()
        and os.access(python, os.X_OK)
        and not any(overlaps(python.resolve(), r) for r in forbidden_roots(env))
    )
    record(
        "engine_python",
        safe_python,
        str(python) if safe_python else "缺少独立解释器或解释器指向宿主 Hermes；不调用安装器",
    )
    if not safe_python or not all(item["ok"] for item in report["checks"]):
        return report
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-S",
                "-B",
                "-c",
                PROBE,
                str(venv),
                json.dumps([str(r) for r in forbidden_roots(env)]),
            ],
            env=child_environment(home, venv, env),
            cwd=project,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise ValueError(
                "解释器探针退出 " + str(result.returncode) + ": " + result.stderr[:500]
            )
        runtime = json.loads(result.stdout)
        report["runtime"] = runtime
        record(
            "engine_version",
            runtime["version"][:2] == [3, 14],
            "引擎要求 Python 3.14；实际 " + str(runtime["version"]),
        )
        record(
            "engine_prefix",
            Path(runtime["prefix"]).resolve() == venv
            and runtime["prefix"] != runtime["base_prefix"],
            "独立 sys.prefix=" + runtime["prefix"],
        )
        record(
            "engine_base",
            not any(
                overlaps(Path(runtime["base_prefix"]).resolve(), r) for r in forbidden_roots(env)
            ),
            "基础解释器=" + runtime["base_prefix"],
        )

        # Revalidate the paths returned by the child before counting any package names.
        def metadata_path_safe(value: str) -> bool:
            path = Path(value).resolve()
            return (path == venv or venv in path.parents) and not any(
                overlaps(path, root) for root in forbidden_roots(env)
            )

        metadata_safe = metadata_path_safe(runtime["purelib"]) and not runtime["metadata_errors"]
        for entry in runtime["metadata_paths"]:
            metadata_safe = (
                metadata_safe
                and metadata_path_safe(entry["directory"])
                and metadata_path_safe(entry["file"])
            )
        record(
            "metadata_boundary",
            metadata_safe,
            "purelib 与实际读取的发行元数据均在独立 venv 内"
            if metadata_safe
            else "拒绝 purelib / 发行元数据 / site-packages 链接借用 venv 外部路径",
        )
        # Metadata only; no upstream or third-party imports, .pth execution, dependency installs.
        required = expected["required_metadata"]
        missing = [name for name in required if name not in runtime["packages"]]
        record(
            "core_metadata",
            metadata_safe and not missing,
            "元数据路径边界无效，不能计入核心包"
            if not metadata_safe
            else (
                "缺少核心发行包: " + ", ".join(missing)
                if missing
                else "核心包元数据存在；完整依赖与导入验收仍须后续构建执行"
            ),
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        record("engine_probe", False, str(exc))
    report["ok"] = all(item["ok"] for item in report["checks"])
    return report


class ConfigParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError("invalid_arguments")


def reject_normal_loopback(values: Mapping[str, object], test_port: int | None) -> None:
    """Additional CLI policy: HTTPS loopback must not bypass explicit test mode."""
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    if test_port is not None:
        return
    hostname = urlsplit(str(values["base_url"])).hostname or ""
    if hostname.rstrip(".").lower() == "localhost":
        raise ValueError("explicit test mode required")
    try:
        address = ip_address(hostname)
    except ValueError:
        return
    if address.is_loopback:
        raise ValueError("explicit test mode required")


def config_main(argv: Sequence[str]) -> int:
    import product_config as config
    import product_home as home

    parser = ConfigParser(prog="jarvis config", allow_abbrev=False)
    sub = parser.add_subparsers(dest="action", required=True, parser_class=ConfigParser)
    for action in ("check", "init"):
        command = sub.add_parser(action, allow_abbrev=False)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--test-port", type=int)
        command.add_argument("--expected-sha256")
        if action == "init":
            for name in ("model", "provider", "base-url", "api-mode", "reasoning-effort"):
                command.add_argument("--" + name, required=True)
            command.add_argument("--context-length", type=int, required=True)
    key = sub.add_parser("key-check", allow_abbrev=False)
    key.add_argument("--key-file", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.action == "key-check":
            if not args.key_file.is_absolute():
                raise config.ConfigSecurityError("absolute path required")
            config.read_key(key_file=args.key_file)
            result = {"ok": True, "key_file": str(args.key_file)}
        else:
            if not args.config.is_absolute() or not args.data_dir.is_absolute():
                raise config.ConfigSecurityError("absolute paths required")
            if args.test_port is not None and not 1 <= args.test_port <= 65535:
                raise config.ConfigError("invalid test port")
            root = home.validate_root(args.data_dir, legacy=Path("/nonexistent-legacy"))
            home.private_directory(root)
            if args.action == "init":
                values = {name: getattr(args, name) for name in config.CONFIG_FIELDS}
                reject_normal_loopback(values, args.test_port)
                # Validate existing lock before write_config can chmod/open it.
                lock = config._safe_path(  # pyright: ignore[reportPrivateUsage]
                    args.config.with_name("." + args.config.name + ".lock")
                )
                if lock.exists():
                    config._check_owner_mode(lock, directory=False)  # pyright: ignore[reportPrivateUsage]
                snapshot = config.write_config(
                    args.config,
                    values,
                    expected_sha256=args.expected_sha256,
                    test_port=args.test_port,
                )
            else:
                snapshot = config.read_config(args.config, test_port=args.test_port)
                if args.expected_sha256 is not None and args.expected_sha256 != snapshot.sha256:
                    raise config.ConfigConflictError("configuration changed")
            reject_normal_loopback(snapshot.values, args.test_port)
            binding = config.bind_route(snapshot.values, root, test_port=args.test_port)
            result = {
                "ok": True,
                "config": str(snapshot.path),
                "data_dir": str(root),
                "config_sha256": binding.config_sha256,
                "data_root_sha256": binding.data_root_sha256,
                "binding_sha256": binding.binding_sha256,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (config.ConfigError, OSError, ValueError, TypeError):
        print(json.dumps({"ok": False}), file=sys.stderr)
        return 2


def chat_main(argv: Sequence[str], *, query: bool = False) -> int:
    """Forward to the product launcher; it owns validation, keys and timeouts."""
    parser = ConfigParser(
        prog="jarvis query-attempt" if query else "jarvis chat", allow_abbrev=False
    )
    parser.add_argument("--data-dir", required=True)
    if query:
        parser.add_argument("--attempt-id", required=True)
    else:
        parser.add_argument("--config", required=True)
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--key-file")
        source.add_argument("--key-fd", type=int)
        parser.add_argument("--prompt", required=True)
        for name in ("session", "attempt-id", "expected-sha256", "expected-binding-sha256"):
            parser.add_argument("--" + name)
        parser.add_argument("--test-port", type=int)
    process: subprocess.Popen[str] | None = None
    handlers: dict[int, Any] = {}
    try:
        args = parser.parse_args(argv)
        command = [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("chat_launcher.py")),
            "--data-dir",
            args.data_dir,
        ]
        descriptors: tuple[int, ...] = ()
        if query:
            command.extend(["--query-attempt", args.attempt_id])
        else:
            command.extend(["--config", args.config, "--prompt", args.prompt])
            if args.key_fd is not None:
                descriptors = (args.key_fd,)
                command.extend(["--key-fd", str(args.key_fd)])
            else:
                command.extend(["--key-file", args.key_file])
            for name in ("session", "attempt_id", "expected_sha256", "expected_binding_sha256"):
                value = getattr(args, name)
                if value is not None:
                    command.extend(["--" + name.replace("_", "-"), value])
            if args.test_port is not None:
                command.extend(["--test-local-port", str(args.test_port)])
        # Preserve only locale and path-denial hints; no provider keys or Python settings.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TZ",
                "HERMES_HOME",
                "HERMES_ROOT",
                "HERMES_DIR",
                "HERMES_REPO_ROOT",
            }
        }
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=PROJECT,
            env=environment,
            pass_fds=descriptors,
            start_new_session=True,
        )

        def interrupt(signum: int, frame: Any) -> None:
            # SIGINT invokes the launcher's cleanup, including its independent engine group.
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupt)
        # No competing CLI timer/retry: the current launcher owns its 65s engine timeout.
        stdout, _ = process.communicate()
        code = process.returncode
        if code == 0:
            sys.stdout.write(stdout)
        else:
            print(json.dumps({"ok": False}), file=sys.stderr)
        return code
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"ok": False}), file=sys.stderr)
        return 2
    finally:
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                process.wait()


WEB_SHUTDOWN_TIMEOUT = 75.0  # Existing server drains for 70s; launcher owns its 65s timer.


def web_main(argv: Sequence[str]) -> int:
    """Forward explicit paths/FDs to the existing loopback server, streaming its URL."""
    parser = ConfigParser(
        prog="jarvis web", description="启动 JARVIS 中文本机网页", allow_abbrev=False
    )
    parser.add_argument("--config", required=True, type=Path, help="自有 0600 配置绝对路径")
    parser.add_argument("--data-dir", required=True, type=Path, help="专用 0700 数据目录绝对路径")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", type=Path, help="专用 0600 凭据文件绝对路径")
    source.add_argument("--key-fd", type=int, help="已打开的凭据 FD（可用 stdin 0）")
    parser.add_argument("--port", type=int, default=0, help="网页端口 0–65535；默认 0 自动分配")
    parser.add_argument("--test-port", type=int, help="仅用于明确 FAKE- 凭据的本机模拟 provider")
    parser.add_argument(
        "--acceptance-mode",
        action="store_true",
        help="仅验收：允许 /tmp/jarvis-acceptance-*/ 内的专用数据目录；非正式发布",
    )
    process: subprocess.Popen[bytes] | None = None
    handlers: dict[int, Any] = {}
    stopping = False
    deadline: float | None = None
    try:
        args = parser.parse_args(argv)
        if (
            not args.config.is_absolute()
            or not args.data_dir.is_absolute()
            or (args.key_file is not None and not args.key_file.is_absolute())
            or (args.key_fd is not None and args.key_fd < 0)
            or not 0 <= args.port <= 65535
            or (args.test_port is not None and not 1 <= args.test_port <= 65535)
        ):
            raise ValueError("invalid_arguments")
        command = [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).with_name("web_server.py")),
            "--config",
            str(args.config),
            "--data-dir",
            str(args.data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ]
        descriptors: tuple[int, ...] = ()
        if args.key_fd is not None:
            descriptors = (args.key_fd,)
            command.extend(["--key-fd", str(args.key_fd)])
        else:
            command.extend(["--key-file", str(args.key_file)])
        if args.test_port is not None:
            command.extend(["--test-local-port", str(args.test_port)])
        if args.acceptance_mode:
            command.append("--acceptance-mode")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TZ",
                "HERMES_HOME",
                "HERMES_ROOT",
                "HERMES_DIR",
                "HERMES_REPO_ROOT",
            }
        }

        def interrupt(signum: int, frame: Any) -> None:
            nonlocal stopping
            stopping = True

        # Install before spawning so early cancellation cannot orphan the new child.
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, interrupt)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=PROJECT,
            env=environment,
            pass_fds=descriptors,
            start_new_session=True,
        )
        assert process.stdout is not None
        pending = b""
        started = False
        unknown = False
        eof = False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not eof or process.poll() is None:
                if stopping and deadline is None:
                    deadline = time.monotonic() + WEB_SHUTDOWN_TIMEOUT
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGINT)
                        except ProcessLookupError:
                            pass
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, WEB_SHUTDOWN_TIMEOUT)
                for _key, _ in selector.select(timeout=0.1):
                    chunk = os.read(process.stdout.fileno(), 1024)
                    if not chunk:
                        selector.unregister(process.stdout)
                        eof = True
                        if pending:
                            raise ValueError("invalid_stdout")
                        continue
                    pending += chunk
                    if len(pending) > 4096:
                        raise ValueError("invalid_stdout")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        value: Any = json.loads(line)
                        if (
                            not started
                            and isinstance(value, dict)
                            and set(value) == {"url", "scope"}
                        ):
                            url = value["url"]
                            if (
                                not isinstance(url, str)
                                or re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", url) is None
                                or not 1 <= int(url.rsplit(":", 1)[1]) <= 65535
                                or value["scope"] != "loopback development only"
                            ):
                                raise ValueError("invalid_stdout")
                            print(
                                json.dumps({"url": url, "scope": "loopback development only"}),
                                flush=True,
                            )
                            started = True
                        elif (
                            started
                            and not unknown
                            and value == {"state": "unknown", "retry_allowed": False}
                        ):
                            print('{"state":"unknown","retry_allowed":false}', flush=True)
                            unknown = True
                        else:
                            raise ValueError("invalid_stdout")
        code = process.wait(timeout=5)
        if code != 0 or not started or unknown:
            print(json.dumps({"ok": False}), file=sys.stderr)
            return code if code != 0 else 2
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, subprocess.SubprocessError):
        print(json.dumps({"ok": False}), file=sys.stderr)
        return 2
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    if deadline is None:
                        deadline = time.monotonic() + WEB_SHUTDOWN_TIMEOUT
                        os.killpg(process.pid, signal.SIGINT)
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                except ProcessLookupError:
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


def self_evaluate() -> dict[str, object]:
    """Evaluate public distribution policy; do not run check() or CLI actions."""
    from datetime import datetime, timezone

    resources = list(RESOURCES)
    child = child_environment(
        Path("/jarvis-evaluation-not-created"), Path("/jarvis-evaluation-venv"), {}
    )
    checks = {
        "safe_resource_manifest": bool(resources)
        and len(set(resources)) == len(resources)
        and all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in resources),
        "relative_fixed_layout": not VENDOR_REL.is_absolute()
        and ".." not in VENDOR_REL.parts
        and not VENV_REL.is_absolute()
        and ".." not in VENV_REL.parts,
        "pinned_commit": bool(re.fullmatch(r"[0-9a-f]{40}", COMMIT)),
        "sanitized_environment": set(child)
        == {
            "HOME",
            "JARVIS_HOME",
            "HERMES_HOME",
            "PATH",
            "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE",
            "UV_OFFLINE",
            "PIP_NO_INDEX",
        },
    }
    return {
        "module": "jarvis",
        "domain": "CLI distribution manifests isolated child environment entrypoint routing",
        "current_version": VERSION,
        "known_limitations": [
            "Distribution checks require control Python 3.12; the embedded engine requires Python 3.14.",
            "Policy checks do not verify installed resources, credentials, child readiness or production delivery.",
        ],
        "improvement_directions": [
            "Keep CLI dispatch and distribution checks separate from credential-bearing execution."
        ],
        "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
        "resources": resources,
        "checks": checks,
        "ok": all(checks.values()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] == "web":
        return web_main(supplied[1:])
    if supplied and supplied[0] == "files":
        from file_task_chain import main as files_main

        return files_main(supplied[1:])
    if supplied and supplied[0] == "task":
        from task_api import main as task_main

        return task_main(supplied[1:])
    if supplied and supplied[0] in {"chat", "query-attempt"}:
        return chat_main(supplied[1:], query=supplied[0] == "query-attempt")
    if supplied and supplied[0] == "config":
        return config_main(supplied[1:])
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="JARVIS 独立发行入口；显式 web/chat/config/task/files/query-attempt 子命令可用",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["version", "check"],
        default="check",
        help="version 查看版本；check 检查发行资源",
    )
    parser.add_argument("--version", action="store_true", help="显示 JARVIS 入口版本")
    parser.add_argument("--check", action="store_true", help="仅检查，不启动引擎")
    parser.add_argument("--home", help="产品数据目录绝对路径；默认 JARVIS_HOME 或 ~/.jarvis")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    if args.version and args.check:
        parser.error("--version 与 --check 不能同时使用")
    if args.version or (args.command == "version" and not args.check):
        value = {
            "product": "JARVIS",
            "version": VERSION,
            "upstream_commit": COMMIT,
            "launch_ready": False,
        }
        print(
            json.dumps(value, ensure_ascii=False)
            if args.json
            else "JARVIS " + VERSION + "（固定引擎 " + COMMIT + "；显式 chat 子命令可用）"
        )
        return 0
    report = check(home_value=args.home)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            print(("通过" if item["ok"] else "失败") + " [" + item["name"] + "] " + item["detail"])
        print("检查通过" if report["ok"] else "发行检查未通过；请先按构建计划准备缺项")
        print("检查通过仅证明检查范围；对话启动、完整依赖、Web/RPC 与生产发布均未验收。")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
