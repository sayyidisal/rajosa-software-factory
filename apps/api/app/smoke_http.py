"""Phase 1B — local HTTP smoke against the official ``run_dev`` entry point.

Starts a disposable mock API on loopback, exercises health / auth / create /
approve, then stops the process group. Never prints API tokens or .env secrets.
Does not call ``/execute-tests`` or enable the execution runner.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from app.smoke_preflight import (
    ACTIVE_TASK_DB_PATH,
    ACTIVE_WORKSPACE_ROOT,
    SmokePreflightError,
    apply_smoke_overrides,
    run_preflight,
)

DEFAULT_SMOKE_HOST = "127.0.0.1"
DEFAULT_SMOKE_PORT = 18000
HEALTH_TIMEOUT_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 10.0
TERMINATE_GRACE_SECONDS = 10.0
KILL_GRACE_SECONDS = 5.0

# Fixed UUID used only for auth probes (task does not exist → 404 after auth).
_AUTH_PROBE_TASK_ID = "00000000-0000-4000-8000-000000000001"


class SmokeHttpError(RuntimeError):
    """Raised when the HTTP smoke cannot complete safely."""


@dataclass
class SmokeHttpResult:
    ok: bool
    smoke_root: Path
    base_url: str
    task_id: str | None = None
    lifecycle_after_create: str | None = None
    lifecycle_after_approve: str | None = None
    health_adapter: str | None = None
    health_runner: str | None = None
    root_removed: bool = False
    process_terminated: bool = False
    notes: list[str] = field(default_factory=list)


def assert_port_free(host: str, port: int) -> None:
    """Fail closed if something is already listening (do not contact it as a client)."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SmokeHttpError(f"smoke host must be loopback, got {host!r}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host != "localhost" else "127.0.0.1", port))
        except OSError as exc:
            raise SmokeHttpError(
                f"port {host}:{port} is not free; refusing to start smoke "
                f"(will not contact whatever is listening): {exc}"
            ) from exc


def build_smoke_environ(
    *,
    smoke_root: Path,
    token: str,
    host: str = DEFAULT_SMOKE_HOST,
    port: int = DEFAULT_SMOKE_PORT,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Copy base environ, apply smoke overrides, force loopback bind."""
    env = dict(os.environ if base is None else base)
    apply_smoke_overrides(
        env,
        smoke_root=smoke_root,
        token=token,
        api_port=str(port),
    )
    env["API_HOST"] = host
    # Avoid inheriting a developer WEB_CONCURRENCY that would fail run_dev.
    env.pop("WEB_CONCURRENCY", None)
    return env


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any] | list[Any] | str | None]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload: dict[str, Any] | list[Any] | str | None
            if not raw:
                payload = None
            else:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw
            return int(response.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        return int(exc.code), payload
    except urllib.error.URLError as exc:
        raise SmokeHttpError(f"HTTP {method} {url} failed: {exc}") from exc


def wait_for_health(
    base_url: str,
    *,
    proc: subprocess.Popen[bytes],
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    http_get: Callable[[str], tuple[int, Any]] | None = None,
) -> dict[str, Any]:
    """Poll GET /health until ok or process dies / timeout."""
    deadline = time.monotonic() + timeout
    health_url = urljoin(base_url.rstrip("/") + "/", "health")
    getter = http_get or (lambda u: _http_json("GET", u, token=None))
    last_error = "no attempt yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SmokeHttpError(
                f"API process exited before health succeeded (code={proc.returncode})"
            )
        try:
            status, payload = getter(health_url)
            if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return payload
            last_error = f"status={status} payload_type={type(payload).__name__}"
        except SmokeHttpError as exc:
            last_error = str(exc)
        sleep(0.25)
    raise SmokeHttpError(f"timed out waiting for health at {health_url}: {last_error}")


def terminate_process_group(
    proc: subprocess.Popen[bytes],
    *,
    grace_seconds: float = TERMINATE_GRACE_SECONDS,
    kill_seconds: float = KILL_GRACE_SECONDS,
) -> bool:
    """SIGTERM the process group, then SIGKILL. Returns True if reaped."""
    if proc.poll() is not None:
        return True
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.poll() is not None
    try:
        proc.wait(timeout=grace_seconds)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return proc.poll() is not None
    try:
        proc.wait(timeout=kill_seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _assert_no_secrets_in_health(payload: dict[str, Any]) -> None:
    blob = json.dumps(payload).lower()
    for needle in ("bearer ", "authorization", "api_key", "sumopod_api_key", "token_urlsafe"):
        if needle in blob:
            raise SmokeHttpError(f"health payload appears to contain secret material ({needle!r})")


def run_http_assertions(
    *,
    base_url: str,
    token: str,
    http: Callable[..., tuple[int, Any]] | None = None,
) -> tuple[str, str, str]:
    """Exercise auth + create + approve. Returns (task_id, life_create, life_approve)."""
    do = http or _http_json
    root = base_url.rstrip("/") + "/"

    # --- Health already validated by caller; re-check runner/adapter flags ---
    status, health = do("GET", urljoin(root, "health"), token=None)
    if status != 200 or not isinstance(health, dict):
        raise SmokeHttpError(f"unexpected health response: {status}")
    _assert_no_secrets_in_health(health)
    if health.get("adapter") != "mock":
        raise SmokeHttpError(f"expected adapter=mock, got {health.get('adapter')!r}")
    if health.get("execution_runner") != "off":
        raise SmokeHttpError(
            f"expected execution_runner=off, got {health.get('execution_runner')!r}"
        )

    probe = urljoin(root, f"tasks/{_AUTH_PROBE_TASK_ID}")

    # Missing token → 401 (dependency runs before handler; no mutation).
    status, body = do("GET", probe, token=None)
    if status != 401:
        raise SmokeHttpError(f"missing token expected 401, got {status}")
    if isinstance(body, dict) and "id" in body:
        raise SmokeHttpError("unauthenticated response unexpectedly included task id")

    # Wrong token → 401
    status, _ = do("GET", probe, token="wrong-token-not-valid-xx")
    if status != 401:
        raise SmokeHttpError(f"wrong token expected 401, got {status}")

    # Correct token accepted (task missing → 404)
    status, _ = do("GET", probe, token=token)
    if status != 404:
        raise SmokeHttpError(f"valid token probe expected 404, got {status}")

    # Unauthenticated create must not succeed
    status, denied = do(
        "POST",
        urljoin(root, "tasks"),
        token=None,
        body={"title": "Denied", "instruction": "must not create", "validation_profile": "generic_text"},
    )
    if status != 401:
        raise SmokeHttpError(f"unauthenticated create expected 401, got {status}")
    if isinstance(denied, dict) and denied.get("id"):
        raise SmokeHttpError("unauthenticated create returned a task id")

    # Create (single attempt — no retry)
    status, created = do(
        "POST",
        urljoin(root, "tasks"),
        token=token,
        body={
            "title": "Smoke Hello",
            "instruction": "Write a greeting",
            "validation_profile": "generic_text",
        },
    )
    if status != 200 or not isinstance(created, dict):
        raise SmokeHttpError(f"create task failed: status={status}")
    task_id = created.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise SmokeHttpError("create response missing task id")
    if created.get("lifecycle") != "awaiting_plan_approval":
        raise SmokeHttpError(
            f"expected lifecycle awaiting_plan_approval, got {created.get('lifecycle')!r}"
        )
    if created.get("plan_approved") is not False:
        raise SmokeHttpError("create must leave plan_approved false")
    if created.get("plan") is None:
        raise SmokeHttpError("create must include a structured plan")
    execution = created.get("execution") or {}
    if execution.get("status") != "not_requested":
        raise SmokeHttpError(
            f"create must not request execution, got {execution.get('status')!r}"
        )
    life_create = str(created["lifecycle"])

    # Retrieve before approve
    status, fetched = do("GET", urljoin(root, f"tasks/{task_id}"), token=token)
    if status != 200 or not isinstance(fetched, dict):
        raise SmokeHttpError(f"get task before approve failed: {status}")
    if fetched.get("lifecycle") != "awaiting_plan_approval":
        raise SmokeHttpError("fetched task lifecycle mismatch before approve")
    if fetched.get("plan") is None:
        raise SmokeHttpError("fetched task missing plan before approve")

    # Approve (single attempt)
    status, approved = do(
        "POST",
        urljoin(root, f"tasks/{task_id}/approve-plan"),
        token=token,
        body={"approved": True},
    )
    if status != 200 or not isinstance(approved, dict):
        raise SmokeHttpError(f"approve-plan failed: status={status}")
    if approved.get("lifecycle") != "completed":
        raise SmokeHttpError(
            f"expected lifecycle completed after approve, got {approved.get('lifecycle')!r}"
        )
    if approved.get("plan_approved") is not True:
        raise SmokeHttpError("approve must set plan_approved true")
    if approved.get("status") != "passed":
        raise SmokeHttpError(f"expected status passed, got {approved.get('status')!r}")
    execution = approved.get("execution") or {}
    if execution.get("status") != "not_requested":
        raise SmokeHttpError(
            "approve must not invoke execution runner "
            f"(execution.status={execution.get('status')!r})"
        )
    life_approve = str(approved["lifecycle"])

    # Retrieve again
    status, again = do("GET", urljoin(root, f"tasks/{task_id}"), token=token)
    if status != 200 or not isinstance(again, dict):
        raise SmokeHttpError(f"get task after approve failed: {status}")
    if again.get("lifecycle") != "completed" or again.get("plan_approved") is not True:
        raise SmokeHttpError("post-approve fetch inconsistent with approval")
    if (again.get("execution") or {}).get("status") != "not_requested":
        raise SmokeHttpError("post-approve fetch shows unexpected execution activity")

    return task_id, life_create, life_approve


def start_api_process(
    *,
    api_cwd: Path,
    environ: dict[str, str],
    log_path: Path,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    """Spawn ``python -m app.run_dev`` in its own session (process group)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("wb")
    try:
        proc = popen(
            [sys.executable, "-m", "app.run_dev"],
            cwd=str(api_cwd),
            env=environ,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    # Parent keeps log_handle open for the child's lifetime; close on terminate.
    proc._smoke_log_handle = log_handle  # type: ignore[attr-defined]
    return proc


def _close_log(proc: subprocess.Popen[bytes]) -> None:
    handle = getattr(proc, "_smoke_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def run_smoke(
    *,
    host: str = DEFAULT_SMOKE_HOST,
    port: int = DEFAULT_SMOKE_PORT,
    api_root: Path | None = None,
    keep_root_on_success: bool = False,
) -> SmokeHttpResult:
    """Full Phase 1B smoke: preflight → start → HTTP → stop → optional cleanup."""
    if host not in {"127.0.0.1", "localhost"}:
        raise SmokeHttpError(f"refusing non-loopback host {host!r}")
    bind_host = "127.0.0.1" if host == "localhost" else host

    api_cwd = (api_root or Path(__file__).resolve().parents[1]).resolve()
    smoke_root = Path(tempfile.mkdtemp(prefix="rajosa-smoke-http.")).resolve()
    (smoke_root / "workspaces").mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    token = secrets.token_urlsafe(32)
    environ = build_smoke_environ(
        smoke_root=smoke_root,
        token=token,
        host=bind_host,
        port=port,
    )

    # Fail closed before bind: same settings path the app will use.
    try:
        settings = run_preflight(environ=environ)
    except SmokePreflightError as exc:
        # Server never started — safe to remove root.
        _rm_tree(smoke_root)
        raise SmokeHttpError(str(exc)) from exc

    if settings.task_db_path.resolve() == ACTIVE_TASK_DB_PATH:
        _rm_tree(smoke_root)
        raise SmokeHttpError("preflight passed but DB still resolves to active path")
    if settings.workspace_root.resolve() == ACTIVE_WORKSPACE_ROOT:
        _rm_tree(smoke_root)
        raise SmokeHttpError("preflight passed but workspace still resolves to active path")

    try:
        assert_port_free(bind_host, port)
    except SmokeHttpError:
        _rm_tree(smoke_root)
        raise

    base_url = f"http://{bind_host}:{port}"
    log_path = smoke_root / "server.log"
    proc: subprocess.Popen[bytes] | None = None
    process_terminated = False
    root_removed = False
    ok = False
    task_id: str | None = None
    life_create: str | None = None
    life_approve: str | None = None
    health_adapter: str | None = None
    health_runner: str | None = None
    failure: BaseException | None = None

    try:
        proc = start_api_process(api_cwd=api_cwd, environ=environ, log_path=log_path)
        health = wait_for_health(base_url, proc=proc)
        health_adapter = str(health.get("adapter"))
        health_runner = str(health.get("execution_runner"))
        task_id, life_create, life_approve = run_http_assertions(
            base_url=base_url,
            token=token,
        )
        notes.append("http assertions passed")
        notes.append("execute-tests endpoint was not called")
        ok = True
    except BaseException as exc:
        failure = exc
        notes.append(f"smoke failed: {exc}")
    finally:
        # Drop token from local scope as soon as assertions finish / fail.
        token = ""
        if proc is not None:
            process_terminated = terminate_process_group(proc)
            _close_log(proc)
            if not process_terminated:
                notes.append(
                    "process group termination not confirmed; "
                    f"retaining smoke root at {smoke_root}"
                )
            elif ok and not keep_root_on_success:
                root_removed = _rm_tree(smoke_root)
                if root_removed:
                    notes.append("disposable smoke root removed after confirmed stop")
                else:
                    notes.append(f"could not remove smoke root: {smoke_root}")
            else:
                notes.append(f"retaining smoke root: {smoke_root}")
        elif not ok:
            # Failed before spawn — safe to remove empty/partial root.
            root_removed = _rm_tree(smoke_root)

    if failure is not None:
        raise SmokeHttpError(str(failure)) from failure

    return SmokeHttpResult(
        ok=True,
        smoke_root=smoke_root,
        base_url=base_url,
        task_id=task_id,
        lifecycle_after_create=life_create,
        lifecycle_after_approve=life_approve,
        health_adapter=health_adapter,
        health_runner=health_runner,
        root_removed=root_removed,
        process_terminated=process_terminated,
        notes=notes,
    )


def _rm_tree(path: Path) -> bool:
    try:
        import shutil

        shutil.rmtree(path)
        return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    del argv
    port = int(os.environ.get("SMOKE_API_PORT", str(DEFAULT_SMOKE_PORT)))
    host = os.environ.get("SMOKE_API_HOST", DEFAULT_SMOKE_HOST)
    keep = os.environ.get("SMOKE_KEEP_ROOT", "").strip() in {"1", "true", "yes"}
    try:
        result = run_smoke(host=host, port=port, keep_root_on_success=keep)
    except SmokeHttpError as exc:
        print(f"error: smoke http failed: {exc}", file=sys.stderr)
        return 2
    # Non-secret summary only.
    print(
        "smoke http ok "
        f"adapter={result.health_adapter!r} runner={result.health_runner!r} "
        f"create={result.lifecycle_after_create!r} "
        f"approve={result.lifecycle_after_approve!r} "
        f"terminated={result.process_terminated} "
        f"root_removed={result.root_removed}",
        file=sys.stderr,
    )
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
