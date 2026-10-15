import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.domain.models import AgentResult
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.quality.limits import sanitize_max_chars
from app.quality.profiles import ProductCatalogConfig, PythonUnitTestConfig
from app.quality.validators.product_catalog import validate_product_catalog_json
from app.quality.validators.python_ast import collect_pytest_test_names, parse_python_source
from app.workspaces.local import LocalWorkspace


def _path(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return LocalWorkspace(root).prepare(str(uuid.uuid4()))


def _write(path: Path, content: str) -> AgentResult:
    (path / "result.txt").write_text(content, encoding="utf-8")
    return AgentResult(output=content)


def test_module_level_test_counted() -> None:
    parsed = parse_python_source(
        "def test_one():\n    assert True\n",
        max_source_chars=1000,
    )
    assert hasattr(parsed, "tree")
    assert collect_pytest_test_names(parsed.tree) == ["test_one"]  # type: ignore[union-attr]


def test_test_class_methods_counted(tmp_path: Path) -> None:
    source = '''
class TestThings:
    def test_alpha(self):
        assert True

    def test_beta(self):
        assert True

    def test_gamma(self):
        assert True
'''
    path = _path(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True
    names = collect_pytest_test_names(
        parse_python_source(source, max_source_chars=10_000).tree  # type: ignore[union-attr]
    )
    assert names == [
        "TestThings.test_alpha",
        "TestThings.test_beta",
        "TestThings.test_gamma",
    ]


def test_non_test_class_methods_not_counted(tmp_path: Path) -> None:
    source = '''
class Helper:
    def test_should_not_count(self):
        assert True

def test_one():
    assert True

def test_two():
    assert True

def test_three():
    assert True
'''
    path = _path(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True
    names = collect_pytest_test_names(
        parse_python_source(source, max_source_chars=10_000).tree  # type: ignore[union-attr]
    )
    assert "Helper.test_should_not_count" not in names
    assert names == ["test_one", "test_two", "test_three"]


def test_nested_and_comment_string_not_counted(tmp_path: Path) -> None:
    source = '''
# def test_commented():
#     assert False
TEXT = """
def test_in_string():
    assert False
"""

def outer():
    def test_nested():
        assert True
    return test_nested

def test_one():
    assert True
def test_two():
    assert True
def test_three():
    assert True
'''
    path = _path(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True


def test_invalid_syntax_fails(tmp_path: Path) -> None:
    path = _path(tmp_path)
    result = _write(path, "def test_one(:\n    pass\n")
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "python_syntax_valid" and not c.passed for c in report.checks)


def test_required_names_support_class_methods(tmp_path: Path, monkeypatch) -> None:
    cfg = PythonUnitTestConfig(
        expected_test_count=2,
        required_test_names=("TestFoo.test_a", "TestFoo.test_b"),
    )
    monkeypatch.setattr("app.quality.basic.PYTHON_UNIT_TEST_CONFIG", cfg)
    source = '''
class TestFoo:
    def test_a(self):
        assert True
    def test_b(self):
        assert True
'''
    path = _path(tmp_path)
    result = _write(path, source)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is True


def test_sanitize_max_chars_rejects_invalid_values() -> None:
    assert sanitize_max_chars(0, default=50) == 50
    assert sanitize_max_chars(-1, default=50) == 50
    assert sanitize_max_chars(True, default=50) == 50  # type: ignore[arg-type]
    assert sanitize_max_chars(100, default=50) == 100


def test_product_invalid_limit_falls_back_to_default() -> None:
    cfg = ProductCatalogConfig(max_output_chars=0)
    checks = validate_product_catalog_json("x" * 100_001, cfg)
    assert any(c.name == "json_input_size" and not c.passed for c in checks)


def test_generic_oversized_output_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.quality.basic.GENERIC_MAX_OUTPUT_CHARS", 20)
    path = _path(tmp_path)
    content = "y" * 21
    result = _write(path, content)
    report = BasicQualityGate().evaluate(result, path, profile="generic_text")
    assert report.passed is False
    assert any(c.name == "output_size" and not c.passed for c in report.checks)


def test_python_oversized_source_fails_before_parse(tmp_path: Path, monkeypatch) -> None:
    cfg = PythonUnitTestConfig(max_source_chars=30, expected_test_count=1)
    monkeypatch.setattr("app.quality.basic.PYTHON_UNIT_TEST_CONFIG", cfg)
    # Keep generic base size large enough so profile-specific limit is exercised.
    monkeypatch.setattr("app.quality.basic.GENERIC_MAX_OUTPUT_CHARS", 10_000)
    path = _path(tmp_path)
    content = "def test_one():\n    assert True\n" + ("# pad\n" * 20)
    result = _write(path, content)
    report = BasicQualityGate().evaluate(result, path, profile="python_unit_tests")
    assert report.passed is False
    assert any(c.name == "source_size" and not c.passed for c in report.checks)


class ExplodingGate:
    def evaluate(self, result, workspace, *, profile="generic_text"):
        raise RuntimeError("secret=/tmp/hidden key=sk-leak")


class FixedAdapter:
    def run(
        self,
        *,
        task_id,
        title,
        instruction,
        workspace: WorkspaceWriter,
        role: str = "implementer",
    ):
        import json

        from tests.orchestration_helpers import DEFAULT_PLAN

        if role == "planner":
            content = json.dumps(DEFAULT_PLAN)
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")
        workspace.write_text(task_id, "result.txt", "ok")
        return AgentResult(output="ok")


def test_exception_logs_context_without_secrets(
    workspace_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        adapter=FixedAdapter(),
        gate=ExplodingGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    client = TestClient(app)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    with caplog.at_level(logging.ERROR):
        body = client.post(
            f"/tasks/{created['id']}/approve-plan",
            json={"approved": True},
        ).json()
    assert body["status"] == "failed"
    assert "sk-leak" not in body.get("error", "")
    assert "/tmp/hidden" not in body.get("error", "")
    assert "sk-leak" not in caplog.text
    assert "/tmp/hidden" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "profile=generic_text" in caplog.text
    assert "task_id=" in caplog.text
