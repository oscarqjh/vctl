"""LB scaling commands — add (idempotent), remove, drain, attach, detach, auto-add."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.commands import lb_scaling
from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState

FIX = Path(__file__).parent / "fixtures"


def _vctl(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path


def test_lb_add_idempotent_first_then_dup(tmp_path: Path) -> None:
    """First call surfaces ADDED, second call surfaces READIED (idempotent re-add).

    Under VCTL_TEST_NO_SOCKET=1, _NoOpClient returns [] from show_servers_state, so
    Reconciler always sees in_haproxy=False. First call: in_state=False → ADDED.
    Second call: in_state=True (state-only) → READIED.
    """
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }

    p1 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p1.returncode == 0, p1.stderr
    assert "ADDED" in p1.stderr
    p2 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p2.returncode == 0, p2.stderr
    assert "READIED" in p2.stderr


def test_lb_remove_after_add(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0


def test_lb_attach_refuses_when_model_not_loaded(tmp_path: Path) -> None:
    """AT-10: empty data array → exit 1, no state mutation."""
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
        "VCTL_TEST_PROBE_RESULT": "empty",
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
            f"model: {{ name: {m} }}\n"
            f"resources: {{ num_gpus: 1, cuda_visible_devices: '0' }}\n"
            f"parallelism: {{ data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }}\n"
            f"server: {{ http_port: 8000 }}\nvllm_args: {{}}\nenv: {{}}\n"
        )
    return tmp_path


def test_lb_add_with_explicit_pool_flag(tmp_path: Path) -> None:
    """`lb add 10.x.x.x:8000 --pool a` registers in pool a's state."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "a", cwd=repo, env=env)
    assert p.returncode == 0, p.stderr
    assert "ADDED" in p.stderr
    assert (state / "10.0.0.1" / "a_backends.txt").read_text().strip() == "10.0.0.5:8000"
    assert (
        not (state / "10.0.0.1" / "b_backends.txt").exists()
        or not (state / "10.0.0.1" / "b_backends.txt").read_text().strip()
    )


def test_lb_add_unknown_pool_exits_3(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "nonexistent", cwd=repo, env=env)
    assert p.returncode == 3
    assert "nonexistent" in p.stderr or "unknown pool" in p.stderr.lower()


def test_lb_remove_finds_pool_automatically(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
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


def _single_pool_lb() -> LbHaproxy:
    return LbHaproxy(
        host="10.0.0.1",
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


# ---------------------------------------------------------------------------
# A4: _do_drain returns 4 when LB admin socket unreachable
# ---------------------------------------------------------------------------


def test_do_drain_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: _do_drain must return 4 when LB admin socket unreachable."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)
    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 4


def test_do_drain_succeeds_when_cli_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 inverse: _do_drain returns 0 when LB admin socket reachable."""
    import vctl.lb.reconciler as reconciler_mod

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
    import vctl.lb.reconciler as reconciler_mod

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
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.runtime import BackendStatus

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
    import vctl.lb.reconciler as reconciler_mod

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


def test_do_remove_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: _do_remove must return 4 and NOT mutate state when LB is unreachable."""
    import vctl.lb.reconciler as reconciler_mod

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
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.runtime import BackendStatus

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
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.runtime import BackendStatus

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
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.runtime import BackendStatus

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
    """A7: _do_auto_add must call set_state ready after add_server."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    set_state_calls: list[tuple[object, ...]] = []
    cli.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    # Must have called set_state with "ready"
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, f"set_state 'ready' not called; calls: {set_state_calls}"


def test_do_auto_add_force_ready_called_even_when_add_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: force-ready must be attempted even when add_server raises."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    cli = MagicMock()
    cli.add_server.side_effect = Exception("already exists")
    set_state_calls: list[tuple[object, ...]] = []
    cli.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, "force-ready must be attempted after add_server failure"


# ---------------------------------------------------------------------------
# A8: _do_remove_cli returns 1 when endpoint not found anywhere
# ---------------------------------------------------------------------------


def test_do_remove_cli_returns_1_when_not_found_and_haproxy_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 1 when endpoint not in any state file and haproxy
    also reports 'no such server'."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    # Do NOT add the endpoint — state file is empty

    cli = MagicMock()
    cli.set_state.side_effect = Exception("No such server")
    cli.remove_server.side_effect = Exception("No such server")
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 1


def test_do_remove_cli_returns_0_when_haproxy_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 0 when ep not in state file but haproxy removal
    succeeds (stale entry cleanup)."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    # Pre-create the state file (empty) so list_pools finds "default"
    bs.add("10.0.0.99:9999")  # different ep so our target is absent
    bs.remove("10.0.0.99:9999")

    cli = MagicMock()
    cli.set_state.return_value = None
    cli.remove_server.return_value = None
    monkeypatch.setattr(lb_scaling, "_client", lambda m: cli)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 0


def test_do_remove_cli_returns_1_no_socket_and_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 1 when endpoint not found in state AND socket is down."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    # No endpoint registered

    monkeypatch.setattr(lb_scaling, "_client", lambda m: None)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    assert rc == 1


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
