from typing import Literal

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


class QualityReport(BaseModel):
    passed: bool
    checks: list[CheckResult]


class AgentResult(BaseModel):
    output: str
    artifact_name: str = "result.txt"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=4000)


class Task(BaseModel):
    id: str
    title: str
    instruction: str
    status: Literal["passed", "failed"]
    output: str
    error: str | None
    quality: QualityReport
