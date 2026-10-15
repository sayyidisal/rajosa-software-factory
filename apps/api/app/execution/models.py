from typing import Any, Literal

from pydantic import BaseModel, Field

from app.execution.limits import (
    APPROVED_RUNTIME_ID,
    APPROVED_TEST_PROFILE,
    DEFAULT_LIMITS,
    ExecutionLimits,
)

ExecutionStatus = Literal[
    "not_requested",
    "starting",
    "completed",
    "timed_out",
    "runner_unavailable",
    "infrastructure_error",
    "cleanup_error",
    "rejected",
    "tests_failed",
]

RunnerErrorCategory = Literal[
    "feature_disabled",
    "daemon_unavailable",
    "image_unavailable",
    "busy",
    "staging_error",
    "timeout",
    "runtime_crash",
    "invalid_result",
    "cleanup_failed",
    "unsupported_profile",
    "authorization_missing",
    "static_validation_failed",
    "seccomp_unavailable",
    "none",
]


class StagedArtifact(BaseModel):
    relative_path: str
    size_bytes: int
    sha256: str | None = None


class ExecutionRequest(BaseModel):
    """Structured runner request — never an arbitrary shell command."""

    runtime_id: Literal["local_docker_python"] = APPROVED_RUNTIME_ID
    test_profile: Literal["python_test_execution"] = APPROVED_TEST_PROFILE
    execution_id: str
    task_id: str
    staged_root: str
    artifacts: list[StagedArtifact]
    limits: ExecutionLimits = Field(default_factory=lambda: DEFAULT_LIMITS)


class SafeArtifactMeta(BaseModel):
    name: str
    size_bytes: int


class ExecutionResult(BaseModel):
    execution_id: str
    status: ExecutionStatus
    exit_code: int | None = None
    timed_out: bool = False
    runner_error_category: RunnerErrorCategory = "none"
    tests_executed: bool = False
    tests_passed: bool = False
    stdout_summary: str = ""
    stderr_summary: str = ""
    duration_ms: int = 0
    artifacts: list[SafeArtifactMeta] = Field(default_factory=list)
    detail: str = ""

    def validated(self) -> "ExecutionResult":
        """Fail closed on contradictory or forged-looking results."""
        if self.tests_passed:
            if self.status != "completed":
                return self.model_copy(
                    update={
                        "tests_passed": False,
                        "tests_executed": False,
                        "status": "infrastructure_error",
                        "runner_error_category": "invalid_result",
                        "detail": "tests_passed claimed without completed status",
                    }
                )
            if not self.tests_executed:
                return self.model_copy(
                    update={
                        "tests_passed": False,
                        "status": "infrastructure_error",
                        "runner_error_category": "invalid_result",
                        "detail": "tests_passed claimed without tests_executed",
                    }
                )
            if self.timed_out:
                return self.model_copy(
                    update={
                        "tests_passed": False,
                        "status": "timed_out",
                        "runner_error_category": "timeout",
                        "detail": "timeout cannot produce tests_passed",
                    }
                )
            if self.exit_code != 0:
                return self.model_copy(
                    update={
                        "tests_passed": False,
                        "status": "tests_failed",
                        "detail": "non-zero exit code cannot produce tests_passed",
                    }
                )
            if self.runner_error_category not in ("none",):
                return self.model_copy(
                    update={
                        "tests_passed": False,
                        "status": "infrastructure_error",
                        "detail": "runner error cannot produce tests_passed",
                    }
                )
        if self.timed_out and self.tests_passed:
            return self.model_copy(
                update={
                    "tests_passed": False,
                    "status": "timed_out",
                    "runner_error_category": "timeout",
                }
            )
        return self


class ExecuteTestsBody(BaseModel):
    """Explicit user authorization — never inferred from profile or model text.

    ``confirm`` is required. A missing field must not default to authorized.
    """

    confirm: Literal[True]
    wall_clock_seconds: int | None = Field(default=None, ge=1, le=60)
    memory_mib: int | None = Field(default=None, ge=64, le=1024)


class ExecutionSummary(BaseModel):
    """Public task-facing execution evidence (no host paths)."""

    execution_id: str
    status: ExecutionStatus
    tests_executed: bool = False
    tests_passed: bool = False
    timed_out: bool = False
    runner_error_category: RunnerErrorCategory = "none"
    exit_code: int | None = None
    duration_ms: int = 0
    stdout_summary: str = ""
    stderr_summary: str = ""
    detail: str = ""
    artifacts: list[SafeArtifactMeta] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: ExecutionResult) -> "ExecutionSummary":
        safe = result.validated()
        return cls(
            execution_id=safe.execution_id,
            status=safe.status,
            tests_executed=safe.tests_executed,
            tests_passed=safe.tests_passed,
            timed_out=safe.timed_out,
            runner_error_category=safe.runner_error_category,
            exit_code=safe.exit_code,
            duration_ms=safe.duration_ms,
            stdout_summary=safe.stdout_summary,
            stderr_summary=safe.stderr_summary,
            detail=safe.detail,
            artifacts=safe.artifacts,
        )

    @classmethod
    def not_requested(cls) -> "ExecutionSummary":
        return cls(
            execution_id="",
            status="not_requested",
            detail="Test execution was not requested",
        )


def execution_result_to_dict(result: ExecutionResult) -> dict[str, Any]:
    return result.validated().model_dump()
