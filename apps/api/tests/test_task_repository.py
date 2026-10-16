"""Phase 4E.0 — TaskRepository contract tests (memory adapter only)."""

from __future__ import annotations

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


def test_round_trip_get_save_legacy_replace() -> None:
    store: TaskRepository = MemoryTaskStore()
    task = _task()
    saved = store.save(task)
    assert saved.id == task.id
    loaded = store.get(task.id)
    assert loaded is not None
    assert loaded.title == "Title"
    assert loaded.lifecycle == "awaiting_plan_approval"
    assert loaded.events == []


def test_save_replace_updates_fields() -> None:
    store = MemoryTaskStore()
    store.save(_task())
    updated = _task().model_copy(
        update={"title": "Updated", "lifecycle": "completed", "status": "passed"}
    )
    store.save(updated)
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.title == "Updated"
    assert loaded.lifecycle == "completed"
    assert loaded.status == "passed"


def test_new_events_append_without_duplication() -> None:
    store = MemoryTaskStore()
    task = _task()
    store.save(task)

    e1 = make_event(task_id="t1", event_type="task.created")
    e2 = make_event(task_id="t1", event_type="plan.awaiting_approval")

    after_first = store.save(task, new_events=[e1, e2])
    assert [e.event_id for e in after_first.events] == [e1.event_id, e2.event_id]

    # Re-save same new_events — must not duplicate.
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


def test_legacy_save_with_events_on_task_does_not_require_new_events() -> None:
    """Current orchestrator path: events merged onto task, then save(task)."""
    store = MemoryTaskStore()
    e1 = make_event(task_id="t1", event_type="task.created")
    task = _task().model_copy(update={"events": [e1]})
    store.save(task)
    loaded = store.get("t1")
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0].event_id == e1.event_id

    e2 = make_event(task_id="t1", event_type="plan.generated")
    store.save(task.model_copy(update={"events": [e1, e2]}))
    loaded = store.get("t1")
    assert loaded is not None
    assert [e.event_id for e in loaded.events] == [e1.event_id, e2.event_id]


def test_list_by_lifecycle_filters() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="a", lifecycle="awaiting_plan_approval"))
    store.save(_task(task_id="b", lifecycle="awaiting_execution_auth"))
    store.save(
        _task(task_id="c", lifecycle="completed", status="passed"),
    )
    store.save(_task(task_id="d", lifecycle="failed", status="failed"))

    waiting = store.list_by_lifecycle(
        ["awaiting_plan_approval", "awaiting_execution_auth"]
    )
    ids = {t.id for t in waiting}
    assert ids == {"a", "b"}

    terminals = store.list_by_lifecycle(["completed", "failed"])
    assert {t.id for t in terminals} == {"c", "d"}

    assert store.list_by_lifecycle(["planning"]) == []


def test_list_task_ids_sorted() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="z"))
    store.save(_task(task_id="a"))
    store.save(_task(task_id="m"))
    assert store.list_task_ids() == ["a", "m", "z"]


def test_get_missing_returns_none() -> None:
    store = MemoryTaskStore()
    assert store.get("missing") is None


def test_new_events_reject_mismatched_task_id() -> None:
    store = MemoryTaskStore()
    store.save(_task(task_id="t1"))
    bad = make_event(task_id="other", event_type="task.created")
    try:
        store.save(_task(task_id="t1"), new_events=[bad])
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "does not match" in str(exc)


def test_store_does_not_enforce_terminal_lifecycle() -> None:
    """Terminal guards live in the orchestrator, not the repository."""
    store = MemoryTaskStore()
    store.save(_task(task_id="t1", lifecycle="completed", status="passed"))
    # Repository allows overwriting a completed task snapshot (no business rules).
    store.save(_task(task_id="t1", lifecycle="failed", status="failed"))
    loaded = store.get("t1")
    assert loaded is not None
    assert loaded.lifecycle == "failed"
