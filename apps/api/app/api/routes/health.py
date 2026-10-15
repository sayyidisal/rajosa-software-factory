from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    adapter = getattr(request.app.state, "agent_adapter", "unknown")
    execution_runner = getattr(request.app.state, "execution_runner", "off")
    return {
        "status": "ok",
        "service": "rajosa-api",
        "adapter": str(adapter),
        "execution_runner": str(execution_runner),
    }
