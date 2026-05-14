"""Unit tests for VllmManager (tctl) — all run under VCTL_TEST_NO_SOCKET=1."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest


def _make_rc(profile_name: str = "qwen3-9b", http_port: int = 8000) -> MagicMock:
    rc = MagicMock()
    rc.profile_name = profile_name
    rc.server.http_port = http_port
    rc.model.name = "Qwen/Qwen3-9B"
    rc.lb.host = "10.0.0.1"
    rc.lb.pools = [MagicMock(name="default", served_model="Qwen/Qwen3-9B", bind_port=8080)]
    rc.lb.pools[0].name = "default"
    rc.cluster.venv = "/opt/venv"
    rc.cluster.state_dir = "/tmp/tctl-state"
    rc.parallelism.data_parallel = 1
    rc.parallelism.tensor_parallel = 1
    rc.parallelism.api_server_count = None
    rc.resources.cuda_visible_devices = "0"
    rc.vllm_args = {}
    rc.env = {}
    return rc


def test_vllm_manager_init_creates_run_dir(tmp_path: Path) -> None:
    """__init__ creates run_dir/vllm/ with parents=True exist_ok=True."""
    from tctl.workloads.vllm.manager import VllmManager

    run_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    rc = _make_rc()
    VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    assert (run_dir / "vllm").is_dir()
    VllmManager(rc, state_dir=state_dir, run_dir=run_dir)


def test_vllm_manager_init_computes_state_paths(tmp_path: Path) -> None:
    """__init__ pre-computes all four state file paths under run_dir/vllm/<host>/."""
    import socket

    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc(profile_name="qwen3-9b")
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    # v0.5.3: state files are host-scoped to avoid collisions when run_dir is
    # on a shared FS shared by multiple pods.
    vllm_dir = tmp_path / "run" / "vllm" / socket.gethostname()
    assert vm.pid_path == vllm_dir / "qwen3-9b.pid"
    assert vm.log_path == vllm_dir / "qwen3-9b.log"
    assert vm.cmd_path == vllm_dir / "qwen3-9b.cmd.json"
    assert vm.host_path == vllm_dir / "qwen3-9b.host"


def test_vllm_manager_init_validates_tmux_name(tmp_path: Path) -> None:
    """__init__ raises ValueError for a profile name that produces an invalid tmux name."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc(profile_name="bad name!")
    with pytest.raises(ValueError, match="invalid tmux session name"):
        VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_vllm_manager_init_session_name(tmp_path: Path) -> None:
    """Session name is tctl-vllm-<profile_name>."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc(profile_name="qwen3-9b")
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    assert vm.session_name == "tctl-vllm-qwen3-9b"


def test_start_refuses_when_session_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() raises RuntimeError (exit 4) when tmux session already exists."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _AlwaysExists:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return True

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _AlwaysExists)
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="already running"):
        vm.start()


def test_start_writes_all_four_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() writes pid, log (via pipe-pane), cmd.json, and host state files."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _FakeTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _FakeTmuxSession)

    fake_pid = 99999

    def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
        proc = MagicMock()
        proc.info = {
            "cmdline": ["python", "vllm", "serve", "--port=8000"],
            "create_time": 1000.0,
            "pid": fake_pid,
        }
        proc.pid = fake_pid
        return [proc]

    monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)
    monkeypatch.setattr(vm_mod, "_wait_for_ready", lambda port, timeout: None)
    monkeypatch.setattr(vm_mod, "_do_add", lambda ep, mgr, bs, pool_name=None: 0)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.start()

    assert vm.pid_path.exists(), "pid file must be written"
    assert int(vm.pid_path.read_text().strip()) == fake_pid
    assert vm.cmd_path.exists(), "cmd.json must be written"
    assert vm.host_path.exists(), "host file must be written"
    assert vm.host_path.read_text().strip() == socket.gethostname()


def test_start_pid_discovery_timeout_kills_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() kills tmux session and raises RuntimeError when PID discovery times out."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    killed: list[str] = []

    class _FakeTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            self._name = name

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            killed.append(self._name)

    monkeypatch.setattr(vm_mod, "TmuxSession", _FakeTmuxSession)
    monkeypatch.setattr(vm_mod.psutil, "process_iter", lambda attrs: [])

    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_TIMEOUT", 0.1)
    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_INTERVAL", 0.05)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="timed out"):
        vm.start()

    assert "tctl-vllm-qwen3-9b" in killed, "session must be killed on timeout"


def test_start_wait_for_ready_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() kills tmux session and unlinks state files when _wait_for_ready raises."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    killed: list[str] = []

    class _FakeTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            self._name = name

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            killed.append(self._name)

    monkeypatch.setattr(vm_mod, "TmuxSession", _FakeTmuxSession)

    fake_pid = 99999

    def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
        proc = MagicMock()
        proc.info = {
            "cmdline": ["python", "vllm", "serve", "--port=8000"],
            "create_time": 1000.0,
            "pid": fake_pid,
        }
        proc.pid = fake_pid
        return [proc]

    monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)

    def _fail_ready(port: int, timeout: float) -> None:
        raise TimeoutError("stubbed timeout")

    monkeypatch.setattr(vm_mod, "_wait_for_ready", _fail_ready)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="did not become ready"):
        vm.start()

    assert not vm.pid_path.exists(), "pid file must be cleaned up"
    assert not vm.cmd_path.exists(), "cmd.json must be cleaned up"
    assert not vm.host_path.exists(), "host file must be cleaned up"
    assert "tctl-vllm-qwen3-9b" in killed


def test_status_all_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status() returns all True fields when session, pid, http, and LB are alive."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _ExistsTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return True

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _ExistsTmuxSession)

    fake_pid = os.getpid()  # use our own PID — guaranteed alive
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    # Write pid file with a live PID
    vm.pid_path.parent.mkdir(parents=True, exist_ok=True)
    vm.pid_path.write_text(str(fake_pid))

    # Patch httpx.get to return a models response
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": [{"id": "m"}]}
    monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)

    # Patch BackendState.list to return our endpoint
    self_ip = vm_mod.detect_self_ip()
    ep = f"{self_ip}:{rc.server.http_port}"
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [ep])

    result = vm.status()
    assert result["tmux_alive"] is True
    assert result["pid_alive"] is True
    assert result["vllm_ready"] is True
    assert result["lb_attached"] is True


def test_status_tmux_dead_pid_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status() reports tmux_alive=False but pid_alive=True for an orphan process."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _DeadTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _DeadTmuxSession)

    fake_pid = os.getpid()
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.pid_path.write_text(str(fake_pid))

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": []}
    monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

    result = vm.status()
    assert result["tmux_alive"] is False
    assert result["pid_alive"] is True


def test_status_pidfile_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status() reports pid_alive=False when pidfile does not exist."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _DeadTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _DeadTmuxSession)
    monkeypatch.setattr(vm_mod.httpx, "get", MagicMock(side_effect=httpx.ConnectError("x")))
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    # pid_path does not exist

    result = vm.status()
    assert result["pid_alive"] is False
    assert result["vllm_ready"] is False
    assert result["lb_attached"] is False


def test_status_cross_host_pidfile_skips_liveness_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status() reports pid_alive=None when host marker is a different host (cross-host read)."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _DeadTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _DeadTmuxSession)
    monkeypatch.setattr(vm_mod.httpx, "get", MagicMock(side_effect=httpx.ConnectError("x")))
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.pid_path.write_text("12345")
    vm.host_path.write_text("other-host-99")

    result = vm.status()
    # Cross-host: cannot check liveness of a PID on a different host
    assert result.get("cross_host") is True
    assert result["pid_alive"] is None


def test_stop_full_drain_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() calls drain → wait_for_idle → remove → send-keys C-c → cleanup."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    calls: list[str] = []

    monkeypatch.setattr(
        vm_mod, "_do_drain", lambda ep, mgr, pool_name=None: calls.append("drain") or 0
    )
    monkeypatch.setattr(vm_mod, "_wait_for_idle", lambda port, timeout: calls.append("idle"))
    monkeypatch.setattr(
        vm_mod, "_do_remove", lambda ep, mgr, bs, pool_name=None: calls.append("remove") or 0
    )

    class _KillTrackingTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return True

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            calls.append("kill")

    monkeypatch.setattr(vm_mod, "TmuxSession", _KillTrackingTmuxSession)
    monkeypatch.setattr(
        vm_mod.subprocess,
        "run",
        lambda *a, **kw: (calls.append("sendkeys"), MagicMock(returncode=0))[1],
    )

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    # Write state files so stop() finds them.
    vm.host_path.write_text(socket.gethostname())
    vm.pid_path.write_text("1")  # PID 1 always dead (not our process)
    vm.cmd_path.write_text(json.dumps(["vllm", "serve"]))

    vm.stop()

    assert "drain" in calls
    assert "idle" in calls
    assert "remove" in calls
    assert "sendkeys" in calls
    # State files cleaned up.
    assert not vm.pid_path.exists()
    assert not vm.cmd_path.exists()
    assert not vm.host_path.exists()


def test_stop_cross_host_guard_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() raises RuntimeError when host marker is a different host."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.host_path.write_text("foreign-host-99")

    with pytest.raises(RuntimeError, match="refusing operation"):
        vm.stop()


def test_stop_force_kill_after_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() calls TmuxSession.kill when pid is still alive after TCTL_KILL_GRACE elapses."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    monkeypatch.setattr(vm_mod, "_do_drain", lambda ep, mgr, pool_name=None: 0)
    monkeypatch.setattr(vm_mod, "_wait_for_idle", lambda port, timeout: None)
    monkeypatch.setattr(vm_mod, "_do_remove", lambda ep, mgr, bs, pool_name=None: 0)
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

    killed: list[str] = []

    class _KillTrackingTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            self._name = name

        def exists(self) -> bool:
            return True

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            killed.append(self._name)

    monkeypatch.setattr(vm_mod, "TmuxSession", _KillTrackingTmuxSession)

    # Use our own PID — always alive — to simulate a process that won't die.
    alive_pid = os.getpid()
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.host_path.write_text(socket.gethostname())
    vm.pid_path.write_text(str(alive_pid))
    vm.cmd_path.write_text(json.dumps(["vllm", "serve"]))

    # Override grace period to near-zero so the test doesn't actually wait.
    monkeypatch.setenv("TCTL_KILL_GRACE", "0.1")

    vm.stop()

    # TmuxSession.kill must have been called since the process never exited.
    assert vm.session_name in killed


def test_restart_warns_on_config_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """restart() logs a warning when cmd.json differs from what fresh rc would produce."""
    import logging

    from tctl.workloads.vllm.manager import VllmManager

    stop_called: list[bool] = []
    start_called: list[bool] = []
    monkeypatch.setattr(VllmManager, "stop", lambda self: stop_called.append(True))
    monkeypatch.setattr(VllmManager, "start", lambda self: start_called.append(True))

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    # Write a cmd.json that differs from the current config
    vm.cmd_path.write_text(json.dumps(["vllm", "serve", "OldModel/Name", "--port=9999"]))
    vm.host_path.write_text(socket.gethostname())

    with caplog.at_level(logging.WARNING, logger="tctl.workloads.vllm.manager"):
        vm.restart()

    assert any(
        "config changed" in r.message.lower() or "drift" in r.message.lower()
        for r in caplog.records
    ), "expected warning about config drift"
    assert stop_called
    assert start_called


def test_restart_calls_stop_then_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """restart() calls stop() before start() — order matters."""
    from tctl.workloads.vllm.manager import VllmManager

    order: list[str] = []
    monkeypatch.setattr(VllmManager, "stop", lambda self: order.append("stop"))
    monkeypatch.setattr(VllmManager, "start", lambda self: order.append("start"))

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    # No cmd.json — restart should still proceed (no drift warning possible)
    vm.host_path.write_text(socket.gethostname())

    vm.restart()
    assert order == ["stop", "start"]


def test_console_calls_execvp_with_correct_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """console() calls os.execvp with the correct tmux attach-session arguments."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _ExistsTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return True

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _ExistsTmuxSession)

    execvp_calls: list[tuple[str, list[str]]] = []

    def fake_execvp(prog: str, argv: list[str]) -> None:
        execvp_calls.append((prog, argv))

    monkeypatch.setattr(vm_mod.os, "execvp", fake_execvp)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.console()

    assert len(execvp_calls) == 1
    prog, argv = execvp_calls[0]
    assert prog == "tmux"
    assert argv == ["tmux", "attach-session", "-t", "tctl-vllm-qwen3-9b"]


def test_console_raises_when_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """console() raises RuntimeError with a helpful message when session does not exist."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    class _DeadTmuxSession:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def exists(self) -> bool:
            return False

        def start(self, argv: object) -> None:
            pass

        def kill(self, **kw: object) -> None:
            pass

    monkeypatch.setattr(vm_mod, "TmuxSession", _DeadTmuxSession)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="no running session"):
        vm.console()


def test_logs_n_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """logs(n=5) prints the last 5 lines of the log file."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    lines = [f"line {i}" for i in range(20)]
    vm.log_path.write_text("\n".join(lines) + "\n")

    result = vm.logs(n=5)
    assert result == 0
    out = capsys.readouterr().out
    printed = [ln for ln in out.strip().splitlines() if ln]
    assert printed == lines[-5:]


def test_logs_follow_invokes_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """logs(follow=True) invokes subprocess.Popen with tail -f and waits."""
    import tctl.workloads.vllm.manager as vm_mod
    from tctl.workloads.vllm.manager import VllmManager

    popen_calls: list[list[str]] = []

    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0
    mock_proc.pid = 12345

    def fake_popen(cmd: list[str], **kw: object) -> MagicMock:
        popen_calls.append(cmd)
        return mock_proc

    monkeypatch.setattr(vm_mod.subprocess, "Popen", fake_popen)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.log_path.write_text("log line\n")

    result = vm.logs(follow=True)
    assert result == 0
    assert len(popen_calls) == 1
    assert popen_calls[0][0] == "tail"
    assert "-f" in popen_calls[0]
    assert str(vm.log_path) in popen_calls[0]


def test_logs_no_log_file_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """logs() returns 1 with an error message when the log file does not exist."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    # log_path does not exist

    result = vm.logs()
    assert result == 1
    err = capsys.readouterr().err
    assert "no log file" in err.lower() or str(vm.log_path) in err


# ---------------------------------------------------------------------------
# Prune tests
# ---------------------------------------------------------------------------


def test_logs_prune_keeps_last_n_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """prune(keep=50) with 200 lines → file has exactly 50 lines, inode preserved."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    lines = [f"line {i}\n" for i in range(200)]
    vm.log_path.write_text("".join(lines))

    inode_before = vm.log_path.stat().st_ino

    result = vm.logs(prune=True, keep=50)
    assert result == 0

    inode_after = vm.log_path.stat().st_ino
    assert inode_before == inode_after, "inode must be preserved (in-place rewrite)"

    content = vm.log_path.read_text()
    written_lines = content.splitlines()
    assert len(written_lines) == 50
    # Should be the last 50 lines
    assert written_lines[0] == "line 150"
    assert written_lines[-1] == "line 199"

    out = capsys.readouterr().out
    assert "kept last 50 lines" in out
    assert "was 200" in out


def test_logs_prune_all_truncates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """prune(prune_all=True) → file is 0 bytes, inode preserved."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.log_path.write_text("some\nlog\nlines\n")

    inode_before = vm.log_path.stat().st_ino

    result = vm.logs(prune=True, prune_all=True)
    assert result == 0

    inode_after = vm.log_path.stat().st_ino
    assert inode_before == inode_after, "inode must be preserved (in-place truncate)"

    assert vm.log_path.stat().st_size == 0, "file must be 0 bytes after prune --all"

    out = capsys.readouterr().out
    assert "removed everything" in out


def test_logs_prune_no_log_file_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """prune=True when log file doesn't exist → return 1 with stderr message."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    # log_path does not exist

    result = vm.logs(prune=True)
    assert result == 1
    err = capsys.readouterr().err
    assert "no log file" in err.lower() or str(vm.log_path) in err


def test_logs_prune_below_keep_threshold_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """prune(keep=100) with only 30 lines → file unchanged, message says 'nothing to do'."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    original_content = "".join(f"line {i}\n" for i in range(30))
    vm.log_path.write_text(original_content)

    result = vm.logs(prune=True, keep=100)
    assert result == 0

    # File must be unchanged
    assert vm.log_path.read_text() == original_content

    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert "30" in out  # mentions the number of lines
    assert "100" in out  # mentions the keep threshold


# ---------------------------------------------------------------------------
# _rolling_restart_session_path tests (AT-9 prep)
# ---------------------------------------------------------------------------


def test_rolling_restart_session_path_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_rolling_restart_session_path() returns ~/.tctl/vllm/rolling-restart/<pool>.json."""
    from tctl.workloads.vllm.manager import VllmManager

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    rc = _make_rc(profile_name="foo")
    mgr = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    p = mgr._rolling_restart_session_path("mypool")
    assert p == tmp_path / ".tctl" / "vllm" / "rolling-restart" / "mypool.json"
    assert p.parent.exists()


def test_rolling_restart_session_path_explicit_state_dir(tmp_path: Path) -> None:
    """_rolling_restart_session_path(state_dir=...) uses the given directory."""
    from tctl.workloads.vllm.manager import VllmManager

    rc = _make_rc(profile_name="foo")
    mgr = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    p = mgr._rolling_restart_session_path("mypool", state_dir=tmp_path / "custom")
    assert p == tmp_path / "custom" / "mypool.json"
    assert p.parent.exists()
