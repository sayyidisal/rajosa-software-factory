"""Task persistence contracts and the in-memory adapter (Phase 4E).

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

    ``save`` upserts task fields. Event history is **append-only** by
    ``event_id`` and is never replaced or cleared by a normal save:

    - ``new_events=None``: upsert task state; append any events present on
      ``task.events`` that are not already stored. Prior stored events that
      are missing from ``task.events`` are **kept** (no wipe).
    - ``new_events=[...]``: upsert task state; append only the provided
      events (skip duplicates by ``event_id``). The ``events`` field on the
      incoming ``task`` is not used as an insert source in this mode.

    Duplicate ``event_id`` values are skipped (idempotent). Events whose
    ``task_id`` does not match ``task.id`` raise ``ValueError``.
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


def _append_new_events(
    *,
    task_id: str,
    prior: list[OrchestrationEvent],
    candidates: Sequence[OrchestrationEvent],
) -> list[OrchestrationEvent]:
    """Return ``prior`` extended with non-duplicate candidates for ``task_id``."""
    seen = {event.event_id for event in prior}
    merged = list(prior)
    for event in candidates:
        if event.event_id in seen:
            continue
        if event.task_id != task_id:
            raise ValueError(
                f"event task_id '{event.task_id}' does not match task '{task_id}'"
            )
        merged.append(event)
        seen.add(event.event_id)
    return merged


class MemoryTaskStore:
    """In-process dict-backed :class:`TaskRepository`.

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
        existing = self._tasks.get(task.id)
        prior = list(existing.events) if existing is not None else []
        candidates: Sequence[OrchestrationEvent]
        if new_events is not None:
            candidates = new_events
        else:
            candidates = task.events
        merged_events = _append_new_events(
            task_id=task.id,
            prior=prior,
            candidates=candidates,
        )
        saved = task.model_copy(update={"events": merged_events})
        self._tasks[task.id] = saved
        return saved

    def list_by_lifecycle(self, lifecycles: Sequence[Lifecycle]) -> list[Task]:
        allowed = frozenset(lifecycles)
        return [task for task in self._tasks.values() if task.lifecycle in allowed]

    def list_task_ids(self) -> list[str]:
        return sorted(self._tasks.keys())


# Explicit alias for Phase 4E naming; same object as MemoryTaskStore.
MemoryTaskRepository = MemoryTaskStore
