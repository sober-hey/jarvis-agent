"""K0-002 probe of raw filesystem behaviour on the volume holding this repository.

Probe only: no product code, artifact, freeze gate or validation. A successful ``fsync()`` is
recorded as "the call returned success", NOT as power-loss durability; an unsupported capability
fails or skips with the observed errno. Results cover only this volume.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="probe requires Darwin")

# Only these errnos mean "directory fsync is unsupported here"; anything else
# (EBADF/EPERM/EROFS/...) indicates a real defect and must fail, never skip.
UNSUPPORTED_DIR_FSYNC_ERRNOS = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
)

_RACE_WORKER = """
import os, sys
prepared, target = sys.argv[1], sys.argv[2]
sys.stdin.readline()
try: os.link(prepared, target)
except OSError as exc:
    print(f"errno={exc.errno}", file=sys.stderr)
    raise SystemExit(3 if isinstance(exc, FileExistsError) else 4) from None
"""

RecordProperty = Callable[[str, object], None]


@contextmanager
def probe_workspace() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(dir=REPO_ROOT, prefix=".k0-002-probe-") as raw:
        root = Path(raw)
        assert os.stat(root).st_dev == os.stat(REPO_ROOT).st_dev, "probe dir left the volume"
        yield root


def _fsync_outcome(fd: int) -> tuple[int, str] | None:
    try:
        os.fsync(fd)
    except OSError as exc:
        return (exc.errno or 0, exc.strerror or "no strerror")
    return None


def _spawn_link_worker(prepared: Path, target: Path) -> subprocess.Popen[str]:
    argv = [sys.executable, "-c", _RACE_WORKER, str(prepared), str(target)]
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _observe_concurrent_link_creation(root: Path) -> tuple[Path, Path, Path, Path]:
    """Link two prepared files to one target concurrently; always reap; return the paths.

    The stdin gate releases a worker only after it exists, so this is a
    concurrent-creation observation, not a synchronised start.
    """
    existing = root / "pre-existing.bin"
    first, second = root / "prepared-a.bin", root / "prepared-b.bin"
    existing.write_bytes(b"pre-existing data must stay unchanged")
    first.write_bytes(b"A" * 4096)
    second.write_bytes(b"B" * 2048)
    target = root / "published.bin"
    workers: list[subprocess.Popen[str]] = []
    try:
        for prepared in (first, second):  # a later spawn failure must still reap earlier workers
            workers.append(_spawn_link_worker(prepared, target))
        for worker in workers:  # release only after every worker exists
            assert worker.stdin is not None, "worker stdin pipe missing"
            worker.stdin.write("go\n")
            worker.stdin.flush()
        results = [worker.communicate(timeout=10) for worker in workers]
        codes = [worker.returncode for worker in workers]
        assert sorted(codes) == [0, 3], (
            f"expected one link success and one FileExistsError, got {codes}: {results}"
        )
    finally:  # no child may outlive the probe directory, and no pipe may leak
        for worker in workers:
            try:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(timeout=5)
            finally:  # wait() does not close the pipes; close them even if reaping raised
                for pipe in (worker.stdin, worker.stdout, worker.stderr):
                    if pipe is not None:
                        pipe.close()
    return existing, first, second, target


def test_probe_workspace_is_same_volume_and_cleans_up_on_error(
    record_property: RecordProperty,
) -> None:
    repo_dev = os.stat(REPO_ROOT).st_dev
    leaked: Path | None = None
    with pytest.raises(RuntimeError, match="injected"):
        with probe_workspace() as root:
            leaked = root
            (root / "partial.bin").write_bytes(b"partial probe write")
            raise RuntimeError("injected failure after a probe write")
    assert leaked is not None and not leaked.exists(), f"probe dir leaked: {leaked}"
    record_property("probe_platform", sys.platform)  # identity of the volume probed here
    record_property("probe_repo_stdev", repo_dev)


@DARWIN_ONLY
def test_same_volume_link_creates_target_then_refuses_to_clobber() -> None:
    with probe_workspace() as root:
        prepared, target = root / "prepared-v1.bin", root / "published.bin"
        prepared.write_bytes(b"prepared-v1")
        os.link(prepared, target)
        source, linked = prepared.stat(), target.stat()
        assert target.read_bytes() == b"prepared-v1"
        assert (linked.st_dev, linked.st_ino) == (source.st_dev, source.st_ino), "not a hard link"
        assert linked.st_nlink == 2, "no second directory entry was created"

        prepared_v2 = root / "prepared-v2.bin"
        prepared_v2.write_bytes(b"prepared-v2-different")
        with pytest.raises(FileExistsError) as excinfo:
            os.link(prepared_v2, target)
        assert excinfo.value.errno == errno.EEXIST, "conflict did not raise EEXIST"
        assert target.read_bytes() == b"prepared-v1", "existing target bytes were overwritten"
        assert target.stat().st_nlink == 2, "conflicting link added a directory entry"
        assert prepared_v2.read_bytes() == b"prepared-v2-different"


@DARWIN_ONLY
def test_dir_fd_nofollow_refuses_symlink_and_fstat_sees_regular_file() -> None:
    with probe_workspace() as root:
        payload = b"regular-file-bytes"
        (root / "regular.bin").write_bytes(payload)
        (root / "sneaky").symlink_to("regular.bin")
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with pytest.raises(OSError) as excinfo:
                os.open("sneaky", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            assert excinfo.value.errno == errno.ELOOP, f"errno {excinfo.value.errno}"
            fd = os.open("regular.bin", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                observed = os.fstat(fd)
                assert stat.S_ISREG(observed.st_mode), "fstat did not report a regular file"
                assert (observed.st_size, observed.st_nlink) == (len(payload), 1)
                assert os.read(fd, len(payload) + 1) == payload
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)


@DARWIN_ONLY
def test_fsync_capabilities_are_recorded_not_assumed() -> None:
    with probe_workspace() as root:
        path, payload = root / "fsync-file.bin", b"payload for the file-fsync probe"
        with path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            file_outcome = _fsync_outcome(handle.fileno())
        assert file_outcome is None, f"file fsync failed on the target volume: {file_outcome}"
        assert path.read_bytes() == payload, "file contents unreadable after fsync"
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            dir_outcome = _fsync_outcome(dir_fd)
        finally:
            os.close(dir_fd)
    if dir_outcome is None:  # recorded: a call result, not a durability guarantee
        return
    number, message = dir_outcome
    note = f"directory fsync failed: errno {number} ({errno.errorcode.get(number, '?')}: {message})"
    assert number in UNSUPPORTED_DIR_FSYNC_ERRNOS, note
    pytest.skip(f"file fsync succeeded but {note}; directory-entry durability is UNVERIFIED")


@DARWIN_ONLY
def test_concurrent_link_creation_observation_yields_at_most_one_winner() -> None:
    with probe_workspace() as root:
        existing, first, second, target = _observe_concurrent_link_creation(root)
        winners = [c for c in (first, second) if c.stat().st_ino == target.stat().st_ino]
        assert len(winners) == 1, "target is not exactly one prepared file's inode"
        assert target.read_bytes() == winners[0].read_bytes(), "target content is not intact"
        assert (first.read_bytes(), second.read_bytes()) == (b"A" * 4096, b"B" * 2048)
        assert existing.read_bytes() == b"pre-existing data must stay unchanged"


@DARWIN_ONLY
def test_second_spawn_failure_still_reaps_the_first_worker(
    monkeypatch: pytest.MonkeyPatch, record_property: RecordProperty
) -> None:
    spawned: list[subprocess.Popen[str]] = []
    real_spawn = _spawn_link_worker

    def flaky_spawn(prepared: Path, target: Path) -> subprocess.Popen[str]:
        if spawned:  # second call: fail the way an exhausted process table would
            raise OSError(errno.EAGAIN, "injected second-spawn failure")
        worker = real_spawn(prepared, target)
        spawned.append(worker)
        return worker

    monkeypatch.setattr(sys.modules[__name__], "_spawn_link_worker", flaky_spawn)
    leaked: Path | None = None
    with pytest.raises(OSError, match="injected second-spawn failure"):
        with probe_workspace() as root:
            leaked = root
            _observe_concurrent_link_creation(root)
    assert len(spawned) == 1, "the first worker was never spawned"
    assert spawned[0].returncode is not None, f"worker pid={spawned[0].pid} was not reaped"
    record_property("first_worker_pid", spawned[0].pid)
    record_property("first_worker_returncode", spawned[0].returncode)
    assert leaked is not None and not leaked.exists(), f"probe dir leaked: {leaked}"
