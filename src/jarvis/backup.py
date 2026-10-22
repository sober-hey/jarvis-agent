"""K0-017 DRAFT: local B1/B2 backup and restore.

NOT_APPROVED / NOT_FROZEN / runtime_enabled=False.
Only used with explicit draft_fixture=True; never wired to production.

B1 = local snapshot of database + snapshot directory.
B2 = compressed .tar.gz archive of B1 (offline, no external cloud).
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path


class BackupManager:
    """Manages B1 (directory) and B2 (compressed archive) local backups.

    B1 is a point-in-time copy of the SQLite database and snapshot directory.
    B2 is a .tar.gz of B1 for offline archival, verified by SHA-256.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_dir: Path,
        backup_root: Path,
        keep_last: int = 5,
        draft_fixture: bool = False,
    ) -> None:
        if not draft_fixture:
            raise ValueError("explicit draft_fixture=True required")
        self._connection = connection
        self._snapshot_dir = snapshot_dir
        self._backup_root = backup_root
        self._keep_last = keep_last
        (backup_root / "b1").mkdir(parents=True, exist_ok=True)
        (backup_root / "b2").mkdir(parents=True, exist_ok=True)

    def create_b1(self, *, quiesced: bool = False) -> Path:
        """Create a B1 backup directory.

        Returns the path to the created B1 directory.
        quiesced=True means the database is in a known consistent state;
        we use SQLite backup API to get a consistent snapshot regardless.
        """
        stamp = str(int(time.time() * 1000))
        dest = self._backup_root / "b1" / stamp
        dest.mkdir(parents=True, exist_ok=True)

        # Database backup via SQLite backup API (consistent, no WAL leakage).
        # Checkpoint WAL first so the backup copy has no WAL file.
        db_dest = dest / "database.sqlite3"
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.commit()
        target_conn = sqlite3.connect(db_dest)
        self._connection.backup(target_conn)
        # Switch target to DELETE journal mode so no WAL file persists after close.
        target_conn.execute("PRAGMA journal_mode=DELETE")
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_conn.close()
        # Remove any residual WAL/SHM files the backup API may have created.
        for suffix in ("-wal", "-shm"):
            residual = db_dest.parent / (db_dest.name + suffix)
            if residual.exists():
                residual.unlink()

        # Snapshot directory copy (deep, preserve hidden files).
        snap_dest = dest / "snapshots"
        shutil.copytree(self._snapshot_dir, snap_dest)

        # Rotate old B1 backups.
        self._rotate("b1")
        return dest

    def verify_b1(self, b1_path: Path) -> bool:
        """Verify a B1 backup directory is intact.

        Checks: database integrity, snapshot directory exists.
        Does NOT re-compare against source (source may have changed).
        """
        db = b1_path / "database.sqlite3"
        if not db.exists():
            return False
        snaps = b1_path / "snapshots"
        if not snaps.is_dir():
            return False
        try:
            with sqlite3.connect(db) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    return False
        except Exception:
            return False
        return True

    def create_b2(self, b1_path: Path) -> Path:
        """Compress B1 into a .tar.gz B2 archive and record its SHA-256."""
        stamp = b1_path.name
        archive = self._backup_root / "b2" / f"{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(b1_path, arcname=stamp)
        # Record SHA-256 alongside.
        digest = _sha256_file(archive)
        (archive.parent / f"{stamp}.sha256").write_text(digest + "\n")
        self._rotate("b2")
        return archive

    def verify_b2(self, archive: Path) -> bool:
        """Verify B2 archive SHA-256 matches stored checksum."""
        sha_file = archive.parent / (archive.stem.replace(".tar", "") + ".sha256")
        if not sha_file.exists():
            return False
        expected = sha_file.read_text().strip()
        return _sha256_file(archive) == expected

    def restore_b2(self, archive: Path, dest_root: Path) -> Path:
        """Extract a B2 archive to dest_root and verify integrity."""
        dest_root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(dest_root)
        # Find the extracted B1 directory.
        members = [p for p in dest_root.iterdir() if p.is_dir()]
        if len(members) != 1:
            raise ValueError(f"unexpected B2 contents: {members}")
        b1 = members[0]
        if not self.verify_b1(b1):
            raise ValueError(f"B1 integrity check failed after restore: {b1}")
        return b1

    def _rotate(self, tier: str) -> None:
        """Keep only the most recent keep_last backups in a tier directory."""
        tier_dir = self._backup_root / tier
        entries = sorted(tier_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        # For b2, pairs of (archive, .sha256) share a stem — count archives only.
        if tier == "b2":
            archives = [e for e in entries if e.suffix == ".gz"]
            to_remove = archives[: max(0, len(archives) - self._keep_last)]
            for arc in to_remove:
                arc.unlink(missing_ok=True)
                sha = arc.parent / (arc.stem.replace(".tar", "") + ".sha256")
                sha.unlink(missing_ok=True)
        else:
            to_remove = [e for e in entries if e.is_dir()][
                : max(0, len([e for e in entries if e.is_dir()]) - self._keep_last)
            ]
            for d in to_remove:
                shutil.rmtree(d, ignore_errors=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
