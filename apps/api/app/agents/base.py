from typing import Literal, Protocol

from app.domain.models import AgentResult

AgentRole = Literal["planner", "implementer"]


class WorkspaceWriter(Protocol):
    def write_text(self, task_id: str, relative: str, content: str) -> object:
        """Write text inside a task workspace and return the contained path."""


class AgentAdapter(Protocol):
    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: AgentRole = "implementer",
    ) -> AgentResult:
        """Execute one agent role and return its result."""
