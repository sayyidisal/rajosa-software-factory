import json

from app.agents.base import AgentRole, WorkspaceWriter
from app.domain.models import AgentResult
from app.quality.profiles import profile_requires_runtime


def build_mock_plan(*, validation_profile: str) -> dict:
    """Deterministic Plan v2 payload for the mock planner."""
    requires_execution = profile_requires_runtime(validation_profile)
    checks = [
        {
            "check_id": "chk_artifact",
            "description": "Primary artifact present after generation",
            "kind": "artifact_present",
        },
        {
            "check_id": "chk_profile",
            "description": "Selected validation profile quality gate passes",
            "kind": "quality_profile",
        },
    ]
    if requires_execution:
        checks.append(
            {
                "check_id": "chk_runtime",
                "description": "Isolated runtime tests pass after explicit authorization",
                "kind": "runtime_tests",
            }
        )
    return {
        "schema_version": "2",
        "summary": (
            "Produce Python unit tests for isolated execution."
            if requires_execution
            else "Produce the requested artifact for the task."
        ),
        "goals": ["Satisfy the validation profile", "Keep the change small"],
        "steps": [
            {
                "step_id": "implement",
                "description": "Generate the primary result artifact.",
                "expected_artifacts": ["result.txt"],
                "acceptance_check_ids": [check["check_id"] for check in checks],
            }
        ],
        "acceptance_checks": checks,
        "requires_execution": requires_execution,
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
        validation_profile: str = "generic_text",
    ) -> AgentResult:
        if role == "planner":
            plan = build_mock_plan(validation_profile=validation_profile)
            content = json.dumps(plan, indent=2)
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")

        content = f"Mock result\nTitle: {title}\nInstruction:\n{instruction}\n"
        workspace.write_text(task_id, "result.txt", content)
        return AgentResult(output=content, artifact_name="result.txt")
