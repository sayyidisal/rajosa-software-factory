"""Supported-topology checks for the local API entry point (Phase 4G.1).

This module does **not** make the FastAPI app multi-process safe. It only
validates launch inputs that the supported ``make dev-api`` / ``python -m
app.run_dev`` path can observe before starting Uvicorn.

Limits (intentional):
- Does not detect a second API instance started by another shell.
- Does not detect ``uvicorn --workers N`` when that CLI is invoked outside
  this entry point and ``WEB_CONCURRENCY`` is unset.
- Application lifespan cannot see sibling worker processes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# Click-style short option with glued integer, e.g. ``-w2`` / ``-w10``.
_GLUED_SHORT_WORKERS = re.compile(r"^-w(\d+)$")


class UnsupportedProcessTopologyError(RuntimeError):
    """Raised when launch configuration requests more than one API worker."""


def _parse_int_workers(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise UnsupportedProcessTopologyError(
            f"invalid uvicorn --workers value '{raw}'"
        ) from exc


def _parse_workers_from_argv(argv: Sequence[str]) -> int | None:
    """Return an explicit ``--workers`` / ``-w`` value from argv, if present.

    Recognizes ``--workers N``, ``--workers=N``, ``-w N``, and glued ``-wN``.
    """
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in ("--workers", "-w"):
            if i + 1 >= len(tokens):
                raise UnsupportedProcessTopologyError(
                    "uvicorn --workers requires an integer value; "
                    "this API supports only a single worker process"
                )
            value = _parse_int_workers(tokens[i + 1])
            i += 2
            return value
        if token.startswith("--workers="):
            return _parse_int_workers(token.split("=", 1)[1])
        glued = _GLUED_SHORT_WORKERS.fullmatch(token)
        if glued is not None:
            return _parse_int_workers(glued.group(1))
        i += 1
    return None


def assert_supported_dev_launch(
    *,
    environ: Mapping[str, str],
    argv: Sequence[str] | None = None,
) -> None:
    """Fail closed if the supported entry point would start multiple workers.

    Checks:
    1. ``WEB_CONCURRENCY`` — Uvicorn defaults ``--workers`` from this env var
       when ``--reload`` is not used. Values other than ``1`` are rejected.
    2. Explicit ``--workers`` / ``-w`` in ``argv`` — rejected unless exactly 1
       (the dev launcher always uses ``--reload``, which is mutually exclusive
       with multi-worker mode).
    """
    raw = environ.get("WEB_CONCURRENCY")
    if raw is not None and str(raw).strip() != "":
        try:
            concurrency = int(str(raw).strip())
        except ValueError as exc:
            raise UnsupportedProcessTopologyError(
                f"invalid WEB_CONCURRENCY value '{raw}'; "
                "unset it or set WEB_CONCURRENCY=1 for this API"
            ) from exc
        if concurrency != 1:
            raise UnsupportedProcessTopologyError(
                f"WEB_CONCURRENCY={concurrency} is unsupported: "
                "exactly one API process is required per SQLite DB/workspace. "
                "Unset WEB_CONCURRENCY or set it to 1. "
                "Multi-worker Uvicorn against one DB is not supported."
            )

    if argv is None:
        return

    workers = _parse_workers_from_argv(argv)
    if workers is None:
        return
    if workers != 1:
        raise UnsupportedProcessTopologyError(
            f"uvicorn --workers {workers} is unsupported: "
            "exactly one API process is required per SQLite DB/workspace. "
            "Use `make dev-api` (single process with --reload) instead."
        )
