"""Read-only workspace reconciliation against the task store (Phase 4E.3).

Never deletes, moves, recreates, or overwrites artifacts. Never mutates task
lifecycle or approval state. Symlinks and escaped paths are skipped and reported.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.store import TaskRepository
from app.workspaces.local import LocalWorkspace, WorkspacePathError

logger = logging.getLogger(__name__)

DIGEST_MIRROR_NAME = "plan.approved.sha256"
EXECUTIONS_DIR_NAME = "_executions"


@dataclass
class DigestMirrorIssue:
    task_id: str
    kind: str  # missing_file | mismatch | unreadable
    detail: str


@dataclass
class ReconciliationReport:
    orphan_workspace_dirs: list[str] = field(default_factory=list)
    missing_workspaces: list[str] = field(default_factory=list)
    digest_mirror_issues: list[DigestMirrorIssue] = field(default_factory=list)
    leftover_executions: list[str] = field(default_factory=list)
    skipped_unsafe: list[str] = field(default_factory=list)
    incomplete: bool = False
    error: str | None = None

    def mark_failed(self, message: str) -> None:
        self.incomplete = True
        self.error = message


def _is_canonical_uuid(name: str) -> bool:
    try:
        parsed = uuid.UUID(name)
    except ValueError:
        return False
    return str(parsed) == name


def _safe_child(root: Path, entry: Path) -> Path | None:
    """Return resolved child if it stays under root; never follow for containment.

    Uses ``entry.name`` joined onto resolved root so a symlink entry is detected
    via ``is_symlink()`` on the join path without walking through it as root.
    """
    if entry.name in ("", ".", ".."):
        return None
    # Reconstruct from trusted root + basename only (no caller-supplied paths).
    candidate = (root / entry.name)
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    if not resolved.is_relative_to(root):
        return None
    return resolved


def reconcile_workspaces(
    *,
    workspace: LocalWorkspace,
    store: TaskRepository,
) -> ReconciliationReport:
    """Scan ``workspace.root`` against ``store`` and return a structured report."""
    report = ReconciliationReport()
    try:
        root = workspace.root.expanduser().resolve()
    except OSError as exc:
        report.mark_failed(f"failed to resolve workspace root: {exc}")
        return report

    if not root.exists():
        # Empty / missing root: every task is "missing workspace"; no orphans.
        try:
            task_ids = store.list_task_ids()
        except Exception as exc:
            report.mark_failed(f"failed to list task ids: {exc}")
            return report
        report.missing_workspaces = list(task_ids)
        return report

    if root.is_symlink():
        report.mark_failed("workspace root is a symlink; refusing to scan")
        report.skipped_unsafe.append(str(root))
        return report

    try:
        task_ids = store.list_task_ids()
    except Exception as exc:
        report.mark_failed(f"failed to list task ids: {exc}")
        return report
    known = set(task_ids)

    try:
        entries = list(root.iterdir())
    except OSError as exc:
        report.mark_failed(f"failed to list workspace root: {exc}")
        return report

    uuid_dirs_present: set[str] = set()

    for entry in entries:
        name = entry.name
        if name == EXECUTIONS_DIR_NAME:
            _scan_executions(root, entry, report)
            continue

        if not _is_canonical_uuid(name):
            # Non-task noise (e.g. stray files) — skip without failing the scan.
            if entry.is_symlink() or name in (".", ".."):
                report.skipped_unsafe.append(name)
            continue

        joined = root / name
        if joined.is_symlink():
            report.skipped_unsafe.append(name)
            continue

        safe = _safe_child(root, entry)
        if safe is None:
            report.skipped_unsafe.append(name)
            continue
        if not safe.is_dir():
            report.skipped_unsafe.append(name)
            continue

        uuid_dirs_present.add(name)
        if name not in known:
            report.orphan_workspace_dirs.append(name)

    for task_id in sorted(known):
        if task_id not in uuid_dirs_present:
            # Confirm absence without following a symlink that might appear as dir.
            path = root / task_id
            if path.is_symlink():
                report.skipped_unsafe.append(task_id)
                report.missing_workspaces.append(task_id)
                continue
            if not path.is_dir():
                report.missing_workspaces.append(task_id)

    _check_digest_mirrors(workspace, store, report)
    return report


def _scan_executions(root: Path, entry: Path, report: ReconciliationReport) -> None:
    joined = root / EXECUTIONS_DIR_NAME
    if joined.is_symlink():
        report.skipped_unsafe.append(EXECUTIONS_DIR_NAME)
        return
    safe = _safe_child(root, entry)
    if safe is None or not safe.is_dir():
        report.skipped_unsafe.append(EXECUTIONS_DIR_NAME)
        return
    try:
        children = list(safe.iterdir())
    except OSError as exc:
        report.mark_failed(f"failed to list _executions: {exc}")
        return
    for child in children:
        if child.is_symlink():
            report.skipped_unsafe.append(f"{EXECUTIONS_DIR_NAME}/{child.name}")
            continue
        child_safe = _safe_child(safe, child)
        if child_safe is None:
            report.skipped_unsafe.append(f"{EXECUTIONS_DIR_NAME}/{child.name}")
            continue
        # Any leftover staging directory is reportable (crash residue).
        if child_safe.is_dir() and _is_canonical_uuid(child.name):
            report.leftover_executions.append(child.name)
        else:
            report.skipped_unsafe.append(f"{EXECUTIONS_DIR_NAME}/{child.name}")


def _check_digest_mirrors(
    workspace: LocalWorkspace,
    store: TaskRepository,
    report: ReconciliationReport,
) -> None:
    try:
        task_ids = store.list_task_ids()
    except Exception as exc:
        report.mark_failed(f"failed to list tasks for digest check: {exc}")
        return

    for task_id in task_ids:
        try:
            task = store.get(task_id)
        except Exception as exc:
            report.mark_failed(f"failed to load task '{task_id}': {exc}")
            return
        if task is None or not task.plan_approved:
            continue
        try:
            path = workspace.resolve(task_id, DIGEST_MIRROR_NAME)
        except WorkspacePathError as exc:
            report.digest_mirror_issues.append(
                DigestMirrorIssue(
                    task_id=task_id,
                    kind="unreadable",
                    detail=str(exc),
                )
            )
            continue
        if path.is_symlink():
            report.skipped_unsafe.append(f"{task_id}/{DIGEST_MIRROR_NAME}")
            report.digest_mirror_issues.append(
                DigestMirrorIssue(
                    task_id=task_id,
                    kind="unreadable",
                    detail="digest mirror path is a symlink",
                )
            )
            continue
        if not path.is_file():
            report.digest_mirror_issues.append(
                DigestMirrorIssue(
                    task_id=task_id,
                    kind="missing_file",
                    detail=f"{DIGEST_MIRROR_NAME} missing for approved plan",
                )
            )
            continue
        try:
            mirror = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            report.digest_mirror_issues.append(
                DigestMirrorIssue(
                    task_id=task_id,
                    kind="unreadable",
                    detail=str(exc),
                )
            )
            continue
        expected = (task.approval_digest or "").strip()
        if not expected or mirror != expected:
            report.digest_mirror_issues.append(
                DigestMirrorIssue(
                    task_id=task_id,
                    kind="mismatch",
                    detail="plan.approved.sha256 does not match approval_digest",
                )
            )
