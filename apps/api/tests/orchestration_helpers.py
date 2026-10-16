"""Shared helpers for Phase 4B/4C orchestration tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.agents.base import AgentRole, WorkspaceWriter
from app.agents.mock import build_mock_plan
from app.domain.models import AgentResult


def make_plan_v2(*, requires_execution: bool = False) -> dict:
    return build_mock_plan(
        validation_profile=(
            "python_test_execution" if requires_execution else "generic_text"
        )
    )


DEFAULT_PLAN = make_plan_v2(requires_execution=False)
DEFAULT_RUNTIME_PLAN = make_plan_v2(requires_execution=True)


class FixedOutputAdapter:
    """Returns a valid Plan v2 for planner; fixed content for implementer."""

    def __init__(
        self,
        content: str,
        artifact_name: str = "result.txt",
        *,
        plan: dict | None = None,
    ) -> None:
        self.content = content
        self.artifact_name = artifact_name
        self.plan = plan
        self.calls = 0
        self.roles: list[str] = []
        self.last_validation_profile: str | None = None

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
        self.calls += 1
        self.roles.append(role)
        self.last_validation_profile = validation_profile
        if role == "planner":
            payload = self.plan
            if payload is None:
                payload = build_mock_plan(validation_profile=validation_profile)
            content = json.dumps(payload, indent=2)
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
