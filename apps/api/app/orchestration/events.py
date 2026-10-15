"""In-memory orchestration events (not durable across restarts)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.orchestration.lifecycle import Lifecycle

EventType = Literal[
    "task.created",
    "plan.generated",
    "plan.invalid",
    "plan.awaiting_approval",
    "plan.approved",
    "plan.rejected",
    "generation.started",
    "generation.completed",
    "generation.failed",
    "quality.passed",
    "quality.failed",
    "execution.auth_required",
    "execution.completed",
    "task.completed",
    "task.failed",
    "transition.rejected",
]


class OrchestrationEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    event_type: EventType
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    from_lifecycle: Lifecycle | None = None
    to_lifecycle: Lifecycle | None = None
    step_id: str | None = None
    error_category: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def make_event(
    *,
    task_id: str,
    event_type: EventType,
    from_lifecycle: Lifecycle | None = None,
    to_lifecycle: Lifecycle | None = None,
    step_id: str | None = None,
    error_category: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> OrchestrationEvent:
    safe_meta: dict[str, Any] = {}
    if metadata:
        for key, value in list(metadata.items())[:16]:
            if key.lower() in {"api_key", "authorization", "secret", "password", "token"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                if isinstance(value, str) and len(value) > 200:
                    safe_meta[key] = value[:200] + "…"
                else:
                    safe_meta[key] = value
    return OrchestrationEvent(
        task_id=task_id,
        event_type=event_type,
        from_lifecycle=from_lifecycle,
        to_lifecycle=to_lifecycle,
        step_id=step_id,
        error_category=error_category,
        metadata=safe_meta,
    )
