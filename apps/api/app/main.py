import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agents.factory import build_agent_adapter
from app.agents.mock import MockAgentAdapter
from app.agents.sumopod import SumoPodAgentAdapter
from app.api.routes import health, tasks
from app.config import get_settings
from app.domain.service import TaskService
from app.domain.store import TaskRepository
from app.execution.concurrency import GLOBAL_EXECUTION_GATE
from app.execution.factory import build_execution_runner
from app.execution.service import ExecutionService
from app.orchestration.recovery import recover_interrupted_tasks
from app.orchestration.service import OrchestratorService
from app.orchestration.startup_diagnostics import build_startup_diagnostics
from app.persistence.sqlite import SqliteTaskRepository
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from app.workspaces.reconcile import ReconciliationReport, reconcile_workspaces

logger = logging.getLogger(__name__)


def _adapter_label(adapter: object) -> str:
    if isinstance(adapter, SumoPodAgentAdapter):
        return "sumopod"
    if isinstance(adapter, MockAgentAdapter):
        return "mock"
    return type(adapter).__name__


def _build_default_store(task_db_path) -> TaskRepository:
    """Open SQLite at ``task_db_path``; migrations run in the constructor.

    Single-process assumption: do not attach multiple uvicorn workers to one DB.
    """
    task_db_path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteTaskRepository(task_db_path)


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
    owns_store = store is None
    selected_store = store if store is not None else _build_default_store(settings.task_db_path)
    selected_runner = (
        execution_runner
        if execution_runner is not None
        else build_execution_runner(settings)
    )
    selected_execution_gate = execution_gate or GLOBAL_EXECUTION_GATE

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = getattr(app.state, "task_store_owned", False)
        if owned and not getattr(app.state, "startup_recovery_completed", False):
            store = app.state.task_store
            workspace = app.state.workspace
            # DB recovery first — StartupRecoveryError aborts startup.
            recovery_report = recover_interrupted_tasks(store)
            app.state.startup_recovery_report = recovery_report
            app.state.startup_recovery_completed = True
            logger.info(
                "startup recovery examined=%s interrupted=%s",
                recovery_report.examined,
                len(recovery_report.interrupted_task_ids),
            )
            try:
                recon = reconcile_workspaces(workspace=workspace, store=store)
            except Exception as exc:
                logger.exception("workspace reconciliation raised")
                recon = ReconciliationReport()
                recon.mark_failed(str(exc))
            app.state.reconciliation_report = recon
            if recon.incomplete:
                logger.error(
                    "workspace reconciliation incomplete: %s", recon.error
                )
            else:
                logger.info(
                    "workspace reconciliation orphans=%s missing=%s "
                    "leftover_exec=%s digest_issues=%s skipped_unsafe=%s",
                    len(recon.orphan_workspace_dirs),
                    len(recon.missing_workspaces),
                    len(recon.leftover_executions),
                    len(recon.digest_mirror_issues),
                    len(recon.skipped_unsafe),
                )
            app.state.startup_diagnostics = build_startup_diagnostics(
                recovery_completed=True,
                recovery_report=recovery_report,
                reconciliation_report=recon,
            )

        yield

        # Only close stores we opened; injected test doubles keep their own lifecycle.
        if not getattr(app.state, "task_store_owned", False):
            return
        repo = getattr(app.state, "task_store", None)
        closer = getattr(repo, "close", None)
        if callable(closer):
            closer()

    app = FastAPI(title="Rajosa AI Software Factory API", lifespan=lifespan)
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
    app.state.task_store = selected_store
    app.state.task_store_owned = owns_store
    app.state.workspace = selected_workspace
    app.state.settings = settings
    app.state.agent_adapter = adapter_name
    app.state.execution_runner = settings.execution_runner
    app.state.startup_recovery_completed = False
    app.state.startup_recovery_report = None
    app.state.reconciliation_report = None
    app.state.startup_diagnostics = build_startup_diagnostics(
        recovery_completed=False,
        recovery_report=None,
        reconciliation_report=None,
    )

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
    logger.info(
        "task store: %s (path=%s)",
        type(selected_store).__name__,
        getattr(settings, "task_db_path", None) if owns_store else "injected",
    )

    app.include_router(health.router)
    app.include_router(tasks.router)
    return app


app = create_app()
