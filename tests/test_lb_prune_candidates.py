"""Unit tests for _collect_prune_candidates filtering logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.errors import LbUnreachable
from vctl.lb.manager import LbManager
from vctl.lb.runtime import BackendStatus


def _make_mgr(tmp_path: Path) -> LbManager:
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


def _fake_row(
    name: str = "b_10_0_0_1_8000",
    endpoint: str = "10.0.0.1:8000",
    op_state: int = 0,
    admin_state: int = 0,
    backend: str = "pool_default",
) -> BackendStatus:
    return BackendStatus(
        name=name,
        endpoint=endpoint,
        op_state=op_state,
        admin_state=admin_state,
        backend=backend,
    )


def _fake_stats(
    pool_section: str,
    server_name: str,
    status: str = "DOWN",
    lastchg: int = 400,
) -> dict[str, dict[str, dict[str, int | str]]]:
    """Build the dict[backend_section, dict[server_name, dict[field]]] shape."""
    return {
        pool_section: {
            server_name: {
                "status": status,
                "lastchg": lastchg,
                "scur": 0,
                "qcur": 0,
                "ep": "10.0.0.1:8000",
            }
        }
    }


# ---- eligible candidates ----


def test_collects_eligible_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin=ready + lastchg=400 + threshold=300 → returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,  # ready
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == [("10.0.0.1:8000", 400)]


# ---- exclusions ----


def test_skips_maint_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin_state=1 (maint) + lastchg=400 → NOT returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=1,  # maint bit set (LB_ADMIN_MAINT_MASK = 0x07)
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_drain_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin_state=0x38 (drain) + lastchg=400 → NOT returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0x38,  # drain bit set (LB_ADMIN_DRAIN_MASK = 0x38)
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_up_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UP + lastchg=400 → NOT returned regardless of threshold."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,  # ready
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="UP", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin=ready + lastchg=200 + threshold=300 → NOT returned (200 < 300)."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=200)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_down_at_exact_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + lastchg==threshold → returned (>= boundary is inclusive)."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(admin_state=0, backend="pool_default")
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=300)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert len(result) == 1


# ---- LbUnreachable ----


def test_lb_unreachable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lb_admin_client` returns None → raises LbUnreachable."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: None)

    from vctl.lb.prune import _collect_prune_candidates

    with pytest.raises(LbUnreachable):
        _collect_prune_candidates(mgr, "default", threshold_s=300)
