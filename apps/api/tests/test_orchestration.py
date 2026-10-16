"""Phase 4B/4C orchestration: Plan v2, approval digests, lifecycle, boundaries."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.mock import build_mock_plan
from app.domain.store import MemoryTaskStore
from app.execution.fake import FakeExecutionRunner
from app.main import create_app
from app.orchestration.lifecycle import (
    LEGAL_TRANSITIONS,
    assert_transition,
    InvalidLifecycleTransition,
    legacy_status_for,
)
from app.quality.profiles import (
    RUNTIME_REQUIRED_PROFILES,
    SUPPORTED_PROFILES,
    is_supported_profile,
    profile_requires_runtime,
)
from app.orchestration.plan import (
    MAX_PLAN_STEPS,
    MAX_SERIALIZED_PLAN_BYTES,
    Plan,
    PlanValidationError,
    compute_approval_digest,
    parse_plan_text,
)
from app.quality.basic import BasicQualityGate
from app.workspaces.local import LocalWorkspace
from tests.orchestration_helpers import (
    DEFAULT_PLAN,
    FixedOutputAdapter,
    approve_plan,
    make_plan_v2,
)

VALID_TESTS = """
def test_one():
    assert True

def test_two():
    assert True

def test_three():
    assert True
"""


def _client(workspace_root: Path, *, adapter=None, runner=None) -> TestClient:
    app = create_app(
        adapter=adapter or FixedOutputAdapter("hello from implementer"),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=MemoryTaskStore(),
        execution_runner=runner if runner is not None else FakeExecutionRunner(),
    )
    return TestClient(app)


def _parse(raw: str, profile: str = "generic_text") -> Plan:
    return parse_plan_text(raw, validation_profile=profile)


# --- plan validation (Phase 4C) ---


def test_valid_minimal_plan_v2_accepted() -> None:
    plan = _parse(json.dumps(DEFAULT_PLAN))
    assert plan.schema_version == "2"
    assert len(plan.steps) == 1
    assert len(plan.acceptance_checks) >= 1


def test_valid_full_plan_accepted() -> None:
    payload = make_plan_v2(requires_execution=False)
    payload["goals"] = [f"goal-{i}" for i in range(8)]
    payload["steps"] = [
        {
            "step_id": f"s{i}",
            "description": f"step {i}",
            "expected_artifacts": ["result.txt"] if i == 0 else [],
            "acceptance_check_ids": ["chk_artifact", "chk_profile"],
        }
        for i in range(MAX_PLAN_STEPS)
    ]
    plan = _parse(json.dumps(payload))
    assert len(plan.steps) == MAX_PLAN_STEPS
    assert plan.steps[1].expected_artifacts == []


def test_malformed_plan_rejected() -> None:
    with pytest.raises(PlanValidationError) as exc:
        _parse("not json")
    assert exc.value.category == "malformed_json"


def test_prose_wrapped_json_rejected() -> None:
    raw = "Here is the plan:\n" + json.dumps(DEFAULT_PLAN)
    with pytest.raises(PlanValidationError) as exc:
        _parse(raw)
    assert exc.value.category == "malformed_json"


def test_markdown_fence_rejected() -> None:
    raw = "```json\n" + json.dumps(DEFAULT_PLAN) + "\n```"
    with pytest.raises(PlanValidationError) as exc:
        _parse(raw)
    assert exc.value.category == "markdown_fence_forbidden"


def test_duplicate_json_keys_rejected() -> None:
    raw = (
        '{"schema_version":"2","summary":"s","summary":"t","goals":["g"],'
        '"steps":[{"step_id":"a","description":"d","expected_artifacts":[],'
        '"acceptance_check_ids":["c1"]}],'
        '"acceptance_checks":[{"check_id":"c1","description":"d","kind":"human_review"}],'
        '"requires_execution":false}'
    )
    with pytest.raises(PlanValidationError) as exc:
        _parse(raw)
    assert exc.value.category == "duplicate_key"


def test_nonfinite_number_rejected() -> None:
    raw = (
        '{"schema_version":"2","summary":"s","goals":["g"],'
        '"steps":[{"step_id":"a","description":"d","expected_artifacts":[],'
        '"acceptance_check_ids":["c1"]}],'
        '"acceptance_checks":[{"check_id":"c1","description":"d","kind":"human_review"}],'
        '"requires_execution":false,"x":NaN}'
    )
    with pytest.raises(PlanValidationError) as exc:
        _parse(raw)
    assert exc.value.category == "nonfinite_number"


def test_missing_required_fields_rejected() -> None:
    payload = {"schema_version": "2", "summary": "x"}
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category == "schema_invalid"


def test_unknown_field_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["extra_tool"] = "curl"
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category in {"schema_invalid", "forbidden_field"}


def test_duplicate_step_ids_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["steps"] = [
        {
            "step_id": "same",
            "description": "one",
            "expected_artifacts": [],
            "acceptance_check_ids": ["chk_artifact", "chk_profile"],
        },
        {
            "step_id": "same",
            "description": "two",
            "expected_artifacts": [],
            "acceptance_check_ids": ["chk_artifact", "chk_profile"],
        },
    ]
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category in {"duplicate_id", "schema_invalid"}


def test_invalid_acceptance_check_reference_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["steps"] = [
        {
            "step_id": "implement",
            "description": "x",
            "expected_artifacts": ["result.txt"],
            "acceptance_check_ids": ["missing_check"],
        }
    ]
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category in {"invalid_reference", "schema_invalid"}


def test_excessive_step_count_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["steps"] = [
        {
            "step_id": f"s{i}",
            "description": f"step {i}",
            "expected_artifacts": [],
            "acceptance_check_ids": ["chk_artifact", "chk_profile"],
        }
        for i in range(MAX_PLAN_STEPS + 1)
    ]
    with pytest.raises(PlanValidationError):
        _parse(json.dumps(payload))


def test_oversized_plan_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["summary"] = "x" * (MAX_SERIALIZED_PLAN_BYTES)
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category in {"plan_too_large", "schema_invalid"}


def test_schema_version_string_2_accepted() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["schema_version"] = "2"
    plan = _parse(json.dumps(payload))
    assert plan.schema_version == "2"


def test_unsupported_schema_version_rejected() -> None:
    for version in ("1", "99"):
        payload = dict(DEFAULT_PLAN)
        payload["schema_version"] = version
        with pytest.raises(PlanValidationError) as exc:
            _parse(json.dumps(payload))
        assert exc.value.category == "unsupported_schema_version"


def test_schema_version_numeric_2_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    # Craft raw JSON with a numeric version — do not let json.dumps coerce.
    raw = json.dumps(payload).replace('"schema_version": "2"', '"schema_version": 2')
    assert '"schema_version": 2' in raw or '"schema_version":2' in raw.replace(" ", "")
    with pytest.raises(PlanValidationError) as exc:
        _parse(raw)
    assert exc.value.category == "unsupported_schema_version"


def test_schema_version_null_and_boolean_rejected() -> None:
    for raw_version in ("null", "true", "false"):
        payload = dict(DEFAULT_PLAN)
        raw = json.dumps(payload).replace(
            '"schema_version": "2"', f'"schema_version": {raw_version}'
        )
        with pytest.raises(PlanValidationError) as exc:
            _parse(raw)
        assert exc.value.category in {
            "unsupported_schema_version",
            "missing_schema_version",
        }, raw_version


def test_forbidden_execution_fields_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["command"] = "rm -rf /"
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category == "forbidden_field"


def test_nested_shell_field_rejected() -> None:
    payload = dict(DEFAULT_PLAN)
    payload["steps"] = [
        {
            "step_id": "s1",
            "description": "x",
            "expected_artifacts": [],
            "acceptance_check_ids": ["chk_artifact", "chk_profile"],
            "shell": "echo hi",
        }
    ]
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload))
    assert exc.value.category in {"forbidden_field", "schema_invalid"}


def test_technical_description_not_rejected_by_denylist() -> None:
    """Legitimate mentions of docker/shell/env in prose must remain valid."""
    payload = dict(DEFAULT_PLAN)
    payload["summary"] = "Document how docker compose and shell scripts are used offline."
    payload["steps"][0]["description"] = (
        "Write notes about environment variables and docker images without executing them."
    )
    plan = _parse(json.dumps(payload))
    assert "docker" in plan.summary.lower()


def test_runtime_profile_requires_runtime_tests_check() -> None:
    payload = make_plan_v2(requires_execution=False)
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload), profile="python_test_execution")
    assert exc.value.category == "profile_incompatible"


def test_static_profile_rejects_runtime_tests_check() -> None:
    payload = make_plan_v2(requires_execution=True)
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(payload), profile="generic_text")
    assert exc.value.category == "profile_incompatible"


def test_server_overrides_requires_execution_for_runtime_profile() -> None:
    payload = make_plan_v2(requires_execution=True)
    payload["requires_execution"] = False
    plan = _parse(json.dumps(payload), profile="python_test_execution")
    assert plan.requires_execution is True


def test_approval_digest_changes_with_plan_or_profile() -> None:
    plan_a = _parse(json.dumps(DEFAULT_PLAN), profile="generic_text")
    plan_b_payload = dict(DEFAULT_PLAN)
    plan_b_payload["summary"] = "Different summary for digest"
    plan_b = _parse(json.dumps(plan_b_payload), profile="generic_text")
    digest_a = compute_approval_digest(plan=plan_a, validation_profile="generic_text")
    digest_b = compute_approval_digest(plan=plan_b, validation_profile="generic_text")
    digest_c = compute_approval_digest(plan=plan_a, validation_profile="python_code")
    assert digest_a != digest_b
    assert digest_a != digest_c


# --- lifecycle table ---


def test_legal_transitions_cover_happy_paths() -> None:
    assert "planning" in LEGAL_TRANSITIONS["created"]
    assert "awaiting_plan_approval" in LEGAL_TRANSITIONS["planning"]
    assert "generating" in LEGAL_TRANSITIONS["awaiting_plan_approval"]
    assert "validating" in LEGAL_TRANSITIONS["generating"]
    assert "completed" in LEGAL_TRANSITIONS["validating"]
    assert "awaiting_execution_auth" in LEGAL_TRANSITIONS["validating"]
    assert "completed" in LEGAL_TRANSITIONS["awaiting_execution_auth"]


def test_forbidden_transitions_raise() -> None:
    with pytest.raises(InvalidLifecycleTransition):
        assert_transition("created", "completed")
    with pytest.raises(InvalidLifecycleTransition):
        assert_transition("awaiting_plan_approval", "completed")
    with pytest.raises(InvalidLifecycleTransition):
        assert_transition("completed", "generating")
    with pytest.raises(InvalidLifecycleTransition):
        assert_transition("failed", "planning")


def test_legacy_status_mapping() -> None:
    assert legacy_status_for("awaiting_plan_approval") == "pending"
    assert legacy_status_for("awaiting_execution_auth") == "pending"
    assert legacy_status_for("completed") == "passed"
    assert legacy_status_for("failed") == "failed"


def test_runtime_profile_detection() -> None:
    assert profile_requires_runtime("python_test_execution") is True
    assert profile_requires_runtime("generic_text") is False
    assert profile_requires_runtime("python_unit_tests") is False


def test_runtime_required_profiles_single_source_of_truth() -> None:
    from app.orchestration import lifecycle as lifecycle_mod

    assert RUNTIME_REQUIRED_PROFILES <= set(SUPPORTED_PROFILES)
    assert lifecycle_mod.RUNTIME_REQUIRED_PROFILES is RUNTIME_REQUIRED_PROFILES
    assert lifecycle_mod.profile_requires_runtime is profile_requires_runtime
    for name in SUPPORTED_PROFILES:
        assert is_supported_profile(name) is True
        expected = name in RUNTIME_REQUIRED_PROFILES
        assert profile_requires_runtime(name) is expected
    assert is_supported_profile("invented_profile") is False


def test_unsupported_validation_profile_rejected_by_plan_parser() -> None:
    with pytest.raises(PlanValidationError) as exc:
        _parse(json.dumps(DEFAULT_PLAN), profile="invented_profile")
    assert exc.value.category == "unsupported_profile"


# --- approval API ---


def test_create_enters_awaiting_plan_approval(workspace_root: Path) -> None:
    client = _client(workspace_root)
    body = client.post(
        "/tasks",
        json={"title": "T", "instruction": "I"},
    ).json()
    assert body["lifecycle"] == "awaiting_plan_approval"
    assert body["status"] == "pending"
    assert body["plan"]["schema_version"] == "2"
    assert body["plan_approved"] is False
    assert not (workspace_root / body["id"] / "result.txt").exists()


def test_implementation_does_not_run_before_approval(workspace_root: Path) -> None:
    adapter = FixedOutputAdapter("secret-impl")
    client = _client(workspace_root, adapter=adapter)
    body = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    assert adapter.roles == ["planner"]
    assert "secret-impl" not in body["output"]


def test_missing_approval_field_rejected(workspace_root: Path) -> None:
    client = _client(workspace_root)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    response = client.post(f"/tasks/{created['id']}/approve-plan", json={})
    assert response.status_code == 422


def test_false_approval_rejects_plan(workspace_root: Path) -> None:
    adapter = FixedOutputAdapter("should-not-run")
    client = _client(workspace_root, adapter=adapter)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    body = approve_plan(client, created["id"], approved=False)
    assert body["lifecycle"] == "failed"
    assert body["status"] == "failed"
    assert "rejected" in (body["error"] or "").lower()
    assert adapter.roles == ["planner"]
    assert not (workspace_root / created["id"] / "result.txt").exists()


def test_valid_approval_starts_implementation(workspace_root: Path) -> None:
    adapter = FixedOutputAdapter("implemented artifact")
    client = _client(workspace_root, adapter=adapter)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    body = approve_plan(client, created["id"])
    assert body["lifecycle"] == "completed"
    assert body["status"] == "passed"
    assert body["plan_approved"] is True
    assert adapter.roles == ["planner", "implementer"]
    assert "implemented artifact" in body["output"]


def test_duplicate_approval_rejected(workspace_root: Path) -> None:
    client = _client(workspace_root)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    approve_plan(client, created["id"])
    response = client.post(
        f"/tasks/{created['id']}/approve-plan",
        json={"approved": True},
    )
    assert response.status_code == 409


def test_approval_wrong_lifecycle_rejected(workspace_root: Path) -> None:
    class BoomPlanner(FixedOutputAdapter):
        def run(self, *, task_id, title, instruction, workspace, role="implementer", validation_profile="generic_text"):
            if role == "planner":
                raise RuntimeError("planner boom")
            return super().run(
                task_id=task_id,
                title=title,
                instruction=instruction,
                workspace=workspace,
                role=role,
                validation_profile=validation_profile,
            )

    client = _client(workspace_root, adapter=BoomPlanner("x"))
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    assert created["lifecycle"] == "failed"
    response = client.post(
        f"/tasks/{created['id']}/approve-plan",
        json={"approved": True},
    )
    assert response.status_code == 409


def test_approve_unknown_task_404(workspace_root: Path) -> None:
    client = _client(workspace_root)
    response = client.post(
        "/tasks/00000000-0000-4000-8000-000000000000/approve-plan",
        json={"approved": True},
    )
    assert response.status_code == 404


def test_invalid_plan_output_fails_safely(workspace_root: Path) -> None:
    class BadPlanAdapter:
        def run(self, *, task_id, title, instruction, workspace, role="implementer", validation_profile="generic_text"):
            if role == "planner":
                workspace.write_text(task_id, "plan.raw.txt", "nonsense")
                from app.domain.models import AgentResult

                return AgentResult(output="nonsense", artifact_name="plan.raw.txt")
            raise AssertionError("implementer must not run")

    client = _client(workspace_root, adapter=BadPlanAdapter())
    body = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    assert body["lifecycle"] == "failed"
    assert body["status"] == "failed"
    assert "Plan validation failed" in (body["error"] or "")


def test_plan_approval_does_not_invoke_execution_runner(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _client(
        workspace_root,
        adapter=FixedOutputAdapter(VALID_TESTS),
        runner=runner,
    )
    created = client.post(
        "/tasks",
        json={
            "title": "Py",
            "instruction": "tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    body = approve_plan(client, created["id"])
    assert body["lifecycle"] == "awaiting_execution_auth"
    assert runner.calls == []
    assert body["execution"]["status"] == "not_requested"


def test_runtime_profile_cannot_complete_without_execution(workspace_root: Path) -> None:
    client = _client(workspace_root, adapter=FixedOutputAdapter(VALID_TESTS))
    created = client.post(
        "/tasks",
        json={
            "title": "Py",
            "instruction": "tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    body = approve_plan(client, created["id"])
    assert body["lifecycle"] == "awaiting_execution_auth"
    assert body["status"] == "pending"
    assert body["quality"]["passed"] is True


def test_successful_execution_completes_runtime_profile(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _client(
        workspace_root,
        adapter=FixedOutputAdapter(VALID_TESTS),
        runner=runner,
    )
    created = client.post(
        "/tasks",
        json={
            "title": "Py",
            "instruction": "tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    approved = approve_plan(client, created["id"])
    body = client.post(
        f"/tasks/{approved['id']}/execute-tests",
        json={"confirm": True},
    ).json()
    assert body["execution"]["tests_passed"] is True
    assert body["lifecycle"] == "completed"
    assert body["status"] == "passed"


def test_concurrent_approval_only_one_implementation(workspace_root: Path) -> None:
    adapter = FixedOutputAdapter("once")
    client = _client(workspace_root, adapter=adapter)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    task_id = created["id"]
    results: list[int] = []

    def _approve() -> None:
        response = client.post(
            f"/tasks/{task_id}/approve-plan",
            json={"approved": True},
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=_approve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(200) == 1
    assert results.count(409) == 7
    assert adapter.roles.count("implementer") == 1


def test_compatibility_fields_present(workspace_root: Path) -> None:
    client = _client(workspace_root)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    assert "status" in created
    assert "execution" in created
    assert "lifecycle" in created
    body = approve_plan(client, created["id"])
    assert body["status"] == "passed"
    assert body["lifecycle"] == "completed"
    assert body["execution"]["status"] == "not_requested"


def test_no_automatic_retry_on_quality_failure(workspace_root: Path) -> None:
    adapter = FixedOutputAdapter("")  # empty → quality fail
    client = _client(workspace_root, adapter=adapter)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    body = approve_plan(client, created["id"])
    assert body["lifecycle"] == "failed"
    assert adapter.roles.count("implementer") == 1


def test_approved_plan_is_passed_to_implementer(workspace_root: Path) -> None:
    class CaptureAdapter(FixedOutputAdapter):
        def __init__(self) -> None:
            super().__init__("implemented with plan")
            self.implementer_instruction = ""

        def run(self, *, task_id, title, instruction, workspace, role="implementer", validation_profile="generic_text"):
            if role == "implementer":
                self.implementer_instruction = instruction
            return super().run(
                task_id=task_id,
                title=title,
                instruction=instruction,
                workspace=workspace,
                role=role,
                validation_profile=validation_profile,
            )

    adapter = CaptureAdapter()
    client = _client(workspace_root, adapter=adapter)
    created = client.post(
        "/tasks",
        json={"title": "T", "instruction": "Do the work carefully"},
    ).json()
    plan_summary = created["plan"]["summary"]
    body = approve_plan(client, created["id"])
    assert body["lifecycle"] == "completed"
    assert "[APPROVED_PLAN" in adapter.implementer_instruction
    assert plan_summary in adapter.implementer_instruction
    assert '"schema_version":"2"' in adapter.implementer_instruction.replace(" ", "")
    assert "Do the work carefully" in adapter.implementer_instruction
    assert (workspace_root / created["id"] / "plan.approved.sha256").is_file()
    approved_events = [
        e for e in body["events"] if e["event_type"] == "plan.approved"
    ]
    assert approved_events
    assert approved_events[0]["metadata"].get("plan_sha256")
    assert approved_events[0]["metadata"].get("validation_profile") == "generic_text"


def test_status_passed_never_with_lifecycle_failed(workspace_root: Path) -> None:
    client = _client(workspace_root)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    failed = approve_plan(client, created["id"], approved=False)
    assert failed["lifecycle"] == "failed"
    assert failed["status"] == "failed"
    assert failed["status"] != "passed"


def test_cannot_fail_completed_task(workspace_root: Path) -> None:
    from app.orchestration.service import OrchestrationConflict, OrchestratorService
    from app.agents.mock import MockAgentAdapter
    from app.domain.store import MemoryTaskStore
    from app.quality.basic import BasicQualityGate
    from app.workspaces.local import LocalWorkspace

    store = MemoryTaskStore()
    orch = OrchestratorService(
        adapter=MockAgentAdapter(),
        gate=BasicQualityGate(),
        workspace=LocalWorkspace(workspace_root),
        store=store,
    )
    task = orch.create_task(title="T", instruction="I")
    task = orch.approve_plan(task.id, approved=True)
    assert task.lifecycle == "completed"
    assert task.status == "passed"
    with pytest.raises(OrchestrationConflict):
        orch._fail(task, error="nope", category="test")
    stored = store.get(task.id)
    assert stored is not None
    assert stored.lifecycle == "completed"
    assert stored.status == "passed"


def test_execute_tests_missing_confirm_rejected(workspace_root: Path) -> None:
    runner = FakeExecutionRunner()
    client = _client(
        workspace_root,
        adapter=FixedOutputAdapter(VALID_TESTS),
        runner=runner,
    )
    created = client.post(
        "/tasks",
        json={
            "title": "Py",
            "instruction": "tests",
            "validation_profile": "python_test_execution",
        },
    ).json()
    approved = approve_plan(client, created["id"])
    assert approved["lifecycle"] == "awaiting_execution_auth"
    response = client.post(f"/tasks/{approved['id']}/execute-tests", json={})
    assert response.status_code == 422
    assert runner.calls == []
    fetched = client.get(f"/tasks/{approved['id']}").json()
    assert fetched["lifecycle"] == "awaiting_execution_auth"
    assert fetched["execution"]["status"] == "not_requested"


def test_forbidden_transition_does_not_mutate(workspace_root: Path) -> None:
    client = _client(workspace_root)
    created = client.post("/tasks", json={"title": "T", "instruction": "I"}).json()
    before = client.get(f"/tasks/{created['id']}").json()
    response = client.post(
        f"/tasks/{created['id']}/approve-plan",
        json={"approved": True},
    )
    assert response.status_code == 200
    conflict = client.post(
        f"/tasks/{created['id']}/approve-plan",
        json={"approved": True},
    )
    assert conflict.status_code == 409
    after = client.get(f"/tasks/{created['id']}").json()
    assert after["lifecycle"] in {"completed", "failed"}
    assert after["lifecycle"] != before["lifecycle"]
    if after["lifecycle"] == "completed":
        again = client.get(f"/tasks/{created['id']}").json()
        assert again["status"] == "passed"
        assert again["lifecycle"] == "completed"


def test_sumopod_planner_prompt_mentions_schema_v2() -> None:
    from app.agents.sumopod import _system_prompt_for_role

    prompt = _system_prompt_for_role("planner")
    assert 'schema_version "2"' in prompt or '"2"' in prompt
    assert "runtime_tests" in prompt
    assert "Markdown" in prompt or "fences" in prompt
