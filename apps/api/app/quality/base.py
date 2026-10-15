from pathlib import Path
from typing import Protocol

from app.domain.models import AgentResult, QualityReport


class QualityGate(Protocol):
    def evaluate(self, result: AgentResult, workspace: Path) -> QualityReport:
        """Assess an agent result against the task workspace."""
