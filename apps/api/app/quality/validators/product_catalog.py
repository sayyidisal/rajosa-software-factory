import json
import logging
import math
import re
from typing import Any

from app.domain.models import CheckResult
from app.quality.profiles import ProductCatalogConfig
from app.quality.limits import sanitize_max_chars

logger = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```")
_DEFAULT_MAX_OUTPUT_CHARS = 100_000


class DuplicateKeyError(ValueError):
    """Raised when strict JSON parsing encounters duplicate object keys."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _name_is_valid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    # Reject invisible-only names (e.g. zero-width spaces).
    return any(ch.isalnum() for ch in stripped)


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {name}")


def _parse_strict_json(text: str) -> Any:
    # Reject NaN/Infinity via parse_constant. Do not rely on allow_nan= (not
    # available on all Python builds present in this environment).
    return json.loads(
        text,
        strict=True,
        parse_constant=_reject_nonstandard_constant,
        object_pairs_hook=_object_pairs_hook,
    )


def validate_product_catalog_json(
    output: str,
    config: ProductCatalogConfig,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    text = output.strip()
    max_chars = sanitize_max_chars(
        config.max_output_chars,
        default=_DEFAULT_MAX_OUTPUT_CHARS,
    )

    # Enforce size before fence scanning or JSON parsing.
    if len(output) > max_chars:
        return [
            CheckResult(
                name="json_input_size",
                passed=False,
                detail=f"Output exceeds maximum allowed size ({max_chars} characters)",
            )
        ]

    if _FENCE_RE.search(text):
        return [
            CheckResult(
                name="json_no_markdown_fence",
                passed=False,
                detail="JSON output must not include Markdown code fences",
            )
        ]

    try:
        data = _parse_strict_json(text)
    except DuplicateKeyError:
        return [
            CheckResult(
                name="json_no_duplicate_keys",
                passed=False,
                detail="JSON objects must not contain duplicate keys",
            )
        ]
    except (json.JSONDecodeError, ValueError, TypeError):
        return [
            CheckResult(
                name="json_parseable",
                passed=False,
                detail="Output is not valid standard JSON",
            )
        ]
    except Exception:
        logger.error("product json validator failed: %s", "UnexpectedError")
        return [
            CheckResult(
                name="validator_error",
                passed=False,
                detail="Validation could not be completed safely",
            )
        ]

    checks.append(
        CheckResult(
            name="json_parseable",
            passed=True,
            detail="Output is valid standard JSON",
        )
    )
    checks.append(
        CheckResult(
            name="json_no_duplicate_keys",
            passed=True,
            detail="JSON objects have unique keys",
        )
    )

    if not isinstance(data, dict):
        checks.append(
            CheckResult(
                name="json_root_object",
                passed=False,
                detail="JSON root must be an object",
            )
        )
        return checks

    checks.append(
        CheckResult(
            name="json_root_object",
            passed=True,
            detail="JSON root is an object",
        )
    )

    if "products" not in data:
        checks.append(
            CheckResult(
                name="products_field_present",
                passed=False,
                detail="Missing required field 'products'",
            )
        )
        return checks

    products = data["products"]
    if not isinstance(products, list):
        checks.append(
            CheckResult(
                name="products_is_array",
                passed=False,
                detail="Field 'products' must be an array",
            )
        )
        return checks

    checks.append(
        CheckResult(
            name="products_is_array",
            passed=True,
            detail="Field 'products' is an array",
        )
    )

    expected = config.exact_product_count
    count_ok = len(products) == expected
    checks.append(
        CheckResult(
            name="products_exact_count",
            passed=count_ok,
            detail=(
                f"products contains exactly {expected} items"
                if count_ok
                else f"products must contain exactly {expected} items, found {len(products)}"
            ),
        )
    )

    inspect_limit = min(len(products), config.max_products_to_inspect)
    for index in range(inspect_limit):
        checks.extend(_validate_product(products[index], index))

    if len(products) > config.max_products_to_inspect:
        checks.append(
            CheckResult(
                name="products_inspection_limit",
                passed=False,
                detail=(
                    "Too many product items to inspect safely; "
                    f"limit is {config.max_products_to_inspect}"
                ),
            )
        )

    return checks


def _validate_product(product: Any, index: int) -> list[CheckResult]:
    prefix = f"product[{index}]"
    if not isinstance(product, dict):
        return [
            CheckResult(
                name=f"{prefix}_is_object",
                passed=False,
                detail=f"{prefix} must be an object",
            )
        ]

    checks: list[CheckResult] = []
    required = ("id", "name", "price", "in_stock")
    for field in required:
        if field not in product:
            checks.append(
                CheckResult(
                    name=f"{prefix}_{field}_present",
                    passed=False,
                    detail=f"{prefix} missing required field '{field}'",
                )
            )

    if "id" in product:
        ok = _is_int(product["id"])
        checks.append(
            CheckResult(
                name=f"{prefix}_id_type",
                passed=ok,
                detail=(
                    f"{prefix}.id is an integer"
                    if ok
                    else f"{prefix}.id must be an integer (booleans are not allowed)"
                ),
            )
        )

    if "name" in product:
        ok = _name_is_valid(product["name"])
        checks.append(
            CheckResult(
                name=f"{prefix}_name_valid",
                passed=ok,
                detail=(
                    f"{prefix}.name is a non-empty string with visible characters"
                    if ok
                    else f"{prefix}.name must be a non-empty string with visible characters"
                ),
            )
        )

    if "price" in product:
        price = product["price"]
        ok = (
            _is_number(price)
            and math.isfinite(float(price))
            and float(price) > 0
        )
        checks.append(
            CheckResult(
                name=f"{prefix}_price_valid",
                passed=ok,
                detail=(
                    f"{prefix}.price is a finite positive number"
                    if ok
                    else (
                        f"{prefix}.price must be a finite positive number "
                        "(booleans and non-finite values are not allowed)"
                    )
                ),
            )
        )

    if "in_stock" in product:
        ok = isinstance(product["in_stock"], bool)
        checks.append(
            CheckResult(
                name=f"{prefix}_in_stock_type",
                passed=ok,
                detail=(
                    f"{prefix}.in_stock is a boolean"
                    if ok
                    else f"{prefix}.in_stock must be a boolean"
                ),
            )
        )

    return checks
