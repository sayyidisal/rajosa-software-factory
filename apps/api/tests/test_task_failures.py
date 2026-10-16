from pathlib import Path

from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.domain.models import AgentResult, CheckResult, QualityReport
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.workspaces.local import LocalWorkspace


class RejectingGate:
    def evaluate(
        self,
        result: AgentResult,
        workspace: Path,
        *,
        profile: str = "generic_text",
    ) -> QualityReport:
        return QualityReport(
            passed=False,
            checks=[
                CheckResult(
                    name="forced_reject",
                    passed=False,
                    detail="rejected by test gate",
                )
            ],
        )


class ExplodingAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.secret = "SECRET_VALUE_DO_NOT_LEAK_abc123"

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace,
        role: str = "implementer",
        validation_profile: str = "generic_text",
    ) -> AgentResult:
        self.calls += 1
        raise RuntimeError(f"boom with {self.secret}")


def test_rejecting_gate_marks_task_failed(workspace_root: Path) -> None:
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=RejectingGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)

    created = client.post(
        "/tasks",
        json={"title": "Gate Fail", "instruction": "Should fail quality"},
    )
    assert created.status_code == 200
    assert created.json()["lifecycle"] == "awaiting_plan_approval"

    response = client.post(
        f"/tasks/{created.json()['id']}/approve-plan",
        json={"approved": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["lifecycle"] == "failed"
    assert body["quality"]["passed"] is False
    assert body["error"] == "Validation failed: rejected by test gate"
    assert body["output"]

    fetched = client.get(f"/tasks/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "failed"
    assert fetched.json()["quality"]["passed"] is False


def test_adapter_exception_marks_task_failed_safely(workspace_root: Path) -> None:
    adapter = ExplodingAdapter()
    app = create_app(
        adapter=adapter,
        gate=RejectingGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)

    response = client.post(
        "/tasks",
        json={"title": "Adapter Fail", "instruction": "Should explode"},
    )
    assert response.status_code == 200
    body = response.json()
    assert adapter.calls == 1
    assert body["status"] == "failed"
    assert body["lifecycle"] == "failed"
    assert body["quality"]["passed"] is False
    assert body["output"] == ""
    assert body["error"] == "The agent could not complete this task."
    assert adapter.secret not in response.text
    assert "RuntimeError" not in response.text
    assert "boom" not in response.text

    fetched = client.get(f"/tasks/{body['id']}")
    assert fetched.status_code == 200
    fetched_body = fetched.json()
    assert fetched_body["status"] == "failed"
    assert fetched_body["quality"]["passed"] is False
    assert adapter.secret not in fetched.text
    assert "RuntimeError" not in fetched.text
