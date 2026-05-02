"""Unit tests for vctl.lb.reconciler — all RuntimeClient calls are mocked."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbHaproxy, LbStats, Pool
from vctl.lb.errors import PoolNotFound
from vctl.lb.manager import LbManager
from vctl.lb.reconciler import Action, Drift, Outcome, Reconciler
from vctl.lb.runtime import BackendStatus
from vctl.lb.state import BackendState


def _concurrent_want_present_worker(
    args: tuple[str, str, str, str],
) -> tuple[str, str]:
    """Module-level worker for spawn-based multiprocessing pickling.

    Sets VCTL_TEST_NO_SOCKET=1 in the spawned process so haproxy admin
    becomes a no-op (parent's monkeypatch does not propagate to spawned
    children). Returns (ep, action.value) for the caller to assert on.
    """
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"

    state_dir, run_dir, lb_host, ep = args

    pools = [Pool(name="default", served_model="*", bind_port=8100)]
    lb = LbHaproxy(
        host=lb_host,
        admin=LbAdmin(bind_port=9999),
        stats=LbStats(bind_port=8404),
        pools=pools,
    )
    mgr = LbManager(lb=lb, state_dir=Path(state_dir), run_dir=Path(run_dir))
    r = Reconciler(mgr)
    outcome = r.want_present(ep, "default")
    return (outcome.ep, outcome.action.value)


def _make_mgr(tmp_path: Path, pool_names: list[str] | None = None) -> LbManager:
    """Build a real LbManager backed by tmp_path with one or more pools."""
    names = pool_names or ["default"]
    pools = [
        Pool(name=n, served_model="*" if i == 0 else f"model-{n}", bind_port=8100 + i)
        for i, n in enumerate(names)
    ]
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9999),
        stats=LbStats(bind_port=8404),
        pools=pools,
    )
    return LbManager(lb=lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_reconciler_constructs_with_mgr(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path)
    r = Reconciler(mgr)
    assert r.mgr is mgr


def test_outcome_is_frozen() -> None:
    o = Outcome(ep="10.0.0.5:8000", pool="default", action=Action.ADDED)
    with pytest.raises((AttributeError, TypeError)):
        o.ep = "changed"  # type: ignore[misc]


def test_drift_is_frozen() -> None:
    d = Drift(
        pool="default",
        lb_reachable=True,
        only_in_state=[],
        only_in_haproxy=[],
        in_both=[],
        statuses={},
    )
    with pytest.raises((AttributeError, TypeError)):
        d.pool = "other"  # type: ignore[misc]


def test_drift_lb_unreachable_default_state() -> None:
    d = Drift(
        pool="default",
        lb_reachable=False,
        only_in_state=["10.0.0.5:8000"],
        only_in_haproxy=[],
        in_both=[],
        statuses={},
    )
    assert d.lb_reachable is False
    assert d.only_in_haproxy == []
    assert d.in_both == []
    assert d.statuses == {}
    assert d.only_in_state == ["10.0.0.5:8000"]


def test_action_enum_has_expected_values() -> None:
    assert Action.NONE.value == "none"
    assert Action.ADDED.value == "added"
    assert Action.REMOVED.value == "removed"
    assert Action.DRAINED.value == "drained"
    assert Action.READIED.value == "readied"
    assert Action.ADOPTED.value == "adopted"
    assert Action.ORPHANED_CLEANED.value == "orphaned_cleaned"


# ---------------------------------------------------------------------------
# Task 5: _validate_pool + _haproxy_servers
# ---------------------------------------------------------------------------


def test_validate_pool_accepts_known_pool(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, pool_names=["default"])
    r = Reconciler(mgr)
    r._validate_pool("default")  # must not raise


def test_validate_pool_raises_on_unknown(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, pool_names=["default"])
    r = Reconciler(mgr)
    with pytest.raises(PoolNotFound) as exc_info:
        r._validate_pool("nonexistent")
    assert "nonexistent" in str(exc_info.value)
    assert "default" in str(exc_info.value)


def test_haproxy_servers_returns_dict_keyed_by_endpoint(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, pool_names=["default"])
    r = Reconciler(mgr)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
        BackendStatus(name="b_10_0_0_6_8000", endpoint="10.0.0.6:8000", op_state=2),
    ]
    result = r._haproxy_servers("pool_default", mock_client)
    assert set(result.keys()) == {"10.0.0.5:8000", "10.0.0.6:8000"}
    assert result["10.0.0.5:8000"].name == "b_10_0_0_5_8000"


def test_haproxy_servers_empty_when_no_rows(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, pool_names=["default"])
    r = Reconciler(mgr)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    assert r._haproxy_servers("pool_default", mock_client) == {}


# ---------------------------------------------------------------------------
# Task 6: diff(pool) and diff_all()
# ---------------------------------------------------------------------------


def test_diff_returns_drift_with_lb_unreachable_when_socket_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: diff returns Drift(lb_reachable=False) and state membership populated."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    bs.add("10.0.0.5:8000")
    bs.add("10.0.0.6:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    drift = r.diff("default")

    assert drift.lb_reachable is False
    assert drift.only_in_state == ["10.0.0.5:8000", "10.0.0.6:8000"]
    assert drift.only_in_haproxy == []
    assert drift.in_both == []
    assert drift.statuses == {}


def test_diff_with_running_lb_classifies_eps_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eps in state only, haproxy only, and in both are classified correctly."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    bs.add("10.0.0.5:8000")  # will be in_both
    bs.add("10.0.0.6:8000")  # only_in_state

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
        BackendStatus(name="b_10_0_0_7_8000", endpoint="10.0.0.7:8000", op_state=2),
    ]
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    drift = r.diff("default")

    assert drift.lb_reachable is True
    assert drift.only_in_state == ["10.0.0.6:8000"]
    assert drift.only_in_haproxy == ["10.0.0.7:8000"]
    assert drift.in_both == ["10.0.0.5:8000"]
    assert "10.0.0.5:8000" in drift.statuses
    assert "10.0.0.7:8000" in drift.statuses
    assert "10.0.0.6:8000" not in drift.statuses


def test_diff_raises_pool_not_found_for_unknown_pool(tmp_path: Path) -> None:
    mgr = _make_mgr(tmp_path, pool_names=["default"])
    r = Reconciler(mgr)
    with pytest.raises(PoolNotFound):
        r.diff("nonexistent")


def test_diff_all_returns_one_drift_per_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path, pool_names=["default", "gpu"])
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    drifts = r.diff_all()

    assert len(drifts) == 2
    pool_names = {d.pool for d in drifts}
    assert pool_names == {"default", "gpu"}
    assert all(d.lb_reachable is False for d in drifts)


# ---------------------------------------------------------------------------
# Task 7: want_present(ep, pool) — full 4-case action mapping
# ---------------------------------------------------------------------------


def test_want_present_registers_ep_in_haproxy_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test: want_present ADDED path — registers in both haproxy and state file."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []  # haproxy empty
    mock_client.add_server.return_value = "new"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_present("10.0.0.5:8000", "default")

    mock_client.add_server.assert_called_once_with(
        "pool_default", "b_10_0_0_5_8000", "10.0.0.5:8000"
    )
    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "ready")
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" in bs.list()
    assert outcome.action == Action.ADDED
    assert outcome.ep == "10.0.0.5:8000"
    assert outcome.pool == "default"


def test_want_present_raises_lb_unreachable_when_socket_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test: want_present raises LbUnreachable and leaves state untouched."""
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import LbUnreachable

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    with pytest.raises(LbUnreachable):
        r.want_present("10.0.0.5:8000", "default")

    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert bs.list() == []


def test_want_present_adopts_orphaned_haproxy_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """in_haproxy=True, in_state=False → Action.ADOPTED; add_server skipped; state written."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
    ]
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_present("10.0.0.5:8000", "default")

    mock_client.add_server.assert_not_called()
    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "ready")
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" in bs.list()
    assert outcome.action == Action.ADOPTED


def test_want_present_re_readies_when_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """in_haproxy=True, in_state=True → Action.READIED; add_server skipped; set_state called."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
    ]
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_present("10.0.0.5:8000", "default")

    mock_client.add_server.assert_not_called()
    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "ready")
    assert outcome.action == Action.READIED
    assert BackendState(mgr.state_dir, mgr.lb.host, pool="default").list() == ["10.0.0.5:8000"]


def test_want_present_re_registers_when_state_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """in_haproxy=False, in_state=True → Action.READIED; add_server called; set_state called."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = "new"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_present("10.0.0.5:8000", "default")

    mock_client.add_server.assert_called_once_with(
        "pool_default", "b_10_0_0_5_8000", "10.0.0.5:8000"
    )
    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "ready")
    assert outcome.action == Action.READIED
    assert BackendState(mgr.state_dir, mgr.lb.host, pool="default").list() == ["10.0.0.5:8000"]


def test_want_present_raises_backend_op_failed_and_leaves_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If add_server raises RuntimeError, BackendOpFailed is raised and state file is unchanged."""
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import BackendOpFailed

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.side_effect = RuntimeError("haproxy add_server failed: bad backend")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    with pytest.raises(BackendOpFailed):
        r.want_present("10.0.0.5:8000", "default")

    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert bs.list() == []


def test_want_present_idempotent_second_call_returns_readied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test: second call to want_present returns READIED and state has one entry."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    call_count = 0

    def fake_client(m: LbManager) -> MagicMock:
        nonlocal call_count
        call_count += 1
        mc = MagicMock()
        if call_count == 1:
            mc.show_servers_state.return_value = []
            mc.add_server.return_value = "new"
        else:
            mc.show_servers_state.return_value = [
                BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
            ]
        return mc

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", fake_client)

    r = Reconciler(mgr)
    r.want_present("10.0.0.5:8000", "default")  # first → ADDED
    outcome2 = r.want_present("10.0.0.5:8000", "default")  # second → READIED

    assert outcome2.action in {Action.NONE, Action.READIED}
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert bs.list().count("10.0.0.5:8000") == 1


# ---------------------------------------------------------------------------
# Task 8: want_absent(ep, pool) — 3-case action mapping
# ---------------------------------------------------------------------------


def test_want_absent_removes_ep_from_haproxy_first_then_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: want_absent sets maint then removes from haproxy before state."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    call_order: list[str] = []
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
    ]

    def track_set_state(backend: str, name: str, state: str) -> None:
        call_order.append(f"set_state:{state}")

    def track_remove_server(backend: str, name: str) -> None:
        call_order.append("remove_server")

    mock_client.set_state.side_effect = track_set_state
    mock_client.remove_server.side_effect = track_remove_server
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_absent("10.0.0.5:8000", "default")

    assert call_order == ["set_state:maint", "remove_server"]
    mock_client.set_state.assert_any_call("pool_default", "b_10_0_0_5_8000", "maint")
    mock_client.remove_server.assert_called_once_with("pool_default", "b_10_0_0_5_8000")
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" not in bs.list()
    assert outcome.action == Action.REMOVED


def test_want_absent_returns_none_when_neither_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ep absent from both, returns NONE without any haproxy calls."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_absent("10.0.0.5:8000", "default")

    mock_client.set_state.assert_not_called()
    mock_client.remove_server.assert_not_called()
    assert outcome.action == Action.NONE


def test_want_absent_orphaned_cleaned_when_state_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """in_haproxy=False, in_state=True → ORPHANED_CLEANED; state file cleaned."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_absent("10.0.0.5:8000", "default")

    mock_client.set_state.assert_not_called()
    mock_client.remove_server.assert_not_called()
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" not in bs.list()
    assert outcome.action == Action.ORPHANED_CLEANED


def test_want_absent_raises_lb_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import LbUnreachable

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    with pytest.raises(LbUnreachable):
        r.want_absent("10.0.0.5:8000", "default")


def test_want_absent_raises_backend_op_failed_and_leaves_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If set_state(maint) raises RuntimeError, BackendOpFailed is raised; state unchanged."""
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import BackendOpFailed

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
    ]
    mock_client.set_state.side_effect = RuntimeError("haproxy set_state failed")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    with pytest.raises(BackendOpFailed):
        r.want_absent("10.0.0.5:8000", "default")

    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" in bs.list()


# ---------------------------------------------------------------------------
# Task 9: want_draining(ep, pool) — drain a registered server
# ---------------------------------------------------------------------------


def test_want_draining_drains_registered_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcome = r.want_draining("10.0.0.5:8000", "default")

    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "drain")
    assert outcome.action == Action.DRAINED
    assert outcome.ep == "10.0.0.5:8000"


def test_want_draining_raises_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import LbUnreachable

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    with pytest.raises(LbUnreachable):
        r.want_draining("10.0.0.5:8000", "default")


def test_want_draining_raises_backend_op_failed_for_unregistered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If set_state raises RuntimeError, BackendOpFailed is raised."""
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import BackendOpFailed

    mgr = _make_mgr(tmp_path)
    mock_client = MagicMock()
    mock_client.set_state.side_effect = RuntimeError("no such server pool_default/b_10_0_0_5_8000")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    with pytest.raises(BackendOpFailed):
        r.want_draining("10.0.0.5:8000", "default")


def test_want_draining_does_not_touch_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drain is transitional — state file represents intended membership."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")

    mock_client = MagicMock()
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    r.want_draining("10.0.0.5:8000", "default")

    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    assert "10.0.0.5:8000" in bs.list()


# ---------------------------------------------------------------------------
# Task 10: reconcile_pool + reconcile_from_state
# ---------------------------------------------------------------------------


def test_reconcile_pool_converges_haproxy_and_state_to_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: reconcile_pool converges haproxy and state to target set."""
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    bs.add("10.0.0.5:8000")
    bs.add("10.0.0.6:8000")

    present_servers = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
        BackendStatus(name="b_10_0_0_7_8000", endpoint="10.0.0.7:8000", op_state=2),
    ]

    def fake_show_servers_state() -> list[BackendStatus]:
        return list(present_servers)

    mock_client = MagicMock()
    mock_client.show_servers_state.side_effect = fake_show_servers_state
    mock_client.add_server.return_value = "new"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcomes = r.reconcile_pool("default", {"10.0.0.5:8000", "10.0.0.6:8000"})

    assert all(isinstance(o, Outcome) for o in outcomes)
    eps_in_outcomes = {o.ep for o in outcomes}
    assert "10.0.0.5:8000" in eps_in_outcomes
    assert "10.0.0.6:8000" in eps_in_outcomes
    assert "10.0.0.7:8000" in eps_in_outcomes
    absent_outcome = next(o for o in outcomes if o.ep == "10.0.0.7:8000")
    assert absent_outcome.action == Action.REMOVED

    final = sorted(bs.list())
    assert final == ["10.0.0.5:8000", "10.0.0.6:8000"]


def test_reconcile_pool_with_empty_target_removes_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    bs.add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [
        BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000", op_state=2),
    ]
    mock_client.add_server.return_value = "new"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcomes = r.reconcile_pool("default", set())

    removed = [o for o in outcomes if o.action == Action.REMOVED]
    assert len(removed) >= 1
    assert bs.list() == []


def test_reconcile_pool_raises_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod
    from vctl.lb.errors import LbUnreachable

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    r = Reconciler(mgr)
    with pytest.raises(LbUnreachable):
        r.reconcile_pool("default", {"10.0.0.5:8000"})


def test_reconcile_from_state_uses_state_file_as_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.lb.reconciler as reconciler_mod

    mgr = _make_mgr(tmp_path)
    bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
    bs.add("10.0.0.5:8000")
    bs.add("10.0.0.6:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = "new"
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    r = Reconciler(mgr)
    outcomes = r.reconcile_from_state("default")

    eps_in_outcomes = {o.ep for o in outcomes}
    assert "10.0.0.5:8000" in eps_in_outcomes
    assert "10.0.0.6:8000" in eps_in_outcomes


# ---------------------------------------------------------------------------
# Task 11: Concurrency — multiprocess want_present same ep
# ---------------------------------------------------------------------------


def test_concurrent_want_present_same_ep_produces_one_state_entry(
    tmp_path: Path,
) -> None:
    """Acceptance: 4 concurrent workers calling want_present for same ep.

    Uses VCTL_TEST_NO_SOCKET=1 so haproxy calls are no-ops (_NoOpClient).
    Relies on BackendState.add's flock for serialization correctness.
    Final state file must have exactly one entry; all Outcomes valid.
    """
    state_dir = str(tmp_path / "state")
    run_dir = str(tmp_path / "run")
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(run_dir, exist_ok=True)
    lb_host = "10.0.0.1"
    ep = "10.0.0.5:8000"

    args = [(state_dir, run_dir, lb_host, ep)] * 4
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=4) as pool:
        results = pool.map(_concurrent_want_present_worker, args)

    valid_actions = {"added", "none", "readied", "adopted"}
    for result_ep, result_action in results:
        assert result_ep == ep, f"unexpected ep: {result_ep}"
        assert result_action in valid_actions, f"unexpected action: {result_action}"

    bs = BackendState(Path(state_dir), lb_host, pool="default")
    final_entries = bs.list()
    assert len(final_entries) == 1, f"expected 1 entry, got {len(final_entries)}: {final_entries}"
    assert final_entries[0] == ep
