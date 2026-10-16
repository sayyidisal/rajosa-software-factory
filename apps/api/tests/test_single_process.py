"""Phase 4G.1 — offline tests for the supported single-process launch guard."""

from __future__ import annotations

import pytest

from app.single_process import (
    UnsupportedProcessTopologyError,
    assert_supported_dev_launch,
)


def test_accepts_unset_web_concurrency() -> None:
    assert_supported_dev_launch(environ={}, argv=[])


def test_accepts_web_concurrency_one() -> None:
    assert_supported_dev_launch(environ={"WEB_CONCURRENCY": "1"}, argv=[])


def test_rejects_web_concurrency_greater_than_one() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="WEB_CONCURRENCY=4"):
        assert_supported_dev_launch(environ={"WEB_CONCURRENCY": "4"}, argv=[])


def test_rejects_invalid_web_concurrency() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="invalid WEB_CONCURRENCY"):
        assert_supported_dev_launch(environ={"WEB_CONCURRENCY": "many"}, argv=[])


def test_rejects_explicit_workers_flag() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="--workers 2"):
        assert_supported_dev_launch(environ={}, argv=["--workers", "2"])


def test_rejects_workers_equals_form() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="--workers 3"):
        assert_supported_dev_launch(environ={}, argv=["--workers=3"])


def test_rejects_short_workers_flag() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="--workers 2"):
        assert_supported_dev_launch(environ={}, argv=["-w", "2"])


def test_rejects_glued_short_workers_flag() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="--workers 2"):
        assert_supported_dev_launch(environ={}, argv=["-w2"])


def test_rejects_glued_short_workers_multi_digit() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="--workers 10"):
        assert_supported_dev_launch(environ={}, argv=["-w10"])


def test_accepts_explicit_workers_one() -> None:
    assert_supported_dev_launch(environ={}, argv=["--workers", "1"])


def test_accepts_glued_short_workers_one() -> None:
    assert_supported_dev_launch(environ={}, argv=["-w1"])


def test_run_dev_exits_before_uvicorn_when_concurrency_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supported launcher must fail closed before binding Uvicorn."""
    import uvicorn

    from app import run_dev

    called: list[bool] = []

    def boom(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("uvicorn.run must not be called")

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setattr(uvicorn, "run", boom)

    with pytest.raises(SystemExit) as excinfo:
        run_dev.main([])

    assert excinfo.value.code == 2
    assert called == []


def test_run_dev_rejects_web_concurrency_zero_before_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WEB_CONCURRENCY=0 must fail closed before uvicorn.run is called."""
    import uvicorn

    from app import run_dev

    called: list[bool] = []

    def boom(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("uvicorn.run must not be called")

    monkeypatch.setenv("WEB_CONCURRENCY", "0")
    monkeypatch.setattr(uvicorn, "run", boom)

    with pytest.raises(SystemExit) as excinfo:
        run_dev.main([])

    assert excinfo.value.code == 2
    assert called == []


def test_rejects_web_concurrency_zero() -> None:
    with pytest.raises(UnsupportedProcessTopologyError, match="WEB_CONCURRENCY=0"):
        assert_supported_dev_launch(environ={"WEB_CONCURRENCY": "0"}, argv=[])
