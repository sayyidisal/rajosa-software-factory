"""Local Docker-based isolated Python test runner.

Does not pull images. Does not execute on the host as a fallback.
Does not pass host environment variables into the job.
Requires a pinned per-container seccomp profile; never falls back to unconfined.
Feature remains unavailable when the daemon or pinned image is missing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from app.execution.limits import (
    APPROVED_RUNTIME_ID,
    APPROVED_TEST_ARGV,
    APPROVED_TEST_PROFILE,
    CONTAINER_WORK_DIR,
    CONTAINER_WORK_USER,
    DEFAULT_EXECUTION_IMAGE,
)
from app.execution.models import ExecutionRequest, ExecutionResult, SafeArtifactMeta
from app.execution.seccomp import (
    SeccompProfileError,
    default_seccomp_profile_path,
    validate_seccomp_profile,
)
from app.execution.staging import collect_output_artifacts, truncate_bytes

logger = logging.getLogger(__name__)

# Docker CLI convention: 125 = daemon/client error starting the container.
_DOCKER_INFRA_EXIT_CODES = frozenset({125, 126, 127})


class LocalDockerExecutionRunner:
    def __init__(
        self,
        *,
        image: str = DEFAULT_EXECUTION_IMAGE,
        docker_bin: str | None = None,
        allow_pull: bool = False,
        seccomp_profile: Path | None = None,
    ) -> None:
        if allow_pull:
            raise ValueError("image pull is not permitted in this phase")
        self.image = image
        self.docker_bin = docker_bin or shutil.which("docker") or "docker"
        # Store the configured path without resolving; validate on use.
        self.seccomp_profile = (
            Path(seccomp_profile)
            if seccomp_profile is not None
            else default_seccomp_profile_path()
        )

    def is_available(self) -> bool:
        try:
            validate_seccomp_profile(self.seccomp_profile)
        except SeccompProfileError:
            return False
        return self._daemon_ok() and self._image_present()

    def _daemon_ok(self) -> bool:
        try:
            completed = subprocess.run(
                [self.docker_bin, "info"],
                capture_output=True,
                timeout=5,
                check=False,
                env={"PATH": _safe_path()},
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _image_present(self) -> bool:
        try:
            completed = subprocess.run(
                [self.docker_bin, "image", "inspect", self.image],
                capture_output=True,
                timeout=5,
                check=False,
                env={"PATH": _safe_path()},
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def build_docker_argv(self, request: ExecutionRequest) -> list[str]:
        """Construct a fixed docker run argv from trusted config only."""
        if request.runtime_id != APPROVED_RUNTIME_ID:
            raise ValueError("unsupported runtime_id")
        if request.test_profile != APPROVED_TEST_PROFILE:
            raise ValueError("unsupported test_profile")

        profile = validate_seccomp_profile(self.seccomp_profile)
        staging = Path(request.staged_root).resolve()
        memory = f"{request.limits.memory_mib}m"
        cpus = str(request.limits.cpu_quota)
        pids = str(request.limits.pids_limit)

        argv: list[str] = [
            self.docker_bin,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            CONTAINER_WORK_USER,
            "--workdir",
            CONTAINER_WORK_DIR,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            # Explicit per-container profile — never unconfined / never omitted.
            "--security-opt",
            f"seccomp={profile}",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            cpus,
            "--pids-limit",
            pids,
            # Dedicated staging only — never repo root, home, or docker.sock.
            "--mount",
            f"type=bind,source={staging},destination={CONTAINER_WORK_DIR}",
            # Empty env: do not forward host environment or secrets.
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            self.image,
            *APPROVED_TEST_ARGV,
        ]
        # Hard guard against accidental unconfined configuration.
        joined = " ".join(argv)
        if "seccomp=unconfined" in joined.lower():
            raise SeccompProfileError("refusing unconfined seccomp configuration")
        return argv

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        try:
            validate_seccomp_profile(self.seccomp_profile)
        except SeccompProfileError:
            logger.error("seccomp profile validation failed")
            return ExecutionResult(
                execution_id=request.execution_id,
                status="infrastructure_error",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="seccomp_unavailable",
                detail=(
                    "Required seccomp profile is missing, invalid, or altered; "
                    "execution refused"
                ),
                duration_ms=_ms(started),
            ).validated()

        if not self._daemon_ok():
            return ExecutionResult(
                execution_id=request.execution_id,
                status="runner_unavailable",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="daemon_unavailable",
                detail="Docker daemon is not available",
                duration_ms=_ms(started),
            ).validated()

        if not self._image_present():
            return ExecutionResult(
                execution_id=request.execution_id,
                status="runner_unavailable",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="image_unavailable",
                detail=(
                    f"Pinned execution image '{self.image}' is not present locally. "
                    "Pull/build is disabled; load the image manually before enabling."
                ),
                duration_ms=_ms(started),
            ).validated()

        try:
            argv = self.build_docker_argv(request)
        except SeccompProfileError:
            logger.error("seccomp profile rejected during argv construction")
            return ExecutionResult(
                execution_id=request.execution_id,
                status="infrastructure_error",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="seccomp_unavailable",
                detail=(
                    "Required seccomp profile is missing, invalid, or altered; "
                    "execution refused"
                ),
                duration_ms=_ms(started),
            ).validated()
        except ValueError as exc:
            return ExecutionResult(
                execution_id=request.execution_id,
                status="rejected",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="unsupported_profile",
                detail=str(exc),
                duration_ms=_ms(started),
            ).validated()

        timeout = request.limits.wall_clock_seconds
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
                # Intentionally minimal env — no os.environ passthrough.
                env={"PATH": _safe_path()},
            )
        except subprocess.TimeoutExpired as exc:
            stdout = truncate_bytes(exc.stdout or b"", request.limits.max_stdout_bytes)
            stderr = truncate_bytes(exc.stderr or b"", request.limits.max_stderr_bytes)
            return ExecutionResult(
                execution_id=request.execution_id,
                status="timed_out",
                timed_out=True,
                exit_code=None,
                tests_executed=False,
                tests_passed=False,
                runner_error_category="timeout",
                stdout_summary=stdout,
                stderr_summary=stderr,
                detail="Execution exceeded wall-clock limit",
                duration_ms=_ms(started),
            ).validated()
        except OSError:
            logger.error("docker runner OSError")
            return ExecutionResult(
                execution_id=request.execution_id,
                status="infrastructure_error",
                tests_executed=False,
                tests_passed=False,
                runner_error_category="runtime_crash",
                detail="Failed to start execution runtime",
                duration_ms=_ms(started),
            ).validated()

        stdout = truncate_bytes(completed.stdout or b"", request.limits.max_stdout_bytes)
        stderr = truncate_bytes(completed.stderr or b"", request.limits.max_stderr_bytes)
        combined_err = (completed.stderr or b"").decode("utf-8", errors="replace").lower()

        # Fail closed if Docker could not apply the profile / start the job.
        if completed.returncode in _DOCKER_INFRA_EXIT_CODES or (
            "seccomp" in combined_err and completed.returncode != 0
        ):
            return ExecutionResult(
                execution_id=request.execution_id,
                status="infrastructure_error",
                exit_code=completed.returncode,
                tests_executed=False,
                tests_passed=False,
                runner_error_category="seccomp_unavailable",
                stdout_summary=stdout,
                stderr_summary=stderr,
                detail="Execution runtime rejected the seccomp configuration",
                duration_ms=_ms(started),
            ).validated()

        artifacts: list[SafeArtifactMeta] = []
        try:
            for name, size in collect_output_artifacts(
                Path(request.staged_root), limits=request.limits
            ):
                artifacts.append(SafeArtifactMeta(name=name, size_bytes=size))
        except ValueError as exc:
            return ExecutionResult(
                execution_id=request.execution_id,
                status="infrastructure_error",
                exit_code=completed.returncode,
                tests_executed=True,
                tests_passed=False,
                runner_error_category="invalid_result",
                stdout_summary=stdout,
                stderr_summary=stderr,
                detail=f"Output collection rejected: {exc}",
                duration_ms=_ms(started),
            ).validated()

        exit_code = completed.returncode
        if exit_code == 0:
            return ExecutionResult(
                execution_id=request.execution_id,
                status="completed",
                exit_code=0,
                tests_executed=True,
                tests_passed=True,
                stdout_summary=stdout,
                stderr_summary=stderr,
                artifacts=artifacts,
                detail="Approved pytest command completed with exit code 0",
                duration_ms=_ms(started),
            ).validated()

        return ExecutionResult(
            execution_id=request.execution_id,
            status="tests_failed",
            exit_code=exit_code,
            tests_executed=True,
            tests_passed=False,
            stdout_summary=stdout,
            stderr_summary=stderr,
            artifacts=artifacts,
            detail=f"Approved pytest command exited with code {exit_code}",
            duration_ms=_ms(started),
        ).validated()


def _safe_path() -> str:
    # Narrow PATH for invoking docker CLI only — not passed into the container.
    return "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
