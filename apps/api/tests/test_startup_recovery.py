"""Phase 4E.3 — startup recovery and workspace reconciliation (offline)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.agents.mock import MockAgentAdapter
from app.domain.models import AgentResult, QualityReport, Task
from app.domain.store import MemoryTaskStore
from app.execution.fake import FakeExecutionRunner
from app.execution.models import ExecutionSummary
from app.main import create_app
from app.orchestration.plan import compute_approval_digest, parse_plan_text
from app.orchestration.recovery import (
    INTERRUPTIBLE_LIFECYCLES,
    StartupRecoveryError,
    recover_interrupted_tasks,
)
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from app.workspaces.reconcile import reconcile_workspaces
from tests.orchestration_helpers import DEFAULT_PLAN, approve_plan
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


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(
    store: SqliteTaskRepository,
    *,
    lifecycle: str,
    task_id: str | None = None,
    plan_approved: bool = False,
    with_plan: bool = False,
    approval_digest: str | None = None,
) -> Task:
    tid = task_id or _uuid()
    plan = None
    digest = approval_digest
    if with_plan:
        plan = parse_plan_text(
            json.dumps(DEFAULT_PLAN), validation_profile="generic_text"
        )
        if plan_approved and digest is None:
            digest = compute_approval_digest(
                plan=plan, validation_profile="generic_text"
            )
    status = "pending"
    if lifecycle == "completed":
        status = "passed"
    elif lifecycle == "failed":
        status = "failed"
    task = Task(
        id=tid,
        title="Seed",
        instruction="seed",
        validation_profile="generic_text",
        status=status,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        output="",
        error="already failed" if lifecycle == "failed" else None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=plan,
        plan_approved=plan_approved,
        approval_digest=digest,
        events=[],
    )
    return store.save(task)


def _owned_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter=None,
    runner=None,
):
    db_path = tmp_path / "owned.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    monkeypatch.setenv("TASK_DB_PATH", str(db_path))
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    selected = adapter or CountingAdapter()
    selected_runner = runner if runner is not None else CountingRunner()
    app = create_app(
        adapter=selected,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=None,
        execution_runner=selected_runner,
    )
    assert app.state.task_store_owned is True
    return app, db_path, ws, selected, selected_runner


def test_interruptible_lifecycles_become_failed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "rec.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    seed = SqliteTaskRepository(db_path)
    ids = {}
    for life in INTERRUPTIBLE_LIFECYCLES:
        ids[life] = _seed_task(seed, lifecycle=life).id
    wait_ap = _seed_task(seed, lifecycle="awaiting_plan_approval").id
    wait_ex = _seed_task(
        seed,
        lifecycle="awaiting_execution_auth",
        with_plan=True,
        plan_approved=True,
    ).id
    done = _seed_task(
        seed, lifecycle="completed", with_plan=True, plan_approved=True
    ).id
    failed = _seed_task(seed, lifecycle="failed").id
    seed.close()

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
        report = client.app.state.startup_recovery_report
        assert report is not None
        assert set(report.interrupted_task_ids) == set(ids.values())
        store: SqliteTaskRepository = client.app.state.task_store
        for life, tid in ids.items():
            task = store.get(tid)
            assert task is not None
            assert task.lifecycle == "failed"
            assert task.status == "failed"
            assert task.error and "interrupted" in task.error.lower()
            interrupted_events = [
                e
                for e in task.events
                if e.error_category == "interrupted" and e.event_type == "task.failed"
            ]
            assert len(interrupted_events) == 1
            assert interrupted_events[0].metadata.get("recovery") == "startup"
            assert interrupted_events[0].from_lifecycle == life

        assert store.get(wait_ap).lifecycle == "awaiting_plan_approval"
        assert store.get(wait_ex).lifecycle == "awaiting_execution_auth"
        assert store.get(done).lifecycle == "completed"
        assert store.get(failed).lifecycle == "failed"
        assert adapter.planner_calls == 0
        assert adapter.implementer_calls == 0
        assert runner.run_calls == 0

    # Second startup: no duplicate interrupted events.
    app2 = create_app(
        adapter=CountingAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=None,
        execution_runner=CountingRunner(),
    )
    with TestClient(app2) as client2:
        report2 = client2.app.state.startup_recovery_report
        assert report2 is not None
        assert report2.interrupted_task_ids == []
        store2: SqliteTaskRepository = client2.app.state.task_store
        for tid in ids.values():
            task = store2.get(tid)
            assert task is not None
            interrupted_events = [
                e for e in task.events if e.error_category == "interrupted"
            ]
            assert len(interrupted_events) == 1


def test_recovery_failure_aborts_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_store):
        raise StartupRecoveryError("forced recovery failure")

    monkeypatch.setattr("app.main.recover_interrupted_tasks", boom)
    app, _, _, _, _ = _owned_app(tmp_path, monkeypatch)
    with pytest.raises(StartupRecoveryError, match="forced recovery failure"):
        with TestClient(app):
            pass


def test_injected_store_skips_recovery_and_is_not_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "inj.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    store = SqliteTaskRepository(db_path)
    closed = {"value": False}
    original_close = store.close

    def tracking_close() -> None:
        closed["value"] = True
        original_close()

    store.close = tracking_close  # type: ignore[method-assign]
    tid = _seed_task(store, lifecycle="planning").id

    app = create_app(
        adapter=CountingAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=store,
        execution_runner=CountingRunner(),
    )
    assert app.state.task_store_owned is False
    with TestClient(app) as client:
        assert client.app.state.startup_recovery_report is None
        assert client.app.state.startup_recovery_completed is False
        assert store.get(tid).lifecycle == "planning"
    assert closed["value"] is False
    assert store.get(tid).lifecycle == "planning"
    store.close()


def test_reconcile_detects_orphans_and_is_read_only(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    db_path = tmp_path / "r.sqlite3"
    store = SqliteTaskRepository(db_path)
    workspace = LocalWorkspace(ws_root)

    orphan_id = _uuid()
    (ws_root / orphan_id).mkdir()

    missing_id = _seed_task(
        store, lifecycle="awaiting_plan_approval", task_id=_uuid()
    ).id

    approved = _seed_task(
        store,
        lifecycle="completed",
        task_id=_uuid(),
        with_plan=True,
        plan_approved=True,
    )
    task_dir = ws_root / approved.id
    task_dir.mkdir()
    (task_dir / "plan.approved.sha256").write_text("0" * 64, encoding="utf-8")

    missing_mirror = _seed_task(
        store,
        lifecycle="completed",
        task_id=_uuid(),
        with_plan=True,
        plan_approved=True,
    )
    (ws_root / missing_mirror.id).mkdir()

    exec_id = _uuid()
    exec_root = ws_root / "_executions" / exec_id
    exec_root.mkdir(parents=True)

    before_ids = store.list_task_ids()
    before_orphan_mtime = (ws_root / orphan_id).stat().st_mtime_ns
    before_exec_mtime = exec_root.stat().st_mtime_ns
    before_digest = (task_dir / "plan.approved.sha256").read_text(encoding="utf-8")

    report = reconcile_workspaces(workspace=workspace, store=store)
    assert report.incomplete is False
    assert orphan_id in report.orphan_workspace_dirs
    assert missing_id in report.missing_workspaces
    assert any(
        i.task_id == approved.id and i.kind == "mismatch"
        for i in report.digest_mirror_issues
    )
    assert any(
        i.task_id == missing_mirror.id and i.kind == "missing_file"
        for i in report.digest_mirror_issues
    )
    assert exec_id in report.leftover_executions

    assert store.list_task_ids() == before_ids
    assert store.get(approved.id).lifecycle == "completed"
    assert store.get(approved.id).approval_digest == approved.approval_digest
    assert (ws_root / orphan_id).is_dir()
    assert (ws_root / orphan_id).stat().st_mtime_ns == before_orphan_mtime
    assert exec_root.is_dir()
    assert exec_root.stat().st_mtime_ns == before_exec_mtime
    assert (task_dir / "plan.approved.sha256").read_text(encoding="utf-8") == before_digest
    store.close()


def test_reconcile_skips_symlinks_outside_root(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")

    link_name = _uuid()
    link_path = ws_root / link_name
    link_path.symlink_to(outside, target_is_directory=True)

    store = MemoryTaskStore()
    report = reconcile_workspaces(
        workspace=LocalWorkspace(ws_root), store=store
    )
    assert link_name in report.skipped_unsafe
    assert link_name not in report.orphan_workspace_dirs
    # Must not have descended into outside as an orphan tree.
    assert "secret.txt" not in report.orphan_workspace_dirs
    assert report.incomplete is False


def test_reconcile_incomplete_on_list_failure(tmp_path: Path) -> None:
    class BoomStore(MemoryTaskStore):
        def list_task_ids(self) -> list[str]:
            raise RuntimeError("list failed")

    report = reconcile_workspaces(
        workspace=LocalWorkspace(tmp_path / "ws"),
        store=BoomStore(),
    )
    # root may not exist → still hits list_task_ids
    assert report.incomplete is True
    assert report.error is not None
    assert "list" in report.error.lower()


def test_owned_startup_runs_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "owned.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    orphan = _uuid()
    (ws / orphan).mkdir()
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
        recon = client.app.state.reconciliation_report
        assert recon is not None
        assert orphan in recon.orphan_workspace_dirs


def test_h1_busy_still_409_without_lifecycle_damage(tmp_path: Path) -> None:
    """Regression: busy execute → 409; lifecycle stays awaiting_execution_auth."""
    from app.execution.concurrency import ExecutionGate
    from app.agents.mock import build_mock_plan
    import threading

    VALID = (
        "def test_one():\n    assert True\n"
        "def test_two():\n    assert True\n"
        "def test_three():\n    assert True\n"
    )

    class PyAdapter:
        def run(self, **kwargs):
            role = kwargs.get("role", "implementer")
            task_id = kwargs["task_id"]
            workspace = kwargs["workspace"]
            profile = kwargs.get("validation_profile", "generic_text")
            if role == "planner":
                content = json.dumps(build_mock_plan(validation_profile=profile))
                workspace.write_text(task_id, "plan.json", content)
                return AgentResult(output=content, artifact_name="plan.json")
            workspace.write_text(task_id, "result.txt", VALID)
            return AgentResult(output=VALID)

    class BlockingRunner(FakeExecutionRunner):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def run(self, request):  # type: ignore[no-untyped-def]
            self.entered.set()
            self.release.wait(timeout=5)
            return super().run(request)

    ws = tmp_path / "ws"
    ws.mkdir()
    runner = BlockingRunner()
    gate = ExecutionGate()
    app = create_app(
        adapter=PyAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=MemoryTaskStore(),
        execution_runner=runner,
        execution_gate=gate,
    )
    client = TestClient(app)
    created = client.post(
        "/tasks",
        json={
            "title": "Py",
            "instruction": "t",
            "validation_profile": "python_test_execution",
        },
    ).json()
    approved = approve_plan(client, created["id"])
    assert approved["lifecycle"] == "awaiting_execution_auth"
    task_id = approved["id"]

    results: list[int] = []

    def owner() -> None:
        r = client.post(
            f"/tasks/{task_id}/execute-tests",
            json={"confirm": True},
        )
        results.append(r.status_code)

    t = threading.Thread(target=owner)
    t.start()
    assert runner.entered.wait(timeout=5)
    busy = client.post(f"/tasks/{task_id}/execute-tests", json={"confirm": True})
    assert busy.status_code == 409
    after = client.get(f"/tasks/{task_id}").json()
    assert after["lifecycle"] == "awaiting_execution_auth"
    runner.release.set()
    t.join(timeout=5)
    assert results == [200]


def test_recover_interrupted_tasks_direct_unit(tmp_path: Path) -> None:
    store = SqliteTaskRepository(tmp_path / "u.sqlite3")
    tid = _seed_task(store, lifecycle="generating").id
    report = recover_interrupted_tasks(store)
    assert tid in report.interrupted_task_ids
    report2 = recover_interrupted_tasks(store)
    assert report2.interrupted_task_ids == []
    assert len([e for e in store.get(tid).events if e.error_category == "interrupted"]) == 1
    store.close()
