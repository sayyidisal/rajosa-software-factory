"""SQLite-backed TaskRepository (Phase 4E.1) — not wired into create_app yet."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.domain.models import QualityReport, Task
from app.domain.store import _append_new_events
from app.execution.models import ExecutionSummary
from app.orchestration.events import OrchestrationEvent
from app.orchestration.lifecycle import Lifecycle
from app.orchestration.plan import Plan, compute_approval_digest
from app.persistence.migrate import apply_migrations


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteTaskRepository:
    """File- or memory-backed SQLite implementation of TaskRepository."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        apply_migrations(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            events = self._load_events(task_id)
            return self._row_to_task(row, events)

    def save(
        self,
        task: Task,
        *,
        new_events: Sequence[OrchestrationEvent] | None = None,
    ) -> Task:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT id, created_at FROM tasks WHERE id = ?", (task.id,)
                ).fetchone()
                prior = self._load_events(task.id) if existing is not None else []
                candidates: Sequence[OrchestrationEvent]
                if new_events is not None:
                    candidates = new_events
                else:
                    candidates = task.events
                # Validate / compute merged list before writing (may raise ValueError).
                merged_events = _append_new_events(
                    task_id=task.id,
                    prior=prior,
                    candidates=candidates,
                )
                newly = [
                    event
                    for event in merged_events
                    if event.event_id not in {e.event_id for e in prior}
                ]

                now = _utc_now()
                created_at = existing["created_at"] if existing is not None else now
                approval_digest = self._approval_digest_for(task)
                plan_json = (
                    task.plan.model_dump_json() if task.plan is not None else None
                )
                quality_json = task.quality.model_dump_json()
                execution_json = (
                    task.execution.model_dump_json()
                    if task.execution is not None
                    else None
                )
                workspace_relpath = task.id

                self._conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, instruction, validation_profile, status, lifecycle,
                        output, error, plan_approved, plan_json, approval_digest,
                        quality_json, execution_json, workspace_relpath,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        instruction = excluded.instruction,
                        validation_profile = excluded.validation_profile,
                        status = excluded.status,
                        lifecycle = excluded.lifecycle,
                        output = excluded.output,
                        error = excluded.error,
                        plan_approved = excluded.plan_approved,
                        plan_json = excluded.plan_json,
                        approval_digest = excluded.approval_digest,
                        quality_json = excluded.quality_json,
                        execution_json = excluded.execution_json,
                        workspace_relpath = excluded.workspace_relpath,
                        updated_at = excluded.updated_at
                    """,
                    (
                        task.id,
                        task.title,
                        task.instruction,
                        task.validation_profile,
                        task.status,
                        task.lifecycle,
                        task.output,
                        task.error,
                        1 if task.plan_approved else 0,
                        plan_json,
                        approval_digest,
                        quality_json,
                        execution_json,
                        workspace_relpath,
                        created_at,
                        now,
                    ),
                )

                for event in newly:
                    self._conn.execute(
                        """
                        INSERT INTO task_events (
                            event_id, task_id, event_type, timestamp,
                            from_lifecycle, to_lifecycle, step_id,
                            error_category, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.task_id,
                            event.event_type,
                            event.timestamp,
                            event.from_lifecycle,
                            event.to_lifecycle,
                            event.step_id,
                            event.error_category,
                            json.dumps(event.metadata, separators=(",", ":")),
                        ),
                    )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            return task.model_copy(update={"events": merged_events})

    def list_by_lifecycle(self, lifecycles: Sequence[Lifecycle]) -> list[Task]:
        allowed = list(lifecycles)
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE lifecycle IN ({placeholders})",
                allowed,
            ).fetchall()
            return [
                self._row_to_task(row, self._load_events(row["id"])) for row in rows
            ]

    def list_task_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks ORDER BY id ASC"
            ).fetchall()
            return [row["id"] for row in rows]

    def _load_events(self, task_id: str) -> list[OrchestrationEvent]:
        rows = self._conn.execute(
            """
            SELECT event_id, task_id, event_type, timestamp, from_lifecycle,
                   to_lifecycle, step_id, error_category, metadata_json
            FROM task_events
            WHERE task_id = ?
            ORDER BY seq ASC
            """,
            (task_id,),
        ).fetchall()
        events: list[OrchestrationEvent] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            events.append(
                OrchestrationEvent(
                    event_id=row["event_id"],
                    task_id=row["task_id"],
                    event_type=row["event_type"],
                    timestamp=row["timestamp"],
                    from_lifecycle=row["from_lifecycle"],
                    to_lifecycle=row["to_lifecycle"],
                    step_id=row["step_id"],
                    error_category=row["error_category"],
                    metadata=metadata,
                )
            )
        return events

    def _row_to_task(
        self, row: sqlite3.Row, events: list[OrchestrationEvent]
    ) -> Task:
        plan = None
        if row["plan_json"]:
            plan = Plan.model_validate_json(row["plan_json"])
        quality = QualityReport.model_validate_json(row["quality_json"])
        execution = None
        if row["execution_json"]:
            execution = ExecutionSummary.model_validate_json(row["execution_json"])
        return Task(
            id=row["id"],
            title=row["title"],
            instruction=row["instruction"],
            validation_profile=row["validation_profile"],
            status=row["status"],
            lifecycle=row["lifecycle"],
            output=row["output"],
            error=row["error"],
            quality=quality,
            execution=execution,
            plan=plan,
            plan_approved=bool(row["plan_approved"]),
            events=events,
        )

    @staticmethod
    def _approval_digest_for(task: Task) -> str | None:
        if not task.plan_approved or task.plan is None:
            return None
        return compute_approval_digest(
            plan=task.plan,
            validation_profile=task.validation_profile,
        )
