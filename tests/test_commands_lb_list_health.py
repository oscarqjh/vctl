"""Tests for lb list/health/wait-ready per-pool grouping (Task 8 v0.2)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vctl.commands import lb as lb_cmd
from vctl.commands import lb_scaling
from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState

FIX = Path(__file__).parent / "fixtures"


def _vctl(
    *args: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 10
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)


def _make_two_pool_repo(tmp_path: Path) -> Path:
    """Cluster with two pools (a serves M/A, b serves M/B)."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster:\n  venv: /v\n  state_dir: /tmp/state\n  env: {}\n"
        "profile: a\n"
        "lb:\n"
        "  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: { bind_port: 9001 }\n"
        "  stats: { bind_port: 9000 }\n"
        "  algorithm: leastconn\n"
        "  health: { path: /health, check_interval: 5s, fall: 3, rise: 2 }\n"
        "  defaults: { maxconn_per_backend: 256, slowstart: 30s, "
        "timeout_connect: 5s, timeout_client: 1h, timeout_server: 1h }\n"
        "  pools:\n"
        "    - { name: a, served_model: M/A, bind_port: 8080 }\n"
        "    - { name: b, served_model: M/B, bind_port: 8081 }\n"
    )
    (tmp_path / "models").mkdir()
    for n, m in [("a", "M/A"), ("b", "M/B")]:
        (tmp_path / "models" / f"{n}.yaml").write_text(
            f"apiVersion: vctl/v1\nkind: Profile\n"
            f"model: {{ name: {m}, served_as: {n} }}\n"
            f"resources: {{ num_gpus: 1, cuda_visible_devices: '0' }}\n"
            f"parallelism: {{ data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }}\n"
            f"server: {{ http_port: 8000 }}\nvllm_args: {{}}\nenv: {{}}\n"
        )
    return tmp_path


# ---------------------------------------------------------------------------
# lb list — grouped by pool
# ---------------------------------------------------------------------------


def test_lb_list_groups_by_pool(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    BackendState(state, "10.0.0.1", pool="a").add("10.0.0.5:8000")
    BackendState(state, "10.0.0.1", pool="b").add("10.0.0.7:8000")
    proc = _vctl(
        "lb",
        "list",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
        },
    )
    assert proc.returncode == 0
    out = proc.stdout
    assert "pool: a" in out
    assert "pool: b" in out
    assert "10.0.0.5:8000" in out
    assert "10.0.0.7:8000" in out


def test_lb_list_empty_pools(tmp_path: Path) -> None:
    """No registered backends → exit 0, show (no backends) placeholder."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    proc = _vctl(
        "lb",
        "list",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
        },
    )
    assert proc.returncode == 0
    out = proc.stdout
    # Each pool still shown
    assert "pool: a" in out
    assert "pool: b" in out
    assert "(no backends)" in out


# ---------------------------------------------------------------------------
# lb health — grouped by pool
# ---------------------------------------------------------------------------


def test_lb_health_groups_by_pool(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    # Register a backend on port 1 — nothing listens there, probe will fail
    BackendState(state, "10.0.0.1", pool="a").add("127.0.0.1:1")
    proc = _vctl(
        "lb",
        "health",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
        },
    )
    # Probe on port 1 will fail → unhealthy count > 0 → exit 1
    assert proc.returncode == 1
    assert "pool: a" in proc.stdout
    assert "FAIL" in proc.stdout


def test_lb_health_no_backends_ok(tmp_path: Path) -> None:
    """No backends in any pool → all healthy (nothing to fail)."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    proc = _vctl(
        "lb",
        "health",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
        },
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# lb wait-ready — pool filter + timeout
# ---------------------------------------------------------------------------


def test_lb_wait_ready_pool_filter_unknown(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    proc = _vctl(
        "lb",
        "wait-ready",
        "1",
        "--pool",
        "nonexistent",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
            "LB_WAIT_TIMEOUT": "1",
        },
        timeout=5,
    )
    assert proc.returncode == 3
    assert "nonexistent" in proc.stderr


def test_lb_wait_ready_no_backends_times_out(tmp_path: Path) -> None:
    """No registered backends → wait-ready blocks then times out."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    proc = _vctl(
        "lb",
        "wait-ready",
        "1",
        cwd=repo,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
            "LB_WAIT_TIMEOUT": "1",
        },
        timeout=5,
    )
    # No backends in any pool — nothing is "all_ok and any_pool_has_backends"
    # so it loops until timeout.  C8: environment timeout → exit 4.
    assert proc.returncode == 4


# ---------------------------------------------------------------------------
# F6 helpers
# ---------------------------------------------------------------------------


def _make_f6_mgr(tmp_path: Path) -> tuple[LbManager, BackendState]:
    """Single-pool LbManager for F6 unit tests."""
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="M/Default", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    return mgr, bs


def _capture_do_list(
    mgr: LbManager, bs: BackendState, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[int, str]:
    """Run _do_list and capture its stdout; return (rc, captured_text)."""
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    rc = lb_cmd._do_list(mgr, bs)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# F6 tests
# ---------------------------------------------------------------------------


def test_list_marks_live_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: state file has 2 endpoints; admin socket reports both → ✓ live."""
    mgr, bs = _make_f6_mgr(tmp_path)
    bs.add("10.1.2.5:8000")
    bs.add("10.1.2.6:8000")

    # LB is running.
    monkeypatch.setattr(mgr, "status", lambda: {"running": True})

    # Fake live registry: both endpoints present in haproxy.
    registry = {"pool_default": {"10.1.2.5:8000", "10.1.2.6:8000"}}
    monkeypatch.setattr(lb_cmd, "_build_live_registry", lambda cli: registry)

    fake_cli = MagicMock()
    monkeypatch.setattr(lb_scaling, "_client", lambda m: fake_cli)

    rc, out = _capture_do_list(mgr, bs, monkeypatch)

    assert rc == 0
    assert "10.1.2.5:8000" in out
    assert "10.1.2.6:8000" in out
    assert "✓ live" in out
    assert "tracked-only" not in out
    assert "untracked" not in out


def test_list_marks_tracked_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: state file has endpoint A; admin socket reports nothing → ⚠ tracked-only."""
    mgr, bs = _make_f6_mgr(tmp_path)
    bs.add("10.1.2.5:8000")

    monkeypatch.setattr(mgr, "status", lambda: {"running": True})

    # Live registry is empty — endpoint not in haproxy.
    registry: dict[str, set[str]] = {}
    monkeypatch.setattr(lb_cmd, "_build_live_registry", lambda cli: registry)

    fake_cli = MagicMock()
    monkeypatch.setattr(lb_scaling, "_client", lambda m: fake_cli)

    rc, out = _capture_do_list(mgr, bs, monkeypatch)

    assert rc == 0
    assert "10.1.2.5:8000" in out
    assert "tracked-only" in out
    assert "⚠" in out
    assert "✓ live" not in out


def test_list_marks_untracked_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: state file empty; admin socket reports endpoint B → ⚠ untracked."""
    mgr, bs = _make_f6_mgr(tmp_path)
    # No entries in state file.

    monkeypatch.setattr(mgr, "status", lambda: {"running": True})

    # Live registry has an endpoint not in state file.
    registry = {"pool_default": {"10.1.2.7:8000"}}
    monkeypatch.setattr(lb_cmd, "_build_live_registry", lambda cli: registry)

    fake_cli = MagicMock()
    monkeypatch.setattr(lb_scaling, "_client", lambda m: fake_cli)

    rc, out = _capture_do_list(mgr, bs, monkeypatch)

    assert rc == 0
    assert "10.1.2.7:8000" in out
    assert "untracked" in out
    assert "⚠" in out
    # Adoption hint present.
    assert "vctl lb add 10.1.2.7:8000" in out


def test_list_admin_unreachable_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: _client → None → WARNING line, no crash, returns 0."""
    mgr, bs = _make_f6_mgr(tmp_path)
    bs.add("10.1.2.5:8000")

    monkeypatch.setattr(mgr, "status", lambda: {"running": True})
    # Admin socket unreachable.
    monkeypatch.setattr(lb_scaling, "_client", lambda m: None)

    rc, out = _capture_do_list(mgr, bs, monkeypatch)

    assert rc == 0
    assert "WARNING" in out
    assert "10.1.2.5:8000" in out


def test_list_lb_stopped_skips_live_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: LB stopped → [LB STOPPED] banner, no admin socket call attempted."""
    mgr, bs = _make_f6_mgr(tmp_path)
    bs.add("10.1.2.5:8000")

    monkeypatch.setattr(mgr, "status", lambda: {"running": False})

    # Spy on _client — must not be called when LB is stopped.
    spy = MagicMock(return_value=None)
    monkeypatch.setattr(lb_scaling, "_client", spy)

    rc, out = _capture_do_list(mgr, bs, monkeypatch)

    assert rc == 0
    assert "[LB STOPPED]" in out
    assert "10.1.2.5:8000" in out
    # _client must NOT have been called.
    spy.assert_not_called()
