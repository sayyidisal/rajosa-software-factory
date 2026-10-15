"""Bounded structured plan schema and validation (Phase 4B)."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PLAN_SCHEMA_VERSION = "1"
MAX_PLAN_STEPS = 8
MAX_GOALS = 8
MAX_ACCEPTANCE_CHECKS = 16
MAX_SUMMARY_CHARS = 500
MAX_GOAL_CHARS = 200
MAX_STEP_DESC_CHARS = 500
MAX_ARTIFACT_NAME_CHARS = 80
MAX_CHECK_CHARS = 200
MAX_STEP_ARTIFACTS = 8
MAX_SERIALIZED_PLAN_BYTES = 12_000

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
    }
)

_STEP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_ARTIFACT_RE = re.compile(r"^[a-zA-Z0-9._/-]{1,80}$")


class PlanValidationError(ValueError):
    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        super().__init__(detail)


class PlanStep(BaseModel):
    step_id: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=MAX_STEP_DESC_CHARS)
    expected_artifacts: list[str] = Field(default_factory=list, max_length=MAX_STEP_ARTIFACTS)
    acceptance_checks: list[str] = Field(default_factory=list, max_length=MAX_ACCEPTANCE_CHECKS)

    @field_validator("step_id")
    @classmethod
    def _step_id_format(cls, value: str) -> str:
        if not _STEP_ID_RE.fullmatch(value):
            raise ValueError("step_id must be alphanumeric, underscore, or hyphen")
        return value

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

    @field_validator("acceptance_checks")
    @classmethod
    def _checks(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = item.strip()
            if not text or len(text) > MAX_CHECK_CHARS:
                raise ValueError("invalid acceptance check")
            cleaned.append(text)
        return cleaned


class Plan(BaseModel):
    schema_version: Literal["1"] = PLAN_SCHEMA_VERSION
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    goals: list[str] = Field(min_length=1, max_length=MAX_GOALS)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    acceptance_checks: list[str] = Field(default_factory=list, max_length=MAX_ACCEPTANCE_CHECKS)
    requires_execution: bool = False

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

    @field_validator("acceptance_checks")
    @classmethod
    def _top_checks(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = item.strip()
            if not text or len(text) > MAX_CHECK_CHARS:
                raise ValueError("invalid acceptance check")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def _unique_steps(self) -> Plan:
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step_id values are not allowed")
        return self


def _reject_forbidden_keys(payload: Any, *, path: str = "$") -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_PLAN_KEYS:
                raise PlanValidationError(
                    "forbidden_field",
                    f"Plan field '{key}' is not allowed",
                )
            _reject_forbidden_keys(value, path=f"{path}.{key}")
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            _reject_forbidden_keys(item, path=f"{path}[{index}]")


def parse_plan_text(raw: str) -> Plan:
    """Parse and validate a plan from adapter text output. Fail closed."""
    text = (raw or "").strip()
    if not text:
        raise PlanValidationError("empty_plan", "Plan output is empty")
    if len(text.encode("utf-8")) > MAX_SERIALIZED_PLAN_BYTES:
        raise PlanValidationError("plan_too_large", "Plan exceeds maximum size")

    # Accept optional fenced JSON.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError("malformed_json", "Plan is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise PlanValidationError("invalid_root", "Plan root must be a JSON object")

    version = payload.get("schema_version")
    if version is None:
        raise PlanValidationError("missing_schema_version", "schema_version is required")
    if str(version) != PLAN_SCHEMA_VERSION:
        raise PlanValidationError(
            "unsupported_schema_version",
            f"Unsupported schema_version '{version}'",
        )

    _reject_forbidden_keys(payload)

    try:
        plan = Plan.model_validate(payload)
    except ValidationError as exc:
        raise PlanValidationError("schema_invalid", "Plan failed schema validation") from exc

    serialized = plan.model_dump_json().encode("utf-8")
    if len(serialized) > MAX_SERIALIZED_PLAN_BYTES:
        raise PlanValidationError("plan_too_large", "Plan exceeds maximum size")
    return plan
