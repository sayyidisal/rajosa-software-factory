import logging
from pathlib import Path

from app.domain.models import AgentResult, CheckResult, QualityReport
from app.quality.profiles import (
    GENERIC_MAX_OUTPUT_CHARS,
    PRODUCT_CATALOG_CONFIG,
    PYTHON_CODE_CONFIG,
    PYTHON_UNIT_TEST_CONFIG,
    SUPPORTED_PROFILES,
)
from app.quality.validators.product_catalog import validate_product_catalog_json
from app.quality.limits import sanitize_max_chars
from app.quality.validators.python_code import validate_python_code
from app.quality.validators.python_tests import validate_python_unit_tests

logger = logging.getLogger(__name__)


class BasicQualityGate:
    def evaluate(
        self,
        result: AgentResult,
        workspace: Path,
        *,
        profile: str = "generic_text",
    ) -> QualityReport:
        try:
            return self._evaluate(result, workspace, profile=profile)
        except Exception as exc:
            logger.error(
                "quality gate failed: profile=%s error_type=%s",
                profile,
                type(exc).__name__,
            )
            return QualityReport(
                passed=False,
                checks=[
                    CheckResult(
                        name="validator_error",
                        passed=False,
                        detail="Validation could not be completed safely",
                    )
                ],
            )

    def _evaluate(
        self,
        result: AgentResult,
        workspace: Path,
        *,
        profile: str,
    ) -> QualityReport:
        normalized = (profile or "generic_text").strip().lower()
        if normalized not in SUPPORTED_PROFILES:
            return QualityReport(
                passed=False,
                checks=[
                    CheckResult(
                        name="validation_profile_supported",
                        passed=False,
                        detail=(
                            f"Unknown validation_profile '{profile}'. "
                            f"Supported: {', '.join(SUPPORTED_PROFILES)}"
                        ),
                    )
                ],
            )

        checks = self._base_checks(result, workspace)
        # Fail closed early on oversized payloads for every profile.
        if any(check.name == "output_size" and not check.passed for check in checks):
            return QualityReport(passed=False, checks=checks)

        if normalized == "generic_text":
            checks.append(
                CheckResult(
                    name="generic_text_scope",
                    passed=True,
                    detail=(
                        "Generic text profile applies artifact, non-empty, and size checks only; "
                        "no semantic correctness score is inferred"
                    ),
                )
            )
        elif normalized == "product_catalog_json":
            checks.extend(
                validate_product_catalog_json(result.output, PRODUCT_CATALOG_CONFIG)
            )
        elif normalized == "python_unit_tests":
            checks.extend(
                validate_python_unit_tests(
                    result.output,
                    workspace,
                    PYTHON_UNIT_TEST_CONFIG,
                )
            )
        elif normalized == "python_test_execution":
            # Static AST validation only on create. Runtime requires an explicit
            # POST /tasks/{id}/execute-tests call — never implied by this profile.
            checks.extend(
                validate_python_unit_tests(
                    result.output,
                    workspace,
                    PYTHON_UNIT_TEST_CONFIG,
                )
            )
            checks.append(
                CheckResult(
                    name="execution_not_requested",
                    passed=True,
                    detail=(
                        "python_test_execution applies static checks on create; "
                        "isolated runtime execution requires an explicit execute-tests request"
                    ),
                )
            )
        elif normalized == "python_code":
            checks.extend(
                validate_python_code(
                    result.output,
                    workspace,
                    PYTHON_CODE_CONFIG,
                )
            )

        return QualityReport(passed=all(check.passed for check in checks), checks=checks)

    def _base_checks(self, result: AgentResult, workspace: Path) -> list[CheckResult]:
        max_chars = sanitize_max_chars(GENERIC_MAX_OUTPUT_CHARS, default=200_000)
        size_ok = len(result.output) <= max_chars
        output_present = bool(result.output.strip())
        artifact_contained = self._artifact_is_contained(workspace, result.artifact_name)
        return [
            CheckResult(
                name="output_size",
                passed=size_ok,
                detail=(
                    "output size is within limits"
                    if size_ok
                    else f"Output exceeds maximum allowed size ({max_chars} characters)"
                ),
            ),
            CheckResult(
                name="output_not_empty",
                passed=output_present,
                detail="output is present" if output_present else "output is empty",
            ),
            CheckResult(
                name="artifact_present",
                passed=artifact_contained,
                detail=(
                    f"{result.artifact_name} is inside the workspace"
                    if artifact_contained
                    else f"{result.artifact_name} is missing"
                ),
            ),
        ]

    def _artifact_is_contained(self, workspace: Path, artifact_name: str) -> bool:
        root = workspace.resolve()
        try:
            artifact = (workspace / artifact_name).resolve()
        except OSError:
            return False
        if not artifact.is_relative_to(root):
            return False
        return artifact.is_file() and artifact.stat().st_size > 0
