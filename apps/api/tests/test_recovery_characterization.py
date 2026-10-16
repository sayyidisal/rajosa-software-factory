"""Phase 4E.4a — characterization tests for existing recovery/persistence behavior.

Production modules are not modified. Failures are injected at the repository
boundary (or via monkeypatch of lifespan helpers) only inside these tests.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.domain.models import QualityReport, Task
from app.execution.fake import FakeExecutionRunner
from app.execution.models import ExecutionSummary
from app.main import create_app
from app.orchestration.events import make_event
from app.orchestration.recovery import StartupRecoveryError, recover_interrupted_tasks
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from app.workspaces.reconcile import ReconciliationReport


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_interruptible(
    store: SqliteTaskRepository,
    *,
    lifecycle: str,
    task_id: str | None = None,
) -> Task:
    tid = task_id or _uuid()
    task = Task(
        id=tid,
        title="Char",
        instruction="seed",
        validation_profile="generic_text",
        status="pending",
        lifecycle=lifecycle,  # type: ignore[arg-type]
        output="",
        error=None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=None,
        plan_approved=False,
        events=[],
    )
    return store.save(task)


def _interrupted_events(task: Task) -> list:
    return [
        e
        for e in task.events
        if e.error_category == "interrupted" and e.event_type == "task.failed"
    ]


def test_t1_partial_multi_task_recovery_then_resume(tmp_path: Path) -> None:
    """First save succeeds, second raises — then a clean recovery finishes the rest."""
    db_path = tmp_path / "partial.sqlite3"
    store = SqliteTaskRepository(db_path)
    first = _seed_interruptible(store, lifecycle="planning")
    second = _seed_interruptible(store, lifecycle="generating")
    first_id, second_id = first.id, second.id
    first_events_before = list(first.events)
    second_lifecycle = second.lifecycle
    second_events_before = list(second.events)
    second_error_before = second.error

    recovery_save_ids: list[str] = []
    original_save = store.save

    def flaky_save(task: Task, *, new_events=None):
        # Count only recovery transitions (interrupted failure marks).
        if (
            task.lifecycle == "failed"
            and task.error
            and "interrupted" in task.error.lower()
        ):
            recovery_save_ids.append(task.id)
            if len(recovery_save_ids) == 2:
                raise RuntimeError("forced second recovery save failure")
        return original_save(task, new_events=new_events)

    store.save = flaky_save  # type: ignore[method-assign]

    with pytest.raises(StartupRecoveryError, match="forced second recovery save"):
        recover_interrupted_tasks(store)

    assert len(recovery_save_ids) == 2
    recovered_id = recovery_save_ids[0]
    failed_attempt_id = recovery_save_ids[1]
    assert {recovered_id, failed_attempt_id} == {first_id, second_id}

    # Reload via a fresh get (same connection) after removing the flaky wrapper.
    store.save = original_save  # type: ignore[method-assign]

    recovered = store.get(recovered_id)
    assert recovered is not None
    assert recovered.lifecycle == "failed"
    assert recovered.status == "failed"
    interrupted = _interrupted_events(recovered)
    assert len(interrupted) == 1
    assert interrupted[0].metadata.get("recovery") == "startup"

    stuck = store.get(failed_attempt_id)
    assert stuck is not None
    if failed_attempt_id == second_id:
        assert stuck.lifecycle == second_lifecycle
        assert stuck.error == second_error_before
        assert [e.event_id for e in stuck.events] == [
            e.event_id for e in second_events_before
        ]
    else:
        assert stuck.lifecycle == "planning"
        assert stuck.error is None
        assert [e.event_id for e in stuck.events] == [
            e.event_id for e in first_events_before
        ]
    assert _interrupted_events(stuck) == []

    # Clean recovery finishes the remaining interruptible task only.
    report = recover_interrupted_tasks(store)
    assert report.interrupted_task_ids == [failed_attempt_id]
    assert recovered_id not in report.interrupted_task_ids

    for tid in (first_id, second_id):
        task = store.get(tid)
        assert task is not None
        assert task.lifecycle == "failed"
        assert len(_interrupted_events(task)) == 1

    store.close()


def test_t2_sqlite_save_rolls_back_task_and_events(tmp_path: Path) -> None:
    """Exception after BEGIN / before COMMIT leaves row and events unchanged."""
    db_path = tmp_path / "rollback.sqlite3"
    repo = SqliteTaskRepository(db_path)
    tid = _uuid()
    original = _seed_interruptible(repo, lifecycle="validating", task_id=tid)
    assert original.lifecycle == "validating"
    assert original.events == []

    repo._conn.execute(
        """
        CREATE TRIGGER fail_event_insert
        BEFORE INSERT ON task_events
        BEGIN
          SELECT RAISE(ABORT, 'forced event failure');
        END
        """
    )
    event = make_event(
        task_id=tid,
        event_type="task.failed",
        from_lifecycle="validating",
        to_lifecycle="failed",
        error_category="interrupted",
        metadata={"recovery": "startup"},
    )
    updated = original.model_copy(
        update={
            "lifecycle": "failed",
            "status": "failed",
            "error": "should not persist",
            "events": list(original.events) + [event],
        }
    )
    with pytest.raises(sqlite3.Error):
        repo.save(updated, new_events=[event])

    loaded = repo.get(tid)
    assert loaded is not None
    assert loaded.lifecycle == "validating"
    assert loaded.status == "pending"
    assert loaded.error is None
    assert loaded.events == []
    count = repo._conn.execute(
        "SELECT COUNT(*) AS c FROM task_events WHERE task_id = ?", (tid,)
    ).fetchone()["c"]
    assert count == 0
    repo.close()


def test_t3_owned_startup_aborts_and_flag_stays_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_store):
        raise StartupRecoveryError("forced recovery failure")

    monkeypatch.setattr("app.main.recover_interrupted_tasks", boom)
    db_path = tmp_path / "abort.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    monkeypatch.setenv("TASK_DB_PATH", str(db_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=None,
        execution_runner=FakeExecutionRunner(),
    )
    assert app.state.task_store_owned is True
    assert app.state.startup_recovery_completed is False

    with pytest.raises(StartupRecoveryError, match="forced recovery failure"):
        with TestClient(app):
            pass

    assert app.state.startup_recovery_completed is False
    assert app.state.startup_recovery_report is None


def test_t4_incomplete_reconcile_does_not_abort_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def incomplete_reconcile(*, workspace, store):
        report = ReconciliationReport()
        report.mark_failed("forced incomplete filesystem scan")
        return report

    monkeypatch.setattr("app.main.reconcile_workspaces", incomplete_reconcile)
    db_path = tmp_path / "recon.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    monkeypatch.setenv("TASK_DB_PATH", str(db_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=None,
        execution_runner=FakeExecutionRunner(),
    )

    with TestClient(app) as client:
        assert client.app.state.startup_recovery_completed is True
        recon = client.app.state.reconciliation_report
        assert recon is not None
        assert recon.incomplete is True
        assert recon.error is not None
        assert "forced incomplete" in recon.error
        assert client.get("/health").status_code == 200
        assert client.get("/health").json()["status"] == "ok"
