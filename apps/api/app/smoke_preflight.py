"""Fail-closed preflight for the local ``make smoke-api`` harness (Phase 1A).

Importing this module does not open SQLite, run migrations, start a server,
or touch the network. It only validates configuration that the official
``python -m app.run_dev`` entry point will read via ``get_settings()``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path

from app.agents.factory import build_agent_adapter, normalize_adapter_name
from app.agents.mock import MockAgentAdapter
from app.auth import normalize_api_token
from app.config import API_ROOT, Settings, get_settings

# Canonical active paths for the supported local topology (must never be used by smoke).
ACTIVE_TASK_DB_PATH = (API_ROOT / "var" / "rajosa.sqlite3").resolve()
ACTIVE_WORKSPACE_ROOT = (API_ROOT / "var" / "workspaces").resolve()

SMOKE_ROOT_ENV = "SMOKE_ROOT"

# Bind addresses allowed for smoke (must match settings.api_host after get_settings).
SMOKE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class SmokePreflightError(RuntimeError):
    """Raised when smoke configuration is unsafe or incomplete."""


def normalize_smoke_bind_host(host: str | None) -> str:
    """Normalize a bind host for loopback comparison (strip + lower-case)."""
    return (host or "").strip().lower()


def apply_smoke_overrides(
    environ: MutableMapping[str, str],
    *,
    smoke_root: Path,
    token: str,
    api_port: str | None = None,
) -> MutableMapping[str, str]:
    """Apply disposable smoke overrides so they win over developer ``.env`` keys.

    Mutates ``environ`` in place and returns it. Does not print the token.
    """
    root = smoke_root.expanduser().resolve()
    environ[SMOKE_ROOT_ENV] = str(root)
    environ["TASK_DB_PATH"] = str(root / "smoke.sqlite3")
    environ["WORKSPACE_ROOT"] = str(root / "workspaces")
    environ["AGENT_ADAPTER"] = "mock"
    environ["EXECUTION_RUNNER"] = "off"
    environ["RAJOSA_API_TOKEN"] = token
    # Smoke is loopback-only; never inherit a non-local bind from developer .env.
    environ["API_HOST"] = "127.0.0.1"
    if api_port is not None:
        environ["API_PORT"] = str(api_port)
    return environ


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def assert_smoke_safe(
    settings: Settings,
    *,
    smoke_root: Path,
) -> None:
    """Fail closed if settings are not safe for an isolated local smoke run."""
    root = smoke_root.expanduser().resolve()
    if not root.is_absolute():
        raise SmokePreflightError(f"smoke root must be absolute: {root}")

    db = settings.task_db_path.expanduser().resolve()
    ws = settings.workspace_root.expanduser().resolve()

    if db == ACTIVE_TASK_DB_PATH:
        raise SmokePreflightError(
            f"refusing active task database path: {db}"
        )
    if ws == ACTIVE_WORKSPACE_ROOT:
        raise SmokePreflightError(
            f"refusing active workspace root: {ws}"
        )
    if not _is_relative_to(db, root):
        raise SmokePreflightError(
            f"task DB path must be under smoke root {root}: {db}"
        )
    if not _is_relative_to(ws, root):
        raise SmokePreflightError(
            f"workspace root must be under smoke root {root}: {ws}"
        )
    # DB file itself should live directly (or nested) under root; workspace too.
    if db.parent != root and not _is_relative_to(db.parent, root):
        raise SmokePreflightError(f"task DB parent outside smoke root: {db}")

    # Effective bind host from get_settings() (env + non-overriding .env load).
    bind_host = normalize_smoke_bind_host(settings.api_host)
    if bind_host not in SMOKE_LOOPBACK_HOSTS:
        raise SmokePreflightError(
            f"smoke requires loopback API_HOST "
            f"(one of {sorted(SMOKE_LOOPBACK_HOSTS)}), got {settings.api_host!r}"
        )

    adapter_name = normalize_adapter_name(settings.agent_adapter)
    if adapter_name != "mock":
        raise SmokePreflightError(
            f"smoke requires AGENT_ADAPTER=mock, got {adapter_name!r}"
        )
    adapter = build_agent_adapter(settings)
    if not isinstance(adapter, MockAgentAdapter):
        raise SmokePreflightError(
            f"smoke requires MockAgentAdapter, got {type(adapter).__name__}"
        )

    runner = (settings.execution_runner or "").strip().lower() or "off"
    if runner != "off":
        raise SmokePreflightError(
            f"smoke requires EXECUTION_RUNNER=off, got {runner!r}"
        )

    token = normalize_api_token(settings.rajosa_api_token)
    if token is None:
        raise SmokePreflightError(
            "smoke requires a usable RAJOSA_API_TOKEN "
            "(non-empty, length >= 16); token value is never logged"
        )


def run_preflight(*, environ: MutableMapping[str, str] | None = None) -> Settings:
    """Load settings from the effective environment and validate smoke safety."""
    env = os.environ if environ is None else environ
    raw_root = env.get(SMOKE_ROOT_ENV, "").strip()
    if not raw_root:
        raise SmokePreflightError(
            f"{SMOKE_ROOT_ENV} must be set to the disposable smoke directory"
        )
    smoke_root = Path(raw_root)
    # Pass environ explicitly when provided so tests do not touch process env
    # secrets; production Makefile uses process environ (environ is None).
    if environ is None:
        settings = get_settings()
    else:
        settings = get_settings(environ=dict(environ))
    assert_smoke_safe(settings, smoke_root=smoke_root)
    return settings


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m app.smoke_preflight`` (exit 0 ok, 2 fail-closed)."""
    del argv  # reserved for future flags; none today
    try:
        settings = run_preflight()
    except SmokePreflightError as exc:
        print(f"error: smoke preflight failed: {exc}", file=sys.stderr)
        return 2
    # Success: print only non-secret confirmation (paths are disposable).
    print(
        "smoke preflight ok "
        f"(adapter={settings.agent_adapter!r}, "
        f"runner={settings.execution_runner!r}, "
        f"host={settings.api_host!r}, "
        f"db={settings.task_db_path}, "
        f"workspace={settings.workspace_root})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
