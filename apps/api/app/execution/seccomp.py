"""Host-side seccomp profile validation for per-container Docker enforcement.

The Docker CLI reads this JSON and sends it to the daemon. The profile must be
repository-controlled, checksum-pinned, and never silently redirected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Official moby/profiles tag seccomp/v0.2.1 (commit 5ad5f40…).
APPROVED_SECCOMP_SHA256 = (
    "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74"
)
APPROVED_SECCOMP_BYTES = 13470


class SeccompProfileError(ValueError):
    """Raised when the required seccomp profile cannot be trusted or applied."""


def default_seccomp_profile_path() -> Path:
    """Return the repository-controlled profile path (not resolved).

    Layout: <repo>/apps/api/app/execution/seccomp.py
         -> <repo>/docker/execution/seccomp/default.json
    """
    # limits.py / seccomp.py live at apps/api/app/execution/ → parents[4] = repo root
    return (
        Path(__file__).resolve().parents[4]
        / "docker"
        / "execution"
        / "seccomp"
        / "default.json"
    )


def expected_seccomp_directory() -> Path:
    return default_seccomp_profile_path().parent


def validate_seccomp_profile(configured: Path) -> Path:
    """Validate and return the absolute path to the approved profile.

    Checks (fail closed):
    - configured path is not a symlink before resolve
    - no intermediate symlink redirect of the leaf name into another tree
    - resolved path is a regular file under the expected seccomp directory
    - SHA-256 matches the approved pin
    - JSON parses and has expected seccomp structure
    """
    if configured.is_symlink():
        raise SeccompProfileError("seccomp profile path must not be a symlink")
    if not configured.exists():
        raise SeccompProfileError("seccomp profile is missing")

    # Resolve only after rejecting a symlinked leaf; still verify containment.
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise SeccompProfileError("seccomp profile path could not be resolved") from exc

    expected_dir = expected_seccomp_directory().resolve(strict=True)
    if not resolved.is_relative_to(expected_dir):
        raise SeccompProfileError("seccomp profile escaped the approved directory")
    if resolved.name != "default.json":
        raise SeccompProfileError("seccomp profile filename is not approved")
    if resolved.is_symlink() or not resolved.is_file():
        raise SeccompProfileError("seccomp profile must be a regular file")

    data = resolved.read_bytes()
    if len(data) != APPROVED_SECCOMP_BYTES:
        raise SeccompProfileError("seccomp profile size does not match the approved pin")
    digest = hashlib.sha256(data).hexdigest()
    if digest != APPROVED_SECCOMP_SHA256:
        raise SeccompProfileError("seccomp profile checksum does not match the approved pin")

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeccompProfileError("seccomp profile JSON is malformed") from exc

    _validate_seccomp_structure(payload)
    return resolved


def _validate_seccomp_structure(payload: object) -> None:
    if not isinstance(payload, dict):
        raise SeccompProfileError("seccomp profile root must be an object")
    if payload.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise SeccompProfileError("seccomp profile defaultAction is not approved")
    arch_map = payload.get("archMap")
    if not isinstance(arch_map, list) or not arch_map:
        raise SeccompProfileError("seccomp profile archMap is missing")
    syscalls = payload.get("syscalls")
    if not isinstance(syscalls, list) or not syscalls:
        raise SeccompProfileError("seccomp profile syscalls list is missing")
    for rule in syscalls:
        if not isinstance(rule, dict) or "action" not in rule:
            raise SeccompProfileError("seccomp profile syscall rule is invalid")
