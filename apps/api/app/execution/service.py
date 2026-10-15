"""Orchestrates explicit test execution for an existing task."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.domain.models import AgentResult, Task
from app.domain.store import MemoryTaskStore
from app.execution.base import ExecutionRunner
from app.execution.concurrency import ExecutionBusyError, ExecutionGate
from app.execution.limits import (
    APPROVED_RUNTIME_ID,
    APPROVED_TEST_PROFILE,
    clamp_limits,
)
from app.execution.models import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionSummary,
)
from app.execution.staging import (
    StagingError,
    cleanup_staging,
    create_execution_staging_root,
    stage_python_test_artifact,
)
from app.quality.base import QualityGate
from app.workspaces.local import LocalWorkspace, WorkspacePathError

logger = logging.getLogger(__name__)

EXECUTABLE_PROFILES = frozenset({"python_test_execution"})


class ExecutionService:
    def __init__(
        self,
        *,
        runner: ExecutionRunner,
        workspace: LocalWorkspace,
        store: MemoryTaskStore,
        gate: QualityGate,
        gate_lock: ExecutionGate,
        staging_base: Path | None = None,
    ) -> None:
        self.runner = runner
        self.workspace = workspace
        self.store = store
        self.gate = gate
        self.gate_lock = gate_lock
        self.staging_base = staging_base or workspace.root

    def execute_tests(
        self,
        *,
        task_id: str,
        confirm: bool,
        wall_clock_seconds: int | None = None,
        memory_mib: int | None = None,
    ) -> Task:
        if confirm is not True:
            raise ValueError("explicit confirmation is required")

        task = self.store.get(task_id)
        if task is None:
            raise LookupError("task not found")

        execution_id = str(uuid.uuid4())
        limits = clamp_limits(
            wall_clock_seconds=wall_clock_seconds,
            memory_mib=memory_mib,
        )

        if task.validation_profile not in EXECUTABLE_PROFILES:
            return self._persist(
                task,
                ExecutionResult(
                    execution_id=execution_id,
                    status="rejected",
                    tests_executed=False,
                    tests_passed=False,
                    runner_error_category="unsupported_profile",
                    detail=(
                        f"Profile '{task.validation_profile}' does not support "
                        "isolated test execution. Use python_test_execution."
                    ),
                ),
            )

        # Re-validate statically before any execution attempt.
        try:
            task_dir = self.workspace.task_directory(task_id)
            artifact = self.workspace.resolve(task_id, "result.txt")
        except WorkspacePathError:
            return self._persist(
                task,
                ExecutionResult(
                    execution_id=execution_id,
                    status="rejected",
                    runner_error_category="staging_error",
                    detail="Task workspace or artifact path is invalid",
                ),
            )

        agent_result = AgentResult(output=task.output, artifact_name="result.txt")
        try:
            report = self.gate.evaluate(
                agent_result,
                task_dir,
                profile=task.validation_profile,
            )
        except Exception:
            logger.error("pre-execution static validation raised")
            return self._persist(
                task,
                ExecutionResult(
                    execution_id=execution_id,
                    status="rejected",
                    runner_error_category="static_validation_failed",
                    detail="Static validation could not be completed safely",
                ),
            )

        if not report.passed or not all(c.passed for c in report.checks):
            return self._persist(
                task,
                ExecutionResult(
                    execution_id=execution_id,
                    status="rejected",
                    runner_error_category="static_validation_failed",
                    detail="Static validation must pass before execution",
                ),
            )

        if not self.runner.is_available():
            # Still invoke run() so disabled/unavailable runners return structured results.
            unavailable = self.runner.run(
                ExecutionRequest(
                    runtime_id=APPROVED_RUNTIME_ID,
                    test_profile=APPROVED_TEST_PROFILE,
                    execution_id=execution_id,
                    task_id=task_id,
                    staged_root=str(self.staging_base),
                    artifacts=[],
                    limits=limits,
                )
            )
            return self._persist(task, unavailable)

        staging_root: Path | None = None
        cleanup_failed = False
        try:
            with self.gate_lock.acquire(execution_id):
                try:
                    staging_root = create_execution_staging_root(
                        self.staging_base, execution_id
                    )
                    artifacts = stage_python_test_artifact(
                        source_file=artifact,
                        staging_root=staging_root,
                        limits=limits,
                    )
                except StagingError as exc:
                    return self._persist(
                        task,
                        ExecutionResult(
                            execution_id=execution_id,
                            status="rejected",
                            runner_error_category="staging_error",
                            detail=str(exc),
                        ),
                    )

                request = ExecutionRequest(
                    runtime_id=APPROVED_RUNTIME_ID,
                    test_profile=APPROVED_TEST_PROFILE,
                    execution_id=execution_id,
                    task_id=task_id,
                    staged_root=str(staging_root),
                    artifacts=artifacts,
                    limits=limits,
                )
                try:
                    result = self.runner.run(request)
                except Exception:
                    logger.error("runner raised unexpectedly")
                    result = ExecutionResult(
                        execution_id=execution_id,
                        status="infrastructure_error",
                        tests_executed=False,
                        tests_passed=False,
                        runner_error_category="runtime_crash",
                        detail="Runner failed unexpectedly",
                    )
        except ExecutionBusyError:
            return self._persist(
                task,
                ExecutionResult(
                    execution_id=execution_id,
                    status="rejected",
                    runner_error_category="busy",
                    detail="Another local execution is already active",
                ),
            )
        finally:
            try:
                cleanup_staging(staging_root)
            except Exception:
                cleanup_failed = True
                logger.error("cleanup failed for execution_id=%s", execution_id)

        safe = result.validated()
        if cleanup_failed:
            # Cleanup failure must never turn failure into success, and must
            # demote a would-be success to cleanup_error.
            safe = safe.model_copy(
                update={
                    "tests_passed": False,
                    "status": "cleanup_error",
                    "runner_error_category": "cleanup_failed",
                    "detail": (safe.detail + "; staging cleanup failed").strip("; "),
                }
            )
        return self._persist(task, safe)

    def _persist(self, task: Task, result: ExecutionResult) -> Task:
        summary = ExecutionSummary.from_result(result)
        updated = task.model_copy(update={"execution": summary})
        # Task status stays based on static quality gate; execution is separate evidence.
        # Never flip task to passed based on execution alone if static failed (already passed).
        if summary.tests_passed and summary.tests_executed:
            # Keep task status as-is when static already passed; do not imply
            # generic success equals tests_passed — execution field carries that.
            pass
        self.store.save(updated)
        return updated
