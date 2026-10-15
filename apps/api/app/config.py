import os
from dataclasses import dataclass
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    cors_origins: list[str]
    workspace_root: Path


def get_settings() -> Settings:
    workspace_value = os.environ.get("WORKSPACE_ROOT", "var/workspaces")
    workspace_root = Path(workspace_value).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = API_ROOT / workspace_root

    raw_origins = os.environ.get("CORS_ORIGINS")
    if raw_origins is None:
        cors_origins = list(DEFAULT_CORS_ORIGINS)
    else:
        cors_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    return Settings(
        api_host=os.environ.get("API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("API_PORT", "8000")),
        cors_origins=cors_origins,
        workspace_root=workspace_root,
    )
