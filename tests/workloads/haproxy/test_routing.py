"""Routing helpers tests."""

from __future__ import annotations

import pytest

from tctl.config.models import (
    LbAdmin,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
    Pool,
)
from tctl.workloads.haproxy.routing import _name_for, pool_for_endpoint, pool_for_model


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


def test_name_for_derives_server_name() -> None:
    assert _name_for("10.0.0.5:8000") == "b_10_0_0_5_8000"


def test_name_for_importable_from_routing() -> None:
    # Verify the symbol is importable from routing (not just lb_scaling)
    from tctl.workloads.haproxy.routing import _name_for as nf

    assert nf("192.168.1.10:9000") == "b_192_168_1_10_9000"


@pytest.mark.parametrize(
    "bad_ep",
    [
        "",  # empty
        "10.0.0.5",  # no port
        "10.0.0.5:",  # empty port
        ":8000",  # no host
        "10.0.0.5:8000\nadd server pool/x 1.1.1.1:1 check",  # admin-socket injection
        "10.0.0.5:8000 ; rm -rf /",  # shell-style injection
        "10.0.0.5:8000/foo",  # slash injection (haproxy backend/name separator)
        "host.example.com:8000",  # hostname not allowed (IPv4 only)
        "10.0.0.5:8000:9000",  # extra colon
        "10.0.0.5:8000.5",  # non-integer port
    ],
)
def test_name_for_rejects_malformed_endpoint(bad_ep: str) -> None:
    """v0.4.1: _name_for raises ValueError on anything but well-formed IPv4:port.

    Hardening against haproxy admin-socket command injection if a malicious
    or malformed ep slips through (e.g. via state-file manipulation).
    """
    with pytest.raises(ValueError, match="invalid endpoint"):
        _name_for(bad_ep)


# ---------------------------------------------------------------------------
# v0.4.3: resolve_pool_ref — accepts pool name OR bind_port (digits-only)
# ---------------------------------------------------------------------------


def test_resolve_pool_ref_by_name() -> None:
    from tctl.workloads.haproxy.routing import resolve_pool_ref

    lb = _two_pool_lb()  # pools: a (8080), b (8081)
    assert resolve_pool_ref(lb, "a").name == "a"
    assert resolve_pool_ref(lb, "b").name == "b"


def test_resolve_pool_ref_by_port() -> None:
    from tctl.workloads.haproxy.routing import resolve_pool_ref

    lb = _two_pool_lb()
    assert resolve_pool_ref(lb, "8080").name == "a"
    assert resolve_pool_ref(lb, "8081").name == "b"


def test_resolve_pool_ref_unknown_name_raises() -> None:
    from tctl.workloads.haproxy.routing import resolve_pool_ref

    lb = _two_pool_lb()
    with pytest.raises(ValueError, match="unknown pool 'nonexistent'"):
        resolve_pool_ref(lb, "nonexistent")


def test_resolve_pool_ref_unknown_port_raises() -> None:
    from tctl.workloads.haproxy.routing import resolve_pool_ref

    lb = _two_pool_lb()
    with pytest.raises(ValueError, match="no pool with bind_port=9999"):
        resolve_pool_ref(lb, "9999")
