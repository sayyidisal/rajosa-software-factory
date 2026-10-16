"""Phase 3B isolated execution tests — use FakeExecutionRunner only.

Never execute model-generated or arbitrary user code in this suite.
Optional Docker live checks are marked and skipped when daemon/image missing.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.agents.base import WorkspaceWriter
from app.domain.models import AgentResult
from app.domain.store import MemoryTaskStore
from app.execution.concurrency import ExecutionBusyError, ExecutionGate
from app.execution.docker_runner import LocalDockerExecutionRunner
from app.execution.fake import FakeExecutionRunner
from app.execution.limits import (
    APPROVED_TEST_ARGV,
    DEFAULT_LIMITS,
    clamp_limits,
)
from app.execution.models import ExecutionRequest, ExecutionResult, StagedArtifact
from app.execution.staging import (
    StagingError,
    cleanup_staging,
    create_execution_staging_root,
    stage_python_test_artifact,
)
from app.main import create_app
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace

VALID_TESTS = """
def test_one():
    assert True

def test_two():
    assert True

def test_three():
    assert True
"""


class FixedOutputAdapter:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
        role: str = "implementer",
        validation_profile: str = "generic_text",
    ) -> AgentResult:
        import json

        from app.agents.mock import build_mock_plan

        self.calls += 1
        if role == "planner":
            content = json.dumps(build_mock_plan(validation_profile=validation_profile))
            workspace.write_text(task_id, "plan.json", content)
            return AgentResult(output=content, artifact_name="plan.json")
        workspace.write_text(task_id, "result.txt", self.content)
        return AgentResult(output=self.content)


def _app(
    workspace_root: Path,
    *,
    runner: FakeExecutionRunner | None = None,
    adapter=None,
    gate_lock: ExecutionGate | None = None,
) -> TestClient:
    app = create_app(
        adapter=adapter or FixedOutputAdapter(VALID_TESTS),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
        execution_runner=runner if runner is not None else FakeExecutionRunner(),
        execution_gate=gate_lock,
    )
    return TestClient(app)


def _create_python_exec_task(client: TestClient) -> dict:
    from tests.orchestration_helpers import approve_plan

    response = client.post(
        "/tasks",
        json={
            "title": "Py tests",
            "instruction": "three tests",
            "validation_profile": "python_test_execution",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"] == "awaiting_plan_approval"
    approved = approve_plan(client, body["id"])
    assert approved["lifecycle"] == "awaiting_execution_auth"
    return approved


# --- static profile compatibility ---


def test_python_unit_tests_still_static_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    task_id = str(uuid.uuid4())
    path = LocalWorkspace(root).prepare(task_id)
    (path / "result.txt").write_text(VALID_TESTS, encoding="utf-8")
    report = BasicQualityGate().evaluate(
        AgentResult(output=VALID_TESTS), path, profile="python_unit_tests"
    )
    assert report.passed is True
    assert not any(c.name == "tests_passed" for c in report.checks)


def test_python_test_execution_awaits_execution_auth(
    workspace_root: Path,
) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    body = _create_python_exec_task(client)
    assert body["status"] == "pending"
    assert body["lifecycle"] == "awaiting_execution_auth"
    assert body["quality"]["passed"] is True
    assert body["execution"]["status"] == "not_requested"
    assert body["execution"]["tests_passed"] is False
    assert any(c["name"] == "execution_not_requested" for c in body["quality"]["checks"])
    assert runner.calls == []


def test_create_task_never_starts_runner(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    response = client.post(
        "/tasks",
        json={"title": "T", "instruction": "I", "validation_profile": "generic_text"},
    )
    assert response.status_code == 200
    assert runner.calls == []
    assert response.json()["execution"]["status"] == "not_requested"
    assert response.json()["lifecycle"] == "awaiting_plan_approval"


def test_profile_alone_does_not_authorize_execution(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    _create_python_exec_task(client)
    assert runner.calls == []


def test_explicit_execute_invokes_runner_with_structured_request(
    workspace_root: Path,
) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    response = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    )
    assert response.status_code == 200
    assert len(runner.calls) == 1
    req = runner.calls[0]
    assert isinstance(req, ExecutionRequest)
    assert req.runtime_id == "local_docker_python"
    assert req.test_profile == "python_test_execution"
    assert req.task_id == task["id"]
    assert req.artifacts
    body = response.json()
    assert body["execution"]["tests_passed"] is True
    assert body["execution"]["tests_executed"] is True
    assert body["lifecycle"] == "completed"
    assert body["status"] == "passed"


def test_execute_without_confirm_rejected(workspace_root: Path) -> None:
    client = _app(workspace_root)
    task = _create_python_exec_task(client)
    response = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": False},
    )
    assert response.status_code == 422


def test_execute_wrong_profile_rejected(workspace_root: Path) -> None:
    from tests.orchestration_helpers import approve_plan

    runner = FakeExecutionRunner()
    client = _app(
        workspace_root,
        runner=runner,
        adapter=FixedOutputAdapter("hello world text"),
    )
    created = client.post(
        "/tasks",
        json={
            "title": "T",
            "instruction": "I",
            "validation_profile": "generic_text",
        },
    ).json()
    approved = approve_plan(client, created["id"])
    response = client.post(
        f"/tasks/{approved['id']}/execute-tests",
        json={"confirm": True},
    )
    body = response.json()
    assert body["execution"]["status"] == "rejected"
    assert body["execution"]["runner_error_category"] == "unsupported_profile"
    assert body["execution"]["tests_passed"] is False
    assert runner.calls == []


def test_execute_before_plan_approval_rejected(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    created = client.post(
        "/tasks",
        json={
            "title": "Py tests",
            "instruction": "three tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    assert created["lifecycle"] == "awaiting_plan_approval"
    response = client.post(
        f"/tasks/{created['id']}/execute-tests",
        json={"confirm": True},
    )
    assert response.status_code == 422
    assert runner.calls == []


def test_runner_unavailable_fails_safely(workspace_root: Path) -> None:
    runner = FakeExecutionRunner(available=False)
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    body = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["status"] == "runner_unavailable"
    assert body["execution"]["tests_passed"] is False
    assert body["execution"]["tests_executed"] is False
    assert body["lifecycle"] == "failed"
    assert body["status"] == "failed"


def test_nonzero_exit_is_test_failure(workspace_root: Path) -> None:
    runner = FakeExecutionRunner(
        next_result=ExecutionResult(
            execution_id="x",
            status="tests_failed",
            exit_code=1,
            tests_executed=True,
            tests_passed=False,
            detail="failed",
        )
    )
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    body = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["tests_passed"] is False
    assert body["execution"]["tests_executed"] is True
    assert body["execution"]["exit_code"] == 1
    assert body["lifecycle"] == "failed"


def test_timeout_never_passes(workspace_root: Path) -> None:
    runner = FakeExecutionRunner(
        next_result=ExecutionResult(
            execution_id="x",
            status="timed_out",
            timed_out=True,
            tests_executed=False,
            tests_passed=False,
            runner_error_category="timeout",
        )
    )
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    body = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["timed_out"] is True
    assert body["execution"]["tests_passed"] is False
    assert body["lifecycle"] == "failed"


def test_malformed_tests_passed_fail_closed() -> None:
    forged = ExecutionResult(
        execution_id="e",
        status="runner_unavailable",
        tests_executed=False,
        tests_passed=True,
        runner_error_category="daemon_unavailable",
    ).validated()
    assert forged.tests_passed is False
    assert forged.status == "infrastructure_error"


def test_zero_exit_without_executed_fail_closed() -> None:
    forged = ExecutionResult(
        execution_id="e",
        status="completed",
        exit_code=0,
        tests_executed=False,
        tests_passed=True,
    ).validated()
    assert forged.tests_passed is False


def test_missing_expected_results_fail_closed(workspace_root: Path) -> None:
    runner = FakeExecutionRunner(
        next_result=ExecutionResult(
            execution_id="x",
            status="completed",
            exit_code=0,
            tests_executed=False,
            tests_passed=True,
        )
    )
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    body = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["tests_passed"] is False


def test_path_traversal_rejected_in_staging(tmp_path: Path) -> None:
    base = tmp_path / "stagebase"
    base.mkdir()
    eid = str(uuid.uuid4())
    root = create_execution_staging_root(base, eid)
    evil = tmp_path / "outside.py"
    evil.write_text("x", encoding="utf-8")
    # Attempt to stage via a name that would escape — stage_python_test_artifact
    # uses fixed relative path; verify absolute/traversal helpers.
    with pytest.raises(StagingError):
        from app.execution.staging import _safe_relative

        _safe_relative("../etc/passwd")


def test_symlink_source_rejected(tmp_path: Path) -> None:
    base = tmp_path / "stagebase"
    base.mkdir()
    root = create_execution_staging_root(base, str(uuid.uuid4()))
    real = tmp_path / "real.py"
    real.write_text(VALID_TESTS, encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(real)
    with pytest.raises(StagingError, match="symlink"):
        stage_python_test_artifact(source_file=link, staging_root=root)


def test_oversized_artifact_rejected(tmp_path: Path) -> None:
    base = tmp_path / "stagebase"
    base.mkdir()
    root = create_execution_staging_root(base, str(uuid.uuid4()))
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (DEFAULT_LIMITS.max_file_bytes + 1))
    with pytest.raises(StagingError, match="size"):
        stage_python_test_artifact(source_file=big, staging_root=root)


def test_forbidden_env_filename_rejected(tmp_path: Path) -> None:
    base = tmp_path / "stagebase"
    base.mkdir()
    root = create_execution_staging_root(base, str(uuid.uuid4()))
    envf = tmp_path / ".env"
    envf.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(StagingError):
        stage_python_test_artifact(source_file=envf, staging_root=root)


def test_docker_argv_enforces_no_network_and_no_host_env(tmp_path: Path) -> None:
    runner = LocalDockerExecutionRunner(image="rajosa-exec-python:3.12-pytest")
    staging = tmp_path / "staging"
    staging.mkdir()
    req = ExecutionRequest(
        execution_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        staged_root=str(staging),
        artifacts=[
            StagedArtifact(relative_path="tests/test_artifact.py", size_bytes=10)
        ],
        limits=clamp_limits(),
    )
    argv = runner.build_docker_argv(req)
    assert "--network" in argv
    assert "none" in argv
    assert "--read-only" in argv
    assert "no-new-privileges:true" in argv
    assert "--cap-drop" in argv
    assert "ALL" in argv
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert "SUMOPOD_API_KEY" not in joined
    assert "SECRET" not in joined
    assert "seccomp=unconfined" not in joined.lower()
    seccomp_opts = [
        argv[i + 1]
        for i, part in enumerate(argv)
        if part == "--security-opt" and i + 1 < len(argv)
    ]
    assert any(opt.startswith("seccomp=") and opt.endswith("default.json") for opt in seccomp_opts)
    assert all("unconfined" not in opt.lower() for opt in seccomp_opts)
    # Command is the approved argv only
    assert argv[-len(APPROVED_TEST_ARGV) :] == list(APPROVED_TEST_ARGV)


def test_secrets_not_passed_to_subprocess_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUMOPOD_API_KEY", "sk-secret-DO-NOT-LEAK")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    runner = LocalDockerExecutionRunner(image="missing-image:never")
    staging = tmp_path / "staging"
    staging.mkdir()
    req = ExecutionRequest(
        execution_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        staged_root=str(staging),
        artifacts=[],
        limits=clamp_limits(),
    )
    # Daemon/image unavailable path — still must not leak secrets if run invoked.
    result = runner.run(req)
    assert result.tests_passed is False
    assert "sk-secret" not in result.detail
    assert "aws-secret" not in result.detail


def test_cleanup_after_success(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    client.post(f"/tasks/{task['id']}/execute-tests", json={"confirm": True})
    exec_dirs = list((workspace_root / "_executions").glob("*")) if (
        workspace_root / "_executions"
    ).exists() else []
    # Staging directories should be removed after success.
    assert exec_dirs == []


def test_cleanup_failure_never_success(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _app(workspace_root, runner=runner)
    task = _create_python_exec_task(client)
    with patch(
        "app.execution.service.cleanup_staging",
        side_effect=OSError("cleanup boom"),
    ):
        body = client.post(
            f"/tasks/{task['id']}/execute-tests",
            json={"confirm": True},
        ).json()
    assert body["execution"]["tests_passed"] is False
    assert body["execution"]["status"] == "cleanup_error"


def test_concurrent_busy_does_not_fail_lifecycle_on_success(
    workspace_root: Path,
) -> None:
    """H1: busy rejection must not finalize lifecycle; owner A may still complete."""
    gate = ExecutionGate()
    a_entered = threading.Event()
    release_a = threading.Event()
    run_calls = 0
    run_lock = threading.Lock()

    class BlockingFake(FakeExecutionRunner):
        def run(self, request: ExecutionRequest) -> ExecutionResult:
            nonlocal run_calls
            with run_lock:
                run_calls += 1
                assert run_calls == 1, "only the gate owner may invoke the runner"
            a_entered.set()
            assert release_a.wait(timeout=5)
            return super().run(request)

    runner = BlockingFake()
    client = _app(workspace_root, runner=runner, gate_lock=gate)
    task = _create_python_exec_task(client)
    task_id = task["id"]

    a_response: list[object] = []

    def run_a() -> None:
        a_response.append(
            client.post(
                f"/tasks/{task_id}/execute-tests",
                json={"confirm": True},
            )
        )

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert a_entered.wait(timeout=5)

    mid = client.get(f"/tasks/{task_id}").json()
    assert mid["lifecycle"] == "awaiting_execution_auth"
    assert mid["execution"]["status"] == "not_requested"

    busy = client.post(
        f"/tasks/{task_id}/execute-tests",
        json={"confirm": True},
    )
    assert busy.status_code == 409
    assert "already active" in str(busy.json().get("detail", "")).lower()

    after_busy = client.get(f"/tasks/{task_id}").json()
    assert after_busy["lifecycle"] == "awaiting_execution_auth"
    assert after_busy["status"] == "pending"
    assert after_busy["execution"]["status"] == "not_requested"
    assert after_busy["execution"]["tests_passed"] is False
    assert after_busy["execution"]["runner_error_category"] in (None, "none")

    release_a.set()
    thread_a.join(timeout=5)
    assert thread_a.is_alive() is False

    assert len(a_response) == 1
    response_a = a_response[0]
    assert getattr(response_a, "status_code") == 200
    body_a = response_a.json()  # type: ignore[union-attr]
    assert body_a["lifecycle"] == "completed"
    assert body_a["status"] == "passed"
    assert body_a["execution"]["tests_passed"] is True
    assert body_a["execution"]["tests_executed"] is True
    assert body_a["execution"]["runner_error_category"] == "none"
    assert run_calls == 1
    assert len(runner.calls) == 1


def test_concurrent_busy_preserves_owner_failure(
    workspace_root: Path,
) -> None:
    """H1: when owner A fails for real, lifecycle reflects A's failure — not B's busy."""
    gate = ExecutionGate()
    a_entered = threading.Event()
    release_a = threading.Event()
    run_calls = 0
    run_lock = threading.Lock()

    class BlockingFailFake(FakeExecutionRunner):
        def run(self, request: ExecutionRequest) -> ExecutionResult:
            nonlocal run_calls
            self.calls.append(request)
            with run_lock:
                run_calls += 1
                assert run_calls == 1, "only the gate owner may invoke the runner"
            a_entered.set()
            assert release_a.wait(timeout=5)
            return ExecutionResult(
                execution_id=request.execution_id,
                status="tests_failed",
                exit_code=1,
                tests_executed=True,
                tests_passed=False,
                detail="owner failure",
            ).validated()

    runner = BlockingFailFake()
    client = _app(workspace_root, runner=runner, gate_lock=gate)
    task = _create_python_exec_task(client)
    task_id = task["id"]

    a_response: list[object] = []

    def run_a() -> None:
        a_response.append(
            client.post(
                f"/tasks/{task_id}/execute-tests",
                json={"confirm": True},
            )
        )

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert a_entered.wait(timeout=5)

    busy = client.post(
        f"/tasks/{task_id}/execute-tests",
        json={"confirm": True},
    )
    assert busy.status_code == 409

    after_busy = client.get(f"/tasks/{task_id}").json()
    assert after_busy["lifecycle"] == "awaiting_execution_auth"
    assert after_busy["execution"]["status"] == "not_requested"

    release_a.set()
    thread_a.join(timeout=5)
    assert thread_a.is_alive() is False

    assert len(a_response) == 1
    response_a = a_response[0]
    assert getattr(response_a, "status_code") == 200
    body_a = response_a.json()  # type: ignore[union-attr]
    assert body_a["lifecycle"] == "failed"
    assert body_a["status"] == "failed"
    assert body_a["execution"]["tests_passed"] is False
    assert body_a["execution"]["tests_executed"] is True
    assert body_a["execution"]["detail"] == "owner failure"
    assert body_a["error"] == "owner failure"
    assert run_calls == 1
    assert len(runner.calls) == 1


def test_clamp_limits_hard_max() -> None:
    limited = clamp_limits(wall_clock_seconds=9999, memory_mib=99999)
    assert limited.wall_clock_seconds == 60
    assert limited.memory_mib == 1024


def test_default_execution_runner_is_disabled(workspace_root: Path, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("EXECUTION_RUNNER", raising=False)
    from app.execution.disabled import DisabledExecutionRunner
    from app.execution.factory import build_execution_runner
    from app.config import get_settings

    settings = get_settings()
    assert settings.execution_runner == "off"
    assert isinstance(build_execution_runner(settings), DisabledExecutionRunner)


def test_disabled_runner_on_explicit_request(workspace_root: Path) -> None:
    from app.execution.disabled import DisabledExecutionRunner

    client = _app(workspace_root, runner=DisabledExecutionRunner())  # type: ignore[arg-type]
    task = _create_python_exec_task(client)
    body = client.post(
        f"/tasks/{task['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["status"] == "runner_unavailable"
    assert body["execution"]["runner_error_category"] == "feature_disabled"
    assert body["execution"]["tests_passed"] is False
    assert body["lifecycle"] == "failed"


def test_existing_generic_flow_compatible(workspace_root: Path) -> None:
    from app.agents.mock import MockAgentAdapter
    from tests.orchestration_helpers import approve_plan

    client = _app(workspace_root, adapter=MockAgentAdapter())
    created = client.post(
        "/tasks",
        json={"title": "Hello", "instruction": "Say hi"},
    ).json()
    assert created["lifecycle"] == "awaiting_plan_approval"
    assert created["execution"]["status"] == "not_requested"
    body = approve_plan(client, created["id"])
    assert body["status"] == "passed"
    assert body["lifecycle"] == "completed"
    assert body["execution"]["status"] == "not_requested"


@pytest.mark.skipif(
    True,
    reason=(
        "Optional live Docker integration disabled in default suite: "
        "daemon was unavailable at Phase 3B inspection; enable manually when "
        "EXECUTION_RUNNER=local_container, daemon up, and pinned image loaded."
    ),
)
def test_optional_live_docker_benign_fixture() -> None:
    assert False, "not run in default suite"


def test_cleanup_staging_helper(tmp_path: Path) -> None:
    base = tmp_path / "b"
    base.mkdir()
    root = create_execution_staging_root(base, str(uuid.uuid4()))
    assert root.exists()
    cleanup_staging(root)
    assert not root.exists()


def test_execution_busy_error_type() -> None:
    gate = ExecutionGate()
    with gate.acquire("a"):
        with pytest.raises(ExecutionBusyError):
            with gate.acquire("b"):
                pass
