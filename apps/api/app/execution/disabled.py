from app.execution.models import ExecutionRequest, ExecutionResult


class DisabledExecutionRunner:
    """Default runner — never executes code. Feature stays off until enabled."""

    def is_available(self) -> bool:
        return False

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            execution_id=request.execution_id,
            status="runner_unavailable",
            tests_executed=False,
            tests_passed=False,
            runner_error_category="feature_disabled",
            detail=(
                "Isolated test execution is disabled "
                "(EXECUTION_RUNNER=off). No code was executed."
            ),
        ).validated()
