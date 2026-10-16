"""Phase 4H.1 — bearer-token gate for task routes (offline)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.auth import normalize_api_token
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from tests.conftest import TEST_API_TOKEN


PROTECTED_PATHS = (
    ("POST", "/tasks", {"title": "T", "instruction": "I"}),
    ("GET", "/tasks/00000000-0000-4000-8000-000000000001", None),
    (
        "POST",
        "/tasks/00000000-0000-4000-8000-000000000001/approve-plan",
        {"approved": True},
    ),
    (
        "POST",
        "/tasks/00000000-0000-4000-8000-000000000001/execute-tests",
        {"confirm": True},
    ),
)


def _app(workspace_root: Path) -> TestClient:
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    return TestClient(app)


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_PATHS)
def test_missing_authorization_returns_401(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
    method: str,
    path: str,
    body: dict | None,
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"
    assert TEST_API_TOKEN not in response.text


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED_PATHS)
def test_wrong_token_returns_401(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
    method: str,
    path: str,
    body: dict | None,
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.request(
        method,
        path,
        json=body,
        headers={"Authorization": "Bearer wrong-token-not-the-test-value"},
    )
    assert response.status_code == 401
    assert "wrong-token" not in response.text
    assert TEST_API_TOKEN not in response.text


def test_unconfigured_token_fail_closed(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_header_inject: dict[str, bool],
) -> None:
    monkeypatch.delenv("RAJOSA_API_TOKEN", raising=False)
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.post(
        "/tasks",
        json={"title": "T", "instruction": "I"},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    assert response.status_code == 401
    assert client.app.state.task_store.list_task_ids() == []


def test_short_token_treated_as_unconfigured(
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_header_inject: dict[str, bool],
) -> None:
    monkeypatch.setenv("RAJOSA_API_TOKEN", "tooshort")
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.post(
        "/tasks",
        json={"title": "T", "instruction": "I"},
        headers={"Authorization": "Bearer tooshort"},
    )
    assert response.status_code == 401
    assert client.app.state.task_store.list_task_ids() == []


def test_valid_token_allows_create_and_get(
    workspace_root: Path,
    api_token: str,
) -> None:
    client = _app(workspace_root)
    created = client.post(
        "/tasks",
        json={"title": "Hello", "instruction": "Write a greeting"},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert created.status_code == 200
    task_id = created.json()["id"]
    fetched = client.get(
        f"/tasks/{task_id}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == task_id
    assert api_token not in fetched.text


def test_failed_auth_does_not_mutate_store_or_workspace(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    store = client.app.state.task_store
    before_ids = store.list_task_ids()
    before_dirs = list(workspace_root.iterdir()) if workspace_root.exists() else []

    response = client.post(
        "/tasks",
        json={"title": "Nope", "instruction": "must not run"},
    )
    assert response.status_code == 401
    assert store.list_task_ids() == before_ids
    after_dirs = list(workspace_root.iterdir()) if workspace_root.exists() else []
    assert after_dirs == before_dirs


def test_health_public_without_auth(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    recovery = (body.get("startup") or {}).get("recovery")
    if recovery is not None:
        assert "interrupted_task_ids" not in recovery
    assert TEST_API_TOKEN not in response.text


def test_token_not_logged_on_auth_failure(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    secret = "super-secret-candidate-token"
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/tasks",
            json={"title": "T", "instruction": "I"},
            headers={"Authorization": f"Bearer {secret}"},
        )
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in combined
    assert TEST_API_TOKEN not in combined


def test_query_and_body_tokens_ignored(
    workspace_root: Path,
    auth_header_inject: dict[str, bool],
    api_token: str,
) -> None:
    auth_header_inject["enabled"] = False
    client = _app(workspace_root)
    response = client.post(
        f"/tasks?access_token={api_token}",
        json={
            "title": "T",
            "instruction": "I",
            "api_token": api_token,
            "authorization": f"Bearer {api_token}",
        },
    )
    assert response.status_code == 401
    assert client.app.state.task_store.list_task_ids() == []


def test_normalize_api_token() -> None:
    assert normalize_api_token(None) is None
    assert normalize_api_token("") is None
    assert normalize_api_token("   ") is None
    assert normalize_api_token("short") is None
    assert normalize_api_token(TEST_API_TOKEN) == TEST_API_TOKEN
