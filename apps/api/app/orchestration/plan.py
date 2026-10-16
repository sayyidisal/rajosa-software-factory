"""Bounded structured plan schema and deterministic validation (Phase 4C).

Plan validation establishes contract reliability only. It does not prove artifact
correctness, approve plans, authorize execution, or replace the quality gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.orchestration.strict_json import (
    DuplicateKeyError,
    NonFiniteNumberError,
    parse_strict_json,
)
from app.quality.profiles import (
    is_supported_profile,
    profile_requires_runtime,
)

PLAN_SCHEMA_VERSION = "2"
APPROVAL_DIGEST_VERSION = "rajosa-plan-approval-v1"

MAX_PLAN_STEPS = 8
MAX_GOALS = 8
MAX_ACCEPTANCE_CHECKS = 16
MAX_SUMMARY_CHARS = 500
MAX_GOAL_CHARS = 200
MAX_STEP_DESC_CHARS = 500
MAX_ARTIFACT_NAME_CHARS = 80
MAX_CHECK_DESC_CHARS = 200
MAX_STEP_ARTIFACTS = 8
MAX_STEP_CHECK_REFS = 8
MAX_SERIALIZED_PLAN_BYTES = 12_000
MAX_JSON_DEPTH = 8
MAX_OBJECT_KEYS = 32
MAX_ARRAY_LENGTH = 16

AcceptanceCheckKind = Literal[
    "human_review",
    "artifact_present",
    "quality_profile",
    "runtime_tests",
]

FORBIDDEN_PLAN_KEYS = frozenset(
    {
        "command",
        "commands",
        "shell",
        "docker",
        "image",
        "mount",
        "mounts",
        "env",
        "environment",
        "network",
        "privileged",
        "execution",
        "execute",
        "authorize_execution",
        "confirm",
        "argv",
        "script",
        "tools",
        "permissions",
        "depends_on",
        "dependency",
        "dependencies",
    }
)

_STEP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_CHECK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_ARTIFACT_RE = re.compile(r"^[a-zA-Z0-9._/-]{1,80}$")
_FENCE_RE = re.compile(r"```")


class PlanValidationError(ValueError):
    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        super().__init__(detail)


class AcceptanceCheck(BaseModel):
    """Declarative acceptance intent — not automatic verification proof."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=MAX_CHECK_DESC_CHARS)
    kind: AcceptanceCheckKind

    @field_validator("check_id")
    @classmethod
    def _check_id_format(cls, value: str) -> str:
        if not _CHECK_ID_RE.fullmatch(value):
            raise ValueError("check_id must be alphanumeric, underscore, or hyphen")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("description must not be empty")
        return text


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=MAX_STEP_DESC_CHARS)
    # Empty list is allowed when a step is organizational / review-only.
    expected_artifacts: list[str] = Field(default_factory=list, max_length=MAX_STEP_ARTIFACTS)
    acceptance_check_ids: list[str] = Field(min_length=1, max_length=MAX_STEP_CHECK_REFS)

    @field_validator("step_id")
    @classmethod
    def _step_id_format(cls, value: str) -> str:
        if not _STEP_ID_RE.fullmatch(value):
            raise ValueError("step_id must be alphanumeric, underscore, or hyphen")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("description must not be empty")
        return text

    @field_validator("expected_artifacts")
    @classmethod
    def _artifacts(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            name = item.strip()
            if not name or len(name) > MAX_ARTIFACT_NAME_CHARS:
                raise ValueError("invalid expected artifact name")
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError("artifact path escape is not allowed")
            if not _ARTIFACT_RE.fullmatch(name):
                raise ValueError("artifact name contains unsupported characters")
            cleaned.append(name)
        return cleaned

    @field_validator("acceptance_check_ids")
    @classmethod
    def _check_refs(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in values:
            ref = item.strip()
            if not ref or not _CHECK_ID_RE.fullmatch(ref):
                raise ValueError("invalid acceptance_check_ids entry")
            if ref in seen:
                raise ValueError("duplicate acceptance_check_ids entry in step")
            seen.add(ref)
            cleaned.append(ref)
        return cleaned


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = PLAN_SCHEMA_VERSION
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    goals: list[str] = Field(min_length=1, max_length=MAX_GOALS)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    acceptance_checks: list[AcceptanceCheck] = Field(
        min_length=1, max_length=MAX_ACCEPTANCE_CHECKS
    )
    requires_execution: bool = False

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("summary must not be empty")
        return text

    @field_validator("goals")
    @classmethod
    def _goals(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = item.strip()
            if not text or len(text) > MAX_GOAL_CHARS:
                raise ValueError("invalid goal")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def _unique_ids_and_refs(self) -> Plan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step_id values are not allowed")

        check_ids = [check.check_id for check in self.acceptance_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("duplicate check_id values are not allowed")

        known = set(check_ids)
        referenced: set[str] = set()
        for step in self.steps:
            for ref in step.acceptance_check_ids:
                if ref not in known:
                    raise ValueError(f"unknown acceptance_check_id '{ref}'")
                referenced.add(ref)

        orphans = known - referenced
        if orphans:
            raise ValueError(
                "acceptance checks must be referenced by at least one step: "
                + ", ".join(sorted(orphans))
            )
        return self


def canonical_plan_json(plan: Plan) -> str:
    """Compact deterministic JSON used for approval digests and implementer envelopes."""
    return plan.model_dump_json()


def approval_digest_preimage(*, plan: Plan, validation_profile: str) -> bytes:
    """Exact preimage hashed for plan approval integrity.

    Format (UTF-8), unambiguous and versioned::

        rajosa-plan-approval-v1\\n
        <validation_profile>\\n
        <canonical_plan_json>

    Where ``canonical_plan_json`` is ``Plan.model_dump_json()`` (compact, field order
    from the Pydantic model). The profile is the server-trusted task validation
    profile, never a model-supplied field.
    """
    profile = (validation_profile or "").strip()
    if not profile:
        raise PlanValidationError("invalid_profile", "validation_profile is required")
    if "\n" in profile or "\r" in profile:
        raise PlanValidationError("invalid_profile", "validation_profile is invalid")
    return (
        f"{APPROVAL_DIGEST_VERSION}\n{profile}\n{canonical_plan_json(plan)}"
    ).encode("utf-8")


def compute_approval_digest(*, plan: Plan, validation_profile: str) -> str:
    """SHA-256 hex digest of :func:`approval_digest_preimage`."""
    return hashlib.sha256(
        approval_digest_preimage(plan=plan, validation_profile=validation_profile)
    ).hexdigest()


def apply_requires_execution_override(
    plan: Plan, *, validation_profile: str
) -> Plan:
    """Server owns requires_execution from the trusted validation profile."""
    required = profile_requires_runtime(validation_profile)
    if plan.requires_execution == required:
        return plan
    return plan.model_copy(update={"requires_execution": required})


def _walk_bounds_and_forbidden(payload: Any, *, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise PlanValidationError("plan_too_large", "Plan JSON nesting is too deep")
    if isinstance(payload, dict):
        if len(payload) > MAX_OBJECT_KEYS:
            raise PlanValidationError("plan_too_large", "Plan object has too many keys")
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_PLAN_KEYS:
                raise PlanValidationError(
                    "forbidden_field",
                    f"Plan field '{key}' is not allowed",
                )
            _walk_bounds_and_forbidden(value, depth=depth + 1)
    elif isinstance(payload, list):
        if len(payload) > MAX_ARRAY_LENGTH:
            raise PlanValidationError("plan_too_large", "Plan array exceeds maximum length")
        for item in payload:
            _walk_bounds_and_forbidden(item, depth=depth + 1)


def _semantic_profile_rules(plan: Plan, *, validation_profile: str) -> None:
    if not is_supported_profile(validation_profile):
        raise PlanValidationError(
            "unsupported_profile",
            f"Unsupported validation_profile '{validation_profile}'",
        )
    kinds = {check.kind for check in plan.acceptance_checks}
    runtime_profile = profile_requires_runtime(validation_profile)
    if runtime_profile and "runtime_tests" not in kinds:
        raise PlanValidationError(
            "profile_incompatible",
            "runtime-required profiles need an acceptance check with kind runtime_tests",
        )
    if not runtime_profile and "runtime_tests" in kinds:
        raise PlanValidationError(
            "profile_incompatible",
            "static profiles cannot declare runtime_tests acceptance checks",
        )


def parse_plan_text(raw: str, *, validation_profile: str) -> Plan:
    """Parse and validate a plan from adapter text. Fail closed. No silent repairs."""
    text = (raw or "").strip()
    if not text:
        raise PlanValidationError("empty_plan", "Plan output is empty")
    if len(text.encode("utf-8")) > MAX_SERIALIZED_PLAN_BYTES:
        raise PlanValidationError("plan_too_large", "Plan exceeds maximum size")

    if _FENCE_RE.search(text):
        raise PlanValidationError(
            "markdown_fence_forbidden",
            "Plan JSON must not include Markdown code fences",
        )

    try:
        payload = parse_strict_json(text)
    except DuplicateKeyError as exc:
        raise PlanValidationError("duplicate_key", "Plan JSON contains duplicate keys") from exc
    except NonFiniteNumberError as exc:
        raise PlanValidationError(
            "nonfinite_number",
            "Plan JSON must not contain NaN or Infinity",
        ) from exc
    except json.JSONDecodeError as exc:
        raise PlanValidationError("malformed_json", "Plan is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise PlanValidationError("invalid_root", "Plan root must be a JSON object")

    version = payload.get("schema_version")
    if version is None:
        raise PlanValidationError("missing_schema_version", "schema_version is required")
    # Require a JSON string — do not coerce numbers/bools via str().
    if not isinstance(version, str):
        raise PlanValidationError(
            "unsupported_schema_version",
            "schema_version must be the string \"2\"",
        )
    if version != PLAN_SCHEMA_VERSION:
        raise PlanValidationError(
            "unsupported_schema_version",
            f"Unsupported schema_version '{version}'",
        )

    _walk_bounds_and_forbidden(payload)

    try:
        plan = Plan.model_validate(payload)
    except ValidationError as exc:
        # Map common uniqueness/ref errors to clearer categories when possible.
        messages = " ".join(err.get("msg", "") for err in exc.errors())
        if "duplicate step_id" in messages or "duplicate check_id" in messages:
            raise PlanValidationError("duplicate_id", "Plan contains duplicate IDs") from exc
        if "unknown acceptance_check_id" in messages or "must be referenced" in messages:
            raise PlanValidationError(
                "invalid_reference",
                "Plan acceptance_check references are invalid",
            ) from exc
        raise PlanValidationError("schema_invalid", "Plan failed schema validation") from exc

    _semantic_profile_rules(plan, validation_profile=validation_profile)
    plan = apply_requires_execution_override(plan, validation_profile=validation_profile)

    serialized = canonical_plan_json(plan).encode("utf-8")
    if len(serialized) > MAX_SERIALIZED_PLAN_BYTES:
        raise PlanValidationError("plan_too_large", "Plan exceeds maximum size")
    return plan
