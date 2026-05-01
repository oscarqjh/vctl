"""Commit-B tests: B9 (health probes actual host), B10 (exit code)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vctl.commands import lb_scaling
from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState


def _single_pool_lb(host: str = "10.0.0.1") -> LbHaproxy:
    return LbHaproxy(
        host=host,
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )


def _make_mgr(tmp_path: Path, lb: LbHaproxy | None = None) -> LbManager:
    if lb is None:
        lb = _single_pool_lb()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


def _ok_probe(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    return {
        "healthy": True,
        "health_code": 200,
        "models_loaded": True,
        "num_requests_running": 0.0,
    }


def _fail_probe(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
    return {
        "healthy": False,
        "health_code": 503,
        "models_loaded": False,
        "num_requests_running": 0.0,
    }


# ---------------------------------------------------------------------------
# B9: _do_health probes actual backend host, not localhost
# ---------------------------------------------------------------------------


def test_do_health_probes_backend_host_not_localhost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B9: probe_vllm must be called with the backend's actual host, not 'localhost'."""
    mgr = _make_mgr(tmp_path, lb=_single_pool_lb("10.0.0.1"))
    state_dir = tmp_path / "state"
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.5.6.7:8000")

    probe_calls: list[tuple[str, int]] = []

    def fake_probe_vllm(host: str, port: int, timeout: float = 2.0) -> dict[str, Any]:
        probe_calls.append((host, port))
        return _ok_probe(host, port)

    monkeypatch.setattr("vctl.commands.lb_scaling.probe_vllm", fake_probe_vllm)
    monkeypatch.setattr("vctl.lb.probe.probe_vllm", fake_probe_vllm)

    lb_scaling._do_health(mgr, bs)

    assert probe_calls, "probe_vllm must be called"
    host_used, port_used = probe_calls[0]
    assert host_used == "10.5.6.7", (
        f"probe_vllm must use backend host '10.5.6.7', got '{host_used}'"
    )
    assert port_used == 8000
    assert host_used != "localhost", "must NOT probe localhost"


# ---------------------------------------------------------------------------
# B10: exit code is 1 (not unhealthy count) when backends are unhealthy
# ---------------------------------------------------------------------------


def test_do_health_returns_1_not_count_when_unhealthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B10: 3 unhealthy backends → exit 1, not 3."""
    mgr = _make_mgr(tmp_path)
    state_dir = tmp_path / "state"
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.2:8000")
    bs.add("10.0.0.3:8000")
    bs.add("10.0.0.4:8000")

    monkeypatch.setattr("vctl.commands.lb_scaling.probe_vllm", _fail_probe)

    rc = lb_scaling._do_health(mgr, bs)
    assert rc == 1, f"exit code must be 1 when unhealthy, got {rc}"
    assert rc != 3, "exit code must NOT be the unhealthy count"


def test_do_health_returns_0_when_all_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B10: all healthy → exit 0."""
    mgr = _make_mgr(tmp_path)
    state_dir = tmp_path / "state"
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.2:8000")

    monkeypatch.setattr("vctl.commands.lb_scaling.probe_vllm", _ok_probe)

    rc = lb_scaling._do_health(mgr, bs)
    assert rc == 0
