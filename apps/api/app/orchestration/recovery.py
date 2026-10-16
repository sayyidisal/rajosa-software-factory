"""Startup recovery for mid-flight tasks (Phase 4E.3).

Marks in-progress lifecycles as ``failed`` with ``error_category=interrupted``.
Does not auto-resume planner, implementer, or runner.

Atomicity: each recovered task is persisted via a single ``TaskRepository.save``
call. SQLite wraps the task upsert and event inserts in one BEGIN/COMMIT.
MemoryTaskStore updates one dict entry. Multi-task recovery is sequential
(at-least-once marking across tasks), not one global transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.domain.models import Task
from app.domain.store import TaskRepository
from app.orchestration.events import make_event
from app.orchestration.lifecycle import Lifecycle, assert_transition, legacy_status_for

logger = logging.getLogger(__name__)

# Mid-flight / anomalous states only. Intentional waits and terminals are skipped.
INTERRUPTIBLE_LIFECYCLES: tuple[Lifecycle, ...] = (
    "created",
    "planning",
    "generating",
    "validating",
)

RECOVERY_ERROR_MESSAGE = "Task interrupted by process restart before completion"
RECOVERY_SOURCE = "startup"


@dataclass
class RecoveryReport:
    examined: int = 0
    interrupted_task_ids: list[str] = field(default_factory=list)
    skipped_task_ids: list[str] = field(default_factory=list)


class StartupRecoveryError(RuntimeError):
    """Raised when startup recovery cannot complete safely."""


def recover_interrupted_tasks(store: TaskRepository) -> RecoveryReport:
    """Fail-closed mark for interruptible lifecycles found at startup.

    Idempotent via persisted lifecycle: once ``failed``, a later restart no-ops.
    """
    report = RecoveryReport()
    try:
        candidates = store.list_by_lifecycle(INTERRUPTIBLE_LIFECYCLES)
    except Exception as exc:
        raise StartupRecoveryError(
            f"failed to list interruptible tasks: {exc}"
        ) from exc

    report.examined = len(candidates)
    for task in candidates:
        if task.lifecycle not in INTERRUPTIBLE_LIFECYCLES:
            report.skipped_task_ids.append(task.id)
            continue
        try:
            recover_one(store, task)
        except Exception as exc:
            raise StartupRecoveryError(
                f"failed to recover task '{task.id}' "
                f"(lifecycle={task.lifecycle}): {exc}"
            ) from exc
        report.interrupted_task_ids.append(task.id)
        logger.warning(
            "startup recovery interrupted task id=%s from_lifecycle=%s",
            task.id,
            task.lifecycle,
        )
    return report


def recover_one(store: TaskRepository, task: Task) -> Task:
    """Transition one interruptible task to ``failed`` and persist atomically."""
    current: Lifecycle = task.lifecycle  # type: ignore[assignment]
    if current not in INTERRUPTIBLE_LIFECYCLES:
        return task
    if current == "failed":
        return task

    assert_transition(current, "failed")
    event = make_event(
        task_id=task.id,
        event_type="task.failed",
        from_lifecycle=current,
        to_lifecycle="failed",
        error_category="interrupted",
        metadata={
            "recovery": RECOVERY_SOURCE,
            "from_lifecycle": current,
        },
    )
    updated = task.model_copy(
        update={
            "lifecycle": "failed",
            "status": legacy_status_for("failed"),
            "error": RECOVERY_ERROR_MESSAGE,
            "events": list(task.events) + [event],
        }
    )
    # Single save: SQLite commits task row + new events together; memory is one write.
    return store.save(updated, new_events=[event])
