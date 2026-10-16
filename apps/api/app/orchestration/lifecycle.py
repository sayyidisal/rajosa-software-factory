"""Task lifecycle states and legal transitions (Phase 4B)."""

from __future__ import annotations

from typing import Literal

# Re-export from the authoritative profile module (avoid duplicated runtime sets).
from app.quality.profiles import (  # noqa: F401
    RUNTIME_REQUIRED_PROFILES,
    profile_requires_runtime,
)

Lifecycle = Literal[
    "created",
    "planning",
    "awaiting_plan_approval",
    "generating",
    "validating",
    "awaiting_execution_auth",
    "completed",
    "failed",
]

LEGAL_TRANSITIONS: dict[Lifecycle, frozenset[Lifecycle]] = {
    "created": frozenset({"planning", "failed"}),
    "planning": frozenset({"awaiting_plan_approval", "failed"}),
    "awaiting_plan_approval": frozenset({"generating", "failed"}),
    "generating": frozenset({"validating", "failed"}),
    "validating": frozenset({"completed", "awaiting_execution_auth", "failed"}),
    "awaiting_execution_auth": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class InvalidLifecycleTransition(ValueError):
    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid lifecycle transition: {current} -> {target}")


def assert_transition(current: Lifecycle, target: Lifecycle) -> None:
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidLifecycleTransition(current, target)


def legacy_status_for(lifecycle: Lifecycle) -> Literal["passed", "failed", "pending"]:
    """Map lifecycle to legacy status without implying runtime success."""
    if lifecycle == "completed":
        return "passed"
    if lifecycle == "failed":
        return "failed"
    return "pending"
