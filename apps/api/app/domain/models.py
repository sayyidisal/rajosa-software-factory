from typing import Any, Literal

from pydantic import BaseModel, Field

from app.execution.models import ExecutionSummary
from app.orchestration.events import OrchestrationEvent
from app.orchestration.lifecycle import Lifecycle
from app.orchestration.plan import Plan
from app.quality.profiles import ValidationProfileName


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str
    details: dict[str, Any] | None = None


class QualityReport(BaseModel):
    passed: bool
    checks: list[CheckResult]


class AgentResult(BaseModel):
    output: str
    artifact_name: str = "result.txt"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)
    validation_profile: ValidationProfileName = "generic_text"
    # Explicit execution is never authorized via task creation.
    # Explicit plan approval is never authorized via task creation.


class PlanDecisionBody(BaseModel):
    """Explicit user plan decision — never inferred from model output.

    ``approved`` is required. ``true`` starts implementation at most once.
    ``false`` rejects the plan without implementation. Missing field → 422.
    """

    approved: bool


class Task(BaseModel):
    id: str
    title: str
    instruction: str
    # Loaded as str so corrupt/unsupported DB values fail closed in integrity
    # checks (HTTP 409) instead of exploding at Pydantic deserialize time.
    # Create API still constrains to ValidationProfileName via TaskCreate.
    validation_profile: str = "generic_text"
    # Legacy compatibility: passed/failed for terminal outcomes; pending otherwise.
    status: Literal["passed", "failed", "pending"]
    lifecycle: Lifecycle = "created"
    output: str
    error: str | None
    quality: QualityReport
    execution: ExecutionSummary | None = None
    plan: Plan | None = None
    plan_approved: bool = False
    # SHA-256 hex from compute_approval_digest at approval time. Evidence only —
    # must be recomputed and compared before privileged operations (Phase 4E.2).
    approval_digest: str | None = None
    events: list[OrchestrationEvent] = Field(default_factory=list)
