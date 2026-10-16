"""Phase 1A — offline smoke preflight (no server, no live DB, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.mock import MockAgentAdapter
from app.auth import normalize_api_token
from app.config import API_ROOT, get_settings
from app.smoke_preflight import (
    ACTIVE_TASK_DB_PATH,
    ACTIVE_WORKSPACE_ROOT,
    SMOKE_LOOPBACK_HOSTS,
    SMOKE_ROOT_ENV,
    SmokePreflightError,
    apply_smoke_overrides,
    assert_smoke_safe,
    normalize_smoke_bind_host,
    run_preflight,
)


SMOKE_TOKEN = "smoke-test-token-0001-xx"  # >= 16 chars; tests only


def _conflicting_developer_environ(tmp_path: Path) -> dict[str, str]:
    """Simulate a shell after sourcing a developer .env that prefers SumoPod."""
    del tmp_path  # reserved for path-based fixtures; env is synthetic
    return {
        "AGENT_ADAPTER": "sumopod",
        "EXECUTION_RUNNER": "local_container",
        "TASK_DB_PATH": str(ACTIVE_TASK_DB_PATH),
        "WORKSPACE_ROOT": str(ACTIVE_WORKSPACE_ROOT),
        "RAJOSA_API_TOKEN": "short",
        "SUMOPOD_API_KEY": "should-not-matter-for-mock",
        # Non-loopback must be overwritten by apply_smoke_overrides / Makefile.
        "API_HOST": "0.0.0.0",
        "API_PORT": "8000",
    }


def _safe_smoke_env(smoke_root: Path, *, api_host: str = "127.0.0.1") -> dict[str, str]:
    return {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
        "API_HOST": api_host,
    }


def test_importing_smoke_preflight_has_no_persistence_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Touching the module must not construct SqliteTaskRepository / migrate."""
    from app.persistence import sqlite as sqlite_mod

    import app.smoke_preflight as smoke_mod

    opens: list[str] = []
    real_init = sqlite_mod.SqliteTaskRepository.__init__

    def spy(self, db_path) -> None:  # type: ignore[no-untyped-def]
        opens.append(str(db_path))
        return real_init(self, db_path)

    monkeypatch.setattr(sqlite_mod.SqliteTaskRepository, "__init__", spy)

    # Re-bind names without importlib.reload (reload replaces exception classes
    # in the module dict and breaks later pytest.raises type checks).
    assert callable(smoke_mod.apply_smoke_overrides)
    assert callable(smoke_mod.run_preflight)
    assert callable(smoke_mod.assert_smoke_safe)
    _ = smoke_mod.ACTIVE_TASK_DB_PATH
    assert opens == []


def test_smoke_overrides_take_precedence_over_developer_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env = _conflicting_developer_environ(tmp_path)
    apply_smoke_overrides(env, smoke_root=smoke_root, token=SMOKE_TOKEN, api_port="18000")

    assert env["AGENT_ADAPTER"] == "mock"
    assert env["EXECUTION_RUNNER"] == "off"
    assert env["RAJOSA_API_TOKEN"] == SMOKE_TOKEN
    assert env["API_HOST"] == "127.0.0.1"
    assert Path(env["TASK_DB_PATH"]).resolve().is_relative_to(smoke_root.resolve())
    assert Path(env["WORKSPACE_ROOT"]).resolve().is_relative_to(smoke_root.resolve())
    assert Path(env["TASK_DB_PATH"]).resolve() != ACTIVE_TASK_DB_PATH
    assert Path(env["WORKSPACE_ROOT"]).resolve() != ACTIVE_WORKSPACE_ROOT

    from app.agents.factory import build_agent_adapter

    settings = get_settings(environ=env)
    assert settings.agent_adapter == "mock"
    assert settings.execution_runner == "off"
    assert settings.api_host == "127.0.0.1"
    assert normalize_api_token(settings.rajosa_api_token) == SMOKE_TOKEN
    assert_smoke_safe(settings, smoke_root=smoke_root)
    assert isinstance(build_agent_adapter(settings), MockAgentAdapter)


def test_active_db_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(ACTIVE_TASK_DB_PATH),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="active task database"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


def test_active_workspace_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(ACTIVE_WORKSPACE_ROOT),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="active workspace"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


def test_paths_outside_disposable_root_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(outside / "other.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="under smoke root"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


def test_non_mock_adapter_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "sumopod",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="AGENT_ADAPTER=mock"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "*", "192.168.1.10", "example.com", ""])
def test_non_loopback_api_host_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_host: str,
) -> None:
    """Caller-supplied non-loopback must fail closed before any bind."""
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env = _safe_smoke_env(smoke_root, api_host=bad_host)
    settings = get_settings(environ=env)
    assert settings.api_host == bad_host
    with pytest.raises(SmokePreflightError, match="loopback API_HOST"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


@pytest.mark.parametrize("ok_host", sorted(SMOKE_LOOPBACK_HOSTS))
def test_loopback_api_host_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ok_host: str,
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env = _safe_smoke_env(smoke_root, api_host=ok_host)
    settings = get_settings(environ=env)
    assert normalize_smoke_bind_host(settings.api_host) == ok_host
    assert_smoke_safe(settings, smoke_root=smoke_root)


def test_dotenv_non_loopback_host_rejected_via_effective_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.env`` API_HOST must be visible in get_settings and refused by preflight.

    Mirrors Makefile sourcing: keys already in the environ win; a file-only
    non-loopback host is applied by load_env_file when the key is absent.
    """
    env_file = tmp_path / "developer.env"
    env_file.write_text(
        "API_HOST=0.0.0.0\nAGENT_ADAPTER=mock\nEXECUTION_RUNNER=off\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.ENV_FILE_PATH", env_file)
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    # Intentionally omit API_HOST so the disposable .env supplies 0.0.0.0.
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    assert settings.api_host == "0.0.0.0"
    with pytest.raises(SmokePreflightError, match="loopback API_HOST"):
        run_preflight(environ=env)


def test_overrides_then_preflight_defeat_dotenv_non_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke overrides must win over a developer .env non-loopback host."""
    env_file = tmp_path / "developer.env"
    env_file.write_text("API_HOST=0.0.0.0\n", encoding="utf-8")
    monkeypatch.setattr("app.config.ENV_FILE_PATH", env_file)
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env = _conflicting_developer_environ(tmp_path)
    apply_smoke_overrides(env, smoke_root=smoke_root, token=SMOKE_TOKEN)
    # load_env_file must not reintroduce 0.0.0.0 once API_HOST is set.
    settings = run_preflight(environ=env)
    assert settings.api_host == "127.0.0.1"


def test_enabled_runner_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "local_container",
        "RAJOSA_API_TOKEN": SMOKE_TOKEN,
    }
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="EXECUTION_RUNNER=off"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


@pytest.mark.parametrize("token", [None, "", "short", "123456789012345"])
def test_missing_or_short_token_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env: dict[str, str] = {
        SMOKE_ROOT_ENV: str(smoke_root),
        "TASK_DB_PATH": str(smoke_root / "smoke.sqlite3"),
        "WORKSPACE_ROOT": str(smoke_root / "workspaces"),
        "AGENT_ADAPTER": "mock",
        "EXECUTION_RUNNER": "off",
    }
    if token is not None:
        env["RAJOSA_API_TOKEN"] = token
    settings = get_settings(environ=env)
    with pytest.raises(SmokePreflightError, match="RAJOSA_API_TOKEN"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


def test_run_preflight_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "smoke-run"
    smoke_root.mkdir()
    env = _conflicting_developer_environ(tmp_path)
    apply_smoke_overrides(env, smoke_root=smoke_root, token=SMOKE_TOKEN)
    settings = run_preflight(environ=env)
    assert settings.task_db_path.resolve().is_relative_to(smoke_root.resolve())
    assert settings.workspace_root.resolve().is_relative_to(smoke_root.resolve())
    assert settings.api_host == "127.0.0.1"


def test_smoke_makefile_uses_official_run_dev_entry() -> None:
    """Launcher wiring: smoke target must invoke run_dev, not a separate ASGI app."""
    root = Path(__file__).resolve().parents[3]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    assert "smoke-api:" in makefile
    assert "python -m app.smoke_preflight" in makefile
    assert "python -m app.run_dev" in makefile
    # Must not recurse into make dev-api (would re-source .env and clobber overrides).
    smoke_block = makefile.split("smoke-api:")[1].split("dev-web:")[0]
    assert "$(MAKE) dev-api" not in smoke_block
    assert "make dev-api" not in smoke_block
    assert "uvicorn" not in smoke_block
    # Force loopback after sourcing .env so inherited API_HOST cannot bind publicly.
    assert "API_HOST=127.0.0.1" in smoke_block
    env_source_idx = smoke_block.find("apps/api/.env")
    host_force_idx = smoke_block.find("API_HOST=127.0.0.1")
    assert env_source_idx != -1 and host_force_idx != -1
    assert env_source_idx < host_force_idx
    # Phase 1A must not change how the separate dev-api target loads dotenv.
    dev_block = makefile.split("dev-api:")[1].split("smoke-api:")[0]
    assert "API_HOST=127.0.0.1" not in dev_block
    assert "smoke_preflight" not in dev_block


def test_run_dev_module_still_uses_factory() -> None:
    from app import run_dev

    source = Path(run_dev.__file__).read_text(encoding="utf-8")
    assert 'uvicorn.run(' in source
    assert '"app.main:create_app"' in source
    assert "factory=True" in source


def test_active_path_constants_match_api_root() -> None:
    assert ACTIVE_TASK_DB_PATH == (API_ROOT / "var" / "rajosa.sqlite3").resolve()
    assert ACTIVE_WORKSPACE_ROOT == (API_ROOT / "var" / "workspaces").resolve()
