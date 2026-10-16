"""Phase 4F.2 — offline characterization of Docker timeout kill (mocked subprocess).

These tests do not start Docker or containers. They do not prove daemon behavior.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.execution.docker_runner import (
    CONTAINER_KILL_TIMEOUT_SECONDS,
    CONTAINER_NAME_PREFIX,
    LocalDockerExecutionRunner,
    best_effort_kill_container,
    container_name_for_execution,
)
from app.execution.limits import APPROVED_TEST_ARGV, clamp_limits
from app.execution.models import ExecutionRequest, StagedArtifact
from app.execution.seccomp import default_seccomp_profile_path


def _request(tmp_path: Path, *, execution_id: str | None = None) -> ExecutionRequest:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return ExecutionRequest(
        execution_id=execution_id or str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        staged_root=str(staging),
        artifacts=[
            StagedArtifact(relative_path="tests/test_artifact.py", size_bytes=10)
        ],
        limits=clamp_limits(wall_clock_seconds=2),
    )


def test_argv_includes_exact_container_name(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    req = _request(tmp_path)
    argv = runner.build_docker_argv(req)
    expected = container_name_for_execution(req.execution_id)
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == expected
    assert expected.startswith(CONTAINER_NAME_PREFIX)
    assert argv[-len(APPROVED_TEST_ARGV) :] == list(APPROVED_TEST_ARGV)


def test_ambiguous_execution_id_rejected_before_run(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    req = _request(tmp_path, execution_id="not-a-uuid")
    with pytest.raises(ValueError, match="UUID"):
        runner.build_docker_argv(req)


def test_timeout_triggers_kill_for_exact_known_name(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    req = _request(tmp_path)
    expected_name = container_name_for_execution(req.execution_id)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=2)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with (
        patch.object(runner, "_daemon_ok", return_value=True),
        patch.object(runner, "_image_present", return_value=True),
        patch("app.execution.docker_runner.validate_seccomp_profile") as vsec,
        patch("app.execution.docker_runner.subprocess.run", side_effect=fake_run),
    ):
        vsec.return_value = str(default_seccomp_profile_path())
        result = runner.run(req)

    assert result.status == "timed_out"
    assert result.timed_out is True
    assert result.tests_passed is False
    assert result.runner_error_category == "timeout"
    assert "container_termination=confirmed" in result.detail
    assert len(calls) == 2
    assert calls[0][1] == "run"
    assert "--name" in calls[0]
    assert expected_name in calls[0]
    assert calls[1] == [runner.docker_bin, "kill", expected_name]
    assert calls[1][2].startswith(CONTAINER_NAME_PREFIX)


def test_kill_subprocess_is_bounded() -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with patch("app.execution.docker_runner.subprocess.run", side_effect=fake_run):
        status = best_effort_kill_container(
            docker_bin="docker",
            container_name=container_name_for_execution(str(uuid.uuid4())),
        )
    assert status == "confirmed"
    assert seen["timeout"] == CONTAINER_KILL_TIMEOUT_SECONDS
    assert seen["argv"][1] == "kill"


def test_missing_identity_does_not_issue_kill() -> None:
    with patch("app.execution.docker_runner.subprocess.run") as run:
        assert (
            best_effort_kill_container(docker_bin="docker", container_name=None)
            == "skipped_no_identity"
        )
        assert (
            best_effort_kill_container(
                docker_bin="docker", container_name="other-prefix-id"
            )
            == "skipped_no_identity"
        )
        run.assert_not_called()


def test_kill_failure_preserves_timeout_result(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    req = _request(tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=2)
        return subprocess.CompletedProcess(argv, 1, b"", b"no such container")

    with (
        patch.object(runner, "_daemon_ok", return_value=True),
        patch.object(runner, "_image_present", return_value=True),
        patch("app.execution.docker_runner.validate_seccomp_profile") as vsec,
        patch("app.execution.docker_runner.subprocess.run", side_effect=fake_run),
    ):
        vsec.return_value = str(default_seccomp_profile_path())
        result = runner.run(req)

    assert result.status == "timed_out"
    assert result.runner_error_category == "timeout"
    assert result.tests_passed is False
    assert "container_termination=failed" in result.detail
    assert "container_termination=confirmed" not in result.detail


def test_normal_completion_unchanged(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    req = _request(tmp_path)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    with (
        patch.object(runner, "_daemon_ok", return_value=True),
        patch.object(runner, "_image_present", return_value=True),
        patch("app.execution.docker_runner.validate_seccomp_profile") as vsec,
        patch("app.execution.docker_runner.collect_output_artifacts", return_value=[]),
        patch("app.execution.docker_runner.subprocess.run", side_effect=fake_run),
    ):
        vsec.return_value = str(default_seccomp_profile_path())
        result = runner.run(req)

    assert result.status == "completed"
    assert result.tests_passed is True
    assert result.runner_error_category == "none"
    assert len(calls) == 1
    assert calls[0][1] == "run"
    assert "kill" not in calls[0]
