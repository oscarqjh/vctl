"""Unit tests for vctl.lb.reconciler — all RuntimeClient calls are mocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from vctl.config.models import LbAdmin, LbHaproxy, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.reconciler import Action, Drift, Outcome, Reconciler


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
