import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

from app.agents.errors import AgentError
from app.agents.sumopod import (
    AUTH_MESSAGE,
    INVALID_RESPONSE_MESSAGE,
    MISSING_KEY_MESSAGE,
    NETWORK_MESSAGE,
    PROVIDER_UNAVAILABLE_MESSAGE,
    RATE_LIMIT_MESSAGE,
    TIMEOUT_MESSAGE,
    SumoPodAgentAdapter,
)
from app.config import Settings
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace

SECRET_KEY = "sk-secret-DO-NOT-LEAK-xyz"


def make_settings(**overrides) -> Settings:
    values = {
        "api_host": "127.0.0.1",
        "api_port": 8000,
        "cors_origins": ["http://127.0.0.1:3000"],
        "workspace_root": Path("/tmp/unused"),
        "agent_adapter": "sumopod",
        "sumopod_api_key": SECRET_KEY,
        "sumopod_base_url": "https://ai.sumopod.com/v1",
        "sumopod_model": "gpt-4.1-mini",
        "sumopod_timeout_seconds": 60.0,
        "sumopod_max_output_chars": 100,
        "execution_runner": "off",
        "execution_image": "rajosa-exec-python:3.12-pytest",
    }
    values.update(overrides)
    return Settings(**values)


def fake_response(content: str | None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def status_error(status_code: int, body: object = None) -> APIStatusError:
    request = httpx.Request("POST", "https://ai.sumopod.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return APIStatusError(
        f"Error {status_code} with {SECRET_KEY}",
        response=response,
        body=body or {"error": SECRET_KEY},
    )


@pytest.fixture
def workspace(tmp_path: Path) -> LocalWorkspace:
    root = tmp_path / "workspaces"
    root.mkdir()
    return LocalWorkspace(root)


def test_sumopod_success_writes_artifact(workspace: LocalWorkspace) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response("Hello from mock provider")
    adapter = SumoPodAgentAdapter(
        make_settings(),
        client_factory=lambda _settings: client,
    )
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)

    result = adapter.run(
        task_id=task_id,
        title="Demo",
        instruction="Say hello",
        workspace=workspace,
    )

    assert result.output == "Hello from mock provider"
    assert (workspace.task_directory(task_id) / "result.txt").read_text(encoding="utf-8") == (
        "Hello from mock provider"
    )
    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4.1-mini"
    assert client.chat.completions.create.call_count == 1


def test_missing_api_key_raises_safe_error(workspace: LocalWorkspace) -> None:
    adapter = SumoPodAgentAdapter(make_settings(sumopod_api_key=None))
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    with pytest.raises(AgentError) as exc_info:
        adapter.run(
            task_id=task_id,
            title="Demo",
            instruction="Say hello",
            workspace=workspace,
        )
    assert exc_info.value.public_message == MISSING_KEY_MESSAGE
    assert SECRET_KEY not in str(exc_info.value)


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (APITimeoutError(httpx.Request("POST", "https://example.test")), TIMEOUT_MESSAGE),
        (
            APIConnectionError(
                message=f"boom {SECRET_KEY}",
                request=httpx.Request("POST", "https://example.test"),
            ),
            NETWORK_MESSAGE,
        ),
        (
            AuthenticationError(
                f"auth {SECRET_KEY}",
                response=httpx.Response(
                    401, request=httpx.Request("POST", "https://example.test")
                ),
                body={"error": SECRET_KEY},
            ),
            AUTH_MESSAGE,
        ),
        (
            PermissionDeniedError(
                f"denied {SECRET_KEY}",
                response=httpx.Response(
                    403, request=httpx.Request("POST", "https://example.test")
                ),
                body={"error": SECRET_KEY},
            ),
            AUTH_MESSAGE,
        ),
        (
            RateLimitError(
                f"rate {SECRET_KEY}",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://example.test")
                ),
                body={"error": SECRET_KEY},
            ),
            RATE_LIMIT_MESSAGE,
        ),
        (status_error(500), PROVIDER_UNAVAILABLE_MESSAGE),
        (status_error(503), PROVIDER_UNAVAILABLE_MESSAGE),
    ],
)
def test_provider_errors_map_to_safe_messages(
    workspace: LocalWorkspace, exc: Exception, message: str
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = exc
    adapter = SumoPodAgentAdapter(
        make_settings(),
        client_factory=lambda _settings: client,
    )
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)

    with pytest.raises(AgentError) as exc_info:
        adapter.run(
            task_id=task_id,
            title="Demo",
            instruction="Say hello",
            workspace=workspace,
        )

    assert exc_info.value.public_message == message
    assert SECRET_KEY not in exc_info.value.public_message
    assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize("content", [None, "", "   "])
def test_empty_or_invalid_response(workspace: LocalWorkspace, content: str | None) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response(content)
    adapter = SumoPodAgentAdapter(
        make_settings(),
        client_factory=lambda _settings: client,
    )
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)

    with pytest.raises(AgentError) as exc_info:
        adapter.run(
            task_id=task_id,
            title="Demo",
            instruction="Say hello",
            workspace=workspace,
        )
    assert exc_info.value.public_message == INVALID_RESPONSE_MESSAGE


def test_output_is_truncated(workspace: LocalWorkspace) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response("x" * 250)
    adapter = SumoPodAgentAdapter(
        make_settings(sumopod_max_output_chars=100),
        client_factory=lambda _settings: client,
    )
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    result = adapter.run(
        task_id=task_id,
        title="Demo",
        instruction="Say hello",
        workspace=workspace,
    )
    assert len(result.output) == 100


def test_no_retry_on_failure(workspace: LocalWorkspace) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = status_error(500)
    adapter = SumoPodAgentAdapter(
        make_settings(),
        client_factory=lambda _settings: client,
    )
    task_id = str(uuid.uuid4())
    workspace.prepare(task_id)
    with pytest.raises(AgentError):
        adapter.run(
            task_id=task_id,
            title="Demo",
            instruction="Say hello",
            workspace=workspace,
        )
    assert client.chat.completions.create.call_count == 1


def test_task_endpoint_maps_provider_failure_safely(
    workspace_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = status_error(
        401, body={"error": f"bad key {SECRET_KEY}"}
    )
    settings = make_settings(workspace_root=workspace_root)
    adapter = SumoPodAgentAdapter(
        settings,
        client_factory=lambda _settings: client,
    )
    app = create_app(
        adapter=adapter,
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    http = TestClient(app)

    with caplog.at_level(logging.ERROR):
        response = http.post(
            "/tasks",
            json={"title": "Fail", "instruction": "trigger provider error"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["quality"]["passed"] is False
    assert body["error"] == AUTH_MESSAGE
    assert SECRET_KEY not in response.text
    assert "Authorization" not in response.text
    assert SECRET_KEY not in caplog.text
    assert "bad key" not in caplog.text


def test_missing_key_through_task_endpoint(workspace_root: Path) -> None:
    settings = make_settings(workspace_root=workspace_root, sumopod_api_key=None)
    app = create_app(
        adapter=SumoPodAgentAdapter(settings),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    http = TestClient(app)
    response = http.post(
        "/tasks",
        json={"title": "Config", "instruction": "needs key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == MISSING_KEY_MESSAGE


def test_http_200_with_empty_content_is_not_passed(workspace_root: Path) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response("")
    settings = make_settings(workspace_root=workspace_root)
    app = create_app(
        adapter=SumoPodAgentAdapter(
            settings,
            client_factory=lambda _settings: client,
        ),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    http = TestClient(app)
    response = http.post(
        "/tasks",
        json={"title": "Empty", "instruction": "provider returned empty"},
    )
    body = response.json()
    assert body["status"] == "failed"
    assert body["quality"]["passed"] is False
    assert body["error"] == INVALID_RESPONSE_MESSAGE
