"""Tests for `vctl lb prune` verb dispatch and flag handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.errors import BackendOpFailed, LbUnreachable
from vctl.lb.manager import LbManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lb(pools: list[Pool] | None = None) -> LbHaproxy:
    if pools is None:
        pools = [Pool(name="default", served_model="*", bind_port=8080)]
    return LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=pools,
    )


def _make_mgr(tmp_path: Path, lb: LbHaproxy | None = None) -> LbManager:
    if lb is None:
        lb = _make_lb()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


# ---------------------------------------------------------------------------
# AT-1: prune removes an eligible DOWN backend
# ---------------------------------------------------------------------------


def test_prune_removes_eligible_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-1: DOWN ep at 400s with default threshold 300s → want_absent called once."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    # Patch _collect_prune_candidates to return one candidate.
    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    want_absent_calls: list[tuple[str, str]] = []

    def fake_want_absent(self: Reconciler, ep: str, pool: str) -> MagicMock:
        want_absent_calls.append((ep, pool))
        m = MagicMock()
        m.action.name = "REMOVED"
        return m

    monkeypatch.setattr(Reconciler, "want_absent", fake_want_absent)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    assert want_absent_calls == [("10.0.0.5:8000", "default")]


# ---------------------------------------------------------------------------
# AT-4: --dry-run does not call want_absent
# ---------------------------------------------------------------------------


def test_prune_dry_run_does_not_call_want_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-4: --dry-run prints would-prune message without calling want_absent."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    def _should_not_be_called(self: Reconciler, ep: str, pool: str) -> None:
        raise AssertionError("want_absent must NOT be called in dry-run mode")

    monkeypatch.setattr(Reconciler, "want_absent", _should_not_be_called)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=True)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    captured = capsys.readouterr()
    assert "would prune" in captured.err


# ---------------------------------------------------------------------------
# AT-2: --threshold flag overrides yaml default
# ---------------------------------------------------------------------------


def test_prune_threshold_flag_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-2: --threshold 1m; ep at 90s is pruned (90 >= 60); ep at 50s is not."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    # _collect_prune_candidates receives threshold_s=60 and returns filtered list.
    # We simulate: 90s ep passes, 50s ep does not.
    captured_thresholds: list[int] = []

    def fake_collect(m: LbManager, pool: str, threshold_s: int) -> list[tuple[str, int]]:
        captured_thresholds.append(threshold_s)
        # Return only ep that meets threshold (threshold_s=60 → 90s qualifies).
        return [("10.0.0.5:8000", 90)] if threshold_s <= 90 else []

    monkeypatch.setattr(prune_mod, "_collect_prune_candidates", fake_collect)

    want_absent_calls: list[str] = []

    def fake_want_absent(self: Reconciler, ep: str, pool: str) -> MagicMock:
        want_absent_calls.append(ep)
        m = MagicMock()
        m.action.name = "REMOVED"
        return m

    monkeypatch.setattr(Reconciler, "want_absent", fake_want_absent)

    import argparse

    ns = argparse.Namespace(pool=None, threshold="1m", dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    assert captured_thresholds == [60]  # 1m → 60s passed to collector
    assert "10.0.0.5:8000" in want_absent_calls


# ---------------------------------------------------------------------------
# AT-3: unknown --pool returns 3
# ---------------------------------------------------------------------------


def test_prune_unknown_pool_returns_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """AT-3: --pool nosuch → exit 3 + stderr message containing 'unknown pool'."""
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    import argparse

    ns = argparse.Namespace(pool="nosuch", threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 3
    captured = capsys.readouterr()
    assert "nosuch" in captured.err
    assert "unknown pool" in captured.err.lower() or "available" in captured.err.lower()


# ---------------------------------------------------------------------------
# LbUnreachable → exit 4
# ---------------------------------------------------------------------------


def test_prune_lb_unreachable_returns_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LbUnreachable from _collect_prune_candidates → exit 4."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    def raise_unreachable(m: LbManager, pool: str, threshold_s: int) -> list[tuple[str, int]]:
        raise LbUnreachable(sock="/tmp/fake.sock", tcp="10.0.0.1:9001")

    monkeypatch.setattr(prune_mod, "_collect_prune_candidates", raise_unreachable)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)
    assert rc == 4


# ---------------------------------------------------------------------------
# Invalid --threshold → exit 1
# ---------------------------------------------------------------------------


def test_prune_invalid_threshold_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--threshold bad → exit 1 with error message."""
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    import argparse

    ns = argparse.Namespace(pool=None, threshold="bad", dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid duration" in captured.err.lower() or "bad" in captured.err


# ---------------------------------------------------------------------------
# BackendOpFailed → exit 1
# ---------------------------------------------------------------------------


def test_prune_backend_op_failed_returns_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BackendOpFailed during want_absent → exit 1."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    def raise_op_failed(self: Reconciler, ep: str, pool: str) -> None:
        raise BackendOpFailed(op="remove_server", ep=ep, backend=f"pool_{pool}")

    monkeypatch.setattr(Reconciler, "want_absent", raise_op_failed)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)
    assert rc == 1
