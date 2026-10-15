from pathlib import Path

from app.domain.models import CheckResult
from app.quality.profiles import PythonCodeConfig
from app.quality.limits import sanitize_max_chars
from app.quality.validators.python_ast import (
    CheckFail,
    has_incomplete_placeholders,
    has_placeholder_imports,
    module_has_def_or_class,
    parse_python_source,
)


def validate_python_code(
    output: str,
    workspace: Path,
    config: PythonCodeConfig,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    artifact = (workspace / config.artifact_name).resolve()
    root = workspace.resolve()

    if not artifact.is_relative_to(root) or not artifact.is_file() or artifact.stat().st_size <= 0:
        return [
            CheckResult(
                name="code_artifact_present",
                passed=False,
                detail=f"Expected code artifact '{config.artifact_name}' is missing",
            )
        ]

    checks.append(
        CheckResult(
            name="code_artifact_present",
            passed=True,
            detail=f"Code artifact '{config.artifact_name}' is present",
        )
    )

    source = artifact.read_text(encoding="utf-8") or output
    max_chars = sanitize_max_chars(config.max_source_chars, default=200_000)
    parsed = parse_python_source(source, max_source_chars=max_chars)
    if isinstance(parsed, CheckFail):
        checks.append(
            CheckResult(name=parsed.name, passed=False, detail=parsed.detail)
        )
        checks.append(
            CheckResult(
                name="code_structure_valid",
                passed=False,
                detail="Code structure is invalid (static check only)",
            )
        )
        return checks

    checks.append(
        CheckResult(
            name="python_syntax_valid",
            passed=True,
            detail="Source has valid Python syntax",
        )
    )

    if config.require_def_or_class:
        has_structure = module_has_def_or_class(parsed.tree)
        checks.append(
            CheckResult(
                name="code_structure_valid",
                passed=has_structure,
                detail=(
                    "Code contains a module-level def or class (static check only)"
                    if has_structure
                    else "Code must contain at least one module-level def or class (static check only)"
                ),
            )
        )

    if config.reject_placeholder_imports:
        has_placeholder = has_placeholder_imports(parsed.tree)
        incomplete = has_incomplete_placeholders(parsed.tree)
        ok = not has_placeholder and not incomplete
        detail = "Code has no unresolved placeholders (static check only)"
        if has_placeholder:
            detail = "Code contains unresolved placeholder import 'your_module'"
        elif incomplete:
            detail = "Code contains incomplete placeholder implementations"
        checks.append(
            CheckResult(
                name="code_no_placeholder_imports",
                passed=ok,
                detail=detail,
            )
        )

    return checks
