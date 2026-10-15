from typing import Protocol

from app.domain.models import AgentResult


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
    ) -> AgentResult:
        """Execute one task and return its result."""
