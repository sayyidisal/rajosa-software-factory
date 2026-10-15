"""Shared helpers for Phase 4B orchestration tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.agents.base import AgentRole, WorkspaceWriter
from app.domain.models import AgentResult

DEFAULT_PLAN = {
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


class FixedOutputAdapter:
    """Returns a valid plan for planner; fixed content for implementer."""

    def __init__(self, content: str, artifact_name: str = "result.txt") -> None:
        self.content = content
        self.artifact_name = artifact_name
        self.calls = 0
        self.roles: list[str] = []

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: AgentRole = "implementer",
    ) -> AgentResult:
        self.calls += 1
        self.roles.append(role)
        if role == "planner":
            content = json.dumps(DEFAULT_PLAN, indent=2)
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")
        workspace.write_text(task_id, self.artifact_name, self.content)
        return AgentResult(output=self.content, artifact_name=self.artifact_name)


def approve_plan(client: TestClient, task_id: str, *, approved: bool = True) -> dict:
    response = client.post(
        f"/tasks/{task_id}/approve-plan",
        json={"approved": approved},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_and_approve(
    client: TestClient,
    *,
    title: str,
    instruction: str,
    validation_profile: str = "generic_text",
) -> dict:
    created = client.post(
        "/tasks",
        json={
            "title": title,
            "instruction": instruction,
            "validation_profile": validation_profile,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["lifecycle"] == "awaiting_plan_approval"
    return approve_plan(client, body["id"], approved=True)
