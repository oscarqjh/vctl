"""`vctl stop` drains + removes self + reaps tree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.commands import lb_scaling
from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState

FIX = Path(__file__).parent / "fixtures"


def test_stop_no_running_serve_is_noop(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (repo / "models").mkdir()
    (repo / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "stop"],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# A1+A2: multi-pool stop — unit tests against _do_drain / _do_remove mocks
# ---------------------------------------------------------------------------


def _make_two_pool_lb(state_dir: Path) -> tuple[LbManager, LbHaproxy]:
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )
    run_dir = state_dir / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    return mgr, lb


def test_stop_drains_and_removes_from_every_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1+A2: stop must iterate ALL pools, drain+remove every matching endpoint."""
    state_dir = tmp_path / "state"
    mgr, lb = _make_two_pool_lb(state_dir)
    self_ip = "10.0.0.2"

    # Pre-populate two pools — one endpoint each.
    ep_a = f"{self_ip}:8000"
    ep_b = f"{self_ip}:8001"
    bs_a = BackendState(state_dir, lb.host, pool="a")
    bs_b = BackendState(state_dir, lb.host, pool="b")
    bs_a.add(ep_a)
    bs_b.add(ep_b)

    drained: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []

    def fake_drain(ep: str, _mgr: LbManager, pool_name: str | None = None) -> int:
        drained.append((ep, pool_name or ""))
        return 0

    def fake_remove(
        ep: str, _mgr: LbManager, _bs: BackendState, pool_name: str | None = None
    ) -> int:
        removed.append((ep, pool_name or ""))
        return 0

    monkeypatch.setattr(lb_scaling, "_do_drain", fake_drain)
    monkeypatch.setattr(lb_scaling, "_do_remove", fake_remove)
    monkeypatch.setattr("vctl.commands.stop.detect_self_ip", lambda: self_ip)
    monkeypatch.setattr("vctl.commands.stop._find_local_vllm", lambda port: [])
    monkeypatch.setattr("vctl.commands.stop._wait_for_idle", lambda port, timeout: None)

    from vctl.commands import stop

    # Construct a minimal rc object.
    rc = MagicMock()
    rc.lb = lb
    rc.cluster.state_dir = str(state_dir)
    rc.server.http_port = 8000

    import argparse

    ns = argparse.Namespace(config=None, profile=None)
    monkeypatch.setattr("vctl.commands.stop.resolve", lambda *a, **kw: rc)
    monkeypatch.setattr(
        "vctl.commands.stop.LbManager",
        lambda *a, **kw: mgr,
    )

    result = stop.run(ns, [])
    assert result == 0
    # Both endpoints drained and removed
    assert ("10.0.0.2:8000", "a") in drained
    assert ("10.0.0.2:8001", "b") in drained
    assert ("10.0.0.2:8000", "a") in removed
    assert ("10.0.0.2:8001", "b") in removed


def test_stop_kills_pid_even_when_drain_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1+A2: stop must kill vllm PID even when drain returns non-zero."""
    state_dir = tmp_path / "state"
    mgr, lb = _make_two_pool_lb(state_dir)
    self_ip = "10.0.0.2"
    ep_a = f"{self_ip}:8000"
    bs_a = BackendState(state_dir, lb.host, pool="a")
    bs_a.add(ep_a)

    killed_pids: list[int] = []

    monkeypatch.setattr(lb_scaling, "_do_drain", lambda ep, m, pool_name=None: 4)  # LB down
    monkeypatch.setattr(
        lb_scaling, "_do_remove", lambda ep, m, bs, pool_name=None: 0
    )
    monkeypatch.setattr("vctl.commands.stop.detect_self_ip", lambda: self_ip)
    monkeypatch.setattr("vctl.commands.stop._find_local_vllm", lambda port: [999])
    monkeypatch.setattr("vctl.commands.stop._wait_for_idle", lambda port, timeout: None)
    monkeypatch.setattr("vctl.commands.stop._kill_tree", lambda pid, **kw: killed_pids.append(pid))

    from vctl.commands import stop
    import argparse

    rc = MagicMock()
    rc.lb = lb
    rc.cluster.state_dir = str(state_dir)
    rc.server.http_port = 8000

    ns = argparse.Namespace(config=None, profile=None)
    monkeypatch.setattr("vctl.commands.stop.resolve", lambda *a, **kw: rc)
    monkeypatch.setattr("vctl.commands.stop.LbManager", lambda *a, **kw: mgr)

    result = stop.run(ns, [])
    # drain failed → exit non-zero
    assert result != 0
    # but PID was still killed
    assert 999 in killed_pids


def test_stop_empty_pools_just_kills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: when list_pools returns [] stop skips detach and only kills PIDs."""
    state_dir = tmp_path / "state"
    mgr, lb = _make_two_pool_lb(state_dir)
    self_ip = "10.0.0.2"

    killed_pids: list[int] = []
    drain_calls: list[str] = []

    monkeypatch.setattr(lb_scaling, "_do_drain", lambda ep, m, pool_name=None: drain_calls.append(ep) or 0)
    monkeypatch.setattr("vctl.commands.stop.detect_self_ip", lambda: self_ip)
    monkeypatch.setattr("vctl.commands.stop._find_local_vllm", lambda port: [777])
    monkeypatch.setattr("vctl.commands.stop._wait_for_idle", lambda port, timeout: None)
    monkeypatch.setattr("vctl.commands.stop._kill_tree", lambda pid, **kw: killed_pids.append(pid))

    from vctl.commands import stop
    import argparse

    rc = MagicMock()
    rc.lb = lb
    rc.cluster.state_dir = str(state_dir)
    rc.server.http_port = 8000

    ns = argparse.Namespace(config=None, profile=None)
    monkeypatch.setattr("vctl.commands.stop.resolve", lambda *a, **kw: rc)
    monkeypatch.setattr("vctl.commands.stop.LbManager", lambda *a, **kw: mgr)

    result = stop.run(ns, [])
    assert result == 0
    # No drain calls (no pools registered)
    assert drain_calls == []
    assert 777 in killed_pids
