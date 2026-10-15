import logging
import uuid

from app.agents.base import AgentAdapter
from app.domain.models import QualityReport, Task
from app.domain.store import MemoryTaskStore
from app.quality.base import QualityGate
from app.workspaces.local import LocalWorkspace

SAFE_AGENT_ERROR = "The agent could not complete this task."
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

    def create_task(self, *, title: str, instruction: str) -> Task:
        task_id = str(uuid.uuid4())
        self.workspace.prepare(task_id)
        try:
            result = self.adapter.run(
                task_id=task_id,
                title=title,
                instruction=instruction,
                workspace=self.workspace,
            )
        except Exception as exc:
            logger.error("agent execution failed: %s", type(exc).__name__)
            task = self._failed_task(
                task_id=task_id,
                title=title,
                instruction=instruction,
                output="",
                error=SAFE_AGENT_ERROR,
                quality=QualityReport(passed=False, checks=[]),
            )
            self.store.save(task)
            return task

        report = self.gate.evaluate(result, self.workspace.task_directory(task_id))
        status = "passed" if report.passed else "failed"
        task = Task(
            id=task_id,
            title=title,
            instruction=instruction,
            status=status,
            output=result.output,
            error=None,
            quality=report,
        )
        self.store.save(task)
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)

    def _failed_task(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        output: str,
        error: str | None,
        quality: QualityReport,
    ) -> Task:
        return Task(
            id=task_id,
            title=title,
            instruction=instruction,
            status="failed",
            output=output,
            error=error,
            quality=quality,
        )
