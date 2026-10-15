"""Injectable fake runner for unit/integration tests — never runs real code."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.execution.models import ExecutionRequest, ExecutionResult


@dataclass
class FakeExecutionRunner:
    available: bool = True
    next_result: ExecutionResult | None = None
    calls: list[ExecutionRequest] = field(default_factory=list)
    raise_on_run: Exception | None = None

    def is_available(self) -> bool:
        return self.available

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if not self.available:
            return ExecutionResult(
                execution_id=request.execution_id,
                status="runner_unavailable",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="daemon_unavailable",
                detail="Fake runner unavailable",
            ).validated()
        if self.next_result is not None:
            result = self.next_result.model_copy(
                update={"execution_id": request.execution_id}
            )
            return result.validated()
        return ExecutionResult(
            execution_id=request.execution_id,
            status="completed",
            exit_code=0,
            tests_executed=True,
            tests_passed=True,
            stdout_summary="1 passed",
            duration_ms=10,
            detail="Fake runner success",
        ).validated()
