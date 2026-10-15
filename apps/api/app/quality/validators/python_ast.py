import ast
import logging
from dataclasses import dataclass

from app.quality.limits import sanitize_max_chars

logger = logging.getLogger(__name__)

PLACEHOLDER_MODULE = "your_module"


@dataclass(frozen=True)
class CheckFail:
    name: str
    detail: str


@dataclass(frozen=True)
class ParsedPythonModule:
    tree: ast.AST
    source: str


def parse_python_source(source: str, *, max_source_chars: int) -> ParsedPythonModule | CheckFail:
    limit = sanitize_max_chars(max_source_chars, default=200_000)
    if len(source) > limit:
        return CheckFail(
            name="source_size",
            detail=f"Source exceeds maximum allowed size ({limit} characters)",
        )
    if not source.strip():
        return CheckFail(
            name="source_not_empty",
            detail="Source file is empty or whitespace only",
        )
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return CheckFail(
            name="python_syntax_valid",
            detail="Source is not valid Python syntax",
        )
    except Exception as exc:
        logger.error("python parse failed: error_type=%s", type(exc).__name__)
        return CheckFail(
            name="validator_error",
            detail="Validation could not be completed safely",
        )
    return ParsedPythonModule(tree=tree, source=source)


def collect_pytest_test_names(tree: ast.AST) -> list[str]:
    """Collect pytest-discoverable test names.

    Supported:
    - module-level functions named test_*
    - methods named test_* on classes named Test*

    Not counted:
    - nested functions
    - test_* methods on non-Test* classes
    - text inside comments or strings
    """
    names: list[str] = []
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                    "test_"
                ):
                    names.append(f"{node.name}.{item.name}")
    return names


# Backward-compatible alias used by older imports/tests.
def module_level_test_names(tree: ast.AST) -> list[str]:
    return collect_pytest_test_names(tree)


def iter_pytest_test_functions(tree: ast.AST):
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            yield node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith(
                    "test_"
                ):
                    yield item


def module_has_def_or_class(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True
    return False


def has_placeholder_imports(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PLACEHOLDER_MODULE or alias.name.startswith(
                    f"{PLACEHOLDER_MODULE}."
                ):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == PLACEHOLDER_MODULE or module.startswith(f"{PLACEHOLDER_MODULE}."):
                return True
    return False


def has_incomplete_placeholders(tree: ast.AST, *, test_callables_only: bool = False) -> bool:
    """Detect unfinished bodies (bare pass/Ellipsis/NotImplementedError).

    When test_callables_only is True, only pytest-discoverable tests are inspected.
    """
    nodes = (
        iter_pytest_test_functions(tree)
        if test_callables_only
        else (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    )
    for node in nodes:
        body = [
            stmt
            for stmt in node.body
            if not (isinstance(stmt, ast.Expr) and _is_docstring_expr(stmt))
        ]
        if not body:
            return True
        if len(body) == 1 and _is_placeholder_stmt(body[0]):
            return True
    return False


def _is_docstring_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_placeholder_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return True
    if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
        func = stmt.exc.func
        if isinstance(func, ast.Name) and func.id == "NotImplementedError":
            return True
    return False
