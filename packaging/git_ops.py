"""Small subprocess-backed Git API, without shell interpolation or GitPython.

Paths are interpreted relative to ``repo_root`` (the cwd at construction by
default). Git failures raise ``subprocess.CalledProcessError`` unless a method
explicitly returns errors/success. Mutating methods never push or force.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any


def self_evaluate() -> dict[str, Any]:
    """Describe Git API boundaries without opening a repository or running Git."""
    from datetime import UTC, datetime

    return {
        "module": "git_ops",
        "domain": "local Git status diff history staging commits and branch operations",
        "current_version": "argv-based Git CLI wrapper with literal pathspecs and NUL-delimited status/history",
        "known_limitations": [
            "Git subprocesses have no timeout or output-size budget; hooks and configured signing may block commits.",
            "add reports per-path errors after valid paths may already be staged; staging is not an all-or-nothing transaction.",
            "Repository mutations are not locked against concurrent clients, and remote fetch/push operations are not implemented.",
        ],
        "improvement_directions": [
            "Add configurable deadlines and bounded diagnostic output to Git invocations.",
            "Introduce explicit mutation coordination and partial-staging recovery reporting without force operations.",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }


class GitOps:
    """Operate on one repository using the installed Git executable."""

    def __init__(self, repo_root: str | None = None):
        self.repo_root = str(Path(repo_root if repo_root is not None else os.getcwd()).resolve())

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        raw = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=self.repo_root,
            capture_output=True,
            check=False,
        )
        # Decode explicitly: text=True would normalize carriage returns inside
        # literal NUL-delimited filenames and patch content.
        result = subprocess.CompletedProcess(
            raw.args,
            raw.returncode,
            raw.stdout.decode("utf-8", errors="surrogateescape"),
            raw.stderr.decode("utf-8", errors="surrogateescape"),
        )
        if check:
            result.check_returncode()
        return result

    def current_branch(self) -> str:
        """Return the branch name, or ``HEAD`` for a detached checkout."""
        result = self._run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode != 1:
            result.check_returncode()
        self._run("rev-parse", "--verify", "HEAD")
        return "HEAD"

    def status(self, porcelain: bool = True) -> dict[str, Any]:
        """Return index/worktree status with literal repo-relative filenames.

        ``porcelain`` is retained for API compatibility; structured output always
        uses Git's stable porcelain v1 protocol, even when it is False. Statuses
        are single index/worktree letters; untracked files have status ``??``.
        Renames report their destination path. Ignored files are excluded.
        """
        output = self._run(
            "-c",
            "status.relativePaths=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout
        result: dict[str, Any] = {
            "staged": [],
            "unstaged": [],
            "untracked": [],
            "branch": self.current_branch(),
        }
        records = iter(output.split("\0"))
        for record in records:
            if not record:
                continue
            index, worktree, path = record[0], record[1], record[3:]
            if index in "RC" or worktree in "RC":
                next(records)  # In -z mode the source follows the destination.
            if index == worktree == "?":
                result["untracked"].append({"path": path, "status": "??"})
                continue
            if index != " ":
                result["staged"].append({"path": path, "status": index})
            if worktree != " ":
                result["unstaged"].append({"path": path, "status": worktree})
        return result

    def diff(self, staged: bool = True, path: str = "") -> str:
        """Return an index (default) or worktree diff; never run diff helpers."""
        args = ["--literal-pathspecs", "diff", "--no-color", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        args.append("--")
        if path:
            args.append(path)
        return self._run(*args).stdout

    def log(self, count: int = 10, format: str = "%H %s") -> list[dict[str, str]]:
        """Return recent commits, newest first; an unborn branch has no entries.

        ``format`` is Git's pretty-format for ``message``. The default returns
        the subject alone (the full hash already has its own field). Custom
        formats render into message while hash/author/date remain structured.
        NULs, including pretty-format ``%x00``, are not allowed as delimiters.
        """
        if type(count) is not int or count < 0:
            raise ValueError("count must be a non-negative integer")
        if "\0" in format or "%x00" in format.lower():
            raise ValueError("format cannot contain NUL delimiters")
        if count == 0:
            return []
        head = self._run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if head.returncode != 0:
            # Only an existing symbolic branch with no commit is an empty log.
            self._run("symbolic-ref", "--quiet", "HEAD")
            if head.returncode != 1:
                head.check_returncode()
            return []
        message_format = "%s" if format == "%H %s" else format
        output = self._run(
            "log",
            f"--max-count={count}",
            "-z",
            "--no-color",
            "--no-show-signature",
            f"--format=%H%x00{message_format}%x00%an%x00%aI",
            "--",
        ).stdout
        fields = output.split("\0")
        if fields[-1] == "":
            fields.pop()
        if len(fields) % 4:
            raise ValueError("Git log returned malformed NUL-delimited output")
        return [
            dict(zip(("hash", "message", "author", "date"), fields[i : i + 4]))
            for i in range(0, len(fields), 4)
        ]

    def add(self, files: list[str]) -> dict[str, Any]:
        """Stage literal files/directories; report per-path failures.

        ``errors`` contains ``{path, error}`` dictionaries. Valid paths can be
        staged even if other paths fail. An empty list stages nothing.
        """
        added: list[str] = []
        errors: list[dict[str, str]] = []
        for path in dict.fromkeys(files):
            if not path or "\0" in path:
                errors.append({"path": path, "error": "path must be nonempty and NUL-free"})
                continue
            result = self._run("--literal-pathspecs", "add", "--", path, check=False)
            if result.returncode == 0:
                added.append(path)
            else:
                errors.append({"path": path, "error": result.stderr.strip()})
        return {"added": added, "errors": errors}

    def commit(self, message: str, files: list[str] | None = None) -> dict[str, str | int]:
        """Commit staged changes, or stage/commit only the explicitly given paths.

        Other staged paths stay staged when ``files`` is provided. The returned
        file count compares the commit with its first parent (or the empty tree
        for an initial commit). Git's hooks and signing settings are honored. No push, force, or retry is performed.
        """
        if not message.strip() or "\0" in message:
            raise ValueError("message must be nonblank and NUL-free")
        args = ["--literal-pathspecs", "commit", "--message", message]
        if files is not None:
            if not files or any(not path or "\0" in path for path in files):
                raise ValueError("files must contain nonempty, NUL-free paths")
            self._run("--literal-pathspecs", "add", "--", *files)
            args.extend(["--only", "--", *files])
        self._run(*args)
        commit_hash = self._run("rev-parse", "--verify", "HEAD").stdout.strip()
        parents = self._run("rev-list", "--parents", "-n", "1", commit_hash).stdout.split()
        if len(parents) > 1:
            changes = self._run(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--name-status",
                "-z",
                "-M",
                parents[1],
                commit_hash,
                "--",
            ).stdout
        else:
            changes = self._run(
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-z",
                "-M",
                commit_hash,
                "--",
            ).stdout
        changed = iter(changes.split("\0"))
        count = 0
        for status in changed:
            if not status:
                continue
            next(changed)  # One path, or source and destination for a rename.
            if status.startswith(("R", "C")):
                next(changed)
            count += 1
        actual_message = self._run(
            "log",
            "-1",
            "--format=%B",
            "--no-show-signature",
            commit_hash,
            "--",
        ).stdout.rstrip("\n")
        return {"hash": commit_hash, "files": count, "message": actual_message}

    def branch(self, name: str = "", create: bool = False, delete: bool = False) -> dict[str, Any]:
        """List local/remote branches, create one, or safely delete a local branch.

        Listing returns ``{branches, current}``; mutations return
        ``{name, created}`` or ``{name, deleted}``. Creation does not check out.
        Deletion uses ``-d`` (never force-deletes unmerged work).
        """
        if create and delete:
            raise ValueError("create and delete are mutually exclusive")
        if create or delete:
            if not name or name.startswith("-") or "\0" in name:
                raise ValueError("a non-option branch name is required")
            self._run("check-ref-format", "--branch", name)
            if create:
                self._run("branch", "--", name)
                return {"name": name, "created": True}
            self._run("branch", "-d", "--", name)
            return {"name": name, "deleted": True}
        branches = self._run(
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes",
        ).stdout.splitlines()
        return {"branches": branches, "current": self.current_branch()}

    def checkout(self, ref: str) -> dict[str, str | bool]:
        """Check out a branch/tag/commit without forcing or restoring file paths."""
        if not ref or ref.startswith("-") or "\0" in ref:
            return {"ref": ref, "success": False}
        valid = self._run(
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
            check=False,
        )
        if valid.returncode != 0:
            return {"ref": ref, "success": False}
        result = self._run("checkout", ref, "--", check=False)
        return {"ref": ref, "success": result.returncode == 0}
