"""Offline SQLite backup / verify helpers (Phase 4H.2).

Uses the SQLite online backup API (``Connection.backup``) after
``PRAGMA wal_checkpoint(FULL)`` (when enabled) so committed WAL pages are
included. This is **not** a plain copy of the live ``.sqlite3`` file alone
(unsafe with WAL).

These helpers never start the API, never invoke planners/implementers/runners,
and never modify a destination that already exists.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Tables required for a Rajosa task DB after migration 001.
_REQUIRED_TABLES = frozenset({"schema_migrations", "tasks", "task_events"})


class BackupError(RuntimeError):
    """Raised when backup or verification cannot complete safely."""


@dataclass(frozen=True)
class BackupResult:
    source_path: Path
    backup_path: Path
    checksum_path: Path
    sha256: str
    task_count: int
    event_count: int
    migration_versions: tuple[int, ...]


@dataclass(frozen=True)
class VerifyResult:
    backup_path: Path
    integrity_ok: bool
    integrity_message: str
    sha256: str
    checksum_matches: bool | None
    task_count: int
    event_count: int
    migration_versions: tuple[int, ...]
    lifecycle_counts: dict[str, int]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_backup_filename(source: Path, *, when: str | None = None) -> str:
    """Return ``<stem>-<utc>.sqlite3`` for operator-friendly naming."""
    stamp = when or _utc_stamp()
    return f"{source.stem}-{stamp}.sqlite3"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _restrictive_file_mode(path: Path) -> None:
    """Best-effort owner-only file mode (no-op semantics on unsupported OS)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows or restricted FS — leave OS default; document in runbook.
        pass


def _require_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise BackupError(f"{label} is not a file: {resolved}")
    return resolved


def _open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _unlink_if_exists(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _cleanup_new_backup_artifacts(dest: Path, checksum_path: Path) -> None:
    """Remove dest/checksum artifacts created by this call only.

    Caller must ensure ``dest`` did not exist before the backup started.
    """
    _unlink_if_exists(Path(str(checksum_path) + ".tmp"))
    _unlink_if_exists(checksum_path)
    _unlink_if_exists(dest)


def wal_checkpoint_full(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Run ``PRAGMA wal_checkpoint(FULL)`` and require a complete checkpoint.

    SQLite returns ``(busy, log, checkpointed)``. ``busy != 0`` means the
    checkpoint did not fully complete — do not treat that as success.
    """
    row = conn.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
    if row is None or len(row) < 3:
        raise BackupError("wal_checkpoint(FULL) returned no result")
    busy = int(row[0])
    log_frames = int(row[1])
    checkpointed = int(row[2])
    if busy != 0:
        raise BackupError(
            "wal_checkpoint(FULL) incomplete "
            f"(busy={busy}, log={log_frames}, checkpointed={checkpointed}); "
            "stop the API and retry, or re-run with checkpoint disabled only "
            "if you accept a possibly incomplete WAL fold"
        )
    return busy, log_frames, checkpointed


def _migration_versions(conn: sqlite3.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return ()
    versions = [
        int(r[0])
        for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version ASC"
        ).fetchall()
    ]
    return tuple(versions)


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not _REQUIRED_TABLES.issubset(tables):
        missing = sorted(_REQUIRED_TABLES - tables)
        raise BackupError(f"backup missing required tables: {missing}")
    tasks = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    events = int(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0])
    return tasks, events


def _lifecycle_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT lifecycle, COUNT(*) FROM tasks GROUP BY lifecycle ORDER BY lifecycle"
    ).fetchall()
    return {str(lifecycle): int(count) for lifecycle, count in rows}


def create_sqlite_backup(
    *,
    source_path: Path,
    dest_path: Path,
    checkpoint: bool = True,
) -> BackupResult:
    """Create a consistent SQLite backup file and adjacent ``.sha256`` checksum.

    Prefer stopping the API process before calling this so application-level
    work is quiescent. When ``checkpoint=True``, a complete
    ``wal_checkpoint(FULL)`` is required before the online backup API runs.

    Never overwrites ``dest_path``. On failure before the checksum is
    published, removes only artifacts created by this call.
    """
    source = _require_file(source_path, label="source database")
    dest = dest_path.expanduser().resolve()
    if dest.exists():
        raise BackupError(f"destination already exists: {dest}")
    if dest == source:
        raise BackupError("destination must differ from source")

    dest.parent.mkdir(parents=True, exist_ok=True)
    checksum_path = Path(str(dest) + ".sha256")
    checksum_tmp = Path(str(checksum_path) + ".tmp")

    source_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        source_conn = _open_rw(source)
        if checkpoint:
            wal_checkpoint_full(source_conn)
        dest_conn = sqlite3.connect(str(dest), isolation_level=None)
        source_conn.backup(dest_conn)
        dest_conn.commit()
    except (sqlite3.Error, BackupError) as exc:
        if dest_conn is not None:
            try:
                dest_conn.close()
            except sqlite3.Error:
                pass
            dest_conn = None
        if source_conn is not None:
            try:
                source_conn.close()
            except sqlite3.Error:
                pass
            source_conn = None
        _cleanup_new_backup_artifacts(dest, checksum_path)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"sqlite backup failed: {exc}") from exc
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if source_conn is not None:
            source_conn.close()

    try:
        _restrictive_file_mode(dest)
        # Fail closed on structural/schema/checksum issues before publishing.
        verify = verify_sqlite_backup(dest_path=dest, expected_sha256=None)
        digest = sha256_file(dest)
        checksum_tmp.write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
        _restrictive_file_mode(checksum_tmp)
        checksum_tmp.replace(checksum_path)
        _restrictive_file_mode(checksum_path)
    except Exception:
        _cleanup_new_backup_artifacts(dest, checksum_path)
        raise

    return BackupResult(
        source_path=source,
        backup_path=dest,
        checksum_path=checksum_path,
        sha256=digest,
        task_count=verify.task_count,
        event_count=verify.event_count,
        migration_versions=verify.migration_versions,
    )


def verify_sqlite_backup(
    *,
    dest_path: Path,
    expected_sha256: str | None = None,
) -> VerifyResult:
    """Validate a backup; raise ``BackupError`` on integrity/schema/checksum failure.

    Successful returns always have ``integrity_ok=True``. Checksum comparison
    proves file content matches an expected digest — not authenticity or trust
    of the signing party.
    """
    backup = _require_file(dest_path, label="backup database")
    digest = sha256_file(backup)
    checksum_matches: bool | None = None
    if expected_sha256 is not None:
        checksum_matches = secrets_compare_hex(digest, expected_sha256.strip().lower())
        if checksum_matches is False:
            raise BackupError("backup checksum mismatch")

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_ro(backup)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        message = str(row[0]) if row else "missing integrity_check result"
        if message != "ok":
            raise BackupError(f"backup failed integrity_check: {message}")
        versions = _migration_versions(conn)
        if not versions:
            raise BackupError("backup has no schema_migrations rows")
        task_count, event_count = _counts(conn)
        lifecycles = _lifecycle_counts(conn)
    except sqlite3.Error as exc:
        raise BackupError(f"backup verification failed: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()

    return VerifyResult(
        backup_path=backup,
        integrity_ok=True,
        integrity_message="ok",
        sha256=digest,
        checksum_matches=checksum_matches,
        task_count=task_count,
        event_count=event_count,
        migration_versions=versions,
        lifecycle_counts=lifecycles,
    )


def secrets_compare_hex(actual: str, expected: str) -> bool:
    """Constant-time compare for hex digests of equal encoding."""
    try:
        return secrets.compare_digest(
            actual.encode("ascii"),
            expected.encode("ascii"),
        )
    except (TypeError, ValueError):
        return False


def read_checksum_file(path: Path) -> str:
    """Parse ``<hex>  <filename>`` lines written by :func:`create_sqlite_backup`."""
    resolved = path.expanduser()
    if not resolved.is_file():
        raise BackupError(f"checksum file is missing: {resolved}")
    text = resolved.read_text(encoding="utf-8").strip()
    if not text:
        raise BackupError(f"empty checksum file: {resolved}")
    first = text.splitlines()[0].strip()
    digest = first.split()[0].strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise BackupError(f"invalid checksum file: {resolved}")
    return digest
