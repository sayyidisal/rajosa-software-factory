"""Phase 4F.1 — execution failure characterization (offline, tests only).

Failure matrix coverage (reuse existing tests; do not duplicate flows):

| Case | Covered by |
|---|---|
| Runner disabled / unavailable | ``test_execution.test_disabled_runner_on_explicit_request``, |
| | ``test_execution.test_runner_unavailable_fails_safely``, |
| | ``test_persistence_integration.test_execute_confirm_and_runner_defaults`` |
| Nonzero exit | ``test_execution.test_nonzero_exit_is_test_failure`` |
| FakeRunner timeout (never passes) | ``test_execution.test_timeout_never_passes`` |
| Busy → HTTP 409, no mutation | ``test_execution.test_concurrent_busy_*``, |
| | ``test_startup_recovery.test_h1_busy_still_409_without_lifecycle_damage`` |
| Approval integrity rejection | ``test_execution.test_execute_before_plan_approval_rejected`` |
| Cleanup after success | ``test_execution.test_cleanup_after_success`` |
| Cleanup failure demotes success | ``test_execution.test_cleanup_failure_never_success`` |

Timeout cleanup (4F.2): on ``TimeoutExpired``, the runner best-effort
``docker kill``s the exact ``--name`` derived from the execution UUID.
Offline tests do **not** prove daemon-level container removal; they only
characterize the CLI argv and failure recording.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.agents.mock import MockAgentAdapter
from app.domain.models import AgentResult, QualityReport, Task
from app.execution.fake import FakeExecutionRunner
from app.execution.models import ExecutionSummary
from app.main import create_app
from app.orchestration.plan import compute_approval_digest, parse_plan_text
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from tests.orchestration_helpers import DEFAULT_RUNTIME_PLAN
import json


class CountingAdapter(MockAgentAdapter):
    def __init__(self) -> None:
        self.planner_calls = 0
        self.implementer_calls = 0

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: str = "implementer",
        validation_profile: str = "generic_text",
    ) -> AgentResult:
        if role == "planner":
            self.planner_calls += 1
        else:
            self.implementer_calls += 1
        return super().run(
            task_id=task_id,
            title=title,
            instruction=instruction,
            workspace=workspace,
            role=role,
            validation_profile=validation_profile,
        )


class CountingRunner(FakeExecutionRunner):
    def __init__(self) -> None:
        super().__init__()
        self.run_calls = 0

    def run(self, request):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        return super().run(request)


def test_t1_crash_restart_preserves_awaiting_execution_and_leftover_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate crash mid-execution: wait state + leftover staging survive startup."""
    db_path = tmp_path / "exec-char.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()

    store = SqliteTaskRepository(db_path)
    # Wait state needs approved plan + digest so GET integrity still passes.
    plan_exec = parse_plan_text(
        json.dumps(DEFAULT_RUNTIME_PLAN),
        validation_profile="python_test_execution",
    )
    digest = compute_approval_digest(
        plan=plan_exec, validation_profile="python_test_execution"
    )
    task_id = str(uuid.uuid4())
    store.save(
        Task(
            id=task_id,
            title="Waiting exec",
            instruction="tests",
            validation_profile="python_test_execution",
            status="pending",
            lifecycle="awaiting_execution_auth",
            output="",
            error=None,
            quality=QualityReport(passed=True, checks=[]),
            execution=ExecutionSummary.not_requested(),
            plan=plan_exec,
            plan_approved=True,
            approval_digest=digest,
            events=[],
        )
    )
    store.close()

    leftover_id = str(uuid.uuid4())
    leftover = ws / "_executions" / leftover_id
    leftover.mkdir(parents=True)
    (leftover / "tests").mkdir()
    marker = leftover / "orphan-marker.txt"
    marker.write_text("crash residue", encoding="utf-8")
    marker_mtime = marker.stat().st_mtime_ns

    monkeypatch.setenv("TASK_DB_PATH", str(db_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    adapter = CountingAdapter()
    runner = CountingRunner()
    app = create_app(
        adapter=adapter,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=None,
        execution_runner=runner,
    )

    with TestClient(app) as client:
        body = client.get(f"/tasks/{task_id}").json()
        assert body["lifecycle"] == "awaiting_execution_auth"
        assert body["status"] == "pending"
        assert body["execution"]["status"] == "not_requested"

        assert adapter.planner_calls == 0
        assert adapter.implementer_calls == 0
        assert runner.run_calls == 0
        assert runner.calls == []

        recon = client.app.state.reconciliation_report
        assert recon is not None
        assert leftover_id in recon.leftover_executions
        assert recon.incomplete is False

        # No automatic execution after startup (characterization of crash recovery).
        assert runner.run_calls == 0
        assert client.app.state.startup_recovery_completed is True

    assert leftover.is_dir()
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == "crash residue"
    assert marker.stat().st_mtime_ns == marker_mtime


def test_t3_cleanup_helpers_are_non_destructive_characterization(
    tmp_path: Path,
) -> None:
    """Staging layout helpers: leftover dirs are detectable; cleanup helper removes only target."""
    from app.execution.staging import cleanup_staging, create_execution_staging_root

    base = tmp_path / "ws"
    base.mkdir()
    exec_id = str(uuid.uuid4())
    root = create_execution_staging_root(base, exec_id)
    assert root.is_dir()
    assert (base / "_executions" / exec_id).is_dir()

    other_id = str(uuid.uuid4())
    other = base / "_executions" / other_id
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("stay", encoding="utf-8")

    cleanup_staging(root)
    assert not root.exists()
    assert other.is_dir()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "stay"


def test_t4_docker_timeout_kill_is_exact_name_only() -> None:
    """Kill helper targets prefixed names only; daemon stop remains unproven offline."""
    from app.execution.docker_runner import (
        CONTAINER_NAME_PREFIX,
        best_effort_kill_container,
        container_name_for_execution,
    )

    exec_id = str(uuid.uuid4())
    name = container_name_for_execution(exec_id)
    assert name.startswith(CONTAINER_NAME_PREFIX)
    assert name == f"{CONTAINER_NAME_PREFIX}{exec_id}"
    assert best_effort_kill_container(docker_bin="docker", container_name=None) == (
        "skipped_no_identity"
    )
    assert best_effort_kill_container(
        docker_bin="docker", container_name="not-our-prefix"
    ) == ("skipped_no_identity")
    assert best_effort_kill_container(
        docker_bin="docker", container_name="../escape"
    ) == ("skipped_no_identity")
