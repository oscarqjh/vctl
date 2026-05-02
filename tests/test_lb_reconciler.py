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
