"""Unit tests for vctl.lb.reconciler — all RuntimeClient calls are mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbHaproxy, LbStats, Pool
from vctl.lb.errors import PoolNotFound
from vctl.lb.manager import LbManager
from vctl.lb.reconciler import Action, Drift, Outcome, Reconciler
from vctl.lb.runtime import BackendStatus
from vctl.lb.state import BackendState


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
