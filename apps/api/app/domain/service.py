import logging
import uuid

from app.agents.base import AgentAdapter
from app.agents.errors import AgentError
from app.domain.models import CheckResult, QualityReport, Task
from app.domain.store import MemoryTaskStore
from app.execution.models import ExecutionSummary
from app.quality.base import QualityGate
from app.quality.profiles import ValidationProfileName
from app.workspaces.local import LocalWorkspace

SAFE_AGENT_ERROR = "The agent could not complete this task."
VALIDATION_FAILED_PREFIX = "Validation failed:"
logger = logging.getLogger(__name__)


class TaskService:
    def __init__(
        self,
        *,
        adapter: AgentAdapter,
        gate: QualityGate,
        workspace: LocalWorkspace,
        store: MemoryTaskStore,
    ) -> None:
        self.adapter = adapter
        self.gate = gate
        self.workspace = workspace
        self.store = store

    def create_task(
        self,
        *,
        title: str,
        instruction: str,
        validation_profile: ValidationProfileName = "generic_text",
    ) -> Task:
        task_id = str(uuid.uuid4())
        self.workspace.prepare(task_id)
        try:
            result = self.adapter.run(
                task_id=task_id,
                title=title,
                instruction=instruction,
                workspace=self.workspace,
            )
        except AgentError as exc:
            logger.error("agent execution failed: AgentError")
            task = self._failed_task(
                task_id=task_id,
                title=title,
                instruction=instruction,
                validation_profile=validation_profile,
                output="",
                error=exc.public_message,
                quality=QualityReport(passed=False, checks=[]),
            )
            self.store.save(task)
            return task
        except Exception as exc:
            logger.error("agent execution failed: %s", type(exc).__name__)
            task = self._failed_task(
                task_id=task_id,
                title=title,
                instruction=instruction,
                validation_profile=validation_profile,
                output="",
                error=SAFE_AGENT_ERROR,
                quality=QualityReport(passed=False, checks=[]),
            )
            self.store.save(task)
            return task

        try:
            report = self.gate.evaluate(
                result,
                self.workspace.task_directory(task_id),
                profile=validation_profile,
            )
        except Exception as exc:
            logger.error(
                "quality evaluation failed: task_id=%s profile=%s error_type=%s",
                task_id,
                validation_profile,
                type(exc).__name__,
            )
            report = QualityReport(
                passed=False,
                checks=[
                    CheckResult(
                        name="validator_error",
                        passed=False,
                        detail="Validation could not be completed safely",
                    )
                ],
            )

        all_checks_passed = bool(report.checks) and all(check.passed for check in report.checks)
        passed = bool(report.passed) and all_checks_passed
        if not passed:
            report = QualityReport(passed=False, checks=report.checks or [
                CheckResult(
                    name="validator_error",
                    passed=False,
                    detail="Validation produced no checks",
                )
            ])

        task = Task(
            id=task_id,
            title=title,
            instruction=instruction,
            validation_profile=validation_profile,
            status="passed" if passed else "failed",
            output=result.output,
            error=None if passed else self._validation_error(report),
            quality=report,
            execution=ExecutionSummary.not_requested(),
        )
        self.store.save(task)
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)

    def _validation_error(self, report: QualityReport) -> str:
        failed = [check.detail for check in report.checks if not check.passed]
        if not failed:
            return f"{VALIDATION_FAILED_PREFIX} one or more checks did not pass"
        first = failed[0]
        if len(failed) == 1:
            return f"{VALIDATION_FAILED_PREFIX} {first}"
        return f"{VALIDATION_FAILED_PREFIX} {first} (+{len(failed) - 1} more)"

    def _failed_task(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        validation_profile: ValidationProfileName,
        output: str,
        error: str | None,
        quality: QualityReport,
    ) -> Task:
        return Task(
            id=task_id,
            title=title,
            instruction=instruction,
            validation_profile=validation_profile,
            status="failed",
            output=output,
            error=error,
            quality=quality,
            execution=ExecutionSummary.not_requested(),
        )
