"""Strict JSON parsing for plan contracts (Phase 4C).

Rejects duplicate object keys and non-finite constants (NaN/Infinity).
Mirrors the product-catalog approach without coupling quality validators to plans.
"""

from __future__ import annotations

import json
from typing import Any


class DuplicateKeyError(ValueError):
    """Raised when a JSON object contains duplicate keys."""


class NonFiniteNumberError(ValueError):
    """Raised when JSON contains NaN or Infinity."""


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(name: str) -> Any:
    raise NonFiniteNumberError(f"non-standard JSON constant: {name}")


def parse_strict_json(text: str) -> Any:
    """Parse JSON with fail-closed semantics for duplicates and non-finites."""
    return json.loads(
        text,
        strict=True,
        parse_constant=_reject_nonstandard_constant,
        object_pairs_hook=_object_pairs_hook,
    )
