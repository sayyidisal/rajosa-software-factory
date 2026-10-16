"""Phase 4H.2 — offline SQLite backup / verify (disposable DBs only)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.domain.models import QualityReport, Task
from app.execution.models import ExecutionSummary
from app.orchestration.events import make_event
from app.orchestration.plan import compute_approval_digest, parse_plan_text
from app.persistence.backup import (
    BackupError,
    create_sqlite_backup,
    read_checksum_file,
    sha256_file,
    verify_sqlite_backup,
    wal_checkpoint_full,
)
from app.persistence.backup_cli import main as backup_cli_main
from app.persistence.sqlite import SqliteTaskRepository
from tests.orchestration_helpers import DEFAULT_PLAN


def _seed_repo(db_path: Path) -> tuple[str, str]:
    repo = SqliteTaskRepository(db_path)
    plan = parse_plan_text(
        json.dumps(DEFAULT_PLAN), validation_profile="generic_text"
    )
    digest = compute_approval_digest(plan=plan, validation_profile="generic_text")
    task_id = "11111111-1111-4111-8111-111111111111"
    e1 = make_event(
        task_id=task_id,
        event_type="task.created",
        from_lifecycle="created",
        to_lifecycle="planning",
    )
    e2 = make_event(
        task_id=task_id,
        event_type="plan.awaiting_approval",
        from_lifecycle="planning",
        to_lifecycle="awaiting_plan_approval",
    )
    task = Task(
        id=task_id,
        title="Backup seed",
        instruction="seed",
        validation_profile="generic_text",
        status="pending",
        lifecycle="awaiting_plan_approval",
        output="",
        error=None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=plan,
        plan_approved=True,
        approval_digest=digest,
        events=[],
    )
    repo.save(task, new_events=[e1, e2])
    repo.close()
    return task_id, e1.event_id


def test_backup_contains_tasks_and_events(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    task_id, event_id = _seed_repo(source)
    dest = tmp_path / "backups" / "copy.sqlite3"

    result = create_sqlite_backup(source_path=source, dest_path=dest)
    assert result.backup_path == dest.resolve()
    assert result.task_count == 1
    assert result.event_count == 2
    assert result.migration_versions == (1,)
    assert result.checksum_path.is_file()
    assert result.sha256 == sha256_file(dest)

    verify = verify_sqlite_backup(
        dest_path=dest,
        expected_sha256=read_checksum_file(result.checksum_path),
    )
    assert verify.integrity_ok is True
    assert verify.integrity_message == "ok"
    assert verify.task_count == 1
    assert verify.event_count == 2
    assert verify.lifecycle_counts.get("awaiting_plan_approval") == 1

    conn = sqlite3.connect(f"{dest.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT id, lifecycle FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert row[1] == "awaiting_plan_approval"
        ev = conn.execute(
            "SELECT event_id FROM task_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        assert ev is not None
    finally:
        conn.close()


def test_backup_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "exists.sqlite3"
    dest.write_bytes(b"nope")
    with pytest.raises(BackupError, match="already exists"):
        create_sqlite_backup(source_path=source, dest_path=dest)
    assert dest.read_bytes() == b"nope"


def test_checksum_mismatch_detected(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "ok.sqlite3"
    create_sqlite_backup(source_path=source, dest_path=dest)
    with pytest.raises(BackupError, match="checksum mismatch"):
        verify_sqlite_backup(
            dest_path=dest,
            expected_sha256="0" * 64,
        )


def test_corrupt_backup_fails_integrity(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-db.sqlite3"
    bogus.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(BackupError):
        verify_sqlite_backup(dest_path=bogus)


def test_backup_does_not_touch_unrelated_active_path(tmp_path: Path) -> None:
    active = tmp_path / "active-sentinel.sqlite3"
    _seed_repo(active)
    before = active.read_bytes()
    before_mtime = active.stat().st_mtime_ns

    source = tmp_path / "other-source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "backup-out.sqlite3"
    create_sqlite_backup(source_path=source, dest_path=dest)

    assert active.read_bytes() == before
    assert active.stat().st_mtime_ns == before_mtime
    assert dest.is_file()
    assert dest.resolve() != active.resolve()


def test_verify_is_read_only_no_runner_imports(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "backup.sqlite3"
    create_sqlite_backup(source_path=source, dest_path=dest)
    report = verify_sqlite_backup(dest_path=dest)
    assert report.integrity_ok is True
    assert "awaiting_plan_approval" in report.lifecycle_counts


def test_cli_backup_and_verify(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "cli-backup.sqlite3"
    assert (
        backup_cli_main(
            ["backup", "--source", str(source), "--dest", str(dest)]
        )
        == 0
    )
    assert dest.is_file()
    assert (
        backup_cli_main(
            [
                "verify",
                "--path",
                str(dest),
                "--checksum",
                str(dest) + ".sha256",
            ]
        )
        == 0
    )


def test_cli_verify_detects_bad_checksum(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "cli-backup.sqlite3"
    assert backup_cli_main(["backup", "--source", str(source), "--dest", str(dest)]) == 0
    bad = tmp_path / "bad.sha256"
    bad.write_text(f"{'a' * 64}  {dest.name}\n", encoding="utf-8")
    assert backup_cli_main(["verify", "--path", str(dest), "--checksum", str(bad)]) == 2


def test_cli_verify_requires_checksum_arg() -> None:
    with pytest.raises(SystemExit) as excinfo:
        backup_cli_main(["verify", "--path", "missing.sqlite3"])
    assert excinfo.value.code == 2


def test_cli_verify_integrity_failure_exits_nonzero_no_verify_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.persistence.backup import VerifyResult

    path = tmp_path / "x.sqlite3"
    path.write_bytes(b"x")
    checksum = tmp_path / "x.sqlite3.sha256"
    checksum.write_text(f"{'ab' * 32}  {path.name}\n", encoding="utf-8")
    fake = VerifyResult(
        backup_path=path,
        integrity_ok=False,
        integrity_message="not ok: simulated",
        sha256="ab" * 32,
        checksum_matches=True,
        task_count=0,
        event_count=0,
        migration_versions=(),
        lifecycle_counts={},
    )
    with (
        patch("app.persistence.backup_cli.read_checksum_file", return_value="ab" * 32),
        patch("app.persistence.backup_cli.verify_sqlite_backup", return_value=fake),
    ):
        rc = backup_cli_main(
            ["verify", "--path", str(path), "--checksum", str(checksum)]
        )
    captured = capsys.readouterr()
    assert rc == 2
    assert "verify_ok" not in captured.out
    assert "integrity" in captured.err.lower() or "error:" in captured.err


def test_cli_verify_integrity_raise_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "x.sqlite3"
    path.write_bytes(b"x")
    checksum = tmp_path / "x.sha256"
    checksum.write_text(f"{'ab' * 32}  {path.name}\n", encoding="utf-8")
    with (
        patch("app.persistence.backup_cli.read_checksum_file", return_value="ab" * 32),
        patch(
            "app.persistence.backup_cli.verify_sqlite_backup",
            side_effect=BackupError("backup failed integrity_check: boom"),
        ),
    ):
        rc = backup_cli_main(
            ["verify", "--path", str(path), "--checksum", str(checksum)]
        )
    captured = capsys.readouterr()
    assert rc == 2
    assert "verify_ok" not in captured.out
    assert "error:" in captured.err


def test_create_backup_cleans_partial_when_verify_raises(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "partial.sqlite3"
    checksum = Path(str(dest) + ".sha256")
    with patch(
        "app.persistence.backup.verify_sqlite_backup",
        side_effect=BackupError("forced verify failure"),
    ):
        with pytest.raises(BackupError, match="forced verify failure"):
            create_sqlite_backup(source_path=source, dest_path=dest)
    assert not dest.exists()
    assert not checksum.exists()
    assert not Path(str(checksum) + ".tmp").exists()


def test_wal_checkpoint_busy_raises() -> None:
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (1, 10, 3)
    with pytest.raises(BackupError, match="incomplete"):
        wal_checkpoint_full(conn)


def test_wal_checkpoint_complete_ok() -> None:
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (0, 4, 4)
    assert wal_checkpoint_full(conn) == (0, 4, 4)


def test_create_backup_propagates_busy_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "out.sqlite3"
    with patch(
        "app.persistence.backup.wal_checkpoint_full",
        side_effect=BackupError("wal_checkpoint(FULL) incomplete (busy=1)"),
    ):
        with pytest.raises(BackupError, match="incomplete"):
            create_sqlite_backup(source_path=source, dest_path=dest)
    assert not dest.exists()
    assert not Path(str(dest) + ".sha256").exists()


def test_read_checksum_missing_empty_malformed(tmp_path: Path) -> None:
    missing = tmp_path / "nope.sha256"
    with pytest.raises(BackupError, match="missing"):
        read_checksum_file(missing)

    empty = tmp_path / "empty.sha256"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(BackupError, match="empty"):
        read_checksum_file(empty)

    bad = tmp_path / "bad.sha256"
    bad.write_text("not-a-digest  file.sqlite3\n", encoding="utf-8")
    with pytest.raises(BackupError, match="invalid"):
        read_checksum_file(bad)


def test_cli_verify_missing_checksum_file(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _seed_repo(source)
    dest = tmp_path / "cli-backup.sqlite3"
    assert backup_cli_main(["backup", "--source", str(source), "--dest", str(dest)]) == 0
    assert (
        backup_cli_main(
            [
                "verify",
                "--path",
                str(dest),
                "--checksum",
                str(tmp_path / "absent.sha256"),
            ]
        )
        == 2
    )
