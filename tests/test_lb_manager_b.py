"""Commit-B tests for LbManager: B1, B3, B4, B5, B11."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vctl.config.models import LbAdmin, LbClient, LbDefaults, LbHaproxy, LbHealth, LbStats
from vctl.lb.manager import LbManager, _verify_pid_is_haproxy


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
# B1: double-start guard
# ---------------------------------------------------------------------------


@patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
@patch("vctl.lb.manager.tmux_session_exists", return_value=False)
@patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
@patch("vctl.lb.manager._verify_pid_is_haproxy", return_value=True)
def test_start_raises_when_already_running(
    mock_verify: MagicMock,
    mock_conn: MagicMock,
    mock_tmux: MagicMock,
    mock_ip: MagicMock,
    tmp_path: Path,
) -> None:
    """B1: start() must raise RuntimeError when status() reports running."""
    mgr = _make_mgr(tmp_path)
    # Write a pidfile pointing to a real process (ourselves).
    pid = os.getpid()
    mgr.pid_path.write_text(str(pid))
    with pytest.raises(RuntimeError, match="already running"):
        mgr.start(force=False)


@patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
@patch("vctl.lb.manager.tmux_session_exists", return_value=False)
@patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
@patch("vctl.lb.manager.tmux_run_detached")
@patch("vctl.lb.manager.ensure_haproxy", return_value="/usr/bin/haproxy")
@patch("vctl.lb.manager._verify_pid_is_haproxy", return_value=True)
def test_start_force_calls_stop_then_starts(
    mock_verify: MagicMock,
    mock_haproxy: MagicMock,
    mock_tmux_run: MagicMock,
    mock_conn: MagicMock,
    mock_tmux_exists: MagicMock,
    mock_ip: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: start(force=True) when running must call stop() then proceed."""
    mgr = _make_mgr(tmp_path)
    stop_called = []

    def fake_stop() -> None:
        stop_called.append(True)
        # Actually clean up the pid file so status() returns not-running next time
        mgr.pid_path.unlink(missing_ok=True)

    monkeypatch.setattr(mgr, "stop", fake_stop)

    # Write a pidfile pointing to ourselves so status() sees running.
    pid = os.getpid()
    mgr.pid_path.write_text(str(pid))

    mgr.start(force=True)
    assert stop_called, "stop() must be called when force=True and already running"
    assert mock_tmux_run.called, "tmux_run_detached must be called after stop()"


# ---------------------------------------------------------------------------
# B3: SIGKILL fallback after 10s wait
# ---------------------------------------------------------------------------


def test_stop_sigkill_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3: if pid never dies after SIGTERM, SIGKILL is sent."""
    mgr = _make_mgr(tmp_path)
    fake_pid = 99999

    # Write pidfile
    mgr.pid_path.write_text(str(fake_pid))

    kill_calls: list[tuple[int, int]] = []
    kill_attempt = 0

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        # Never raise ProcessLookupError — pid "stays alive"
        nonlocal kill_attempt
        kill_attempt += 1

    # time.sleep is used in the poll loop; patch it to be instant and
    # patch time.monotonic to advance past the 10s deadline quickly.
    start_time = time.monotonic()
    tick = [0.0]

    def fake_monotonic() -> float:
        val = start_time + tick[0]
        # Advance by 0.5s per call so we blow past the 10s deadline fast.
        tick[0] += 0.6
        return val

    monkeypatch.setattr("vctl.lb.manager.os.kill", fake_kill)
    monkeypatch.setattr("vctl.lb.manager.time.monotonic", fake_monotonic)
    monkeypatch.setattr("vctl.lb.manager.time.sleep", lambda _: None)
    monkeypatch.setattr("vctl.lb.manager.tmux_kill", lambda _: None)

    mgr.stop()

    sigterm_calls = [(p, s) for p, s in kill_calls if s == signal.SIGTERM]
    sigkill_calls = [(p, s) for p, s in kill_calls if s == signal.SIGKILL]
    assert sigterm_calls, "SIGTERM must be sent"
    assert sigkill_calls, "SIGKILL must be sent after 10s if pid still alive"
    assert sigkill_calls[0][0] == fake_pid


# ---------------------------------------------------------------------------
# B4: sock_path unlinked after stop
# ---------------------------------------------------------------------------


def test_stop_unlinks_sock_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B4: stop() must unlink the admin socket file."""
    mgr = _make_mgr(tmp_path)
    # Create sentinel socket file.
    mgr.sock_path.write_text("sentinel")
    assert mgr.sock_path.exists()

    # Fake pid that "dies" immediately on the first os.kill(0) poll check.
    fake_pid = 12345
    mgr.pid_path.write_text(str(fake_pid))

    def fake_kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            return  # SIGTERM sends fine
        # os.kill(pid, 0) during poll → raises ProcessLookupError → loop breaks.
        raise ProcessLookupError(pid)

    _real_monotonic = time.monotonic
    monkeypatch.setattr("vctl.lb.manager.os.kill", fake_kill)
    monkeypatch.setattr("vctl.lb.manager.time.monotonic", _real_monotonic)
    monkeypatch.setattr("vctl.lb.manager.time.sleep", lambda _: None)
    monkeypatch.setattr("vctl.lb.manager.tmux_kill", lambda _: None)

    mgr.stop()
    assert not mgr.sock_path.exists(), "sock_path must be unlinked after stop()"


# ---------------------------------------------------------------------------
# B5: PID staleness via /proc/<pid>/comm
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc required")
def test_status_stale_pid_returns_not_alive(tmp_path: Path) -> None:
    """B5: if /proc/<pid>/comm is not 'haproxy', pid_alive must be False."""
    mgr = _make_mgr(tmp_path)
    # Use our own pid — comm will be "python" or "pytest", not "haproxy".
    pid = os.getpid()
    mgr.pid_path.write_text(str(pid))
    result = mgr.status()
    # Our process exists but is NOT haproxy → pid_alive=False.
    assert result["pid_alive"] is False, (
        f"pid {pid} should be stale (comm is not haproxy); got {result}"
    )


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc required")
def test_verify_pid_is_haproxy_rejects_non_haproxy() -> None:
    """B5: _verify_pid_is_haproxy returns False for our own process."""
    assert _verify_pid_is_haproxy(os.getpid()) is False


# ---------------------------------------------------------------------------
# B11: reload precheck and error capture
# ---------------------------------------------------------------------------


def test_reload_raises_on_config_syntax_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B11: reload() must raise RuntimeError with stderr when haproxy -c fails."""
    mgr = _make_mgr(tmp_path)
    # Create a pid file (reload checks for it).
    mgr.pid_path.write_text("12345")
    # Create a junk cfg.
    mgr.cfg_path.write_text("this is not valid haproxy config\n")

    # Stub ensure_haproxy to avoid network/install.
    monkeypatch.setattr("vctl.lb.manager.ensure_haproxy", lambda: "/fake/haproxy")

    # Stub subprocess.run: first call (-c -f) fails, second should never run.
    run_calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, capture_output: bool = False, text: bool = False, **kw: object
    ) -> subprocess.CompletedProcess:
        run_calls.append(list(args))
        if "-c" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="config syntax error at line 1"
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("vctl.lb.manager.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="config syntax error"):
        mgr.reload()

    # Verify precheck was the one called.
    assert any("-c" in c for c in run_calls), "haproxy -c precheck must be called"


def test_reload_raises_calledprocesserror_on_reload_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B11: reload() must raise CalledProcessError (with output) when reload fails."""
    mgr = _make_mgr(tmp_path)
    mgr.pid_path.write_text("12345")
    mgr.cfg_path.write_text("# dummy config\n")

    monkeypatch.setattr("vctl.lb.manager.ensure_haproxy", lambda: "/fake/haproxy")

    def fake_run(
        args: list[str], *, capture_output: bool = False, text: bool = False, **kw: object
    ) -> subprocess.CompletedProcess:
        if "-c" in args:
            # Precheck passes.
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        # Reload fails.
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr="bind error on :8080"
        )

    monkeypatch.setattr("vctl.lb.manager.subprocess.run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        mgr.reload()
    assert "bind error" in (exc_info.value.stderr or "")
