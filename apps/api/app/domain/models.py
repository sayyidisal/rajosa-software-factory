from typing import Any, Literal

from pydantic import BaseModel, Field

from app.execution.models import ExecutionSummary
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
    # Use POST /tasks/{id}/execute-tests with confirm=true.


class Task(BaseModel):
    id: str
    title: str
    instruction: str
    validation_profile: ValidationProfileName = "generic_text"
    status: Literal["passed", "failed"]
    output: str
    error: str | None
    quality: QualityReport
    execution: ExecutionSummary | None = None
