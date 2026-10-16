from typing import Any

from fastapi import APIRouter, Request

from app.orchestration.startup_diagnostics import build_startup_diagnostics

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    adapter = getattr(request.app.state, "agent_adapter", "unknown")
    execution_runner = getattr(request.app.state, "execution_runner", "off")
    diagnostics = getattr(request.app.state, "startup_diagnostics", None)
    if diagnostics is None:
        diagnostics = build_startup_diagnostics(
            recovery_completed=bool(
                getattr(request.app.state, "startup_recovery_completed", False)
            ),
            recovery_report=getattr(request.app.state, "startup_recovery_report", None),
            reconciliation_report=getattr(
                request.app.state, "reconciliation_report", None
            ),
        )
    return {
        "status": "ok",
        "service": "rajosa-api",
        "adapter": str(adapter),
        "execution_runner": str(execution_runner),
        "startup": diagnostics,
    }
