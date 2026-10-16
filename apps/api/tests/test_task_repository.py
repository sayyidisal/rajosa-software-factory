"""Phase 4E — TaskRepository contract tests (memory + shared expectations)."""

from __future__ import annotations

import pytest

from app.domain.models import QualityReport, Task
from app.domain.store import MemoryTaskRepository, MemoryTaskStore, TaskRepository
from app.execution.models import ExecutionSummary
from app.orchestration.events import make_event


def _task(
    *,
    task_id: str = "t1",
    lifecycle: str = "awaiting_plan_approval",
    status: str = "pending",
) -> Task:
    return Task(
        id=task_id,
        title="Title",
        instruction="Do the thing",
        validation_profile="generic_text",
        status=status,  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        output="",
        error=None,
        quality=QualityReport(passed=False, checks=[]),
        execution=ExecutionSummary.not_requested(),
        plan=None,
        plan_approved=False,
        events=[],
    )


def test_memory_store_is_task_repository() -> None:
    store = MemoryTaskStore()
    assert isinstance(store, TaskRepository)
    assert MemoryTaskRepository is MemoryTaskStore


def test_round_trip_get_save() -> None:
    store: TaskRepository = MemoryTaskStore()
    task = _task()
    saved = store.save(task)
    assert saved.id == task.id
    loaded = store.get(task.id)
    assert loaded is not None
    assert loaded.title == "Title"
    assert loaded.lifecycle == "awaiting_plan_approval"
    assert loaded.events == []


def test_save_without_new_events_preserves_history() -> None:
    store = MemoryTaskStore()
    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.awaiting_approval")
    store.save(_task(), new_events=[e1, e2])

    # Legacy-style save with empty events must NOT wipe history.
    store.save(_task().model_copy(update={"title": "Renamed", "events": []}))
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.title == "Renamed"
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]


def test_save_discovers_new_events_from_task_snapshot() -> None:
    store = MemoryTaskStore()
    e1 = make_event(task_id="t1", event_type="task.created")
    store.save(_task().model_copy(update={"events": [e1]}))
    loaded = store.get("t1")
    assert loaded is not None
    assert [e.event_id for e in loaded.events] == [e1.event_id]

    e2 = make_event(task_id="t1", event_type="plan.generated")
    store.save(_task().model_copy(update={"events": [e1, e2]}))
    loaded = store.get("t1")
    assert loaded is not None
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]


def test_new_events_append_without_duplication() -> None:
    store = MemoryTaskStore()
    task = _task()
    store.save(task)

    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.awaiting_approval")

    after_first = store.save(task, new_events=[e1, e2])
    assert [e.event_id for e in after_first.events] == [e1.event_id, e2.event_id]

    after_second = store.save(
        after_first.model_copy(update={"output": "x"}),
        new_events=[e1, e2],
    )
    assert [e.event_id for e in after_second.events] == [e1.event_id, e2.event_id]
    assert after_second.output == "x"

    e3 = make_event(task_id="t1", event_type="plan.approved")
    after_third = store.save(after_second, new_events=[e2, e3])
    assert [e.event_id for e in after_third.events] == [
        e1.event_id,
        e2.event_id,
        e3.event_id,
    ]


def test_list_by_lifecycle_filters() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="a", lifecycle="awaiting_plan_approval"))
    store.save(_task(task_id="b", lifecycle="awaiting_execution_auth"))
    store.save(_task(task_id="c", lifecycle="completed", status="passed"))
    store.save(_task(task_id="d", lifecycle="failed", status="failed"))

    waiting = store.list_by_lifecycle(
        ["awaiting_plan_approval", "awaiting_execution_auth"]
    )
    assert {t.id for t in waiting} == {"a", "b"}
    assert {t.id for t in store.list_by_lifecycle(["completed", "failed"])} == {
        "c",
        "d",
    }
    assert store.list_by_lifecycle(["planning"]) == []


def test_list_task_ids_sorted() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="z"))
    store.save(_task(task_id="a"))
    store.save(_task(task_id="m"))
    assert store.list_task_ids() == ["a", "m", "z"]


def test_get_missing_returns_none() -> None:
    assert MemoryTaskStore().get("missing") is None


def test_new_events_reject_mismatched_task_id() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="t1"))
    bad = make_event(task_id="other", event_type="task.created")
    with pytest.raises(ValueError, match="does not match"):
        store.save(_task(task_id="t1"), new_events=[bad])


def test_store_does_not_enforce_terminal_lifecycle() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="t1", lifecycle="completed", status="passed"))
    store.save(_task(task_id="t1", lifecycle="failed", status="failed"))
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.lifecycle == "failed"


def test_more_than_100_events_retained_in_memory() -> None:
    store = MemoryTaskStore()
    store.save(_task())
    events = [
        make_event(task_id="t1", event_type="task.created") for _ in range(120)
    ]
    # Unique event_ids from make_event defaults.
    store.save(_task(), new_events=events)
    loaded = store.get("t1")
    assert loaded is not None
    assert len(loaded.events) == 120
