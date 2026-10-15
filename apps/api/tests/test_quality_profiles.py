import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.domain.models import AgentResult
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace

VALID_PRODUCTS = {
    "products": [
        {"id": 1, "name": "Alpha", "price": 10.5, "in_stock": True},
        {"id": 2, "name": "Beta", "price": 3, "in_stock": False},
        {"id": 3, "name": "Gamma", "price": 99.99, "in_stock": True},
    ]
}


class FixedOutputAdapter:
    def __init__(self, content: str, artifact_name: str = "result.txt") -> None:
        self.content = content
        self.artifact_name = artifact_name
        self.calls = 0

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: str = "implementer",
    ) -> AgentResult:
        from tests.orchestration_helpers import DEFAULT_PLAN

        self.calls += 1
        if role == "planner":
            plan = json.dumps(DEFAULT_PLAN)
            workspace.write_text(task_id, "plan.json", plan)
            return AgentResult(output=plan, artifact_name="plan.json")
        workspace.write_text(task_id, self.artifact_name, self.content)
        return AgentResult(output=self.content, artifact_name=self.artifact_name)


def _gate() -> BasicQualityGate:
    return BasicQualityGate()


def _workspace(tmp_path: Path) -> tuple[LocalWorkspace, Path]:
    root = tmp_path / "workspaces"
    root.mkdir()
    ws = LocalWorkspace(root)
    task_id = str(uuid.uuid4())
    path = ws.prepare(task_id)
    return ws, path


def _write_result(path: Path, content: str) -> AgentResult:
    (path / "result.txt").write_text(content, encoding="utf-8")
    return AgentResult(output=content)


def test_generic_nonempty_with_artifact_passes(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    result = _write_result(path, "hello")
    report = _gate().evaluate(result, path, profile="generic_text")
    assert report.passed is True
    assert {c.name for c in report.checks} >= {"output_not_empty", "artifact_present"}


def test_generic_empty_output_fails(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    (path / "result.txt").write_text("x", encoding="utf-8")
    report = _gate().evaluate(AgentResult(output=""), path, profile="generic_text")
    assert report.passed is False
    assert any(c.name == "output_not_empty" and not c.passed for c in report.checks)


def test_generic_missing_artifact_fails(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    report = _gate().evaluate(AgentResult(output="present"), path, profile="generic_text")
    assert report.passed is False
    assert any(c.name == "artifact_present" and not c.passed for c in report.checks)


def test_product_valid_json_passes(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    content = json.dumps(VALID_PRODUCTS)
    result = _write_result(path, content)
    report = _gate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is True


@pytest.mark.parametrize(
    ("content", "failed_name"),
    [
        ("{not-json", "json_parseable"),
        ("```json\n" + json.dumps(VALID_PRODUCTS) + "\n```", "json_no_markdown_fence"),
        ("Here you go:\n" + json.dumps(VALID_PRODUCTS), "json_parseable"),
        (json.dumps({"items": []}), "products_field_present"),
        (json.dumps({"products": {"id": 1}}), "products_is_array"),
        (
            json.dumps({"products": VALID_PRODUCTS["products"][:2]}),
            "products_exact_count",
        ),
        (
            json.dumps({"products": VALID_PRODUCTS["products"] + [VALID_PRODUCTS["products"][0]]}),
            "products_exact_count",
        ),
    ],
)
def test_product_invalid_shapes_fail(tmp_path: Path, content: str, failed_name: str) -> None:
    _, path = _workspace(tmp_path)
    result = _write_result(path, content)
    report = _gate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is False
    assert any(c.name == failed_name and not c.passed for c in report.checks)


@pytest.mark.parametrize(
    ("mutator", "failed_fragment"),
    [
        (lambda p: {**p, "id": True}, "id_type"),
        (lambda p: {**p, "price": True}, "price_valid"),
        (lambda p: {**p, "price": 0}, "price_valid"),
        (lambda p: {**p, "price": -1}, "price_valid"),
        (lambda p: {**p, "name": "  "}, "name_valid"),
        (lambda p: {k: v for k, v in p.items() if k != "name"}, "name_present"),
        (lambda p: {**p, "in_stock": "yes"}, "in_stock_type"),
    ],
)
def test_product_field_rules_fail(tmp_path: Path, mutator, failed_fragment: str) -> None:
    _, path = _workspace(tmp_path)
    products = [mutator(VALID_PRODUCTS["products"][0])] + VALID_PRODUCTS["products"][1:]
    content = json.dumps({"products": products})
    result = _write_result(path, content)
    report = _gate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is False
    assert any(failed_fragment in c.name and not c.passed for c in report.checks)


VALID_TESTS = """
def test_one():
    assert True

def test_two():
    assert True

def test_three():
    assert True
"""


def test_python_unit_tests_structure_passes(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    result = _write_result(path, VALID_TESTS)
    report = _gate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True
    assert any(c.name == "test_structure_valid" and c.passed for c in report.checks)
    assert not any(c.name == "tests_passed" for c in report.checks)


def test_python_unit_tests_placeholder_import_fails(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    content = "from your_module import foo\n\n" + VALID_TESTS
    result = _write_result(path, content)
    report = _gate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "test_direct_usability" and not c.passed for c in report.checks)


def test_python_unit_tests_count_mismatch_fails(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    content = "def test_one():\n    assert True\n"
    result = _write_result(path, content)
    report = _gate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "test_function_count" and not c.passed for c in report.checks)


def test_python_unit_tests_never_claims_execution(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    result = _write_result(path, VALID_TESTS)
    report = _gate().evaluate(result, path, profile="python_unit_tests")
    joined = " ".join(c.name + " " + c.detail for c in report.checks).lower()
    assert "tests_passed" not in joined
    assert "were not executed" in joined


def test_unknown_profile_fails_clearly(tmp_path: Path) -> None:
    _, path = _workspace(tmp_path)
    result = _write_result(path, "x")
    report = _gate().evaluate(result, path, profile="not_a_real_profile")
    assert report.passed is False
    assert any(c.name == "validation_profile_supported" and not c.passed for c in report.checks)


def test_task_validation_failure_marks_failed(workspace_root: Path) -> None:
    from tests.orchestration_helpers import approve_plan

    adapter = FixedOutputAdapter("{bad")
    app = create_app(
        adapter=adapter,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)
    created = client.post(
        "/tasks",
        json={
            "title": "Catalog",
            "instruction": "Return products JSON",
            "validation_profile": "product_catalog_json",
        },
    )
    assert created.status_code == 200
    assert created.json()["lifecycle"] == "awaiting_plan_approval"
    body = approve_plan(client, created.json()["id"])
    assert body["status"] == "failed"
    assert body["quality"]["passed"] is False
    assert body["error"] and body["error"].startswith("Validation failed:")
    assert adapter.calls == 2


def test_task_validation_success_marks_passed(workspace_root: Path) -> None:
    from tests.orchestration_helpers import approve_plan

    adapter = FixedOutputAdapter(json.dumps(VALID_PRODUCTS))
    app = create_app(
        adapter=adapter,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)
    created = client.post(
        "/tasks",
        json={
            "title": "Catalog",
            "instruction": "Return products JSON",
            "validation_profile": "product_catalog_json",
        },
    ).json()
    body = approve_plan(client, created["id"])
    assert body["status"] == "passed"
    assert body["lifecycle"] == "completed"
    assert body["quality"]["passed"] is True
    assert body["error"] is None
    assert body["validation_profile"] == "product_catalog_json"


def test_unknown_profile_rejected_by_api(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={
            "title": "X",
            "instruction": "Y",
            "validation_profile": "invented_profile",
        },
    )
    assert response.status_code == 422


def test_mock_adapter_still_passes_generic(client: TestClient) -> None:
    from tests.orchestration_helpers import approve_plan

    response = client.post(
        "/tasks",
        json={"title": "Hello", "instruction": "Write a greeting"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "awaiting_plan_approval"
    approved = approve_plan(client, body["id"])
    assert approved["status"] == "passed"
    assert approved["validation_profile"] == "generic_text"
    assert approved["output"].startswith("Mock result")
