"""Unit tests for _collect_prune_candidates filtering logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbPrune, LbStats, Pool
from tctl.workloads.haproxy.errors import LbUnreachable
from tctl.workloads.haproxy.manager import LbManager
from tctl.workloads.haproxy.runtime import BackendStatus


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
    import tctl.workloads.haproxy.prune as prune_mod

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

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == [("10.0.0.1:8000", 400)]


# ---- exclusions ----


def test_skips_maint_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin_state=1 (maint) + lastchg=400 → NOT returned."""
    import tctl.workloads.haproxy.prune as prune_mod

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

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_drain_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin_state=0x38 (drain) + lastchg=400 → NOT returned."""
    import tctl.workloads.haproxy.prune as prune_mod

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

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_up_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UP + lastchg=400 → NOT returned regardless of threshold."""
    import tctl.workloads.haproxy.prune as prune_mod

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

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + admin=ready + lastchg=200 + threshold=300 → NOT returned (200 < 300)."""
    import tctl.workloads.haproxy.prune as prune_mod

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

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_down_at_exact_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOWN + lastchg==threshold → returned (>= boundary is inclusive)."""
    import tctl.workloads.haproxy.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(admin_state=0, backend="pool_default")
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=300)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert len(result) == 1


# ---- LbUnreachable ----


def test_lb_unreachable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lb_admin_client` returns None → raises LbUnreachable."""
    import tctl.workloads.haproxy.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: None)

    from tctl.workloads.haproxy.prune import _collect_prune_candidates

    with pytest.raises(LbUnreachable):
        _collect_prune_candidates(mgr, "default", threshold_s=300)


# ============================================================================
# From test_lb_prune_config.py
# ============================================================================

"""Schema tests for the LbPrune pydantic class and LbHaproxy.prune field (below)."""


def _single_pool_lb(**overrides: object) -> LbHaproxy:
    """Build a minimal valid LbHaproxy for testing."""
    kwargs: dict[str, object] = dict(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    kwargs.update(overrides)
    return LbHaproxy(**kwargs)  # type: ignore[arg-type]


# ---- LbPrune defaults ----


def test_lb_prune_default_enabled() -> None:
    """LbPrune() with no args should produce enabled=True."""
    p = LbPrune()
    assert p.enabled is True


def test_lb_prune_default_threshold() -> None:
    """LbPrune() with no args should produce threshold='5m'."""
    p = LbPrune()
    assert p.threshold == "5m"


def test_lb_prune_default_watch_interval() -> None:
    """LbPrune() with no args should produce watch_interval='30s'."""
    p = LbPrune()
    assert p.watch_interval == "30s"


def test_lb_haproxy_prune_field_defaults() -> None:
    """LbHaproxy.prune should be populated with defaults when 'prune:' block omitted."""
    lb = _single_pool_lb()
    assert lb.prune.enabled is True
    assert lb.prune.threshold == "5m"
    assert lb.prune.watch_interval == "30s"


def test_lb_prune_enabled_false() -> None:
    """LbPrune(enabled=False) should be accepted and preserved."""
    p = LbPrune(enabled=False)
    assert p.enabled is False


# ---- custom values ----


def test_lb_prune_custom_threshold() -> None:
    """threshold='10m' should be accepted and preserved."""
    p = LbPrune(threshold="10m")
    assert p.threshold == "10m"


def test_lb_prune_custom_watch_interval() -> None:
    """watch_interval='2m' should be accepted and preserved."""
    p = LbPrune(watch_interval="2m")
    assert p.watch_interval == "2m"


def test_lb_haproxy_with_custom_prune_block() -> None:
    """LbHaproxy with explicit prune block should carry the custom values."""
    lb = _single_pool_lb(prune=LbPrune(threshold="10m", watch_interval="60s"))
    assert lb.prune.threshold == "10m"
    assert lb.prune.watch_interval == "60s"


# ---- invalid values ----


def test_lb_prune_invalid_threshold_raises() -> None:
    """threshold='bad' must raise ValidationError."""
    with pytest.raises(ValidationError, match="invalid duration"):
        LbPrune(threshold="bad")


def test_lb_prune_invalid_watch_interval_raises() -> None:
    """watch_interval='5x' must raise ValidationError."""
    with pytest.raises(ValidationError, match="invalid duration"):
        LbPrune(watch_interval="5x")
