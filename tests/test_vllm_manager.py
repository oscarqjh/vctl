"""Unit tests for VllmManager — all run under VCTL_TEST_NO_SOCKET=1."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import MagicMock

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
    rc.cluster.state_dir = "/tmp/vctl-state"
    rc.parallelism.data_parallel = 1
    rc.parallelism.tensor_parallel = 1
    rc.parallelism.api_server_count = None
    rc.resources.cuda_visible_devices = "0"
    rc.vllm_args = {}
    rc.env = {}
    return rc


def test_vllm_manager_init_creates_run_dir(tmp_path: Path) -> None:
    """__init__ creates run_dir/vllm/ with parents=True exist_ok=True."""
    from vctl.vllm_manager import VllmManager

    run_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    rc = _make_rc()
    VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    assert (run_dir / "vllm").is_dir()
    VllmManager(rc, state_dir=state_dir, run_dir=run_dir)


def test_vllm_manager_init_computes_state_paths(tmp_path: Path) -> None:
    """__init__ pre-computes all four state file paths under run_dir/vllm/."""
    from vctl.vllm_manager import VllmManager

    rc = _make_rc(profile_name="qwen3-9b")
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vllm_dir = tmp_path / "run" / "vllm"
    assert vm.pid_path == vllm_dir / "qwen3-9b.pid"
    assert vm.log_path == vllm_dir / "qwen3-9b.log"
    assert vm.cmd_path == vllm_dir / "qwen3-9b.cmd.json"
    assert vm.host_path == vllm_dir / "qwen3-9b.host"


def test_vllm_manager_init_validates_tmux_name(tmp_path: Path) -> None:
    """__init__ raises ValueError for a profile name that produces an invalid tmux name."""
    from vctl.vllm_manager import VllmManager

    rc = _make_rc(profile_name="bad name!")
    with pytest.raises(ValueError, match="invalid tmux session name"):
        VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_vllm_manager_init_session_name(tmp_path: Path) -> None:
    """Session name is vctl-vllm-<profile_name>."""
    from vctl.vllm_manager import VllmManager

    rc = _make_rc(profile_name="qwen3-9b")
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    assert vm.session_name == "vctl-vllm-qwen3-9b"


def test_start_refuses_when_session_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() raises RuntimeError (exit 4) when tmux session already exists."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: True)
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="already running"):
        vm.start()


def test_start_writes_all_four_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start() writes pid, log (via pipe-pane), cmd.json, and host state files."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
    monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)
    monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: None)

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
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
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
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
    monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)

    killed: list[str] = []
    monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: killed.append(name))
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
    monkeypatch.setattr(vm_mod.psutil, "process_iter", lambda attrs: [])

    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_TIMEOUT", 0.1)
    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_INTERVAL", 0.05)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="timed out"):
        vm.start()

    assert "vctl-vllm-qwen3-9b" in killed, "session must be killed on timeout"


def test_start_wait_for_ready_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() kills tmux session and unlinks state files when _wait_for_ready raises."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
    monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)

    killed: list[str] = []
    monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: killed.append(name))

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
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

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
    assert "vctl-vllm-qwen3-9b" in killed
