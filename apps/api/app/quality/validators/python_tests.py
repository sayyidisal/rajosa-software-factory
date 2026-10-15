from pathlib import Path

from app.domain.models import CheckResult
from app.quality.profiles import PythonUnitTestConfig
from app.quality.limits import sanitize_max_chars
from app.quality.validators.python_ast import (
    CheckFail,
    collect_pytest_test_names,
    has_incomplete_placeholders,
    has_placeholder_imports,
    parse_python_source,
)


def validate_python_unit_tests(
    output: str,
    workspace: Path,
    config: PythonUnitTestConfig,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    artifact = (workspace / config.artifact_name).resolve()
    root = workspace.resolve()

    if not artifact.is_relative_to(root) or not artifact.is_file() or artifact.stat().st_size <= 0:
        return [
            CheckResult(
                name="test_artifact_present",
                passed=False,
                detail=f"Expected test artifact '{config.artifact_name}' is missing",
            )
        ]

    checks.append(
        CheckResult(
            name="test_artifact_present",
            passed=True,
            detail=f"Test artifact '{config.artifact_name}' is present",
        )
    )

    content = artifact.read_text(encoding="utf-8")
    source = content if content.strip() else output
    max_chars = sanitize_max_chars(config.max_source_chars, default=200_000)
    parsed = parse_python_source(source, max_source_chars=max_chars)
    if isinstance(parsed, CheckFail):
        checks.append(
            CheckResult(name=parsed.name, passed=False, detail=parsed.detail)
        )
        checks.append(
            CheckResult(
                name="test_structure_valid",
                passed=False,
                detail=(
                    "Test artifact structure is invalid "
                    "(static check only; tests were not executed)"
                ),
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

    names = collect_pytest_test_names(parsed.tree)
    count_ok = len(names) == config.expected_test_count
    checks.append(
        CheckResult(
            name="test_function_count",
            passed=count_ok,
            detail=(
                f"Found {config.expected_test_count} pytest-discoverable tests"
                if count_ok
                else (
                    f"Expected {config.expected_test_count} pytest-discoverable tests, "
                    f"found {len(names)}"
                )
            ),
        )
    )

    if config.required_test_names:
        missing = [name for name in config.required_test_names if name not in names]
        checks.append(
            CheckResult(
                name="required_test_names",
                passed=not missing,
                detail=(
                    "All required test names are present"
                    if not missing
                    else f"Missing required test names: {', '.join(missing)}"
                ),
            )
        )

    if config.require_direct_usability:
        has_placeholder = has_placeholder_imports(parsed.tree)
        incomplete = has_incomplete_placeholders(parsed.tree, test_callables_only=True)
        usable = not has_placeholder and not incomplete
        detail = "Test file appears directly usable (static check only)"
        if has_placeholder:
            detail = "Test file contains unresolved placeholder import 'your_module'"
        elif incomplete:
            detail = "Test file contains incomplete placeholder implementations"
        checks.append(
            CheckResult(
                name="test_direct_usability",
                passed=usable,
                detail=detail,
            )
        )

    structure_ok = all(check.passed for check in checks)
    checks.append(
        CheckResult(
            name="test_structure_valid",
            passed=structure_ok,
            detail=(
                "Test artifact structure is valid (static check only; tests were not executed)"
                if structure_ok
                else "Test artifact structure is invalid (static check only; tests were not executed)"
            ),
        )
    )
    return checks
