"""Phase 1B — offline unit tests for HTTP smoke orchestrator (mocked boundaries)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.smoke_http import (
    SmokeHttpError,
    assert_port_free,
    build_smoke_environ,
    run_http_assertions,
    run_smoke,
    terminate_process_group,
    wait_for_health,
)
from app.smoke_preflight import ACTIVE_TASK_DB_PATH, ACTIVE_WORKSPACE_ROOT


SMOKE_TOKEN = "smoke-http-token-0001xxxx"


def test_build_smoke_environ_forces_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "root"
    smoke_root.mkdir()
    base = {
        "AGENT_ADAPTER": "sumopod",
        "EXECUTION_RUNNER": "local_container",
        "TASK_DB_PATH": str(ACTIVE_TASK_DB_PATH),
        "WORKSPACE_ROOT": str(ACTIVE_WORKSPACE_ROOT),
        "API_HOST": "0.0.0.0",
        "WEB_CONCURRENCY": "4",
    }
    env = build_smoke_environ(
        smoke_root=smoke_root,
        token=SMOKE_TOKEN,
        host="127.0.0.1",
        port=18000,
        base=base,
    )
    assert env["AGENT_ADAPTER"] == "mock"
    assert env["EXECUTION_RUNNER"] == "off"
    assert env["API_HOST"] == "127.0.0.1"
    assert env["API_PORT"] == "18000"
    assert env["RAJOSA_API_TOKEN"] == SMOKE_TOKEN
    assert "WEB_CONCURRENCY" not in env
    assert Path(env["TASK_DB_PATH"]).resolve().is_relative_to(smoke_root.resolve())
    assert Path(env["WORKSPACE_ROOT"]).resolve().is_relative_to(smoke_root.resolve())
    # Token present in env dict for the child process — not printed by this test.
    assert len(env["RAJOSA_API_TOKEN"]) >= 16


def test_assert_port_free_rejects_occupied(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as socket_mod

    class FakeSock:
        def setsockopt(self, *args: object) -> None:
            return None

        def bind(self, addr: object) -> None:
            raise OSError("address already in use")

        def __enter__(self) -> FakeSock:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(socket_mod, "socket", lambda *a, **k: FakeSock())
    with pytest.raises(SmokeHttpError, match="not free"):
        assert_port_free("127.0.0.1", 18000)


def test_assert_port_free_rejects_non_loopback() -> None:
    with pytest.raises(SmokeHttpError, match="loopback"):
        assert_port_free("0.0.0.0", 18000)


def test_phase1b_preflight_rejects_inherited_non_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1B remains consistent: effective non-loopback settings fail closed."""
    from app.config import get_settings
    from app.smoke_preflight import (
        SMOKE_ROOT_ENV,
        SmokePreflightError,
        assert_smoke_safe,
    )

    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    smoke_root = tmp_path / "root"
    smoke_root.mkdir()
    env = build_smoke_environ(
        smoke_root=smoke_root,
        token=SMOKE_TOKEN,
        host="127.0.0.1",
        port=18000,
        base={"API_HOST": "0.0.0.0"},
    )
    assert env["API_HOST"] == "127.0.0.1"
    # Tamper after build to prove preflight checks settings.api_host, not hope.
    env["API_HOST"] = "0.0.0.0"
    settings = get_settings(environ=env)
    assert settings.api_host == "0.0.0.0"
    assert env[SMOKE_ROOT_ENV] == str(smoke_root.resolve())
    with pytest.raises(SmokePreflightError, match="loopback API_HOST"):
        assert_smoke_safe(settings, smoke_root=smoke_root)


def test_wait_for_health_process_exits_early() -> None:
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.returncode = 1
    with pytest.raises(SmokeHttpError, match="exited before health"):
        wait_for_health(
            "http://127.0.0.1:18000",
            proc=proc,
            timeout=1.0,
            sleep=lambda _s: None,
            http_get=lambda _u: (500, {}),
        )


def test_wait_for_health_timeout() -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    calls = {"n": 0}

    def sleep(_s: float) -> None:
        calls["n"] += 1
        if calls["n"] > 3:
            # Force timeout by advancing monotonic via short timeout + many sleeps
            pass

    with pytest.raises(SmokeHttpError, match="timed out"):
        wait_for_health(
            "http://127.0.0.1:18000",
            proc=proc,
            timeout=0.01,
            sleep=sleep,
            http_get=lambda _u: (_ for _ in ()).throw(
                SmokeHttpError("connection refused")
            ),
        )


def test_wait_for_health_success() -> None:
    proc = MagicMock()
    proc.poll.return_value = None
    payload = {"status": "ok", "adapter": "mock", "execution_runner": "off"}
    result = wait_for_health(
        "http://127.0.0.1:18000",
        proc=proc,
        timeout=1.0,
        sleep=lambda _s: None,
        http_get=lambda _u: (200, payload),
    )
    assert result["adapter"] == "mock"


def _scripted_http(script: list[tuple[str, str, int, Any]]):
    """Return http callable that matches method+url suffix and pops responses."""

    def http(
        method: str,
        url: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]:
        if not script:
            raise AssertionError(f"unexpected HTTP {method} {url}")
        exp_method, exp_suffix, status, payload = script.pop(0)
        assert method == exp_method
        assert url.endswith(exp_suffix)
        return status, payload

    return http


def test_run_http_assertions_happy_path() -> None:
    task_id = "11111111-1111-4111-8111-111111111111"
    plan = {"schema_version": "2", "steps": []}
    create_body = {
        "id": task_id,
        "lifecycle": "awaiting_plan_approval",
        "plan_approved": False,
        "plan": plan,
        "execution": {"status": "not_requested"},
        "status": "pending",
    }
    approved_body = {
        "id": task_id,
        "lifecycle": "completed",
        "plan_approved": True,
        "plan": plan,
        "execution": {"status": "not_requested"},
        "status": "passed",
        "quality": {"passed": True, "checks": []},
    }
    script = [
        ("GET", "/health", 200, {"status": "ok", "adapter": "mock", "execution_runner": "off"}),
        ("GET", f"/tasks/{'00000000-0000-4000-8000-000000000001'}", 401, {"detail": "Authentication required"}),
        ("GET", f"/tasks/{'00000000-0000-4000-8000-000000000001'}", 401, {"detail": "Authentication required"}),
        ("GET", f"/tasks/{'00000000-0000-4000-8000-000000000001'}", 404, {"detail": "Task not found"}),
        ("POST", "/tasks", 401, {"detail": "Authentication required"}),
        ("POST", "/tasks", 200, create_body),
        ("GET", f"/tasks/{task_id}", 200, create_body),
        ("POST", f"/tasks/{task_id}/approve-plan", 200, approved_body),
        ("GET", f"/tasks/{task_id}", 200, approved_body),
    ]
    tid, lc, la = run_http_assertions(
        base_url="http://127.0.0.1:18000",
        token=SMOKE_TOKEN,
        http=_scripted_http(script),
    )
    assert tid == task_id
    assert lc == "awaiting_plan_approval"
    assert la == "completed"
    assert script == []


def test_run_http_assertions_auth_negative() -> None:
    script = [
        ("GET", "/health", 200, {"status": "ok", "adapter": "mock", "execution_runner": "off"}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 200, {"id": "leak"}),
    ]
    with pytest.raises(SmokeHttpError, match="expected 401"):
        run_http_assertions(
            base_url="http://127.0.0.1:18000",
            token=SMOKE_TOKEN,
            http=_scripted_http(script),
        )


def test_run_http_assertions_create_failure_no_retry() -> None:
    script = [
        ("GET", "/health", 200, {"status": "ok", "adapter": "mock", "execution_runner": "off"}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 401, {}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 401, {}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 404, {}),
        ("POST", "/tasks", 401, {}),
        ("POST", "/tasks", 500, {"detail": "boom"}),
    ]
    with pytest.raises(SmokeHttpError, match="create task failed"):
        run_http_assertions(
            base_url="http://127.0.0.1:18000",
            token=SMOKE_TOKEN,
            http=_scripted_http(script),
        )
    assert script == []  # no extra POST retry


def test_run_http_assertions_approve_failure() -> None:
    task_id = "22222222-2222-4222-8222-222222222222"
    create_body = {
        "id": task_id,
        "lifecycle": "awaiting_plan_approval",
        "plan_approved": False,
        "plan": {"schema_version": "2"},
        "execution": {"status": "not_requested"},
    }
    script = [
        ("GET", "/health", 200, {"status": "ok", "adapter": "mock", "execution_runner": "off"}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 401, {}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 401, {}),
        ("GET", "/tasks/00000000-0000-4000-8000-000000000001", 404, {}),
        ("POST", "/tasks", 401, {}),
        ("POST", "/tasks", 200, create_body),
        ("GET", f"/tasks/{task_id}", 200, create_body),
        ("POST", f"/tasks/{task_id}/approve-plan", 409, {"detail": "conflict"}),
    ]
    with pytest.raises(SmokeHttpError, match="approve-plan failed"):
        run_http_assertions(
            base_url="http://127.0.0.1:18000",
            token=SMOKE_TOKEN,
            http=_scripted_http(script),
        )


def test_run_http_assertions_rejects_non_mock_adapter() -> None:
    script = [
        ("GET", "/health", 200, {"status": "ok", "adapter": "sumopod", "execution_runner": "off"}),
    ]
    with pytest.raises(SmokeHttpError, match="adapter=mock"):
        run_http_assertions(
            base_url="http://127.0.0.1:18000",
            token=SMOKE_TOKEN,
            http=_scripted_http(script),
        )


def test_run_http_assertions_rejects_runner_enabled() -> None:
    script = [
        (
            "GET",
            "/health",
            200,
            {"status": "ok", "adapter": "mock", "execution_runner": "local_container"},
        ),
    ]
    with pytest.raises(SmokeHttpError, match="execution_runner=off"):
        run_http_assertions(
            base_url="http://127.0.0.1:18000",
            token=SMOKE_TOKEN,
            http=_scripted_http(script),
        )


def test_terminate_process_group_already_dead() -> None:
    proc = MagicMock()
    proc.poll.return_value = 0
    assert terminate_process_group(proc) is True


def test_terminate_process_group_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.side_effect = [None, 0]
    proc.wait.return_value = 0
    killed: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr("app.smoke_http.os.killpg", fake_killpg)
    assert terminate_process_group(proc, grace_seconds=0.01, kill_seconds=0.01) is True
    assert killed and killed[0][0] == 4242


def test_run_smoke_port_busy_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    monkeypatch.setattr("app.smoke_http.tempfile.mkdtemp", lambda prefix="": str(tmp_path / "s"))
    (tmp_path / "s").mkdir()
    monkeypatch.setattr(
        "app.smoke_http.assert_port_free",
        lambda host, port: (_ for _ in ()).throw(SmokeHttpError("port not free")),
    )
    started: list[object] = []

    def fake_start(**kwargs: object) -> object:
        started.append(kwargs)
        raise AssertionError("must not start")

    monkeypatch.setattr("app.smoke_http.start_api_process", fake_start)
    with pytest.raises(SmokeHttpError, match="not free"):
        run_smoke(port=18000, api_root=tmp_path)
    assert started == []


def test_run_smoke_success_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    root = tmp_path / "smoke-ok"
    root.mkdir()
    monkeypatch.setattr("app.smoke_http.tempfile.mkdtemp", lambda prefix="": str(root))
    monkeypatch.setattr("app.smoke_http.assert_port_free", lambda host, port: None)

    proc = MagicMock()
    proc.poll.return_value = 0
    proc.pid = 99

    monkeypatch.setattr(
        "app.smoke_http.start_api_process",
        lambda **kwargs: proc,
    )
    monkeypatch.setattr(
        "app.smoke_http.wait_for_health",
        lambda *a, **k: {"status": "ok", "adapter": "mock", "execution_runner": "off"},
    )
    monkeypatch.setattr(
        "app.smoke_http.run_http_assertions",
        lambda **k: ("tid", "awaiting_plan_approval", "completed"),
    )
    monkeypatch.setattr("app.smoke_http.terminate_process_group", lambda *a, **k: True)

    result = run_smoke(port=18000, api_root=tmp_path, keep_root_on_success=False)
    assert result.ok is True
    assert result.process_terminated is True
    assert result.root_removed is True
    assert result.lifecycle_after_approve == "completed"
    assert not root.exists()


def test_run_smoke_retains_root_when_termination_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.ENV_FILE_PATH", tmp_path / "missing.env")
    root = tmp_path / "smoke-keep"
    root.mkdir()
    monkeypatch.setattr("app.smoke_http.tempfile.mkdtemp", lambda prefix="": str(root))
    monkeypatch.setattr("app.smoke_http.assert_port_free", lambda host, port: None)
    proc = MagicMock()
    proc.pid = 77
    monkeypatch.setattr("app.smoke_http.start_api_process", lambda **k: proc)
    monkeypatch.setattr(
        "app.smoke_http.wait_for_health",
        lambda *a, **k: {"status": "ok", "adapter": "mock", "execution_runner": "off"},
    )
    monkeypatch.setattr(
        "app.smoke_http.run_http_assertions",
        lambda **k: ("tid", "awaiting_plan_approval", "completed"),
    )
    monkeypatch.setattr("app.smoke_http.terminate_process_group", lambda *a, **k: False)

    result = run_smoke(port=18000, api_root=tmp_path)
    assert result.process_terminated is False
    assert result.root_removed is False
    assert root.exists()
    assert any("termination not confirmed" in n for n in result.notes)


def test_makefile_smoke_http_uses_run_dev_and_not_execute() -> None:
    root = Path(__file__).resolve().parents[3]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    assert "smoke-api-http:" in makefile
    http_block = makefile.split("smoke-api-http:")[1].split("dev-web:")[0]
    assert "python -m app.smoke_http" in http_block
    source = (root / "apps/api/app/smoke_http.py").read_text(encoding="utf-8")
    assert "app.run_dev" in source
    assert "approve-plan" in source
    assert 'f"tasks/{task_id}/execute-tests"' not in source
    assert '"/tasks/{id}/execute-tests"' not in source


def test_dev_api_target_unchanged_semantics() -> None:
    root = Path(__file__).resolve().parents[3]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    # Original dev-api block still sources .env then run_dev without smoke overrides.
    assert "dev-api:" in makefile
    dev_block = makefile.split("dev-api:")[1].split("smoke-api:")[0]
    assert "set -a" in dev_block
    assert "python -m app.run_dev" in dev_block
    assert "smoke_preflight" not in dev_block
    assert "AGENT_ADAPTER=mock" not in dev_block
    assert "API_HOST=127.0.0.1" not in dev_block
