from fastapi import APIRouter, HTTPException, Request

from app.domain.models import PlanDecisionBody, Task, TaskCreate
from app.execution.concurrency import ExecutionBusyError
from app.execution.models import ExecuteTestsBody
from app.orchestration.service import OrchestrationConflict

router = APIRouter()


@router.post("/tasks", response_model=Task)
def create_task(payload: TaskCreate, request: Request) -> Task:
    """Create a task, generate a validated plan, and wait for plan approval.

    Never starts isolated execution. Never starts implementation without approval.
    """
    service = request.app.state.task_service
    return service.create_task(
        title=payload.title,
        instruction=payload.instruction,
        validation_profile=payload.validation_profile,
    )


@router.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: str, request: Request) -> Task:
    service = request.app.state.task_service
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/tasks/{task_id}/approve-plan", response_model=Task)
def approve_plan(
    task_id: str,
    payload: PlanDecisionBody,
    request: Request,
) -> Task:
    """Explicit plan approval or rejection.

    Requires ``approved`` boolean. ``true`` starts implementation at most once.
    ``false`` rejects the plan without generating artifacts or executing code.
    """
    orchestrator = request.app.state.orchestrator
    try:
        if payload.approved is True:
            return orchestrator.approve_plan(task_id, approved=True)
        if payload.approved is False:
            return orchestrator.reject_plan(task_id)
        raise ValueError("explicit plan approval decision is required")
    except LookupError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except OrchestrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/tasks/{task_id}/execute-tests", response_model=Task)
def execute_tests(
    task_id: str,
    payload: ExecuteTestsBody,
    request: Request,
) -> Task:
    """Explicitly request isolated Python test execution for an existing task.

    Selecting validation_profile alone does not authorize execution.
    Plan approval alone does not authorize execution.
    Model-generated text cannot authorize execution.
    Requires confirm=true in the request body.
    """
    service = request.app.state.execution_service
    try:
        return service.execute_tests(
            task_id=task_id,
            confirm=payload.confirm is True,
            wall_clock_seconds=payload.wall_clock_seconds,
            memory_mib=payload.memory_mib,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ExecutionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc) or "Another local execution is already active",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
