from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.config import API_ROOT
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace

# Offline test credential only — never a production secret.
TEST_API_TOKEN = "test-rajosa-api-token-0001"

# Autouse TestClient header injection; auth-negative tests set enabled=False.
_AUTH_HEADER_INJECT = {"enabled": True}

# Resolved once: refuse any SqliteTaskRepository open of the live app DB.
ACTIVE_TASK_DB_PATH = (API_ROOT / "var" / "rajosa.sqlite3").resolve()

# Process-wide guard (collection + execution). Autouse monkeypatch alone only
# covers the test call phase, not import/collection side effects.
_ORIGINAL_SQLITE_INIT = SqliteTaskRepository.__init__


def _guarded_sqlite_repository_init(self, db_path):  # type: ignore[no-untyped-def]
    resolved = Path(db_path).expanduser().resolve()
    if resolved == ACTIVE_TASK_DB_PATH:
        raise RuntimeError(
            "test isolation: refused opening active task DB "
            f"{ACTIVE_TASK_DB_PATH}"
        )
    return _ORIGINAL_SQLITE_INIT(self, db_path)


SqliteTaskRepository.__init__ = _guarded_sqlite_repository_init  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _isolate_developer_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Point ``ENV_FILE_PATH`` at a disposable missing file for each test.

    Prevents ``get_settings()`` / ``create_app()`` from loading ``apps/api/.env``.
    Tests that intentionally exercise ``load_env_file`` pass an explicit path;
    tests that need a custom env file may override ``ENV_FILE_PATH`` again.
    """
    isolated = tmp_path / "pytest-no-developer.env"
    monkeypatch.setattr("app.config.ENV_FILE_PATH", isolated)
    return isolated


@pytest.fixture(autouse=True)
def _configure_api_token_and_client_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a test token and inject Bearer headers into TestClient requests."""
    monkeypatch.setenv("RAJOSA_API_TOKEN", TEST_API_TOKEN)
    monkeypatch.setenv("EXECUTION_RUNNER", "off")
    _AUTH_HEADER_INJECT["enabled"] = True

    original = TestClient.request

    def request_with_auth(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        headers = dict(kwargs.get("headers") or {})
        lower = {key.lower() for key in headers}
        if _AUTH_HEADER_INJECT["enabled"] and "authorization" not in lower:
            headers["Authorization"] = f"Bearer {TEST_API_TOKEN}"
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", request_with_auth)


@pytest.fixture
def auth_header_inject() -> dict[str, bool]:
    """Control automatic Authorization injection for the current test."""
    return _AUTH_HEADER_INJECT


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def client(workspace_root: Path) -> TestClient:
    app = create_app(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
    )
    return TestClient(app)


@pytest.fixture
def api_token() -> str:
    return TEST_API_TOKEN
