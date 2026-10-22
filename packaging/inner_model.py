"""Single-call mind adapter using the existing launcher's verified FD bootstrap."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import chat_launcher as launcher
from context_limits import INNER_CONTEXT_BYTE_LIMIT


def self_evaluate() -> dict[str, object]:
    """Describe the adapter without loading routes, credentials or model state."""
    return {
        "module": "inner_model",
        "domain": "single-call inner cognition model adapter credential pipe and response envelope validation",
        "current_version": "one-shot isolated launcher subprocess with FD credentials, inner persona mode and allowlisted result fields",
        "known_limitations": [
            "Each call starts a subprocess and waits for a complete response; there is no streaming or process reuse.",
            "Credential payloads are capped at 4096 bytes and captured stdout at 262144 bytes.",
            "Output size and literal key checks run after communicate has buffered the child output, not during capture.",
            "Literal key suppression is not general private-data redaction, and failed envelopes retain only an error class name.",
        ],
        "improvement_directions": [
            "Bound subprocess output while reading it and test timeout, overflow and cleanup failure paths.",
            "Measure one-shot startup overhead before designing streaming or lifecycle-safe process reuse.",
            "Define structured non-sensitive diagnostics and stronger disclosure policies without exposing route credentials.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class ProductRequestFailure(RuntimeError):
    """Only a validated exception class name crosses the subprocess boundary."""

    def __init__(self, error_type: object) -> None:
        self.error_type = (
            error_type
            if isinstance(error_type, str)
            and error_type.isidentifier()
            and error_type.isascii()
            and len(error_type) <= 100
            else "UnknownProductError"
        )
        super().__init__(self.error_type)


class ProductModel:
    @staticmethod
    def self_evaluate() -> dict[str, object]:
        """Independent of model construction, runtime config and network access."""
        return self_evaluate()

    def __init__(self, config: dict[str, Any], key: str, root: Path) -> None:
        self.root = launcher.prepare_root(root)
        self.binding = launcher.checked_binding(config, root, None)
        self.key = key

    def __call__(self, prompt: str) -> dict[str, Any]:
        if self.key in prompt or len(prompt.encode("utf-8")) > INNER_CONTEXT_BYTE_LIMIT:
            raise ValueError("Unsafe or oversized prompt")
        route = {**dict(self.binding.values), "api_key": self.key}
        launcher.chat.validate_route(route)
        blob = json.dumps(
            {"route": route, "binding": launcher.binding_summary(self.binding)}
        ).encode()
        if len(blob) > 4096:
            raise ValueError("Credential pipe overflow")
        read_fd, write_fd = os.pipe()
        process: subprocess.Popen[str] | None = None
        command = [
            str(launcher.chat.engine.PYTHON),
            "-I",
            "-S",
            "-B",
            "-c",
            launcher.CHILD_BOOTSTRAP,
            str(Path(launcher.__file__).with_name("chat_entry.py")),
            "--credentials-fd",
            str(read_fd),
            "--data-dir",
            str(self.root),
        ]
        for protected in launcher.chat.home.protected_paths(self.root):
            command.extend(["--protected-host", str(protected)])
        try:
            os.write(write_fd, blob)
            os.close(write_fd)
            write_fd = -1
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=launcher.chat.engine.environment(self.root),
                cwd=self.root,
                pass_fds=(read_fd,),
                start_new_session=True,
            )
            # Explicit inner mode disables dialogue persona and hidden extraction.
            stdout, stderr = process.communicate(
                json.dumps({"prompt": prompt, "persona_mode": "inner"}),
                timeout=launcher.chat.launcher_timeout_seconds(route["reasoning_effort"]),
            )
            if self.key in stdout or self.key in stderr or len(stdout.encode()) > 262144:
                raise ValueError("Unsafe model output suppressed")
            raw: object = json.loads(stdout)
            if not isinstance(raw, dict):
                raise ValueError("Invalid model envelope")
            result = cast(dict[str, Any], raw)
            if process.returncode or result.get("ok") is not True:
                raise ProductRequestFailure(result.get("error_type"))
            return {
                name: result.get(name)
                for name in (
                    "text",
                    "usage",
                    "model_attempts",
                    "response_id",
                    "request_id",
                    "model",
                    "requested_model",
                    "provider",
                    "api_mode",
                    "reasoning_effort",
                    "memory_extraction",
                )
            }
        finally:
            if process is not None:
                launcher.kill_group(process)
                process.communicate(timeout=5)
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
