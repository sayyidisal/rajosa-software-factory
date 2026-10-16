"""Phase 4E.2 — SQLite app wiring and approval integrity (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.agents.mock import MockAgentAdapter, build_mock_plan
from app.domain.models import AgentResult
from app.domain.store import MemoryTaskStore
from app.execution.fake import FakeExecutionRunner
from app.main import create_app
from app.persistence.migrate import MigrationError
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from tests.orchestration_helpers import approve_plan
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


def _sqlite_app(
    tmp_path: Path,
    *,
    adapter=None,
    runner=None,
) -> tuple[TestClient, Path, object]:
    db_path = tmp_path / "tasks.sqlite3"
    ws = tmp_path / "workspaces"
    ws.mkdir()
    selected_adapter = adapter or CountingAdapter()
    app = create_app(
        adapter=selected_adapter,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=SqliteTaskRepository(db_path),
        execution_runner=runner if runner is not None else FakeExecutionRunner(),
    )
    return TestClient(app), db_path, selected_adapter


def test_task_and_events_survive_app_recreate(tmp_path: Path) -> None:
    client, db_path, adapter = _sqlite_app(tmp_path)
    created = client.post(
        "/tasks",
        json={"title": "Persist", "instruction": "Keep me"},
    ).json()
    task_id = created["id"]
    assert created["lifecycle"] == "awaiting_plan_approval"
    event_ids = [e["event_id"] for e in created["events"]]
    assert len(event_ids) >= 2
    planner_calls = adapter.planner_calls
    implementer_calls = adapter.implementer_calls

    client.app.state.task_store.close()

    # Recreate app against the same DB — no auto planner/implementer.
    adapter2 = CountingAdapter()
    app2 = create_app(
        adapter=adapter2,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(tmp_path / "workspaces"),
        store=SqliteTaskRepository(db_path),
        execution_runner=FakeExecutionRunner(),
    )
    client2 = TestClient(app2)
    fetched = client2.get(f"/tasks/{task_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["id"] == task_id
    assert body["lifecycle"] == "awaiting_plan_approval"
    assert [e["event_id"] for e in body["events"]] == event_ids
    assert adapter2.planner_calls == 0
    assert adapter2.implementer_calls == 0
    assert planner_calls >= 1
    assert implementer_calls == 0
    client2.app.state.task_store.close()


def test_terminal_and_valid_digest_survive_restart(tmp_path: Path) -> None:
    client, db_path, _ = _sqlite_app(tmp_path)
    created = client.post(
        "/tasks",
        json={"title": "Done", "instruction": "Finish"},
    ).json()
    approved = approve_plan(client, created["id"])
    assert approved["lifecycle"] == "completed"
    assert approved["approval_digest"]
    digest = approved["approval_digest"]
    events = [e["event_id"] for e in approved["events"]]

    client.app.state.task_store.close()
    app2 = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(tmp_path / "workspaces"),
        store=SqliteTaskRepository(db_path),
    )
    client2 = TestClient(app2)
    body = client2.get(f"/tasks/{created['id']}").json()
    assert body["lifecycle"] == "completed"
    assert body["status"] == "passed"
    assert body["approval_digest"] == digest
    assert [e["event_id"] for e in body["events"]] == events
    client2.app.state.task_store.close()


def test_digest_mismatch_rejected_without_overwrite(tmp_path: Path) -> None:
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

    client, _, _ = _sqlite_app(
        tmp_path, adapter=PyAdapter(), runner=FakeExecutionRunner()
    )
    created = client.post(
        "/tasks",
        json={
            "title": "Exec",
            "instruction": "tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    approved = approve_plan(client, created["id"])
    assert approved["lifecycle"] == "awaiting_execution_auth"
    good_digest = approved["approval_digest"]

    store: SqliteTaskRepository = client.app.state.task_store
    store._conn.execute(
        "UPDATE tasks SET approval_digest = ? WHERE id = ?",
        ("0" * 64, approved["id"]),
    )

    get_resp = client.get(f"/tasks/{approved['id']}")
    assert get_resp.status_code == 409
    assert "integrity" in get_resp.json()["detail"].lower()

    exec_resp = client.post(
        f"/tasks/{approved['id']}/execute-tests",
        json={"confirm": True},
    )
    assert exec_resp.status_code == 409

    row = store._conn.execute(
        "SELECT approval_digest, plan_json FROM tasks WHERE id = ?",
        (approved["id"],),
    ).fetchone()
    assert row["approval_digest"] == "0" * 64
    assert row["plan_json"]
    assert row["approval_digest"] != good_digest
    store.close()


def test_missing_digest_rejected(tmp_path: Path) -> None:
    client, db_path, _ = _sqlite_app(tmp_path)
    created = client.post(
        "/tasks",
        json={"title": "X", "instruction": "Y"},
    ).json()
    approved = approve_plan(client, created["id"])
    store: SqliteTaskRepository = client.app.state.task_store
    store._conn.execute(
        "UPDATE tasks SET approval_digest = NULL WHERE id = ?",
        (approved["id"],),
    )
    assert client.get(f"/tasks/{approved['id']}").status_code == 409
    store.close()


def test_broken_plan_rejected_without_repair(tmp_path: Path) -> None:
    client, _, _ = _sqlite_app(tmp_path)
    created = client.post(
        "/tasks",
        json={"title": "Broken", "instruction": "plan"},
    ).json()
    approved = approve_plan(client, created["id"])
    store: SqliteTaskRepository = client.app.state.task_store
    original = store._conn.execute(
        "SELECT plan_json, approval_digest FROM tasks WHERE id = ?",
        (approved["id"],),
    ).fetchone()
    store._conn.execute(
        "UPDATE tasks SET plan_json = ? WHERE id = ?",
        ("{not-valid-json", approved["id"]),
    )
    resp = client.get(f"/tasks/{approved['id']}")
    assert resp.status_code == 409
    row = store._conn.execute(
        "SELECT plan_json, approval_digest FROM tasks WHERE id = ?",
        (approved["id"],),
    ).fetchone()
    assert row["plan_json"] == "{not-valid-json"
    assert row["approval_digest"] == original["approval_digest"]
    store.close()


def test_unsupported_profile_rejected_without_repair(tmp_path: Path) -> None:
    client, _, _ = _sqlite_app(tmp_path)
    created = client.post(
        "/tasks",
        json={"title": "Profile", "instruction": "plan"},
    ).json()
    approved = approve_plan(client, created["id"])
    store: SqliteTaskRepository = client.app.state.task_store
    original = store._conn.execute(
        "SELECT plan_json, approval_digest, validation_profile FROM tasks WHERE id = ?",
        (approved["id"],),
    ).fetchone()
    store._conn.execute(
        "UPDATE tasks SET validation_profile = ? WHERE id = ?",
        ("not_a_real_profile", approved["id"]),
    )
    resp = client.get(f"/tasks/{approved['id']}")
    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "integrity" in detail or "unsupported" in detail
    row = store._conn.execute(
        "SELECT plan_json, approval_digest, validation_profile FROM tasks WHERE id = ?",
        (approved["id"],),
    ).fetchone()
    assert row["validation_profile"] == "not_a_real_profile"
    assert row["plan_json"] == original["plan_json"]
    assert row["approval_digest"] == original["approval_digest"]
    store.close()


def test_execute_confirm_and_runner_defaults(tmp_path: Path, monkeypatch) -> None:
    from app.execution.disabled import DisabledExecutionRunner
    from app.config import get_settings

    # Explicit environ dict + suite ENV_FILE stub: do not read apps/api/.env.
    monkeypatch.delenv("EXECUTION_RUNNER", raising=False)
    settings = get_settings(environ={"EXECUTION_RUNNER": "off"})
    assert settings.execution_runner == "off"

    VALID = (
        "def test_one():\n    assert True\n"
        "def test_two():\n    assert True\n"
        "def test_three():\n    assert True\n"
    )

    class Fixed:
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

    client, _, _ = _sqlite_app(
        tmp_path, adapter=Fixed(), runner=DisabledExecutionRunner()
    )
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

    missing = client.post(f"/tasks/{approved['id']}/execute-tests", json={})
    assert missing.status_code == 422

    false_confirm = client.post(
        f"/tasks/{approved['id']}/execute-tests",
        json={"confirm": False},
    )
    assert false_confirm.status_code == 422

    disabled = client.post(
        f"/tasks/{approved['id']}/execute-tests",
        json={"confirm": True},
    )
    assert disabled.status_code == 200
    assert disabled.json()["execution"]["runner_error_category"] == "feature_disabled"
    assert disabled.json()["lifecycle"] == "failed"
    client.app.state.task_store.close()


def test_migration_failure_fails_create_app(tmp_path: Path, monkeypatch) -> None:
    def boom(conn):
        raise MigrationError("forced migration failure")

    monkeypatch.setattr("app.persistence.sqlite.apply_migrations", boom)
    monkeypatch.setenv("TASK_DB_PATH", str(tmp_path / "fail.sqlite3"))
    # Clear cached env pollution: get_settings reads os.environ
    with pytest.raises(MigrationError, match="forced migration failure"):
        create_app(
            adapter=MockAgentAdapter(),
            gate=BasicQualityGate(),
            workspace=LocalWorkspace(tmp_path / "ws"),
            store=None,
        )


def test_memory_store_injection_still_works(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=MemoryTaskStore(),
    )
    assert isinstance(app.state.task_store, MemoryTaskStore)
    client = TestClient(app)
    body = client.post(
        "/tasks",
        json={"title": "Mem", "instruction": "ok"},
    ).json()
    assert body["lifecycle"] == "awaiting_plan_approval"
    # Restart simulation with a new Memory store loses state (expected).
    app2 = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=MemoryTaskStore(),
    )
    assert TestClient(app2).get(f"/tasks/{body['id']}").status_code == 404


def test_default_settings_task_db_path(monkeypatch, tmp_path: Path) -> None:
    from app.config import API_ROOT, get_settings

    # Empty environ + missing ENV_FILE (suite autouse): formula default only.
    # Does not open the database file.
    monkeypatch.delenv("TASK_DB_PATH", raising=False)
    settings = get_settings(environ={})
    assert settings.task_db_path == API_ROOT / "var" / "rajosa.sqlite3"
