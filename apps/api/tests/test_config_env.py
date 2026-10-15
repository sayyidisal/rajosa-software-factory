from pathlib import Path

from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.agents.sumopod import (
    MISSING_KEY_MESSAGE,
    SumoPodAgentAdapter,
)
from app.config import get_settings, load_env_file
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace


def test_load_env_file_applies_keys_without_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_ADAPTER=sumopod\nSUMOPOD_MODEL=from-file\nEXISTING=keep-me\n",
        encoding="utf-8",
    )
    environ = {"EXISTING": "already-set"}
    applied = load_env_file(env_file, environ=environ)
    assert applied == 2
    assert environ["AGENT_ADAPTER"] == "sumopod"
    assert environ["SUMOPOD_MODEL"] == "from-file"
    assert environ["EXISTING"] == "already-set"


def test_load_env_file_ignores_invalid_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("not a valid line\n# comment\nAGENT_ADAPTER=mock\n", encoding="utf-8")
    environ: dict[str, str] = {}
    applied = load_env_file(env_file, environ=environ)
    assert applied == 1
    assert environ == {"AGENT_ADAPTER": "mock"}


def test_get_settings_reads_agent_adapter_from_environ(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.setenv("AGENT_ADAPTER", "sumopod")
    monkeypatch.setenv("SUMOPOD_API_KEY", "")
    settings = get_settings()
    assert settings.agent_adapter == "sumopod"


def test_create_app_uses_sumopod_when_configured(monkeypatch, workspace_root: Path, tmp_path: Path) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.setenv("AGENT_ADAPTER", "sumopod")
    monkeypatch.delenv("SUMOPOD_API_KEY", raising=False)
    app = create_app(
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    assert isinstance(app.state.task_service.adapter, SumoPodAgentAdapter)
    assert app.state.agent_adapter == "sumopod"

    client = TestClient(app)
    health = client.get("/health").json()
    assert health["adapter"] == "sumopod"

    response = client.post(
        "/tasks",
        json={"title": "Needs key", "instruction": "should fail safely"},
    )
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == MISSING_KEY_MESSAGE
    assert "Mock result" not in body.get("output", "")


def test_create_app_uses_mock_by_default(monkeypatch, workspace_root: Path, tmp_path: Path) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("AGENT_ADAPTER", raising=False)
    monkeypatch.delenv("SUMOPOD_API_KEY", raising=False)
    app = create_app(
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    assert isinstance(app.state.task_service.adapter, MockAgentAdapter)
    assert app.state.agent_adapter == "mock"
