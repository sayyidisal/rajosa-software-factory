"""Phase 4H.3 — isolated SQLite restore drill (disposable DBs only).

Does **not** touch ``var/rajosa.sqlite3``, start the API, or invoke
planner / implementer / execution runners. Passing these tests is **not**
a claim that production recovery has been validated.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
import pytest

from app.domain.models import QualityReport, Task
from app.execution.models import ExecutionSummary
from app.orchestration.events import make_event
from app.orchestration.plan import compute_approval_digest, parse_plan_text
from app.persistence.backup import (
    BackupError,
    create_sqlite_backup,
    materialize_verified_restore,
    read_checksum_file,
    sha256_file,
    verify_sqlite_backup,
)
from app.persistence.sqlite import SqliteTaskRepository
from tests.orchestration_helpers import DEFAULT_PLAN

# Modules that must not be newly imported by the restore materialization path.
_FORBIDDEN_IMPORT_PREFIXES = (
    "app.agents.",
    "app.execution.docker_runner",
    "app.execution.factory",
    "app.orchestration.service",
)


def _plan_and_digest():
    plan = parse_plan_text(
        json.dumps(DEFAULT_PLAN), validation_profile="generic_text"
    )
    digest = compute_approval_digest(plan=plan, validation_profile="generic_text")
    return plan, digest


def _seed_representative_db(db_path: Path) -> dict[str, object]:
    """Build a disposable DB via the real repository + schema/migrations."""
    repo = SqliteTaskRepository(db_path)
    plan, digest = _plan_and_digest()

    waiting_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    completed_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    e1 = make_event(
        task_id=waiting_id,
        event_type="task.created",
        from_lifecycle="created",
        to_lifecycle="planning",
    )
    e2 = make_event(
        task_id=waiting_id,
        event_type="plan.awaiting_approval",
        from_lifecycle="planning",
        to_lifecycle="awaiting_plan_approval",
    )
    waiting = Task(
        id=waiting_id,
        title="Drill waiting",
        instruction="awaiting approval seed",
        validation_profile="generic_text",
        status="pending",
        lifecycle="awaiting_plan_approval",
        output="",
        error=None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=plan,
        plan_approved=False,
        approval_digest=digest,
        events=[],
    )
    repo.save(waiting, new_events=[e1, e2])

    e3 = make_event(
        task_id=completed_id,
        event_type="task.created",
        from_lifecycle="created",
        to_lifecycle="planning",
    )
    e4 = make_event(
        task_id=completed_id,
        event_type="task.completed",
        from_lifecycle="validating",
        to_lifecycle="completed",
    )
    completed = Task(
        id=completed_id,
        title="Drill completed",
        instruction="completed seed",
        validation_profile="generic_text",
        status="passed",
        lifecycle="completed",
        output="done",
        error=None,
        quality=QualityReport(passed=True, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=plan,
        plan_approved=True,
        approval_digest=digest,
        events=[],
    )
    repo.save(completed, new_events=[e3, e4])
    repo.close()

    return {
        "waiting_id": waiting_id,
        "completed_id": completed_id,
        "event_ids": (e1.event_id, e2.event_id, e3.event_id, e4.event_id),
        "task_count": 2,
        "event_count": 4,
        "migration_versions": (1,),
        "lifecycles": {
            "awaiting_plan_approval": 1,
            "completed": 1,
        },
    }


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _assert_db_contents(path: Path, seed: dict[str, object]) -> None:
    conn = _open_ro(path)
    try:
        versions = [
            int(r[0])
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == list(seed["migration_versions"])

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"schema_migrations", "tasks", "task_events"} <= tables

        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        assert task_count == seed["task_count"]
        assert event_count == seed["event_count"]

        waiting = conn.execute(
            "SELECT lifecycle, plan_approved FROM tasks WHERE id = ?",
            (seed["waiting_id"],),
        ).fetchone()
        assert waiting is not None
        assert waiting[0] == "awaiting_plan_approval"
        assert waiting[1] == 0

        completed = conn.execute(
            "SELECT lifecycle, status, output FROM tasks WHERE id = ?",
            (seed["completed_id"],),
        ).fetchone()
        assert completed is not None
        assert completed[0] == "completed"
        assert completed[1] == "passed"
        assert completed[2] == "done"

        for event_id in seed["event_ids"]:
            row = conn.execute(
                "SELECT event_id FROM task_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert row is not None

        # Append-only: seq is monotonic and unique.
        seqs = [
            r[0]
            for r in conn.execute(
                "SELECT seq FROM task_events ORDER BY seq"
            ).fetchall()
        ]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
    finally:
        conn.close()


def test_restore_drill_happy_path(tmp_path: Path) -> None:
    source = tmp_path / "drill-source.sqlite3"
    seed = _seed_representative_db(source)
    source_bytes_before = source.read_bytes()

    backup_path = tmp_path / "backups" / "drill.sqlite3"
    backup = create_sqlite_backup(source_path=source, dest_path=backup_path)
    assert backup.task_count == seed["task_count"]
    assert backup.event_count == seed["event_count"]
    assert backup.migration_versions == seed["migration_versions"]

    verify = verify_sqlite_backup(
        dest_path=backup.backup_path,
        expected_sha256=read_checksum_file(backup.checksum_path),
    )
    assert verify.integrity_ok is True
    assert verify.lifecycle_counts == seed["lifecycles"]

    restore_path = tmp_path / "restored" / "drill-restored.sqlite3"
    modules_before = set(sys.modules)
    result = materialize_verified_restore(
        backup_path=backup.backup_path,
        checksum_path=backup.checksum_path,
        restore_path=restore_path,
    )
    newly_imported = set(sys.modules) - modules_before
    for name in newly_imported:
        assert not any(
            name == prefix.rstrip(".") or name.startswith(prefix)
            for prefix in _FORBIDDEN_IMPORT_PREFIXES
        ), f"restore drill imported forbidden module: {name}"

    assert result.restore_path == restore_path.resolve()
    assert result.restore_checksum_path.is_file()
    assert result.source_verify.task_count == seed["task_count"]
    assert result.restore_verify.task_count == seed["task_count"]
    assert result.restore_verify.event_count == seed["event_count"]
    assert result.restore_verify.lifecycle_counts == seed["lifecycles"]
    assert result.restore_verify.migration_versions == seed["migration_versions"]

    _assert_db_contents(result.restore_path, seed)

    # Source disposable DB and original backup unchanged by restore.
    assert source.read_bytes() == source_bytes_before
    assert sha256_file(backup.backup_path) == backup.sha256
    assert read_checksum_file(backup.checksum_path) == backup.sha256


def test_bad_checksum_rejected_before_restore(tmp_path: Path) -> None:
    source = tmp_path / "src.sqlite3"
    seed = _seed_representative_db(source)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    backup_path = tmp_path / "ok.sqlite3"
    backup = create_sqlite_backup(source_path=source, dest_path=backup_path)
    backup_bytes = backup.backup_path.read_bytes()
    checksum_bytes = backup.checksum_path.read_bytes()

    # Tamper checksum file only.
    backup.checksum_path.write_text(f"{'0' * 64}  {backup.backup_path.name}\n")

    restore_path = tmp_path / "should-not-exist.sqlite3"
    with pytest.raises(BackupError, match="checksum mismatch"):
        materialize_verified_restore(
            backup_path=backup.backup_path,
            checksum_path=backup.checksum_path,
            restore_path=restore_path,
        )

    assert not restore_path.exists()
    assert not Path(str(restore_path) + ".sha256").exists()
    assert backup.backup_path.read_bytes() == backup_bytes
    # Source disposable DB unchanged.
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    _assert_db_contents(source, seed)

    # Restore checksum file for isolation of later assertions in other tests.
    backup.checksum_path.write_bytes(checksum_bytes)


def test_corrupt_backup_rejected_before_restore(tmp_path: Path) -> None:
    source = tmp_path / "src.sqlite3"
    seed = _seed_representative_db(source)
    source_bytes = source.read_bytes()

    backup_path = tmp_path / "good.sqlite3"
    backup = create_sqlite_backup(source_path=source, dest_path=backup_path)
    valid_backup_bytes = backup.backup_path.read_bytes()
    valid_checksum = backup.checksum_path.read_bytes()

    # Corrupt the backup body but keep an old checksum (will fail checksum first).
    backup.backup_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    restore_path = tmp_path / "out.sqlite3"
    with pytest.raises(BackupError, match="checksum mismatch"):
        materialize_verified_restore(
            backup_path=backup.backup_path,
            checksum_path=backup.checksum_path,
            restore_path=restore_path,
        )
    assert not restore_path.exists()
    assert source.read_bytes() == source_bytes

    # Corrupt file with matching checksum of the corrupt bytes → integrity fail.
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not-a-sqlite-database")
    digest = sha256_file(corrupt)
    corrupt_checksum = tmp_path / "corrupt.sqlite3.sha256"
    corrupt_checksum.write_text(f"{digest}  {corrupt.name}\n")
    with pytest.raises(BackupError):
        materialize_verified_restore(
            backup_path=corrupt,
            checksum_path=corrupt_checksum,
            restore_path=tmp_path / "from-corrupt.sqlite3",
        )
    assert not (tmp_path / "from-corrupt.sqlite3").exists()
    assert source.read_bytes() == source_bytes
    _assert_db_contents(source, seed)

    # Valid backup artifact from earlier step still intact on disk.
    backup.backup_path.write_bytes(valid_backup_bytes)
    backup.checksum_path.write_bytes(valid_checksum)
    assert sha256_file(backup.backup_path) == read_checksum_file(backup.checksum_path)


def test_failed_restore_does_not_damage_valid_backup(tmp_path: Path) -> None:
    source = tmp_path / "src.sqlite3"
    _seed_representative_db(source)
    backup_path = tmp_path / "valid.sqlite3"
    backup = create_sqlite_backup(source_path=source, dest_path=backup_path)
    backup_sha = backup.sha256
    backup_bytes = backup.backup_path.read_bytes()

    occupied = tmp_path / "occupied.sqlite3"
    occupied.write_bytes(b"preexisting")
    with pytest.raises(BackupError, match="already exists"):
        materialize_verified_restore(
            backup_path=backup.backup_path,
            checksum_path=backup.checksum_path,
            restore_path=occupied,
        )
    assert occupied.read_bytes() == b"preexisting"
    assert backup.backup_path.read_bytes() == backup_bytes
    assert sha256_file(backup.backup_path) == backup_sha
    assert read_checksum_file(backup.checksum_path) == backup_sha


def test_backup_refuses_preexisting_orphan_checksum(tmp_path: Path) -> None:
    source = tmp_path / "src.sqlite3"
    _seed_representative_db(source)
    dest = tmp_path / "new.sqlite3"
    orphan = Path(str(dest) + ".sha256")
    orphan.write_text(f"{'a' * 64}  {dest.name}\n")
    with pytest.raises(BackupError, match="checksum destination already exists"):
        create_sqlite_backup(source_path=source, dest_path=dest)
    assert not dest.exists()
    assert orphan.is_file()
    assert orphan.read_text().startswith("a" * 64)


def test_restore_drill_does_not_touch_active_db_path(tmp_path: Path) -> None:
    """Guard: drill paths stay under tmp_path; never resolve to var/rajosa.sqlite3."""
    from app.config import API_ROOT

    active = (API_ROOT / "var" / "rajosa.sqlite3").resolve()
    source = tmp_path / "src.sqlite3"
    _seed_representative_db(source)
    backup_path = tmp_path / "b.sqlite3"
    backup = create_sqlite_backup(source_path=source, dest_path=backup_path)
    restore_path = tmp_path / "r.sqlite3"
    result = materialize_verified_restore(
        backup_path=backup.backup_path,
        checksum_path=backup.checksum_path,
        restore_path=restore_path,
    )
    tmp_root = tmp_path.resolve()
    for path in (
        source.resolve(),
        backup.backup_path.resolve(),
        result.restore_path.resolve(),
    ):
        assert path != active
        assert path.is_relative_to(tmp_root)
