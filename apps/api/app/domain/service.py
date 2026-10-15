"""Compatibility facade — Phase 4B orchestration owns create/get."""

from __future__ import annotations

from app.domain.models import Task
from app.orchestration.service import OrchestratorService
from app.quality.profiles import ValidationProfileName


class TaskService:
    """Thin wrapper kept so existing imports and app.state.task_service still work."""

    def __init__(self, orchestrator: OrchestratorService) -> None:
        self._orchestrator = orchestrator
        self.adapter = orchestrator.adapter
        self.gate = orchestrator.gate
        self.workspace = orchestrator.workspace
        self.store = orchestrator.store

    def create_task(
        self,
        *,
        title: str,
        instruction: str,
        validation_profile: ValidationProfileName = "generic_text",
    ) -> Task:
        return self._orchestrator.create_task(
            title=title,
            instruction=instruction,
            validation_profile=validation_profile,
        )

    def get_task(self, task_id: str) -> Task | None:
        return self._orchestrator.get_task(task_id)

    def approve_plan(self, task_id: str, *, approved: bool) -> Task:
        if approved is True:
            return self._orchestrator.approve_plan(task_id, approved=True)
        return self._orchestrator.reject_plan(task_id)
