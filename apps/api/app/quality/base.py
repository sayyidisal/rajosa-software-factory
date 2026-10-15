from pathlib import Path
from typing import Protocol

from app.domain.models import AgentResult, QualityReport


class QualityGate(Protocol):
    def evaluate(
        self,
        result: AgentResult,
        workspace: Path,
        *,
        profile: str = "generic_text",
    ) -> QualityReport:
        """Assess an agent result against the task workspace and validation profile."""
