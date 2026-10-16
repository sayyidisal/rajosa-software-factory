"""Task persistence contracts and the in-memory adapter (Phase 4E.0).

Business rules (lifecycle transitions, approval, execution auth) remain in the
orchestrator / execution services — not in the repository.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.domain.models import Task
from app.orchestration.events import OrchestrationEvent
from app.orchestration.lifecycle import Lifecycle


@runtime_checkable
class TaskRepository(Protocol):
    """Minimum persistence surface for Phase 4E.

    ``save`` upserts task fields. Events are append-only by ``event_id``:

    - ``new_events=None`` (legacy / current callers): store the task snapshot
      as provided, including its ``events`` list (full replace of that task's
      event list in memory). Callers that already merge events onto the task
      before ``save`` keep working unchanged.
    - ``new_events=[...]``: upsert scalar/nested task fields from ``task``,
      then append only events whose ``event_id`` is not already stored.
      Duplicate ids in ``new_events`` or across saves are skipped. The stored
      ``task.events`` becomes prior events plus newly appended ones (the
      ``events`` field on the incoming ``task`` is not used as an insert
      source in this mode).
    """

    def get(self, task_id: str) -> Task | None:
        """Return the task or ``None`` if missing."""

    def save(
        self,
        task: Task,
        *,
        new_events: Sequence[OrchestrationEvent] | None = None,
    ) -> Task:
        """Persist ``task``; see class docstring for event semantics."""

    def list_by_lifecycle(self, lifecycles: Sequence[Lifecycle]) -> list[Task]:
        """Return tasks whose lifecycle is in ``lifecycles`` (any order)."""

    def list_task_ids(self) -> list[str]:
        """Return all known task ids (stable sort recommended)."""


class MemoryTaskStore:
    """In-process dict-backed :class:`TaskRepository` (Phase 4E.0 memory adapter).

    Kept under the historical name so existing tests and ``create_app`` injectors
    remain compatible. Does not enforce lifecycle or terminal-state rules.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def save(
        self,
        task: Task,
        *,
        new_events: Sequence[OrchestrationEvent] | None = None,
    ) -> Task:
        if new_events is None:
            self._tasks[task.id] = task
            return task

        existing = self._tasks.get(task.id)
        prior = list(existing.events) if existing is not None else []
        seen = {event.event_id for event in prior}
        for event in new_events:
            if event.event_id in seen:
                continue
            if event.task_id != task.id:
                raise ValueError(
                    f"event task_id '{event.task_id}' does not match task '{task.id}'"
                )
            prior.append(event)
            seen.add(event.event_id)

        saved = task.model_copy(update={"events": prior})
        self._tasks[task.id] = saved
        return saved

    def list_by_lifecycle(self, lifecycles: Sequence[Lifecycle]) -> list[Task]:
        allowed = frozenset(lifecycles)
        return [task for task in self._tasks.values() if task.lifecycle in allowed]

    def list_task_ids(self) -> list[str]:
        return sorted(self._tasks.keys())


# Explicit alias for Phase 4E naming; same object as MemoryTaskStore.
MemoryTaskRepository = MemoryTaskStore
