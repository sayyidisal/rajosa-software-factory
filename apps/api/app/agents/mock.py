from app.agents.base import WorkspaceWriter
from app.domain.models import AgentResult


class MockAgentAdapter:
    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
    ) -> AgentResult:
        content = f"Mock result\nTitle: {title}\nInstruction:\n{instruction}\n"
        workspace.write_text(task_id, "result.txt", content)
        return AgentResult(output=content)
