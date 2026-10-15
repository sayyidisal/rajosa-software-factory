"""Safe staging of approved artifacts into a dedicated per-execution directory."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path

from app.execution.limits import (
    DEFAULT_LIMITS,
    OUTPUT_DIR_RELATIVE,
    STAGED_TEST_RELATIVE,
    ExecutionLimits,
)
from app.execution.models import StagedArtifact

logger = logging.getLogger(__name__)

FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".git",
        ".gitignore",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "docker.sock",
    }
)
ALLOWED_SOURCE_SUFFIXES = frozenset({".py", ".txt"})


class StagingError(ValueError):
    """Raised when staging input is unsafe or exceeds limits."""


def _is_forbidden_name(name: str) -> bool:
    lower = name.lower()
    if lower in FORBIDDEN_NAMES:
        return True
    if lower.startswith(".env"):
        return True
    return False


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise StagingError("symlinks are not allowed in staging inputs")


def _safe_relative(name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
        raise StagingError("absolute or traversal paths are not allowed")
    if any(_is_forbidden_name(part) for part in relative.parts):
        raise StagingError("forbidden filename in staging path")
    return relative


def create_execution_staging_root(base: Path, execution_id: str) -> Path:
    """Create a unique staging directory under base/_executions/<id>."""
    try:
        uuid.UUID(execution_id)
    except ValueError as exc:
        raise StagingError("invalid execution id") from exc
    root = (base / "_executions" / execution_id).resolve()
    base_resolved = base.resolve()
    if not root.is_relative_to(base_resolved):
        raise StagingError("staging path escaped base")
    if root.exists():
        raise StagingError("execution staging directory already exists")
    root.mkdir(parents=True, exist_ok=False)
    (root / "tests").mkdir()
    (root / OUTPUT_DIR_RELATIVE).mkdir()
    return root


def stage_python_test_artifact(
    *,
    source_file: Path,
    staging_root: Path,
    limits: ExecutionLimits = DEFAULT_LIMITS,
) -> list[StagedArtifact]:
    """Copy one approved Python/text artifact into the fixed staged test path.

    Does not follow symlinks. Does not write into the canonical task workspace.
    """
    _reject_symlink(source_file)
    if not source_file.is_file():
        raise StagingError("source artifact is not a regular file")
    if source_file.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
        raise StagingError("unsupported source file type")
    if _is_forbidden_name(source_file.name):
        raise StagingError("forbidden source filename")

    size = source_file.stat().st_size
    if size <= 0:
        raise StagingError("source artifact is empty")
    if size > limits.max_file_bytes:
        raise StagingError("source artifact exceeds per-file size limit")
    if size > limits.max_total_staged_bytes:
        raise StagingError("source artifact exceeds total staged size limit")

    dest_rel = _safe_relative(STAGED_TEST_RELATIVE)
    dest = (staging_root / dest_rel).resolve()
    if not dest.is_relative_to(staging_root.resolve()):
        raise StagingError("destination escaped staging root")

    # Copy via open/read/write to avoid following destination symlinks.
    if dest.exists() or dest.is_symlink():
        raise StagingError("destination already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    with open(source_file, "rb") as src, open(dest, "wb") as out:
        # O_NOFOLLOW is not portable on all open modes; reject symlink dest above.
        remaining = size
        while remaining > 0:
            chunk = src.read(min(65_536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            remaining -= len(chunk)

    written = dest.stat().st_size
    if written != size:
        raise StagingError("staged size mismatch")
    if written > limits.max_total_staged_bytes:
        raise StagingError("staged total size exceeded")

    return [
        StagedArtifact(
            relative_path=STAGED_TEST_RELATIVE,
            size_bytes=written,
            sha256=digest.hexdigest(),
        )
    ]


def collect_output_artifacts(
    staging_root: Path,
    *,
    limits: ExecutionLimits = DEFAULT_LIMITS,
) -> list[tuple[str, int]]:
    """Collect metadata from the designated output directory only."""
    out_dir = (staging_root / OUTPUT_DIR_RELATIVE).resolve()
    if not out_dir.is_relative_to(staging_root.resolve()):
        raise StagingError("output directory escaped staging root")
    if not out_dir.is_dir() or out_dir.is_symlink():
        return []

    collected: list[tuple[str, int]] = []
    for entry in sorted(out_dir.iterdir()):
        if entry.is_symlink():
            raise StagingError("symlink in output directory rejected")
        if not entry.is_file():
            continue
        if _is_forbidden_name(entry.name):
            raise StagingError("forbidden output filename")
        if entry.suffix.lower() not in {".txt", ".json", ".xml", ".log"}:
            raise StagingError("unsupported output file type")
        size = entry.stat().st_size
        if size > limits.max_output_artifact_bytes:
            raise StagingError("output artifact exceeds size limit")
        collected.append((entry.name, size))
        if len(collected) > limits.max_output_files:
            raise StagingError("too many output files")
    return collected


def cleanup_staging(staging_root: Path | None) -> None:
    """Best-effort removal of a staging directory. Failures are logged only."""
    if staging_root is None:
        return
    try:
        resolved = staging_root.resolve()
        if not resolved.exists():
            return
        # Refuse to delete paths that look like repo roots.
        if resolved.name in ("", ".", "/") or len(resolved.parts) < 3:
            logger.error("refusing unsafe staging cleanup path")
            return
        shutil.rmtree(resolved, ignore_errors=False)
    except OSError as exc:
        logger.error("staging cleanup failed: %s", type(exc).__name__)
        raise


def truncate_bytes(data: bytes, limit: int) -> str:
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        return text + "\n...[truncated]"
    return text


def ensure_not_following_symlink_copy(src: Path, dest: Path) -> None:
    """Helper used by tests — copy file contents without preserving symlinks."""
    _reject_symlink(src)
    if dest.exists() and dest.is_symlink():
        raise StagingError("destination is a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    data = src.read_bytes()
    fd = os.open(dest, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
