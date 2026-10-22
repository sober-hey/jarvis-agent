"""Code search backed by ripgrep (no shell or Python regex translation)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


def self_evaluate() -> dict[str, Any]:
    """Describe search constraints without locating rg or searching user files."""
    from datetime import UTC, datetime

    return {
        "module": "code_search",
        "domain": "ripgrep-backed literal-path code search and matching-line counts",
        "current_version": "sorted rg subprocess search with JSON content and NUL-delimited filenames",
        "known_limitations": [
            "Requires an executable ripgrep; the bundled fallback path is specific to darwin-arm64.",
            "Result limits truncate parsed results only; rg output is captured in full before limiting, so memory use is not bounded by limit.",
            "Search follows rg ignore, hidden and binary-file defaults and has a fixed 60-second subprocess timeout.",
        ],
        "improvement_directions": [
            "Stream rg output with explicit byte/result budgets while retaining requested context lines.",
            "Expose timeout and ignore/hidden-file policy as explicit search options.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class CodeSearch:
    """Use an explicit rg executable, PATH, or Hermes's bundled ripgrep.

    Searches retain ripgrep's normal ignore/hidden/binary-file rules. Results
    follow sorted file paths. Invalid expressions and filesystem errors raise
    RuntimeError; no matches is a normal empty result. Commands time out after
    60 seconds rather than leaving an unbounded child process running.
    """

    def __init__(self, rg_path: str | None = None):
        candidates = (
            [rg_path]
            if rg_path is not None
            else [
                shutil.which("rg"),
                str(Path.home() / ".hermes/tools/ripgrep-15.2.0-darwin-arm64/rg"),
            ]
        )
        for candidate in candidates:
            if candidate:
                executable = shutil.which(candidate)
                if executable:
                    self.rg_path = os.path.abspath(executable)
                    break
        else:
            raise FileNotFoundError("ripgrep executable not found; install rg or set rg_path")

    def _run(self, arguments: list[str], *, cwd: str | None = None) -> bytes:
        result = subprocess.run(
            [self.rg_path, "--no-config", "--sort", "path", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
            cwd=cwd,
        )
        if result.returncode not in (0, 1):
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ripgrep failed (exit {result.returncode}): {detail}")
        return result.stdout

    @staticmethod
    def _text(field: dict[str, str], *, filename: bool = False) -> str:
        if "text" in field:
            return field["text"]
        raw = base64.b64decode(field["bytes"])
        return os.fsdecode(raw) if filename else raw.decode("utf-8", errors="replace")

    def search(
        self,
        pattern: str,
        path: str = ".",
        file_glob: str = "",
        context_lines: int = 0,
        limit: int = 50,
        output_mode: str = "content",
    ) -> list[dict[str, Any]]:
        """Return one result per matching line, capped globally by limit.

        Every row has file, line (1-based), content (without its line ending),
        and context. With context_lines > 0, context contains the surrounding
        lines in source order, including the matching line itself. No shell
        interpolation is performed; patterns beginning with '-' are safe.
        files_only returns the same schema with line=0 and empty content/context.
        count returns line=0, a string content count, and an integer count key.
        """
        if output_mode not in ("content", "files_only", "count"):
            raise ValueError("output_mode must be 'content', 'files_only', or 'count'")
        if context_lines < 0 or limit < 0:
            raise ValueError("context_lines and limit must be non-negative")
        if limit == 0:
            return []
        if output_mode == "files_only":
            arguments = ["--files-with-matches", "--null", "--color", "never"]
            if file_glob:
                arguments += ["--glob", file_glob]
            output = self._run([*arguments, "-e", pattern, "--", os.fspath(path)])
            return [
                {"file": os.fsdecode(filename), "line": 0, "content": "", "context": []}
                for filename in output.split(b"\0")
                if filename
            ][:limit]
        if output_mode == "count":
            return [
                {"file": filename, "line": 0, "content": str(total), "context": [], "count": total}
                for filename, total in list(self.count(pattern, path, file_glob).items())[:limit]
            ]
        arguments = ["--json", "--context", str(context_lines)]
        if file_glob:
            arguments += ["--glob", file_glob]
        arguments += ["-e", pattern, "--", os.fspath(path)]
        output = self._run(arguments)
        rows: list[dict[str, Any]] = []
        lines: dict[str, dict[int, str]] = {}
        for event in output.splitlines():
            item = json.loads(event)
            if item["type"] not in ("match", "context"):
                continue
            data = item["data"]
            filename = self._text(data["path"], filename=True)
            number = data["line_number"]
            content = self._text(data["lines"]).removesuffix("\n").removesuffix("\r")
            lines.setdefault(filename, {})[number] = content
            if item["type"] == "match" and len(rows) < limit:
                rows.append({"file": filename, "line": number, "content": content, "context": []})
        if context_lines:
            for row in rows:
                available = lines[row["file"]]
                row["context"] = [
                    available[number]
                    for number in range(
                        max(1, row["line"] - context_lines),
                        row["line"] + context_lines + 1,
                    )
                    if number in available
                ]
        return rows

    def find_files(self, pattern: str, path: str = ".", limit: int = 50) -> list[str]:
        """Find files using rg glob syntax, with a global result limit."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        root = os.fspath(path)
        directory = os.path.isdir(root)
        output = self._run(
            ["--files", "--null", "--glob", pattern, "--", "." if directory else root],
            cwd=root if directory else None,
        )
        filenames = [os.fsdecode(name) for name in output.split(b"\0") if name][:limit]
        return [
            os.path.join(root, name.removeprefix("./")) if directory else name for name in filenames
        ]

    def count(self, pattern: str, path: str = ".", file_glob: str = "") -> dict[str, int]:
        """Return each matching file's matching-line count (not occurrences).

        As with rg --count, files with zero matches are omitted. NUL-delimited
        filenames permit colons, newlines, and non-UTF-8 paths unambiguously.
        """
        arguments = ["--count", "--with-filename", "--null", "--color", "never"]
        if file_glob:
            arguments += ["--glob", file_glob]
        output = self._run([*arguments, "-e", pattern, "--", os.fspath(path)])
        counts: dict[str, int] = {}
        offset = 0
        while offset < len(output):
            end = output.index(b"\0", offset)
            filename = os.fsdecode(output[offset:end])
            number_end = output.index(b"\n", end + 1)
            counts[filename] = int(output[end + 1 : number_end])
            offset = number_end + 1
        return counts
