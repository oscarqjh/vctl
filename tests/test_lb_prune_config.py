"""Schema tests for the LbPrune pydantic class and LbHaproxy.prune field."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbPrune, LbStats, Pool


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
