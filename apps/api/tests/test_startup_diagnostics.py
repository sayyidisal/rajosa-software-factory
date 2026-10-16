"""Phase 4E.4b — safe startup diagnostics on /health (offline)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.domain.models import QualityReport, Task
from app.execution.fake import FakeExecutionRunner
from app.execution.models import ExecutionSummary
from app.main import create_app
from app.orchestration.recovery import RecoveryReport
from app.orchestration.startup_diagnostics import build_startup_diagnostics
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from app.workspaces.reconcile import DigestMirrorIssue, ReconciliationReport


def test_build_startup_diagnostics_excludes_paths_and_raw_errors() -> None:
    recovery = RecoveryReport(
        examined=2,
        interrupted_task_ids=["b-id", "a-id"],
        skipped_task_ids=[],
    )
    recon = ReconciliationReport(
        orphan_workspace_dirs=["/tmp/secret-orphan", "uuid-looking"],
        missing_workspaces=["missing-id"],
        digest_mirror_issues=[
            DigestMirrorIssue(
                task_id="t1",
                kind="mismatch",
                detail="/var/workspaces/t1/plan.approved.sha256 drifted",
            )
        ],
        leftover_executions=["exec-1"],
        skipped_unsafe=["../escape", "/etc/passwd"],
        incomplete=True,
        error="failed to list /Users/me/project/apps/api/var/workspaces: boom",
    )
    payload = build_startup_diagnostics(
        recovery_completed=True,
        recovery_report=recovery,
        reconciliation_report=recon,
    )
    serialized = json.dumps(payload)
    assert "/tmp/" not in serialized
    assert "/var/" not in serialized
    assert "/Users/" not in serialized
    assert "/etc/" not in serialized
    assert "plan.approved" not in serialized
    assert "boom" not in serialized
    assert "secret" not in serialized
    assert payload["recovery_completed"] is True
    assert payload["recovery"]["interrupted_count"] == 2
    assert "interrupted_task_ids" not in payload["recovery"]
    assert "a-id" not in serialized
    assert "b-id" not in serialized
    assert payload["reconciliation"]["incomplete"] is True
    assert payload["reconciliation"]["orphan_workspace_count"] == 2
    assert payload["reconciliation"]["missing_workspace_count"] == 1
    assert payload["reconciliation"]["digest_mirror_issue_count"] == 1
    assert payload["reconciliation"]["leftover_execution_count"] == 1
    assert payload["reconciliation"]["skipped_unsafe_count"] == 2
    assert "error" not in payload["reconciliation"]
    assert "orphan_workspace_dirs" not in payload["reconciliation"]


def test_owned_health_includes_startup_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "diag.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    orphan = str(uuid.uuid4())
    (ws / orphan).mkdir()

    store = SqliteTaskRepository(db_path)
    tid = str(uuid.uuid4())
    store.save(
        Task(
            id=tid,
            title="Mid",
            instruction="x",
            validation_profile="generic_text",
            status="pending",
            lifecycle="planning",
            output="",
            error=None,
            quality=QualityReport(passed=False, checks=[]),
            execution=ExecutionSummary.not_requested(),
            events=[],
        )
    )
    store.close()

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
        body = client.get("/health").json()
        assert body["execution_runner"] == "off"
        startup = body["startup"]
        assert startup["recovery_completed"] is True
        assert startup["recovery"]["interrupted_count"] == 1
        assert "interrupted_task_ids" not in startup["recovery"]
        assert startup["reconciliation"]["incomplete"] is False
        assert startup["reconciliation"]["orphan_workspace_count"] == 1
        serialized = json.dumps(body)
        assert str(ws.resolve()) not in serialized
        # Orphan directory names and task UUIDs must not appear — only counts.
        assert orphan not in serialized
        assert tid not in serialized
