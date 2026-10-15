from typing import Protocol

from app.execution.models import ExecutionRequest, ExecutionResult


class ExecutionRunner(Protocol):
    """Runs an approved structured request inside an isolated environment."""

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute tests. Must never accept arbitrary shell commands."""
        ...

    def is_available(self) -> bool:
        """Return whether the runner can accept work right now."""
        ...
