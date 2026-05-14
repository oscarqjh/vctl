"""Scaling tests: B9/B10 (health probes, exit codes) + CLI tests (add/remove/drain/attach)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from tctl.workloads.haproxy import scaling as lb_scaling
from tctl.workloads.haproxy.errors import (
    BackendOpFailed,
    LbUnreachable,
    PoolNotFound,
    ReconcilerError,
)
from tctl.workloads.haproxy.manager import LbManager
from tctl.workloads.haproxy.state import BackendState

FIX = Path(__file__).parent.parent.parent / "fixtures"

_TCTL_CLI_AVAILABLE = bool(importlib.util.find_spec("tctl.cli"))
_skip_no_cli = pytest.mark.skipif(
    not _TCTL_CLI_AVAILABLE,
    reason="tctl.cli not built yet (available in Task 9)",
)


def _vctl(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "tctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "tctl_sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text(
        (FIX / "tctl_sample_profile.yaml").read_text()
    )
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path


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

    monkeypatch.setattr("tctl.workloads.haproxy.scaling.probe_vllm", fake_probe_vllm)
    monkeypatch.setattr("tctl.workloads.haproxy.probe.probe_vllm", fake_probe_vllm)

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

    monkeypatch.setattr("tctl.workloads.haproxy.scaling.probe_vllm", _fail_probe)

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

    monkeypatch.setattr("tctl.workloads.haproxy.scaling.probe_vllm", _ok_probe)

    rc = lb_scaling._do_health(mgr, bs)
    assert rc == 0


# ============================================================================
# From test_commands_lb_scaling.py
# ============================================================================


@_skip_no_cli
def test_lb_add_idempotent_first_then_dup(tmp_path: Path) -> None:
    """First call surfaces ADDED, second call surfaces READIED (idempotent re-add).

    Under TCTL_TEST_NO_SOCKET=1, _NoOpClient returns [] from show_servers_state, so
    Reconciler always sees in_haproxy=False. First call: in_state=False → ADDED.
    Second call: in_state=True (state-only) → READIED.
    """
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "TCTL_TEST_NO_SOCKET": "1",
    }

    p1 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p1.returncode == 0, p1.stderr
    assert "ADDED" in p1.stderr
    p2 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p2.returncode == 0, p2.stderr
    assert "READIED" in p2.stderr


@_skip_no_cli
def test_lb_remove_after_add(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "TCTL_TEST_NO_SOCKET": "1",
    }
    _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0


@_skip_no_cli
def test_lb_attach_refuses_when_model_not_loaded(tmp_path: Path) -> None:
    """AT-10: empty data array → exit 1, no state mutation."""
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "TCTL_TEST_NO_SOCKET": "1",
        "TCTL_TEST_PROBE_RESULT": "empty",
    }
    p = _vctl("lb", "attach", "8000", cwd=repo, env=env)
    assert p.returncode == 1
    assert "not loaded" in p.stderr.lower() or "empty" in p.stderr.lower()


# ---------------------------------------------------------------------------
# Multi-pool helpers + tests
# ---------------------------------------------------------------------------


def _make_two_pool_repo(tmp_path: Path) -> Path:
    """Cluster with two pools (a serves M/A, b serves M/B)."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: tctl/v1\nkind: Cluster\n"
        "cluster:\n  venv: /v\n  state_dir: /tmp/state\n  env: {}\n"
        "profile: a\n"
        "haproxy:\n"
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
            f"apiVersion: tctl/v1\nkind: Profile\n"
            f"model: {{ name: {m} }}\n"
            f"resources: {{ num_gpus: 1, cuda_visible_devices: '0' }}\n"
            f"parallelism: {{ data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }}\n"
            f"server: {{ http_port: 8000 }}\nvllm_args: {{}}\nenv: {{}}\n"
        )
    return tmp_path


@_skip_no_cli
def test_lb_add_with_explicit_pool_flag(tmp_path: Path) -> None:
    """`lb add 10.x.x.x:8000 --pool a` registers in pool a's state."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(state),
        "TCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "a", cwd=repo, env=env)
    assert p.returncode == 0, p.stderr
    assert "ADDED" in p.stderr
    assert (state / "10.0.0.1" / "a_backends.txt").read_text().strip() == "10.0.0.5:8000"
    assert (
        not (state / "10.0.0.1" / "b_backends.txt").exists()
        or not (state / "10.0.0.1" / "b_backends.txt").read_text().strip()
    )


@_skip_no_cli
def test_lb_add_unknown_pool_exits_3(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(state),
        "TCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "nonexistent", cwd=repo, env=env)
    assert p.returncode == 3
    assert "nonexistent" in p.stderr or "unknown pool" in p.stderr.lower()


@_skip_no_cli
def test_lb_remove_finds_pool_automatically(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "TCTL_CLUSTER__STATE_DIR": str(state),
        "TCTL_TEST_NO_SOCKET": "1",
    }
    # Add to pool a
    _vctl("lb", "add", "10.0.0.5:8000", "--pool", "a", cwd=repo, env=env)
    # Remove without --pool — should find it in pool a
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0
    assert (state / "10.0.0.1" / "a_backends.txt").read_text().strip() == ""


# ---------------------------------------------------------------------------
# Helpers shared by A4-A8 unit tests
# ---------------------------------------------------------------------------
# (helpers _single_pool_lb and _make_mgr defined above in the first section)


# ---------------------------------------------------------------------------
# A4: _do_drain returns 4 when LB admin socket unreachable
# ---------------------------------------------------------------------------


def test_do_drain_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: _do_drain must return 4 when LB admin socket unreachable."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)
    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 4


def test_do_drain_succeeds_when_cli_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 inverse: _do_drain returns 0 when LB admin socket reachable."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    cli = MagicMock()
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)
    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 0
    cli.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "drain")


# ---------------------------------------------------------------------------
# A5: _do_add propagates haproxy errors (does NOT swallow non-idempotent failures)
# ---------------------------------------------------------------------------


def test_do_add_propagates_haproxy_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A5: _do_add must return non-zero when haproxy add_server raises RuntimeError;
    state file is NOT written (haproxy-first invariant)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    cli = MagicMock()
    cli.show_servers_state.return_value = []  # haproxy empty
    cli.add_server.side_effect = RuntimeError("connection refused")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 1  # BackendOpFailed → exit 1
    # State file untouched (haproxy-first invariant; closes F11)
    assert "10.0.0.5:8000" not in bs.list()


def test_do_add_idempotent_already_present_is_readied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5 successor: when ep is already in haproxy, _do_add returns 0 with READIED/ADOPTED
    (Reconciler reads pre-state, never calls add_server, just re-readies)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    cli.add_server.assert_not_called()


def test_do_add_haproxy_runtime_error_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciler normalises all non-idempotency haproxy errors via BackendOpFailed → exit 1.

    Phase 2 BREAKING: legacy _do_add distinguished 'No such backend' (exit 3) from
    other errors (exit 1). Reconciler does not parse error strings — all RuntimeError
    paths become BackendOpFailed → exit 1. Operators inspect stderr for the haproxy
    error message instead of the exit code.
    """
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    cli = MagicMock()
    cli.show_servers_state.return_value = []
    cli.add_server.side_effect = RuntimeError("No such backend: pool_default")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 1
    assert "10.0.0.5:8000" not in bs.list()


# ---------------------------------------------------------------------------
# A6: _do_remove haproxy-first ordering
# ---------------------------------------------------------------------------


def test_do_add_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: _do_add must return 4 and NOT write the state file when LB unreachable.

    F11 regression guard — Phase 1 ordering invariant: state file is never written
    before haproxy ack. If the admin socket is unreachable, no haproxy ack is
    possible, so the state file must remain untouched.
    """
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 4
    assert "10.0.0.5:8000" not in bs.list()


def test_do_remove_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: _do_remove must return 4 and NOT mutate state when LB is unreachable."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 4
    assert "10.0.0.5:8000" in bs.list()


def test_do_remove_state_unchanged_when_set_maint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: state file must NOT be mutated if set_state maint fails."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    cli.set_state.side_effect = RuntimeError("haproxy reloading")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 1  # BackendOpFailed
    assert "10.0.0.5:8000" in bs.list()


def test_do_remove_state_unchanged_when_del_server_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: state file must NOT be mutated if remove_server fails after maint."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    cli.set_state.return_value = None  # maint succeeds
    cli.remove_server.side_effect = RuntimeError("Operation not permitted")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 1  # BackendOpFailed
    assert "10.0.0.5:8000" in bs.list()


def test_do_remove_happy_path_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A6: on success, set_state maint precedes remove_server precedes state-file cleanup."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    call_order: list[str] = []
    cli.set_state.side_effect = lambda *a, **kw: call_order.append("set_state")
    cli.remove_server.side_effect = lambda *a, **kw: call_order.append("remove_server")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    assert call_order == ["set_state", "remove_server"]
    assert "10.0.0.5:8000" not in bs.list()


# ---------------------------------------------------------------------------
# A7: _do_auto_add calls force-ready after add
# ---------------------------------------------------------------------------


def test_do_auto_add_calls_force_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A7: _do_auto_add via Reconciler.reconcile_from_state must call set_state ready."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = []  # haproxy empty → triggers add_server + set_state
    set_state_calls: list[tuple[object, ...]] = []
    cli.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, f"set_state 'ready' not called; calls: {set_state_calls}"


def test_do_auto_add_idempotent_when_haproxy_already_has_ep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciler-style: when ep already in haproxy, add_server is not called;
    set_state ready still runs to heal lingering drain/maint (READIED outcome)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    set_state_calls: list[tuple[object, ...]] = []
    cli.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    cli.add_server.assert_not_called()
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, "set_state 'ready' must still run for idempotent re-heal"


def test_do_auto_add_exits_1_on_per_pool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F12 fix: _do_auto_add no longer suppresses haproxy errors. If reconcile_from_state
    raises ReconcilerError for any pool, that pool is logged as failed and the function
    returns 1 (was: silent suppress, always 0)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.show_servers_state.return_value = []
    cli.add_server.side_effect = RuntimeError("haproxy refused")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 1


# ---------------------------------------------------------------------------
# A8: _do_remove_cli returns 1 when endpoint not found anywhere
# ---------------------------------------------------------------------------


def test_do_remove_cli_returns_1_when_not_found_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 1 when ep not in any state file and not in haproxy.
    Reconciler.want_absent returns Action.NONE on each pool; nothing logged; exit 1."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    cli = MagicMock()
    cli.show_servers_state.return_value = []  # haproxy empty
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 1


def test_do_remove_cli_returns_0_when_haproxy_orphan_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 0 when ep not in state file but haproxy still has it.
    Reconciler.want_absent returns Action.REMOVED for that pool (orphan cleanup)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    cli = MagicMock()
    cli.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2)
    ]
    cli.set_state.return_value = None
    cli.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 0


def test_do_remove_cli_exits_4_when_lb_unreachable_in_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.3.0 BREAKING: when ep not found in state AND LB is down, fallback loop's
    Reconciler.want_absent raises LbUnreachable → _exit_for → 4 (was: 1, not-found).
    """
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 4


# ---------------------------------------------------------------------------
# AC-7 LbUnreachable variant: _do_auto_add per-pool LbUnreachable → exit 1
# ---------------------------------------------------------------------------


def test_do_auto_add_exits_1_when_pool_reconcile_raises_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: per-pool LbUnreachable surfaces as failed-pool in stderr; exit 1."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 1


# ---------------------------------------------------------------------------
# AC-?: _do_detach LbUnreachable path (drain phase) → exit 4
# ---------------------------------------------------------------------------


def test_do_detach_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_do_detach must return 4 if LB is unreachable during the drain phase.
    State file is untouched (Reconciler haproxy-first invariant)."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    pbs = BackendState(state_dir, "10.0.0.1", pool="default")
    pbs.add("127.0.0.1:8000")

    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_detach(mgr, bs)
    assert rc == 4
    assert "127.0.0.1:8000" in pbs.list()


# ---------------------------------------------------------------------------
# Task 1: _exit_for helper — exit-code mapping
# ---------------------------------------------------------------------------


def test_exit_for_lb_unreachable_returns_4() -> None:
    exc = LbUnreachable(sock="/run/haproxy.sock", tcp="10.0.0.1:9001")
    assert lb_scaling._exit_for(exc) == 4


def test_exit_for_pool_not_found_returns_3() -> None:
    exc = PoolNotFound(requested="nonexistent", available=["default"])
    assert lb_scaling._exit_for(exc) == 3


def test_exit_for_backend_op_failed_returns_1() -> None:
    exc = BackendOpFailed(op="add_server", ep="10.0.0.5:8000", backend="pool_default")
    assert lb_scaling._exit_for(exc) == 1


def test_exit_for_arbitrary_reconciler_error_subclass_returns_1() -> None:
    class _UnknownError(ReconcilerError):
        pass

    exc = _UnknownError("unknown haproxy failure")
    assert lb_scaling._exit_for(exc) == 1


# ---------------------------------------------------------------------------
# v0.4.5: stale state-file pools (no longer in cluster.yaml) are skipped
# ---------------------------------------------------------------------------


def test_state_pools_in_config_skips_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v0.4.5: state-file pools not in mgr.lb.pools are skipped with stderr warning."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    BackendState(state_dir, "10.0.0.1", pool="default").add("10.0.0.99:9999")
    BackendState(state_dir, "10.0.0.1", pool="qwen3-5-9b").add("10.0.0.5:8000")

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        pools=[Pool(name="qwen3-5-9b", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="qwen3-5-9b")

    valid = lb_scaling._state_pools_in_config(mgr, bs)
    assert valid == ["qwen3-5-9b"]
    err = capsys.readouterr().err
    assert "stale" in err
    assert "default" in err


def test_state_pools_in_config_falls_back_to_configured_when_state_empty(
    tmp_path: Path,
) -> None:
    """If state file is empty, fall back to all configured pools."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        pools=[
            Pool(name="qwen3-5-9b", served_model="A", bind_port=8080),
            Pool(name="qwen3-vl-30b", served_model="B", bind_port=8081),
        ],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="qwen3-5-9b")

    assert sorted(lb_scaling._state_pools_in_config(mgr, bs)) == [
        "qwen3-5-9b",
        "qwen3-vl-30b",
    ]


def test_haproxy_scur_returns_int_for_known_server() -> None:
    """v0.4.7: _haproxy_scur parses scur from `show stat` CSV for the named server."""
    cli = MagicMock()
    cli._send.return_value = (
        "# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout\n"
        "pool_default,b_10_0_0_5_8000,0,0,7,10,256,12345,1024,2048\n"
        "pool_default,BACKEND,0,0,7,10,256,12345,1024,2048\n"
    )
    assert lb_scaling._haproxy_scur(cli, "pool_default", "b_10_0_0_5_8000") == 7


def test_haproxy_scur_returns_none_for_unknown_server() -> None:
    cli = MagicMock()
    cli._send.return_value = (
        "# pxname,svname,qcur,qmax,scur,smax,slim,stot\n"
        "pool_default,b_10_0_0_99_8000,0,0,3,10,256,1\n"
    )
    assert lb_scaling._haproxy_scur(cli, "pool_default", "b_10_0_0_5_8000") is None


def test_haproxy_scur_returns_none_on_send_error() -> None:
    cli = MagicMock()
    cli._send.side_effect = RuntimeError("socket closed")
    assert lb_scaling._haproxy_scur(cli, "pool_default", "b_x") is None


def test_do_detach_skips_stale_pool_no_longer_in_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.4.5 regression: lb detach must not exit 3 when state dir contains a stale pool."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    BackendState(state_dir, "10.0.0.1", pool="default").add("10.0.0.99:9999")

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        pools=[Pool(name="qwen3-5-9b", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="qwen3-5-9b")

    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "127.0.0.1")
    cli = MagicMock()
    cli.show_servers_state.return_value = []
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_detach(mgr, bs)
    assert rc == 0  # not 3 — stale 'default' was skipped, not validated


# ---------------------------------------------------------------------------
# v0.4.8: lb detach --force calls shutdown_sessions_server before remove
# ---------------------------------------------------------------------------


def test_do_detach_force_calls_shutdown_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.4.8: detach(force=True) calls shutdown_sessions_server right before
    want_absent so a backend stuck with half-open TCP can be removed."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus, RuntimeClient

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    BackendState(state_dir, "10.0.0.1", pool="default").add("127.0.0.1:8000")

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "127.0.0.1")

    cli = MagicMock(spec=RuntimeClient)
    cli.show_servers_state.return_value = [
        BackendStatus(
            name="b_127_0_0_1_8000",
            endpoint="127.0.0.1:8000",
            op_state=2,
            backend="pool_default",
        )
    ]
    # Drain wait: claim vllm idle so loop exits immediately
    monkeypatch.setattr(
        "tctl.workloads.haproxy.probe.probe_local_vllm",
        lambda port: {"num_requests_running": 0.0},
    )
    # haproxy reports scur=0 immediately
    cli._send.return_value = "# pxname,svname,qcur,qmax,scur\npool_default,b_127_0_0_1_8000,0,0,0\n"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_detach(mgr, bs, force=True)
    assert rc == 0
    cli.shutdown_sessions_server.assert_called_once_with("pool_default", "b_127_0_0_1_8000")


def test_do_detach_no_force_does_not_call_shutdown_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --force, shutdown_sessions_server is never invoked."""
    import tctl.workloads.haproxy.reconciler as reconciler_mod
    from tctl.workloads.haproxy.runtime import BackendStatus, RuntimeClient

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    BackendState(state_dir, "10.0.0.1", pool="default").add("127.0.0.1:8000")

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "127.0.0.1")
    cli = MagicMock(spec=RuntimeClient)
    cli.show_servers_state.return_value = [
        BackendStatus(
            name="b_127_0_0_1_8000",
            endpoint="127.0.0.1:8000",
            op_state=2,
            backend="pool_default",
        )
    ]
    monkeypatch.setattr(
        "tctl.workloads.haproxy.probe.probe_local_vllm",
        lambda port: {"num_requests_running": 0.0},
    )
    cli._send.return_value = "# pxname,svname,qcur,qmax,scur\npool_default,b_127_0_0_1_8000,0,0,0\n"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_detach(mgr, bs)  # no force
    assert rc == 0
    cli.shutdown_sessions_server.assert_not_called()
