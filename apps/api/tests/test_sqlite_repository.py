"""Phase 4E.1 — SQLite TaskRepository and migration tests (offline)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.domain.models import QualityReport, Task
from app.domain.store import MemoryTaskStore, TaskRepository
from app.execution.models import ExecutionSummary
from app.orchestration.events import make_event
from app.orchestration.plan import Plan, parse_plan_text
from app.persistence.migrate import MigrationError, apply_migrations, applied_versions
from app.persistence.sqlite import SqliteTaskRepository
from tests.orchestration_helpers import DEFAULT_PLAN
import json


def _task(
    *,
    task_id: str = "t1",
    lifecycle: str = "awaiting_plan_approval",
    status: str = "pending",
    plan: Plan | None = None,
    plan_approved: bool = False,
) -> Task:
    return Task(
        id=task_id,
        title="Title",
        instruction="Do the thing",
        validation_profile="generic_text",
        status=status,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        output="",
        error=None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=plan,
        plan_approved=plan_approved,
        events=[],
    )


def _repo(tmp_path: Path) -> SqliteTaskRepository:
    return SqliteTaskRepository(tmp_path / "tasks.sqlite3")


def test_sqlite_is_task_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert isinstance(repo, TaskRepository)
    repo.close()


def test_sqlite_round_trip_task_and_events(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = parse_plan_text(
        json.dumps(DEFAULT_PLAN), validation_profile="generic_text"
    )
    e1 = make_event(
        task_id="t1",
        event_type="task.created",
        from_lifecycle="created",
        to_lifecycle="planning",
        metadata={"validation_profile": "generic_text"},
    )
    e2 = make_event(
        task_id="t1",
        event_type="plan.awaiting_approval",
        step_id=None,
        error_category=None,
        metadata={"steps": 1},
    )
    from app.orchestration.plan import compute_approval_digest

    digest = compute_approval_digest(plan=plan, validation_profile="generic_text")
    task = _task(plan=plan, plan_approved=True).model_copy(
        update={
            "output": "plan-json",
            "quality": QualityReport(passed=True, checks=[]),
            "approval_digest": digest,
        }
    )
    saved = repo.save(task, new_events=[e1, e2])
    assert len(saved.events) == 2

    loaded = repo.get("t1")
    assert loaded is not None
    assert loaded.title == "Title"
    assert loaded.plan is not None
    assert loaded.plan.schema_version == "2"
    assert loaded.plan_approved is True
    assert loaded.approval_digest == digest
    assert loaded.quality.passed is True
    assert loaded.execution is not None
    assert loaded.execution.status == "not_requested"
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]
    assert loaded.events[0].metadata["validation_profile"] == "generic_text"
    assert loaded.events[0].from_lifecycle == "created"
    assert loaded.events[1].metadata["steps"] == 1

    row = repo._conn.execute(
        "SELECT approval_digest, workspace_relpath FROM tasks WHERE id = ?",
        ("t1",),
    ).fetchone()
    assert row["workspace_relpath"] == "t1"
    assert row["approval_digest"] == digest
    repo.close()


def test_sqlite_save_without_new_events_preserves_history(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.generated")
    repo.save(_task(), new_events=[e1, e2])
    repo.save(_task().model_copy(update={"title": "Later", "events": []}))
    loaded = repo.get("t1")
    assert loaded is not None
    assert loaded.title == "Later"
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]
    repo.close()


def test_sqlite_new_events_dedupe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.awaiting_approval")
    repo.save(_task(), new_events=[e1, e2])
    repo.save(_task().model_copy(update={"output": "x"}), new_events=[e1, e2])
    loaded = repo.get("t1")
    assert loaded is not None
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]
    assert loaded.output == "x"
    repo.close()


def test_sqlite_mismatched_event_task_id_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save(_task())
    bad = make_event(task_id="other", event_type="task.created")
    with pytest.raises(ValueError, match="does not match"):
        repo.save(_task(), new_events=[bad])
    assert repo.get("t1") is not None
    assert repo.get("t1").events == []  # type: ignore[union-attr]
    repo.close()


def test_sqlite_event_insert_failure_rolls_back_task(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save(_task().model_copy(update={"title": "Original"}))
    repo._conn.execute(
        """
        CREATE TRIGGER fail_event_insert
        BEFORE INSERT ON task_events
        BEGIN
          SELECT RAISE(ABORT, 'forced event failure');
        END
        """
    )
    e1 = make_event(task_id="t1", event_type="task.created")
    with pytest.raises(sqlite3.Error):
        repo.save(
            _task().model_copy(update={"title": "ShouldRollback"}),
            new_events=[e1],
        )
    loaded = repo.get("t1")
    assert loaded is not None
    assert loaded.title == "Original"
    assert loaded.events == []
    count = repo._conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert count == 0
    repo.close()


def test_sqlite_list_by_lifecycle_and_ids(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.save(_task(task_id="z", lifecycle="awaiting_plan_approval"))
    repo.save(_task(task_id="a", lifecycle="completed", status="passed"))
    repo.save(_task(task_id="m", lifecycle="failed", status="failed"))
    assert {t.id for t in repo.list_by_lifecycle(["completed", "failed"])} == {
        "a",
        "m",
    }
    assert repo.list_task_ids() == ["a", "m", "z"]
    repo.close()


def test_sqlite_more_than_100_events_retained(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    events = [make_event(task_id="t1", event_type="task.created") for _ in range(120)]
    repo.save(_task(), new_events=events)
    loaded = repo.get("t1")
    assert loaded is not None
    assert len(loaded.events) == 120
    repo.close()


def test_fresh_database_applies_migration(tmp_path: Path) -> None:
    db = tmp_path / "fresh.sqlite3"
    conn = sqlite3.connect(db)
    applied = apply_migrations(conn)
    assert applied == [1]
    assert applied_versions(conn) == {1}
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "tasks" in tables
    assert "task_events" in tables
    assert "schema_migrations" in tables
    # Idempotent re-apply
    assert apply_migrations(conn) == []
    conn.close()


def test_failed_migration_does_not_leave_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "001_bad.sql").write_text(
        "CREATE TABLE boom (id TEXT NOT NULL);\n"
        "INSERT INTO boom(id) VALUES (NULL);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.persistence.migrate.MIGRATIONS_DIR", mig_dir)
    db = tmp_path / "bad.sqlite3"
    conn = sqlite3.connect(db)
    with pytest.raises(MigrationError, match="Migration 1"):
        apply_migrations(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "boom" not in tables
    assert 1 not in applied_versions(conn)
    conn.close()


def test_memory_and_sqlite_contract_parity(tmp_path: Path) -> None:
    """Same save sequence yields the same observable task/event state."""
    memory: TaskRepository = MemoryTaskStore()
    sqlite_repo: TaskRepository = _repo(tmp_path)

    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.awaiting_approval")

    for repo in (memory, sqlite_repo):
        repo.save(_task(), new_events=[e1])
        repo.save(_task().model_copy(update={"title": "T2", "events": []}))
        repo.save(_task().model_copy(update={"title": "T3"}), new_events=[e1, e2])
        loaded = repo.get("t1")
        assert loaded is not None
        assert loaded.title == "T3"
        assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]
        assert {t.id for t in repo.list_by_lifecycle(["awaiting_plan_approval"])} == {
            "t1"
        }
        assert repo.list_task_ids() == ["t1"]

    sqlite_repo.close()
