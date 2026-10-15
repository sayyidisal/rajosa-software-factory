import json

from app.agents.base import AgentRole, WorkspaceWriter
from app.domain.models import AgentResult

_PLAN = {
    "schema_version": "1",
    "summary": "Produce the requested artifact for the task.",
    "goals": ["Satisfy the validation profile", "Keep the change small"],
    "steps": [
        {
            "step_id": "implement",
            "description": "Generate the primary result artifact.",
            "expected_artifacts": ["result.txt"],
            "acceptance_checks": ["Artifact present", "Quality gate passes"],
        }
    ],
    "acceptance_checks": ["Deterministic quality gate passes"],
    "requires_execution": False,
}


class MockAgentAdapter:
    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: AgentRole = "implementer",
    ) -> AgentResult:
        if role == "planner":
            # Align requires_execution with common python_test_execution keyword in instruction.
            plan = dict(_PLAN)
            lowered = instruction.lower()
            plan["requires_execution"] = "python_test_execution" in lowered or "runtime" in lowered
            if plan["requires_execution"]:
                plan["summary"] = "Produce Python unit tests for isolated execution."
                plan["acceptance_checks"] = [
                    "Static AST checks pass",
                    "Isolated runtime tests pass after explicit authorization",
                ]
            content = json.dumps(plan, indent=2)
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")

        content = f"Mock result\nTitle: {title}\nInstruction:\n{instruction}\n"
        workspace.write_text(task_id, "result.txt", content)
        return AgentResult(output=content, artifact_name="result.txt")
