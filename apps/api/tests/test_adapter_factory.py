from pathlib import Path

import pytest

from app.agents.factory import build_agent_adapter
from app.agents.mock import MockAgentAdapter
from app.agents.sumopod import SumoPodAgentAdapter
from app.config import Settings


def _settings(agent_adapter: str, *, api_key: str | None = None) -> Settings:
    return Settings(
        api_host="127.0.0.1",
        api_port=8000,
        cors_origins=["http://127.0.0.1:3000"],
        workspace_root=Path("/tmp/unused"),
        task_db_path=Path("/tmp/unused-tasks.sqlite3"),
        agent_adapter=agent_adapter,
        sumopod_api_key=api_key,
        sumopod_base_url="https://ai.sumopod.com/v1",
        sumopod_model="gpt-4.1-mini",
        sumopod_timeout_seconds=60.0,
        sumopod_max_output_chars=16000,
        execution_runner="off",
        execution_image="rajosa-exec-python:3.12-pytest",
        rajosa_api_token="test-rajosa-api-token-0001",
    )


def test_factory_defaults_to_mock() -> None:
    assert isinstance(build_agent_adapter(_settings("mock")), MockAgentAdapter)


def test_factory_selects_sumopod() -> None:
    assert isinstance(build_agent_adapter(_settings("sumopod")), SumoPodAgentAdapter)


def test_factory_sumopod_without_api_key_still_selects_sumopod() -> None:
    adapter = build_agent_adapter(_settings("sumopod", api_key=None))
    assert isinstance(adapter, SumoPodAgentAdapter)


def test_factory_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="Unknown AGENT_ADAPTER"):
        build_agent_adapter(_settings("unknown"))
