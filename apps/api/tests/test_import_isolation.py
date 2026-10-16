"""Phase 1 Exit Gate — import / factory isolation (no live DB, no server)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.config import API_ROOT
from app.domain.store import MemoryTaskStore
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from tests.conftest import ACTIVE_TASK_DB_PATH


def test_reloading_main_does_not_open_sqlite_or_migrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing / reloading ``app.main`` must not construct the default DB."""
    from app.persistence import sqlite as sqlite_mod

    repo_opens: list[str] = []
    migration_calls: list[object] = []
    real_repo_init = sqlite_mod.SqliteTaskRepository.__init__
    # Bound name inside sqlite.py — patch there, not migrate module alone.
    real_apply = sqlite_mod.apply_migrations

    def spy_repo_init(self, db_path) -> None:  # type: ignore[no-untyped-def]
        repo_opens.append(str(db_path))
        return real_repo_init(self, db_path)

    def spy_apply(conn) -> None:  # type: ignore[no-untyped-def]
        migration_calls.append(conn)
        return real_apply(conn)

    monkeypatch.setattr(sqlite_mod.SqliteTaskRepository, "__init__", spy_repo_init)
    monkeypatch.setattr(sqlite_mod, "apply_migrations", spy_apply)

    import app.main as main_mod

    reloaded = importlib.reload(main_mod)

    assert repo_opens == []
    assert migration_calls == []
    assert "app" not in reloaded.__dict__
    assert callable(reloaded.create_app)
    assert reloaded._lazy_app is None


def test_create_app_with_disposable_store_after_isolated_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory still builds apps with injected / disposable stores."""
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.setenv("EXECUTION_RUNNER", "off")
    monkeypatch.setenv("RAJOSA_API_TOKEN", "test-rajosa-api-token-0001")

    import app.main as main_mod

    importlib.reload(main_mod)

    ws = tmp_path / "ws"
    ws.mkdir()
    memory_app = main_mod.create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=MemoryTaskStore(),
    )
    assert isinstance(memory_app.state.task_store, MemoryTaskStore)
    assert memory_app.state.task_store_owned is False

    db_path = tmp_path / "disposable.sqlite3"
    sqlite_app = main_mod.create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(ws),
        store=SqliteTaskRepository(db_path),
    )
    client = TestClient(sqlite_app)
    created = client.post(
        "/tasks",
        json={"title": "Iso", "instruction": "ok"},
    )
    assert created.status_code == 200
    assert created.json()["lifecycle"] == "awaiting_plan_approval"
    sqlite_app.state.task_store.close()


def test_run_dev_wires_create_app_factory_without_starting_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make dev-api`` path must target the factory; uvicorn.run is not entered for real."""
    import uvicorn

    from app import run_dev

    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8000")
    monkeypatch.setenv("EXECUTION_RUNNER", "off")

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    # Ensure create_app is not invoked merely by wiring the launcher.
    import app.main as main_mod

    before = main_mod._lazy_app
    run_dev.main([])
    assert captured.get("app") == "app.main:create_app"
    assert captured.get("factory") is True
    assert captured.get("reload") is True
    assert main_mod._lazy_app is before


def test_active_task_db_open_is_refused() -> None:
    """Process-wide guard must refuse the live DB path (collection + tests)."""
    assert ACTIVE_TASK_DB_PATH == (API_ROOT / "var" / "rajosa.sqlite3").resolve()
    with pytest.raises(RuntimeError, match="refused opening active task DB"):
        SqliteTaskRepository(ACTIVE_TASK_DB_PATH)


def test_suite_env_file_path_is_not_developer_dotenv(
    tmp_path: Path,
    _isolate_developer_dotenv: Path,
) -> None:
    """Autouse isolation must not point ``ENV_FILE_PATH`` at apps/api/.env."""
    from app import config as app_config

    developer_dotenv = (API_ROOT / ".env").resolve()
    assert app_config.ENV_FILE_PATH.resolve() != developer_dotenv
    assert app_config.ENV_FILE_PATH == _isolate_developer_dotenv
    assert app_config.ENV_FILE_PATH.parent == tmp_path
