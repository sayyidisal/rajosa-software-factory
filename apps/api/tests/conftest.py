from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import MockAgentAdapter
from app.domain.store import MemoryTaskStore
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace


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
