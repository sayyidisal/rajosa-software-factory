"""Supported local API launcher (Phase 4G.1).

Invoked by ``make dev-api``. Always starts a single Uvicorn process with
``--reload`` (mutually exclusive with multi-worker mode). Refuses
``WEB_CONCURRENCY!=1`` and explicit ``--workers N`` (N!=1) before bind.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> None:
    from app.config import get_settings
    from app.single_process import (
        UnsupportedProcessTopologyError,
        assert_supported_dev_launch,
    )

    args = list(sys.argv[1:] if argv is None else argv)
    try:
        assert_supported_dev_launch(environ=os.environ, argv=args)
    except UnsupportedProcessTopologyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    settings = get_settings()
    # reload=True ⇒ Uvicorn ignores a workers count; we never pass workers.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
