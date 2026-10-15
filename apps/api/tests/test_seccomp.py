"""Seccomp profile validation and fail-closed runner behavior."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from app.execution.docker_runner import LocalDockerExecutionRunner
from app.execution.limits import clamp_limits
from app.execution.models import ExecutionRequest, StagedArtifact
from app.execution.seccomp import (
    APPROVED_SECCOMP_SHA256,
    SeccompProfileError,
    default_seccomp_profile_path,
    validate_seccomp_profile,
)


MINIMAL_STRUCT = {
    "defaultAction": "SCMP_ACT_ERRNO",
    "defaultErrnoRet": 1,
    "archMap": [{"architecture": "SCMP_ARCH_AARCH64", "subArchitectures": ["SCMP_ARCH_ARM"]}],
    "syscalls": [{"names": ["read", "write"], "action": "SCMP_ACT_ALLOW"}],
}


def _req(staging: Path) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        staged_root=str(staging),
        artifacts=[StagedArtifact(relative_path="tests/test_artifact.py", size_bytes=1)],
        limits=clamp_limits(),
    )


def test_approved_profile_validates() -> None:
    path = validate_seccomp_profile(default_seccomp_profile_path())
    assert path.name == "default.json"
    assert path.is_file()
    assert not path.is_symlink()


def test_missing_profile_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "default.json"
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: missing,
    )
    with pytest.raises(SeccompProfileError, match="missing"):
        validate_seccomp_profile(missing)


def test_symlink_profile_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = default_seccomp_profile_path()
    link = tmp_path / "default.json"
    link.symlink_to(real)
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: link,
    )
    with pytest.raises(SeccompProfileError, match="symlink"):
        validate_seccomp_profile(link)


def test_malformed_json_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "default.json"
    raw = b"{not-json"
    profile.write_bytes(raw)
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: profile,
    )
    monkeypatch.setattr("app.execution.seccomp.APPROVED_SECCOMP_BYTES", len(raw))
    monkeypatch.setattr(
        "app.execution.seccomp.APPROVED_SECCOMP_SHA256",
        __import__("hashlib").sha256(raw).hexdigest(),
    )
    with pytest.raises(SeccompProfileError, match="malformed"):
        validate_seccomp_profile(profile)


def test_checksum_mismatch_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "default.json"
    shutil.copyfile(default_seccomp_profile_path(), profile)
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: profile,
    )
    monkeypatch.setattr(
        "app.execution.seccomp.APPROVED_SECCOMP_SHA256",
        "0" * 64,
    )
    with pytest.raises(SeccompProfileError, match="checksum"):
        validate_seccomp_profile(profile)


def test_structure_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "default.json"
    bad = {"defaultAction": "SCMP_ACT_ALLOW", "archMap": [], "syscalls": []}
    raw = json.dumps(bad).encode("utf-8")
    profile.write_bytes(raw)
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: profile,
    )
    monkeypatch.setattr("app.execution.seccomp.APPROVED_SECCOMP_BYTES", len(raw))
    monkeypatch.setattr(
        "app.execution.seccomp.APPROVED_SECCOMP_SHA256",
        __import__("hashlib").sha256(raw).hexdigest(),
    )
    with pytest.raises(SeccompProfileError):
        validate_seccomp_profile(profile)


def test_runner_fail_closed_when_profile_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "default.json"
    monkeypatch.setattr(
        "app.execution.docker_runner.default_seccomp_profile_path",
        lambda: missing,
    )
    monkeypatch.setattr(
        "app.execution.seccomp.default_seccomp_profile_path",
        lambda: missing,
    )
    runner = LocalDockerExecutionRunner(
        image="rajosa-exec-python:3.12-pytest",
        seccomp_profile=missing,
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    result = runner.run(_req(staging))
    assert result.tests_passed is False
    assert result.tests_executed is False
    assert result.runner_error_category == "seccomp_unavailable"
    assert "seccomp" in result.detail.lower()
    # Public detail must not leak absolute host paths.
    assert str(tmp_path) not in result.detail


def test_runner_argv_includes_absolute_seccomp_never_unconfined(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    staging = tmp_path / "staging"
    staging.mkdir()
    argv = runner.build_docker_argv(_req(staging))
    joined = " ".join(argv)
    assert "seccomp=unconfined" not in joined.lower()
    assert f"seccomp={default_seccomp_profile_path().resolve()}" in joined
    assert APPROVED_SECCOMP_SHA256  # pin still imported / documented for audits


def test_approved_checksum_constant() -> None:
    assert (
        APPROVED_SECCOMP_SHA256
        == "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74"
    )
