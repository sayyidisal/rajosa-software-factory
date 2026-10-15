import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.domain.models import AgentResult, CheckResult, QualityReport
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.quality.profiles import PRODUCT_CATALOG_CONFIG, ProductCatalogConfig
from app.quality.validators.product_catalog import validate_product_catalog_json
from app.workspaces.local import LocalWorkspace

VALID_PRODUCTS = {
    "products": [
        {"id": 1, "name": "Alpha", "price": 10.5, "in_stock": True},
        {"id": 2, "name": "Beta", "price": 3, "in_stock": False},
        {"id": 3, "name": "Gamma", "price": 99.99, "in_stock": True},
    ]
}


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    ws = LocalWorkspace(root)
    path = ws.prepare(str(uuid.uuid4()))
    return path


def _write(path: Path, content: str) -> AgentResult:
    (path / "result.txt").write_text(content, encoding="utf-8")
    return AgentResult(output=content)


@pytest.mark.parametrize(
    "payload",
    [
        '{"products":[{"id":1,"name":"A","price":NaN,"in_stock":true}]}',
        '{"products":[{"id":1,"name":"A","price":Infinity,"in_stock":true}]}',
        '{"products":[{"id":1,"name":"A","price":-Infinity,"in_stock":true}]}',
    ],
)
def test_product_rejects_nonstandard_constants(payload: str) -> None:
    checks = validate_product_catalog_json(payload, PRODUCT_CATALOG_CONFIG)
    assert any(not c.passed for c in checks)
    assert any(c.name in {"json_parseable", "product[0]_price_valid"} and not c.passed for c in checks)


def test_product_rejects_duplicate_keys() -> None:
    payload = '{"products":[], "products":[]}'
    checks = validate_product_catalog_json(payload, PRODUCT_CATALOG_CONFIG)
    assert any(c.name == "json_no_duplicate_keys" and not c.passed for c in checks)


def test_product_allows_extra_fields_by_policy(tmp_path: Path) -> None:
    data = {
        "products": [
            {**VALID_PRODUCTS["products"][0], "sku": "A1"},
            VALID_PRODUCTS["products"][1],
            VALID_PRODUCTS["products"][2],
        ],
        "meta": {"source": "test"},
    }
    path = _ws(tmp_path)
    result = _write(path, json.dumps(data))
    report = BasicQualityGate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is True


def test_product_rejects_oversized_input() -> None:
    config = ProductCatalogConfig(max_output_chars=100)
    checks = validate_product_catalog_json("x" * 101, config)
    assert any(c.name == "json_input_size" and not c.passed for c in checks)


def test_product_rejects_invisible_name(tmp_path: Path) -> None:
    data = {
        "products": [
            {"id": 1, "name": "\u200b\u200b", "price": 1, "in_stock": True},
            VALID_PRODUCTS["products"][1],
            VALID_PRODUCTS["products"][2],
        ]
    }
    path = _ws(tmp_path)
    result = _write(path, json.dumps(data))
    report = BasicQualityGate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is False
    assert any("name_valid" in c.name and not c.passed for c in report.checks)


def test_product_unicode_visible_name_passes(tmp_path: Path) -> None:
    data = {
        "products": [
            {"id": 1, "name": "カフェ", "price": 1.5, "in_stock": True},
            VALID_PRODUCTS["products"][1],
            VALID_PRODUCTS["products"][2],
        ]
    }
    path = _ws(tmp_path)
    result = _write(path, json.dumps(data, ensure_ascii=False))
    report = BasicQualityGate().evaluate(result, path, profile="product_catalog_json")
    assert report.passed is True


def test_python_ignores_def_in_comments_and_strings(tmp_path: Path) -> None:
    source = '''
# def test_fake():
#     assert False
COMMENT = """
def test_also_fake():
    assert False
"""

def test_one():
    assert True

def test_two():
    assert True

def test_three():
    assert True
'''
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True
    count = next(c for c in report.checks if c.name == "test_function_count")
    assert count.passed is True


def test_python_ignores_nested_test_functions(tmp_path: Path) -> None:
    source = '''
def helper():
    def test_nested():
        assert True
    return test_nested

def test_one():
    assert True

def test_two():
    assert True
'''
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "test_function_count" and not c.passed for c in report.checks)


def test_python_invalid_syntax_fails(tmp_path: Path) -> None:
    path = _ws(tmp_path)
    result = _write(path, "def test_one(:\n    pass\n")
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "python_syntax_valid" and not c.passed for c in report.checks)


def test_python_missing_required_test_names(tmp_path: Path, monkeypatch) -> None:
    from app.quality import profiles

    monkeypatch.setattr(
        profiles,
        "PYTHON_UNIT_TEST_CONFIG",
        profiles.PythonUnitTestConfig(
            expected_test_count=3,
            required_test_names=("test_one", "test_two", "test_three"),
        ),
    )
    # basic.py imported config at module level - need to patch where used
    monkeypatch.setattr(
        "app.quality.basic.PYTHON_UNIT_TEST_CONFIG",
        profiles.PythonUnitTestConfig(
            expected_test_count=3,
            required_test_names=("test_one", "test_two", "test_three"),
        ),
    )
    source = '''
def test_one():
    assert True
def test_two():
    assert True
def test_other():
    assert True
'''
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "required_test_names" and not c.passed for c in report.checks)


def test_python_empty_source_fails(tmp_path: Path) -> None:
    path = _ws(tmp_path)
    (path / "result.txt").write_text("   \n", encoding="utf-8")
    result = AgentResult(output="   \n")
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False


def test_python_ordinary_functions_without_tests_fail(tmp_path: Path) -> None:
    source = '''
def helper():
    return 1

def compute(x):
    return x + 1

def another():
    return 2
'''
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "test_function_count" and not c.passed for c in report.checks)


def test_python_code_rejects_def_only_in_string(tmp_path: Path) -> None:
    source = 'TEXT = """\ndef foo():\n    return 1\n"""\nx = 1\n'
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_code")
    assert report.passed is False
    assert any(c.name == "code_structure_valid" and not c.passed for c in report.checks)


def test_python_never_emits_tests_passed(tmp_path: Path) -> None:
    source = '''
def test_one():
    assert True
def test_two():
    assert True
def test_three():
    assert True
'''
    path = _ws(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert all(c.name != "tests_passed" for c in report.checks)


class ExplodingGate:
    def evaluate(self, result, workspace, *, profile="generic_text"):
        raise RuntimeError("boom /secret/path/key=abc")


class FixedAdapter:
    def __init__(self, content: str) -> None:
        self.content = content

    def run(
        self,
        *,
        task_id,
        title,
        instruction,
        workspace: WorkspaceWriter,
        role: str = "implementer",
    ):
        from tests.orchestration_helpers import DEFAULT_PLAN
        import json

        if role == "planner":
            content = json.dumps(DEFAULT_PLAN)
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")
        workspace.write_text(task_id, "result.txt", self.content)
        return AgentResult(output=self.content)


def test_validator_exception_does_not_pass_task(workspace_root: Path) -> None:
    from tests.orchestration_helpers import approve_plan

    app = create_app(
        adapter=FixedAdapter("hello"),
        gate=ExplodingGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"})
    assert created.status_code == 200
    response = client.post(
        f"/tasks/{created.json()['id']}/approve-plan",
        json={"approved": True},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["quality"]["passed"] is False
    assert "boom" not in response.text
    assert "/secret/path" not in response.text
    assert "key=abc" not in response.text
    assert "Traceback" not in response.text


def test_failed_check_never_marks_passed(workspace_root: Path) -> None:
    class LyingGate:
        def evaluate(self, result, workspace, *, profile="generic_text"):
            return QualityReport(
                passed=True,
                checks=[
                    CheckResult(name="forced", passed=False, detail="must fail"),
                ],
            )

    app = create_app(
        adapter=FixedAdapter("hello"),
        gate=LyingGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    body = client.post(
        f"/tasks/{created['id']}/approve-plan",
        json={"approved": True},
    ).json()
    assert body["status"] == "failed"
    assert body["quality"]["passed"] is False


def test_omit_validation_profile_remains_compatible(client: TestClient) -> None:
    from tests.orchestration_helpers import approve_plan

    response = client.post("/tasks", json={"title": "Hello", "instruction": "Write greeting"})
    assert response.status_code == 200
    body = response.json()
    assert body["validation_profile"] == "generic_text"
    assert body["lifecycle"] == "awaiting_plan_approval"
    approved = approve_plan(client, body["id"])
    assert approved["status"] == "passed"
    assert approved["lifecycle"] == "completed"


def test_unknown_profile_http_422(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "T", "instruction": "I", "validation_profile": "nope"},
    )
    assert response.status_code == 422
