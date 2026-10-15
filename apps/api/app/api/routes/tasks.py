from fastapi import APIRouter, HTTPException, Request

from app.domain.models import Task, TaskCreate
from app.execution.models import ExecuteTestsBody

router = APIRouter()


@router.post("/tasks", response_model=Task)
def create_task(payload: TaskCreate, request: Request) -> Task:
    """Create and statically validate a task. Never starts isolated execution."""
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


@router.post("/tasks/{task_id}/execute-tests", response_model=Task)
def execute_tests(
    task_id: str,
    payload: ExecuteTestsBody,
    request: Request,
) -> Task:
    """Explicitly request isolated Python test execution for an existing task.

    Selecting validation_profile alone does not authorize execution.
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
