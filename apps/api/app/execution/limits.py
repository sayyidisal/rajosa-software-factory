"""Hard application-defined execution bounds.

API input cannot raise these above the maximums. Defaults are conservative
initial policy values for local development only.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionLimits:
    wall_clock_seconds: int = 30
    memory_mib: int = 512
    cpu_quota: float = 1.0
    pids_limit: int = 64
    max_stdout_bytes: int = 16_384
    max_stderr_bytes: int = 16_384
    max_files: int = 32
    max_file_bytes: int = 200_000
    max_total_staged_bytes: int = 512_000
    max_output_artifact_bytes: int = 64_000
    max_output_files: int = 8


# Absolute ceilings — callers may only request lower values.
HARD_MAX = ExecutionLimits(
    wall_clock_seconds=60,
    memory_mib=1024,
    cpu_quota=2.0,
    pids_limit=128,
    max_stdout_bytes=65_536,
    max_stderr_bytes=65_536,
    max_files=64,
    max_file_bytes=400_000,
    max_total_staged_bytes=1_048_576,
    max_output_artifact_bytes=128_000,
    max_output_files=16,
)

DEFAULT_LIMITS = ExecutionLimits()

APPROVED_RUNTIME_ID = "local_docker_python"
APPROVED_TEST_PROFILE = "python_test_execution"
APPROVED_IMAGE_ENV = "EXECUTION_IMAGE"
DEFAULT_EXECUTION_IMAGE = "rajosa-exec-python:3.12-pytest"

# Fixed argv inside the container — never interpolated from task/model text.
APPROVED_TEST_ARGV = (
    "python",
    "-m",
    "pytest",
    "/workspace/tests",
    "-q",
    "--tb=line",
    "--no-header",
)

CONTAINER_WORK_USER = "65534:65534"  # nobody
CONTAINER_WORK_DIR = "/workspace"
STAGED_TEST_RELATIVE = "tests/test_artifact.py"
OUTPUT_DIR_RELATIVE = "output"


def clamp_limits(
    *,
    wall_clock_seconds: int | None = None,
    memory_mib: int | None = None,
) -> ExecutionLimits:
    """Return limits capped by HARD_MAX and never below 1 for time/memory."""
    base = DEFAULT_LIMITS
    wall = wall_clock_seconds if wall_clock_seconds is not None else base.wall_clock_seconds
    mem = memory_mib if memory_mib is not None else base.memory_mib
    return ExecutionLimits(
        wall_clock_seconds=max(1, min(wall, HARD_MAX.wall_clock_seconds)),
        memory_mib=max(64, min(mem, HARD_MAX.memory_mib)),
        cpu_quota=base.cpu_quota,
        pids_limit=base.pids_limit,
        max_stdout_bytes=base.max_stdout_bytes,
        max_stderr_bytes=base.max_stderr_bytes,
        max_files=base.max_files,
        max_file_bytes=base.max_file_bytes,
        max_total_staged_bytes=base.max_total_staged_bytes,
        max_output_artifact_bytes=base.max_output_artifact_bytes,
        max_output_files=base.max_output_files,
    )
