"""Deterministic orchestrator with mandatory plan approval (Phase 4B)."""

from __future__ import annotations

import logging
import threading
import uuid

from pydantic import ValidationError

from app.agents.base import AgentAdapter
from app.agents.errors import AgentError
from app.domain.models import CheckResult, QualityReport, Task
from app.domain.store import TaskRepository
from app.execution.models import ExecutionSummary
from app.orchestration.events import OrchestrationEvent, make_event
from app.orchestration.lifecycle import (
    Lifecycle,
    assert_transition,
    legacy_status_for,
)
from app.quality.profiles import profile_requires_runtime
from app.orchestration.integrity import (
    PlanIntegrityError,
    assert_approved_plan_integrity,
    assert_plan_approvable,
)
from app.orchestration.plan import (
    PlanValidationError,
    canonical_plan_json,
    compute_approval_digest,
    parse_plan_text,
)
from app.quality.base import QualityGate
from app.quality.profiles import ValidationProfileName
from app.workspaces.local import LocalWorkspace

logger = logging.getLogger(__name__)
SAFE_AGENT_ERROR = "The agent could not complete this task."
VALIDATION_FAILED_PREFIX = "Validation failed:"


class OrchestrationConflict(ValueError):
    """Raised for illegal concurrent or out-of-order transitions."""


class OrchestratorService:
    def __init__(
        self,
        *,
        adapter: AgentAdapter,
        gate: QualityGate,
        workspace: LocalWorkspace,
        store: TaskRepository,
    ) -> None:
        self.adapter = adapter
        self.gate = gate
        self.workspace = workspace
        self.store = store
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            if task_id not in self._locks:
                self._locks[task_id] = threading.Lock()
            return self._locks[task_id]

    def _load_task(self, task_id: str) -> Task | None:
        """Load task from store; corrupt persisted rows become PlanIntegrityError."""
        try:
            return self.store.get(task_id)
        except ValidationError as exc:
            raise PlanIntegrityError(
                "invalid_stored_task",
                "Stored task data failed schema validation",
            ) from exc

    def get_task(self, task_id: str) -> Task | None:
        task = self._load_task(task_id)
        if task is None:
            return None
        if task.plan_approved:
            # Fail-closed for approved tasks: digest/plan/profile must still verify.
            assert_approved_plan_integrity(task)
        return task

    def create_task(
        self,
        *,
        title: str,
        instruction: str,
        validation_profile: ValidationProfileName = "generic_text",
    ) -> Task:
        task_id = str(uuid.uuid4())
        self.workspace.prepare(task_id)
        task = Task(
            id=task_id,
            title=title,
            instruction=instruction,
            validation_profile=validation_profile,
            status="pending",
            lifecycle="created",
            output="",
            error=None,
            quality=QualityReport(passed=False, checks=[]),
            execution=ExecutionSummary.not_requested(),
            plan=None,
            plan_approved=False,
            events=[],
        )
        task = self._transition(
            task,
            "planning",
            event_type="task.created",
            metadata={"validation_profile": validation_profile},
        )
        self.store.save(task)

        try:
            result = self.adapter.run(
                task_id=task_id,
                title=title,
                instruction=instruction,
                workspace=self.workspace,
                role="planner",
                validation_profile=validation_profile,
            )
        except AgentError as exc:
            return self._fail(task, error=exc.public_message, category="agent_error")
        except Exception:
            logger.error("planner failed: unexpected")
            return self._fail(task, error=SAFE_AGENT_ERROR, category="agent_error")

        try:
            plan = parse_plan_text(
                result.output,
                validation_profile=validation_profile,
            )
        except PlanValidationError as exc:
            self.workspace.write_text(task_id, "plan.raw.txt", result.output[:4000])
            return self._fail(
                task,
                error=f"Plan validation failed: {exc}",
                category=exc.category,
                event_type="plan.invalid",
            )

        # requires_execution already overridden inside parse_plan_text from profile.
        self.workspace.write_text(task_id, "plan.json", plan.model_dump_json(indent=2))
        task = task.model_copy(update={"plan": plan, "output": result.output})
        task = self._append_event(
            task,
            make_event(
                task_id=task_id,
                event_type="plan.generated",
                from_lifecycle="planning",
                to_lifecycle="planning",
                metadata={
                    "steps": len(plan.steps),
                    "schema_version": plan.schema_version,
                    "requires_execution": plan.requires_execution,
                },
            ),
        )
        task = self._transition(
            task,
            "awaiting_plan_approval",
            event_type="plan.awaiting_approval",
        )
        self.store.save(task)
        return task

    def approve_plan(self, task_id: str, *, approved: bool) -> Task:
        if approved is not True:
            raise ValueError("explicit plan approval is required")

        lock = self._lock_for(task_id)
        if not lock.acquire(blocking=False):
            raise OrchestrationConflict("another plan approval is already in progress")
        try:
            task = self._load_task(task_id)
            if task is None:
                raise LookupError("task not found")
            if task.lifecycle != "awaiting_plan_approval":
                raise OrchestrationConflict(
                    f"plan approval is not allowed in lifecycle '{task.lifecycle}'"
                )
            if task.plan_approved or task.plan is None:
                raise OrchestrationConflict("plan is already approved or missing")

            # Re-validate plan+profile before approval (no digest yet).
            approved_plan = assert_plan_approvable(task).model_copy(deep=True)
            plan_digest = compute_approval_digest(
                plan=approved_plan,
                validation_profile=task.validation_profile,
            )
            self.workspace.write_text(
                task_id, "plan.json", approved_plan.model_dump_json(indent=2)
            )
            self.workspace.write_text(task_id, "plan.approved.sha256", plan_digest)

            task = task.model_copy(
                update={
                    "plan_approved": True,
                    "plan": approved_plan,
                    "approval_digest": plan_digest,
                }
            )
            task = self._append_event(
                task,
                make_event(
                    task_id=task_id,
                    event_type="plan.approved",
                    from_lifecycle="awaiting_plan_approval",
                    to_lifecycle="generating",
                    metadata={
                        "plan_sha256": plan_digest,
                        "validation_profile": task.validation_profile,
                    },
                ),
            )
            task = self._transition(task, "generating", event_type="generation.started")
            self.store.save(task)
            return self._run_implementation(task, plan_digest=plan_digest)
        finally:
            lock.release()

    def reject_plan(self, task_id: str) -> Task:
        lock = self._lock_for(task_id)
        with lock:
            task = self._load_task(task_id)
            if task is None:
                raise LookupError("task not found")
            if task.lifecycle != "awaiting_plan_approval":
                raise OrchestrationConflict(
                    f"plan rejection is not allowed in lifecycle '{task.lifecycle}'"
                )
            return self._fail(
                task,
                error="Plan was rejected by the user",
                category="plan_rejected",
                event_type="plan.rejected",
            )

    def apply_execution_outcome(self, task: Task) -> Task:
        """Update lifecycle after ExecutionService returns. Never bypasses the service."""
        lock = self._lock_for(task.id)
        with lock:
            current = self.store.get(task.id) or task
            # Prefer the execution summary from the just-returned task.
            merged = current.model_copy(
                update={
                    "execution": task.execution,
                    "events": current.events,
                }
            )
            if merged.lifecycle != "awaiting_execution_auth":
                # Execution may be requested later; do not invent transitions.
                self.store.save(merged)
                return merged

            execution = merged.execution
            if execution is None:
                return self._fail(
                    merged,
                    error="Execution result missing",
                    category="invalid_result",
                )

            # Busy is a rejected concurrent request, not an owned execution outcome.
            # Never finalize lifecycle or overwrite prior evidence for busy.
            if execution.runner_error_category == "busy":
                return current

            # Require validated success shape — never complete on claims alone.
            if (
                execution.status == "completed"
                and execution.tests_passed
                and execution.tests_executed
                and not execution.timed_out
                and execution.runner_error_category in ("none",)
            ):
                merged = self._append_event(
                    merged,
                    make_event(
                        task_id=merged.id,
                        event_type="execution.completed",
                        from_lifecycle="awaiting_execution_auth",
                        to_lifecycle="completed",
                        metadata={"tests_passed": True},
                    ),
                )
                return self._complete(merged)

            category = execution.runner_error_category or "tests_failed"
            detail = execution.detail or "Execution did not pass"
            return self._fail(
                merged,
                error=detail,
                category=category,
                event_type="execution.completed",
            )

    def _run_implementation(self, task: Task, *, plan_digest: str) -> Task:
        if task.plan is None:
            return self._fail(
                task,
                error="Approved plan missing at implementation time",
                category="plan_missing",
            )
        current_digest = compute_approval_digest(
            plan=task.plan,
            validation_profile=task.validation_profile,
        )
        if current_digest != plan_digest:
            return self._fail(
                task,
                error="Approved plan integrity check failed",
                category="plan_integrity",
            )

        # Pass the exact approved canonical plan to the implementer.
        plan_json = canonical_plan_json(task.plan)
        implementer_instruction = (
            f"{task.instruction}\n\n"
            f"[APPROVED_PLAN digest={plan_digest} "
            f"profile={task.validation_profile}]\n"
            f"{plan_json}\n"
            f"[/APPROVED_PLAN]\n"
        )
        try:
            result = self.adapter.run(
                task_id=task.id,
                title=task.title,
                instruction=implementer_instruction,
                workspace=self.workspace,
                role="implementer",
                validation_profile=task.validation_profile,
            )
        except AgentError as exc:
            return self._fail(task, error=exc.public_message, category="agent_error")
        except Exception:
            logger.error("implementer failed: unexpected")
            return self._fail(task, error=SAFE_AGENT_ERROR, category="agent_error")

        task = task.model_copy(update={"output": result.output})
        task = self._transition(task, "validating", event_type="generation.completed")
        self.store.save(task)

        try:
            report = self.gate.evaluate(
                result,
                self.workspace.task_directory(task.id),
                profile=task.validation_profile,
            )
        except Exception as exc:
            logger.error(
                "quality evaluation failed: task_id=%s profile=%s error_type=%s",
                task.id,
                task.validation_profile,
                type(exc).__name__,
            )
            report = QualityReport(
                passed=False,
                checks=[
                    CheckResult(
                        name="validator_error",
                        passed=False,
                        detail="Validation could not be completed safely",
                    )
                ],
            )

        all_checks = bool(report.checks) and all(c.passed for c in report.checks)
        passed = bool(report.passed) and all_checks
        if not passed:
            report = QualityReport(
                passed=False,
                checks=report.checks
                or [
                    CheckResult(
                        name="validator_error",
                        passed=False,
                        detail="Validation produced no checks",
                    )
                ],
            )
            task = task.model_copy(update={"quality": report})
            return self._fail(
                task,
                error=self._validation_error(report),
                category="quality_failed",
                event_type="quality.failed",
            )

        task = task.model_copy(update={"quality": report})
        task = self._append_event(
            task,
            make_event(
                task_id=task.id,
                event_type="quality.passed",
                from_lifecycle="validating",
                metadata={"checks": len(report.checks)},
            ),
        )

        if profile_requires_runtime(task.validation_profile):
            task = self._transition(
                task,
                "awaiting_execution_auth",
                event_type="execution.auth_required",
            )
            self.store.save(task)
            return task

        return self._complete(task)

    def _complete(self, task: Task) -> Task:
        task = self._transition(task, "completed", event_type="task.completed")
        task = task.model_copy(
            update={
                "status": legacy_status_for("completed"),
                "error": None,
            }
        )
        self.store.save(task)
        return task

    def _fail(
        self,
        task: Task,
        *,
        error: str,
        category: str,
        event_type: str = "task.failed",
    ) -> Task:
        current = task.lifecycle
        if current == "failed":
            # Already terminal failure — keep existing error; do not rewrite success.
            self.store.save(task)
            return task
        if current == "completed":
            raise OrchestrationConflict("cannot transition a completed task to failed")
        assert_transition(current, "failed")
        task = self._append_event(
            task,
            make_event(
                task_id=task.id,
                event_type=event_type,  # type: ignore[arg-type]
                from_lifecycle=current,
                to_lifecycle="failed",
                error_category=category,
            ),
        )
        task = task.model_copy(
            update={
                "lifecycle": "failed",
                "status": "failed",
                "error": error,
            }
        )
        self.store.save(task)
        return task

    def _transition(
        self,
        task: Task,
        target: Lifecycle,
        *,
        event_type: str,
        metadata: dict | None = None,
    ) -> Task:
        assert_transition(task.lifecycle, target)
        updated = self._append_event(
            task,
            make_event(
                task_id=task.id,
                event_type=event_type,  # type: ignore[arg-type]
                from_lifecycle=task.lifecycle,
                to_lifecycle=target,
                metadata=metadata,
            ),
        )
        return updated.model_copy(
            update={
                "lifecycle": target,
                "status": legacy_status_for(target),
            }
        )

    def _append_event(self, task: Task, event: OrchestrationEvent) -> Task:
        # Append-only in the in-memory task view. Durable stores keep full
        # history via TaskRepository save semantics (no repository-side cap).
        return task.model_copy(update={"events": list(task.events) + [event]})

    def _validation_error(self, report: QualityReport) -> str:
        failed = [check.detail for check in report.checks if not check.passed]
        if not failed:
            return f"{VALIDATION_FAILED_PREFIX} one or more checks did not pass"
        first = failed[0]
        if len(failed) == 1:
            return f"{VALIDATION_FAILED_PREFIX} {first}"
        return f"{VALIDATION_FAILED_PREFIX} {first} (+{len(failed) - 1} more)"
