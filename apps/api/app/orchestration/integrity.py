"""Approved-plan integrity checks (Phase 4E.2).

Stored ``approval_digest`` is evidence to verify, never proof by itself.
Mismatch / missing / corrupt plan data is rejected fail-closed without repair.
"""

from __future__ import annotations

from app.domain.models import Task
from app.orchestration.plan import (
    Plan,
    PlanValidationError,
    canonical_plan_json,
    compute_approval_digest,
    parse_plan_text,
)
from app.quality.profiles import is_supported_profile


class PlanIntegrityError(ValueError):
    """Raised when an approved plan cannot be trusted for further operations."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


def _revalidate_plan_object(plan: Plan, *, validation_profile: str) -> Plan:
    """Re-parse canonical plan JSON through the official Phase 4C validator."""
    if not is_supported_profile(validation_profile):
        raise PlanIntegrityError(
            "unsupported_profile",
            f"Unsupported validation_profile '{validation_profile}'",
        )
    try:
        return parse_plan_text(
            canonical_plan_json(plan),
            validation_profile=validation_profile,
        )
    except PlanValidationError as exc:
        raise PlanIntegrityError(exc.category, str(exc)) from exc


def assert_plan_approvable(task: Task) -> Plan:
    """Validate plan+profile before first approval. Does not require a digest."""
    if task.plan is None:
        raise PlanIntegrityError("missing_plan", "Plan is missing")
    return _revalidate_plan_object(
        task.plan, validation_profile=task.validation_profile
    )


def assert_approved_plan_integrity(task: Task) -> Plan:
    """Fail-closed checks for tasks that claim ``plan_approved``.

    Recomputes the Phase 4C digest from the stored plan + trusted profile and
    compares to ``task.approval_digest``. Never mutates the task or store.
    """
    if not task.plan_approved:
        raise PlanIntegrityError("not_approved", "Plan is not approved")
    if task.plan is None:
        raise PlanIntegrityError("missing_plan", "Approved plan is missing")
    plan = _revalidate_plan_object(
        task.plan, validation_profile=task.validation_profile
    )
    if not task.approval_digest:
        raise PlanIntegrityError(
            "missing_digest",
            "Approved plan is missing approval digest",
        )
    computed = compute_approval_digest(
        plan=plan,
        validation_profile=task.validation_profile,
    )
    if computed != task.approval_digest:
        raise PlanIntegrityError(
            "digest_mismatch",
            "Approval digest does not match plan and validation profile",
        )
    return plan
