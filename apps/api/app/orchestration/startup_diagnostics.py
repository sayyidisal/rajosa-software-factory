"""Secret-safe startup diagnostics for operators (Phase 4E.4b).

Exposes counts and flags only. Never includes absolute filesystem paths,
credentials, env contents, or raw exception strings that may embed paths.
"""

from __future__ import annotations

from typing import Any

from app.orchestration.recovery import RecoveryReport
from app.workspaces.reconcile import ReconciliationReport


def build_startup_diagnostics(
    *,
    recovery_completed: bool,
    recovery_report: RecoveryReport | None,
    reconciliation_report: ReconciliationReport | None,
) -> dict[str, Any]:
    """Build a JSON-serializable startup summary safe for ``GET /health``."""
    recovery: dict[str, Any] | None = None
    if recovery_report is not None:
        # Public /health: counts only — never task UUIDs (Phase 4H.1).
        recovery = {
            "examined": int(recovery_report.examined),
            "interrupted_count": len(recovery_report.interrupted_task_ids),
            "skipped_count": len(recovery_report.skipped_task_ids),
        }

    reconciliation: dict[str, Any] | None = None
    if reconciliation_report is not None:
        reconciliation = {
            "incomplete": bool(reconciliation_report.incomplete),
            "orphan_workspace_count": len(reconciliation_report.orphan_workspace_dirs),
            "missing_workspace_count": len(reconciliation_report.missing_workspaces),
            "digest_mirror_issue_count": len(reconciliation_report.digest_mirror_issues),
            "leftover_execution_count": len(reconciliation_report.leftover_executions),
            "skipped_unsafe_count": len(reconciliation_report.skipped_unsafe),
        }

    return {
        "recovery_completed": bool(recovery_completed),
        "recovery": recovery,
        "reconciliation": reconciliation,
    }
