from dataclasses import dataclass
from typing import Literal

ValidationProfileName = Literal[
    "generic_text",
    "product_catalog_json",
    "python_unit_tests",
    "python_code",
    "python_test_execution",
]

SUPPORTED_PROFILES: tuple[str, ...] = (
    "generic_text",
    "product_catalog_json",
    "python_unit_tests",
    "python_code",
    "python_test_execution",
)

# Extra product object fields are intentionally allowed for forward compatibility.
# Required fields and types are still enforced.
PRODUCT_EXTRA_FIELDS_POLICY = "allow"

# Applied to all profiles via base checks before profile-specific validation.
GENERIC_MAX_OUTPUT_CHARS = 200_000


@dataclass(frozen=True)
class ProductCatalogConfig:
    exact_product_count: int = 3
    max_output_chars: int = 100_000
    max_products_to_inspect: int = 32


@dataclass(frozen=True)
class PythonUnitTestConfig:
    artifact_name: str = "result.txt"
    expected_test_count: int = 3
    require_direct_usability: bool = True
    required_test_names: tuple[str, ...] = ()
    max_source_chars: int = 200_000


@dataclass(frozen=True)
class PythonCodeConfig:
    artifact_name: str = "result.txt"
    require_def_or_class: bool = True
    reject_placeholder_imports: bool = True
    max_source_chars: int = 200_000


PRODUCT_CATALOG_CONFIG = ProductCatalogConfig()
PYTHON_UNIT_TEST_CONFIG = PythonUnitTestConfig()
PYTHON_CODE_CONFIG = PythonCodeConfig()
