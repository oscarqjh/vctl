"""Tests for F7 (serve teardown), F8 (PPID watchdog), F10 (multi-pid haproxy)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbClient, LbDefaults, LbHaproxy, LbHealth, LbStats
from vctl.lb.manager import (
    LbManager,
    _find_all_haproxy_pids_by_cfg,
    _find_haproxy_pid_by_cfg,
)

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _lb(host: str = "10.0.0.1") -> LbHaproxy:
    return LbHaproxy(
        kind="haproxy",
        host=host,
        client=LbClient(bind_port=8080),
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
    )


def _make_mgr(tmp_path: Path, host: str = "10.0.0.1") -> LbManager:
    return LbManager(_lb(host), state_dir=tmp_path / "state", run_dir=tmp_path / "run")


# ---------------------------------------------------------------------------
# F7: _force_cleanup_vctl_serve_for_path helper contract
# ---------------------------------------------------------------------------


def test_force_cleanup_kills_matching_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: _force_cleanup_vctl_serve_for_path kills any process whose cmdline
    contains the given stub path and returns the count of killed processes."""
    from tests.conftest import _force_cleanup_vctl_serve_for_path

    stub_path = tmp_path / "bin"
    stub_path.mkdir()
    killed: list[int] = []

    class _FakeProc:
        def __init__(self, cmd: list[str]) -> None:
            self._info: dict[str, object] = {"cmdline": cmd}

        @property
        def info(self) -> dict[str, object]:
            return self._info

        def send_signal(self, sig: int) -> None:
            killed.append(sig)

    fake_procs = [
        _FakeProc(["/usr/bin/python3", "-m", "vctl", "serve", str(stub_path / "vllm")]),
        _FakeProc(["/usr/bin/python3", str(stub_path / "vllm"), "--port=8000"]),
        _FakeProc(["/usr/bin/python3", "-m", "vctl", "serve", "/home/user/venv/bin/vllm"]),
    ]

    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)

    count = _force_cleanup_vctl_serve_for_path(stub_path)
    # Should match 2 (the two that reference stub_path), not the prod path.
    assert count == 2
    assert len(killed) == 2
    assert all(s == signal.SIGKILL for s in killed)


def test_force_cleanup_never_matches_prod_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: cleanup helper must NOT match production serve paths."""
    from tests.conftest import _force_cleanup_vctl_serve_for_path

    stub_path = tmp_path / "bin"
    stub_path.mkdir()
    killed: list[int] = []

    class _FakeProc:
        def __init__(self, cmd: list[str]) -> None:
            self._info: dict[str, object] = {"cmdline": cmd}

        @property
        def info(self) -> dict[str, object]:
            return self._info

        def send_signal(self, sig: int) -> None:
            killed.append(sig)

    # Production-looking paths — must NOT be matched.
    fake_procs = [
        _FakeProc(["/usr/bin/python3", "-m", "vctl", "serve", "/home/user/.vctl/lb/vllm"]),
        _FakeProc(["/mnt/aigc/python3", "-m", "vctl", "serve"]),
    ]

    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)

    count = _force_cleanup_vctl_serve_for_path(stub_path)
    assert count == 0
    assert killed == []


# ---------------------------------------------------------------------------
# F8: PPID watchdog
# ---------------------------------------------------------------------------


def test_ppid_watchdog_triggers_shutdown_when_ppid_is_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8: when os.getppid()==1 and watchdog is enabled, the poll loop must
    call the shutdown callback."""
    shutdown_calls: list[tuple[int, object]] = []

    fake_proc = MagicMock()
    fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="vllm", timeout=0.5)

    def fake_shutdown(signum: int, frame: object) -> None:
        shutdown_calls.append((signum, frame))
        raise SystemExit(130)

    monkeypatch.setattr(os, "getppid", lambda: 1)
    monkeypatch.delenv("VCTL_NO_PPID_WATCHDOG", raising=False)

    _watchdog_enabled = os.environ.get("VCTL_NO_PPID_WATCHDOG", "0") not in ("1", "true", "yes")
    _watchdog_tick = 0

    with pytest.raises(SystemExit) as exc_info:
        for _ in range(15):  # More than 10 ticks to trigger watchdog
            with contextlib.suppress(subprocess.TimeoutExpired):
                fake_proc.wait(timeout=0.5)

            if _watchdog_enabled:
                _watchdog_tick += 1
                if _watchdog_tick >= 10:
                    _watchdog_tick = 0
                    if os.getppid() == 1:
                        fake_shutdown(signal.SIGTERM, None)

    assert exc_info.value.code == 130
    assert len(shutdown_calls) == 1
    assert shutdown_calls[0][0] == signal.SIGTERM


def test_ppid_watchdog_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F8: VCTL_NO_PPID_WATCHDOG=1 must suppress watchdog even when PPID==1."""
    shutdown_calls: list[int] = []

    fake_proc = MagicMock()
    fake_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="vllm", timeout=0.5)

    monkeypatch.setattr(os, "getppid", lambda: 1)
    monkeypatch.setenv("VCTL_NO_PPID_WATCHDOG", "1")

    _watchdog_enabled = os.environ.get("VCTL_NO_PPID_WATCHDOG", "0") not in ("1", "true", "yes")
    _watchdog_tick = 0

    # Run 20 ticks — watchdog should never fire.
    for _ in range(20):
        with contextlib.suppress(subprocess.TimeoutExpired):
            fake_proc.wait(timeout=0.5)

        if _watchdog_enabled:
            _watchdog_tick += 1
            if _watchdog_tick >= 10 and os.getppid() == 1:
                shutdown_calls.append(signal.SIGTERM)

    assert shutdown_calls == [], "watchdog must not fire when VCTL_NO_PPID_WATCHDOG=1"


# ---------------------------------------------------------------------------
# F10: _find_all_haproxy_pids_by_cfg + _find_haproxy_pid_by_cfg
# ---------------------------------------------------------------------------


def _make_fake_proc(pid: int, cfg_path: str, create_time: float) -> MagicMock:
    """Build a fake psutil.Process-like object matching a haproxy cmd."""
    proc = MagicMock()
    proc.pid = pid
    proc.info = {
        "name": "haproxy",
        "cmdline": ["haproxy", "-f", cfg_path, "-p", "/tmp/haproxy.pid"],
        "create_time": create_time,
    }
    return proc


def test_find_all_haproxy_pids_returns_all_sorted_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: _find_all_haproxy_pids_by_cfg must return all 3 PIDs sorted oldest-first."""
    import psutil

    cfg = tmp_path / "haproxy.cfg"
    cfg.write_text("# dummy\n")

    fake_procs = [
        _make_fake_proc(pid=300, cfg_path=str(cfg), create_time=3000.0),  # youngest
        _make_fake_proc(pid=100, cfg_path=str(cfg), create_time=1000.0),  # oldest
        _make_fake_proc(pid=200, cfg_path=str(cfg), create_time=2000.0),  # middle
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)

    result = _find_all_haproxy_pids_by_cfg(cfg)
    assert result == [100, 200, 300], f"expected [100, 200, 300] oldest-first, got {result}"


def test_find_haproxy_pid_by_cfg_returns_youngest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: _find_haproxy_pid_by_cfg must return the youngest (last) PID."""
    import psutil

    cfg = tmp_path / "haproxy.cfg"
    cfg.write_text("# dummy\n")

    fake_procs = [
        _make_fake_proc(pid=300, cfg_path=str(cfg), create_time=3000.0),
        _make_fake_proc(pid=100, cfg_path=str(cfg), create_time=1000.0),
        _make_fake_proc(pid=200, cfg_path=str(cfg), create_time=2000.0),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)

    result = _find_haproxy_pid_by_cfg(cfg)
    assert result == 300, f"expected youngest PID 300, got {result}"


def test_find_haproxy_pid_by_cfg_returns_none_when_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: _find_haproxy_pid_by_cfg returns None when no matching process exists."""
    import psutil

    cfg = tmp_path / "haproxy.cfg"
    cfg.write_text("# dummy\n")

    other_cfg = tmp_path / "other.cfg"
    fake_procs = [
        _make_fake_proc(pid=100, cfg_path=str(other_cfg), create_time=1000.0),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)

    result = _find_haproxy_pid_by_cfg(cfg)
    assert result is None


def test_reload_includes_all_pids_in_sf_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: reload() must pass all discovered PIDs to -sf."""
    import psutil

    mgr = _make_mgr(tmp_path)
    cfg = mgr.cfg_path
    cfg.write_text("# dummy\n")

    # Simulate 3 haproxy processes sharing the cfg.
    fake_procs = [
        _make_fake_proc(pid=100, cfg_path=str(cfg), create_time=1000.0),
        _make_fake_proc(pid=200, cfg_path=str(cfg), create_time=2000.0),
        _make_fake_proc(pid=300, cfg_path=str(cfg), create_time=3000.0),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)
    monkeypatch.setattr("vctl.lb.manager.ensure_haproxy", lambda: "/fake/haproxy")

    # Stub render_config so it doesn't fail.
    monkeypatch.setattr(mgr, "render_config", lambda: "# rendered cfg\n")

    run_calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, capture_output: bool = False, text: bool = False, **kw: object
    ) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        run_calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("vctl.lb.manager.subprocess.run", fake_run)

    mgr.reload()

    # Find the reload call (not the -c precheck).
    reload_call = next((c for c in run_calls if "-sf" in c), None)
    assert reload_call is not None, "reload subprocess.run call with -sf must have been made"
    sf_idx = reload_call.index("-sf")
    sf_pids = reload_call[sf_idx + 1 :]
    assert set(sf_pids) == {"100", "200", "300"}, f"all 3 pids must appear after -sf; got {sf_pids}"


def test_stop_kills_all_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F10: stop() must SIGTERM all matched haproxy PIDs."""
    import time as _time_mod

    import psutil

    mgr = _make_mgr(tmp_path)
    cfg = mgr.cfg_path
    cfg.write_text("# dummy\n")

    fake_procs = [
        _make_fake_proc(pid=100, cfg_path=str(cfg), create_time=1000.0),
        _make_fake_proc(pid=200, cfg_path=str(cfg), create_time=2000.0),
        _make_fake_proc(pid=300, cfg_path=str(cfg), create_time=3000.0),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: fake_procs)
    monkeypatch.setattr("vctl.lb.manager.tmux_kill", lambda _: None)

    sigterm_targets: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            sigterm_targets.append(pid)
        else:
            # Any signal(0) probes → raise ProcessLookupError (process is gone)
            raise ProcessLookupError(pid)

    monkeypatch.setattr("vctl.lb.manager.os.kill", fake_kill)
    monkeypatch.setattr("vctl.lb.manager.time.sleep", lambda _: None)
    monkeypatch.setattr("vctl.lb.manager.time.monotonic", _time_mod.monotonic)

    mgr.stop()

    assert set(sigterm_targets) == {100, 200, 300}, (
        f"SIGTERM must be sent to all 3 pids; got {sigterm_targets}"
    )
