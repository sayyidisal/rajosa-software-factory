import logging
import os
from dataclasses import dataclass
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
DEFAULT_SUMOPOD_BASE_URL = "https://ai.sumopod.com/v1"
DEFAULT_SUMOPOD_MODEL = "gpt-4.1-mini"
ENV_FILE_PATH = API_ROOT / ".env"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    api_host: str
    api_port: int
    cors_origins: list[str]
    workspace_root: Path
    agent_adapter: str
    sumopod_api_key: str | None
    sumopod_base_url: str
    sumopod_model: str
    sumopod_timeout_seconds: float
    sumopod_max_output_chars: int
    execution_runner: str
    execution_image: str


def load_env_file(path: Path = ENV_FILE_PATH, *, environ: dict[str, str] | None = None) -> int:
    """Load KEY=VALUE pairs from a .env file without overriding existing env vars.

    Returns the number of keys applied. Invalid lines are ignored.
    Values are never logged.
    """
    target = os.environ if environ is None else environ
    if not path.is_file():
        return 0

    applied = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if key in target:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        target[key] = value
        applied += 1
    return applied


def get_settings(*, environ: dict[str, str] | None = None) -> Settings:
    load_env_file(ENV_FILE_PATH, environ=environ)
    env = os.environ if environ is None else environ

    workspace_value = env.get("WORKSPACE_ROOT", "var/workspaces")
    workspace_root = Path(workspace_value).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = API_ROOT / workspace_root

    raw_origins = env.get("CORS_ORIGINS")
    if raw_origins is None:
        cors_origins = list(DEFAULT_CORS_ORIGINS)
    else:
        cors_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    raw_key = env.get("SUMOPOD_API_KEY")
    sumopod_api_key = raw_key.strip() if raw_key and raw_key.strip() else None

    return Settings(
        api_host=env.get("API_HOST", "127.0.0.1"),
        api_port=int(env.get("API_PORT", "8000")),
        cors_origins=cors_origins,
        workspace_root=workspace_root,
        agent_adapter=env.get("AGENT_ADAPTER", "mock").strip() or "mock",
        sumopod_api_key=sumopod_api_key,
        sumopod_base_url=env.get("SUMOPOD_BASE_URL", DEFAULT_SUMOPOD_BASE_URL).rstrip("/"),
        sumopod_model=env.get("SUMOPOD_MODEL", DEFAULT_SUMOPOD_MODEL),
        sumopod_timeout_seconds=float(env.get("SUMOPOD_TIMEOUT_SECONDS", "60")),
        sumopod_max_output_chars=int(env.get("SUMOPOD_MAX_OUTPUT_CHARS", "16000")),
        # Isolated execution stays off until mandatory controls are verified.
        execution_runner=env.get("EXECUTION_RUNNER", "off").strip() or "off",
        execution_image=env.get(
            "EXECUTION_IMAGE", "rajosa-exec-python:3.12-pytest"
        ).strip()
        or "rajosa-exec-python:3.12-pytest",
    )
