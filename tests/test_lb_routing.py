"""Routing helpers tests."""

from __future__ import annotations

import pytest

from vctl.config.models import (
    LbAdmin,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
    Pool,
)
from vctl.lb.routing import pool_for_endpoint, pool_for_model


def _two_pool_lb() -> LbHaproxy:
    return LbHaproxy(
        kind="haproxy",
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )


def _legacy_wildcard_lb() -> LbHaproxy:
    return LbHaproxy(
        kind="haproxy",
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )


def test_pool_for_model_exact_match() -> None:
    p = pool_for_model(_two_pool_lb(), "M/A")
    assert p.name == "a"


def test_pool_for_model_no_match_exits_3(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        pool_for_model(_two_pool_lb(), "M/Unknown")
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "M/Unknown" in err
    assert "M/A (a)" in err and "M/B (b)" in err


def test_pool_for_model_wildcard_fallback() -> None:
    p = pool_for_model(_legacy_wildcard_lb(), "M/Anything")
    assert p.name == "default"


def test_pool_for_endpoint_probes_v1_models(monkeypatch) -> None:
    """Mock httpx.get to return a known model id."""
    import httpx

    class _MockResp:
        def json(self) -> dict:
            return {"data": [{"id": "M/A"}]}

    def _mock_get(url: str, timeout: float = 3.0) -> _MockResp:
        assert url == "http://10.0.0.5:8000/v1/models"
        return _MockResp()

    monkeypatch.setattr(httpx, "get", _mock_get)
    p = pool_for_endpoint(_two_pool_lb(), "10.0.0.5:8000")
    assert p.name == "a"


def test_pool_for_endpoint_empty_data_exits_1(monkeypatch, capsys) -> None:
    import httpx

    class _MockResp:
        def json(self) -> dict:
            return {"data": []}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _MockResp())
    with pytest.raises(SystemExit) as exc:
        pool_for_endpoint(_two_pool_lb(), "10.0.0.5:8000")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "model not loaded" in err.lower()


def test_pool_for_endpoint_probe_failure_exits_3(monkeypatch, capsys) -> None:
    import httpx

    def _raise(*a, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _raise)
    with pytest.raises(SystemExit) as exc:
        pool_for_endpoint(_two_pool_lb(), "10.0.0.5:8000")
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "probe" in err.lower()
