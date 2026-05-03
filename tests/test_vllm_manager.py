"""Unit tests for VllmManager — all run under VCTL_TEST_NO_SOCKET=1."""

from __future__ import annotations

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
