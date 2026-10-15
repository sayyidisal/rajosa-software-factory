"""Local single-flight execution lock — no unbounded queue."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class ExecutionBusyError(RuntimeError):
    """Raised when another local execution is already active."""


class ExecutionGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: str | None = None

    @property
    def active_execution_id(self) -> str | None:
        return self._active

    @contextmanager
    def acquire(self, execution_id: str) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise ExecutionBusyError("another execution is already active")
        try:
            self._active = execution_id
            yield
        finally:
            self._active = None
            self._lock.release()


# Process-wide gate for local development (one execution at a time).
GLOBAL_EXECUTION_GATE = ExecutionGate()
