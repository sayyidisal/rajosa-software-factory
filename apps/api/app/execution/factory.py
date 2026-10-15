from app.config import Settings
from app.execution.base import ExecutionRunner
from app.execution.disabled import DisabledExecutionRunner
from app.execution.docker_runner import LocalDockerExecutionRunner
from app.execution.limits import DEFAULT_EXECUTION_IMAGE


def build_execution_runner(settings: Settings) -> ExecutionRunner:
    mode = (settings.execution_runner or "off").strip().lower()
    if mode in {"", "off", "disabled", "none"}:
        return DisabledExecutionRunner()
    if mode == "local_container":
        return LocalDockerExecutionRunner(
            image=settings.execution_image or DEFAULT_EXECUTION_IMAGE,
            allow_pull=False,
        )
    # Unknown modes fail closed — never host execution.
    return DisabledExecutionRunner()
