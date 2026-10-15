from pathlib import Path

from fastapi.testclient import TestClient

from app.domain.models import AgentResult
from app.quality.basic import BasicQualityGate


def test_create_task_passes(client: TestClient, workspace_root: Path) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Hello", "instruction": "Write a greeting"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["error"] is None
    assert body["quality"]["passed"] is True
    assert body["output"]
    assert "Hello" in body["output"]

    task_dir = workspace_root / body["id"]
    assert (task_dir / "result.txt").is_file()

    fetched = client.get(f"/tasks/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "passed"


def test_invalid_payload_returns_422(client: TestClient) -> None:
    response = client.post("/tasks", json={"title": "", "instruction": "x"})
    assert response.status_code == 422


def test_basic_quality_gate_rejects_empty_output(tmp_path: Path) -> None:
    gate = BasicQualityGate()
    report = gate.evaluate(AgentResult(output=""), tmp_path)
    assert report.passed is False
    assert any(not check.passed for check in report.checks)


def test_basic_quality_gate_rejects_missing_artifact(tmp_path: Path) -> None:
    gate = BasicQualityGate()
    report = gate.evaluate(AgentResult(output="present"), tmp_path)
    assert report.passed is False
    missing = next(check for check in report.checks if check.name == "artifact_present")
    assert missing.passed is False


def test_unknown_task_returns_404(client: TestClient) -> None:
    response = client.get("/tasks/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404
