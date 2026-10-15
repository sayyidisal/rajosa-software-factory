from fastapi import APIRouter, HTTPException, Request

from app.domain.models import Task, TaskCreate

router = APIRouter()


@router.post("/tasks", response_model=Task)
def create_task(payload: TaskCreate, request: Request) -> Task:
    service = request.app.state.task_service
    return service.create_task(title=payload.title, instruction=payload.instruction)


@router.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: str, request: Request) -> Task:
    service = request.app.state.task_service
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
