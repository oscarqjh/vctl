"""Tests for `vctl lb start` watcher spawn integration (Task 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbPrune, LbStats, Pool
from vctl.lb.manager import LbManager

# ---------------------------------------------------------------------------
# Helpers (mirrors test_commands_lb_prune.py)
# ---------------------------------------------------------------------------


def _make_lb(prune: LbPrune | None = None, pools: list[Pool] | None = None) -> LbHaproxy:
    if pools is None:
        pools = [Pool(name="default", served_model="*", bind_port=8080)]
    if prune is None:
        prune = LbPrune()
    return LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        prune=prune,
        pools=pools,
    )


def _make_mgr(tmp_path: Path, lb: LbHaproxy | None = None) -> LbManager:
    if lb is None:
        lb = _make_lb()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


# ---------------------------------------------------------------------------
# Task 5: watcher spawn integration into lb start
# ---------------------------------------------------------------------------


def test_lb_start_spawns_watcher_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-7: prune.enabled=True → vctl-lb-watch session spawned + pidfile written."""
    import vctl.commands.lb as lb_mod

    lb = _make_lb(prune=LbPrune(enabled=True))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = tmp_path / "run" / "watch.pid"

    spawned_sessions: list[str] = []

    def fake_tmux(name: str, argv: list[str]) -> None:
        spawned_sessions.append(name)

    monkeypatch.setattr(lb_mod, "_tmux_run_detached_argv", fake_tmux)
    monkeypatch.setattr(lb_mod, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr("vctl.lb.prune.tmux_run_detached_argv", fake_tmux)

    from vctl.commands.lb import _spawn_watcher_if_enabled

    _spawn_watcher_if_enabled(mgr, Path("/tmp/cluster.yaml"))

    assert "vctl-lb-watch" in spawned_sessions
    assert watch_pid.exists()
    assert watch_pid.read_text().strip() == "tmux:vctl-lb-watch"


def test_lb_start_skips_watcher_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-7b: prune.enabled=False → no watcher session spawned, no pidfile written."""
    lb = _make_lb(prune=LbPrune(enabled=False))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = tmp_path / "run" / "watch.pid"

    spawned_sessions: list[str] = []

    def fake_tmux(name: str, argv: list[str]) -> None:
        spawned_sessions.append(name)

    monkeypatch.setattr("vctl.lb.prune.tmux_run_detached_argv", fake_tmux)

    from vctl.commands.lb import _spawn_watcher_if_enabled

    _spawn_watcher_if_enabled(mgr, Path("/tmp/cluster.yaml"))

    assert "vctl-lb-watch" not in spawned_sessions
    assert not watch_pid.exists()


# ---------------------------------------------------------------------------
# Task 6: watcher kill integration into lb stop + state in lb status
# ---------------------------------------------------------------------------


def test_lb_stop_kills_watcher_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-8: lb stop calls _stop_watcher → tmux_kill("vctl-lb-watch") + pidfile removed."""
    lb = _make_lb(prune=LbPrune(enabled=True))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = mgr.run_dir / "watch.pid"
    watch_pid.parent.mkdir(parents=True, exist_ok=True)
    watch_pid.write_text("tmux:vctl-lb-watch\n")

    killed: list[str] = []
    monkeypatch.setattr("vctl.lb.prune.tmux_kill", lambda name: killed.append(name))

    from vctl.lb.prune import _stop_watcher

    _stop_watcher(mgr)

    assert "vctl-lb-watch" in killed
    assert not watch_pid.exists()


def test_lb_stop_watcher_idempotent_when_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stop_watcher when nothing running → no error, exit cleanly."""
    lb = _make_lb()
    mgr = _make_mgr(tmp_path, lb=lb)

    monkeypatch.setattr("vctl.lb.prune.tmux_kill", lambda name: None)

    from vctl.lb.prune import _stop_watcher

    _stop_watcher(mgr)  # must not raise


def test_lb_status_reports_watcher_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-9: watcher session alive + pidfile present → state='running'."""
    lb = _make_lb(prune=LbPrune(enabled=True))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = mgr.run_dir / "watch.pid"
    watch_pid.parent.mkdir(parents=True, exist_ok=True)
    watch_pid.write_text("tmux:vctl-lb-watch\n")

    monkeypatch.setattr("vctl.lb.prune.tmux_session_exists", lambda name: True)

    from vctl.lb.prune import _watcher_status

    result = _watcher_status(mgr)

    assert result["state"] == "running"
    assert result["enabled"] is True


def test_lb_status_reports_watcher_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-9: prune.enabled=False → state='disabled' regardless of session."""
    lb = _make_lb(prune=LbPrune(enabled=False))
    mgr = _make_mgr(tmp_path, lb=lb)

    monkeypatch.setattr("vctl.lb.prune.tmux_session_exists", lambda name: True)

    from vctl.lb.prune import _watcher_status

    result = _watcher_status(mgr)

    assert result["state"] == "disabled"
    assert result["enabled"] is False
