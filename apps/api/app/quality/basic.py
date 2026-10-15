from pathlib import Path

from app.domain.models import AgentResult, CheckResult, QualityReport


class BasicQualityGate:
    def evaluate(self, result: AgentResult, workspace: Path) -> QualityReport:
        output_present = bool(result.output.strip())
        artifact_contained = self._artifact_is_contained(workspace, result.artifact_name)
        checks = [
            CheckResult(
                name="output_not_empty",
                passed=output_present,
                detail="output is present" if output_present else "output is empty",
            ),
            CheckResult(
                name="artifact_present",
                passed=artifact_contained,
                detail=(
                    "result.txt is inside the workspace"
                    if artifact_contained
                    else "result.txt is missing"
                ),
            ),
        ]
        return QualityReport(passed=all(check.passed for check in checks), checks=checks)

    def _artifact_is_contained(self, workspace: Path, artifact_name: str) -> bool:
        root = workspace.resolve()
        artifact = (workspace / artifact_name).resolve()
        if not artifact.is_relative_to(root):
            return False
        return artifact.is_file() and artifact.stat().st_size > 0
