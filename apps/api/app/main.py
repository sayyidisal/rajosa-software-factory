import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.factory import build_agent_adapter
from app.agents.mock import MockAgentAdapter
from app.agents.sumopod import SumoPodAgentAdapter
from app.api.routes import health, tasks
from app.config import get_settings
from app.domain.service import TaskService
from app.domain.store import MemoryTaskStore, TaskRepository
from app.execution.concurrency import GLOBAL_EXECUTION_GATE
from app.execution.factory import build_execution_runner
from app.execution.service import ExecutionService
from app.orchestration.service import OrchestratorService
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace

logger = logging.getLogger(__name__)


def _adapter_label(adapter: object) -> str:
    if isinstance(adapter, SumoPodAgentAdapter):
        return "sumopod"
    if isinstance(adapter, MockAgentAdapter):
        return "mock"
    return type(adapter).__name__


def create_app(
    *,
    adapter=None,
    gate=None,
    workspace: LocalWorkspace | None = None,
    store: TaskRepository | None = None,
    execution_runner=None,
    execution_gate=None,
) -> FastAPI:
    settings = get_settings()
    selected_adapter = (
        adapter if adapter is not None else build_agent_adapter(settings)
    )
    adapter_name = _adapter_label(selected_adapter)
    selected_gate = gate or BasicQualityGate()
    selected_workspace = workspace or LocalWorkspace(settings.workspace_root)
    selected_store = store or MemoryTaskStore()
    selected_runner = (
        execution_runner
        if execution_runner is not None
        else build_execution_runner(settings)
    )
    selected_execution_gate = execution_gate or GLOBAL_EXECUTION_GATE

    app = FastAPI(title="Rajosa AI Software Factory API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    orchestrator = OrchestratorService(
        adapter=selected_adapter,
        gate=selected_gate,
        workspace=selected_workspace,
        store=selected_store,
    )
    task_service = TaskService(orchestrator)
    execution_service = ExecutionService(
        runner=selected_runner,
        workspace=selected_workspace,
        store=selected_store,
        gate=selected_gate,
        gate_lock=selected_execution_gate,
        staging_base=selected_workspace.root,
        lifecycle_hook=orchestrator.apply_execution_outcome,
    )
    app.state.orchestrator = orchestrator
    app.state.task_service = task_service
    app.state.execution_service = execution_service
    app.state.settings = settings
    app.state.agent_adapter = adapter_name
    app.state.execution_runner = settings.execution_runner

    logger.info(
        "agent adapter selected: %s (class=%s)",
        adapter_name,
        type(selected_adapter).__name__,
    )
    logger.info(
        "execution runner mode: %s (class=%s)",
        settings.execution_runner,
        type(selected_runner).__name__,
    )

    app.include_router(health.router)
    app.include_router(tasks.router)
    return app


app = create_app()
