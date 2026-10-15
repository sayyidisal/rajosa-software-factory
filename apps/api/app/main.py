from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.mock import MockAgentAdapter
from app.api.routes import health, tasks
from app.config import get_settings
from app.domain.service import TaskService
from app.domain.store import MemoryTaskStore
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace


def create_app(
    *,
    adapter=None,
    gate=None,
    workspace: LocalWorkspace | None = None,
    store: MemoryTaskStore | None = None,
) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Rajosa AI Software Factory API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    task_service = TaskService(
        adapter=adapter or MockAgentAdapter(),
        gate=gate or BasicQualityGate(),
        workspace=workspace or LocalWorkspace(settings.workspace_root),
        store=store or MemoryTaskStore(),
    )
    app.state.task_service = task_service
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(tasks.router)
    return app


app = create_app()
