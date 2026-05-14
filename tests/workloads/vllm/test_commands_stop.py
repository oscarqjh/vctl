"""tests/workloads/vllm/test_commands_stop.py — merged stop command tests (AT-8)."""

from __future__ import annotations

from pathlib import Path
import pytest


# ---------------------------------------------------------------------------
# AT-8: merged stop — drain + tmux-kill + process sweep
# ---------------------------------------------------------------------------


def _write_cfg(cfg_file: Path) -> None:
    cfg_file.write_text(
        "apiVersion: tctl/v1\n"
        "cluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 127.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_model: '*'}]\n"
        "vllm:\n  default_profile: null\n"
    )


def test_at8_stop_calls_drain_kill_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT-8: tctl vllm stop performs all 3 actions: drain + tmux-kill + sweep."""
    drain_calls: list[str] = []
    kill_calls: list[str] = []
    sweep_calls: list[bool] = []

    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._drain_from_lb",
        lambda profile, cfg: drain_calls.append(profile),
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._kill_tmux_session",
        lambda name: kill_calls.append(name),
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._sweep_local_vllm",
        lambda: sweep_calls.append(True),
    )

    cfg_file = tmp_path / "cluster.yaml"
    _write_cfg(cfg_file)

    import tctl.workloads.vllm.commands as cmds
    import argparse

    ns = argparse.Namespace(config=str(cfg_file), profile="myprofile")
    rc = cmds._cmd_stop(ns, [])
    assert rc == 0
    assert drain_calls == ["myprofile"]
    assert "tctl-vllm-myprofile" in kill_calls
    assert sweep_calls, "process sweep must run"


def test_at8_stop_sweep_runs_even_if_drain_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT-8: process sweep must run even when drain raises."""
    sweep_calls: list[bool] = []

    def _drain_raises(profile: str, cfg: object) -> None:
        raise RuntimeError("LB unreachable")

    monkeypatch.setattr("tctl.workloads.vllm.commands._drain_from_lb", _drain_raises)
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._kill_tmux_session", lambda name: None
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._sweep_local_vllm", lambda: sweep_calls.append(True)
    )

    cfg_file = tmp_path / "cluster.yaml"
    _write_cfg(cfg_file)

    import tctl.workloads.vllm.commands as cmds
    import argparse

    ns = argparse.Namespace(config=str(cfg_file), profile="myprofile")
    rc = cmds._cmd_stop(ns, [])
    # Non-fatal drain failure — stop still cleans up
    assert rc == 0
    assert sweep_calls, "sweep must run even when drain fails"


def test_at8_stop_no_profile_returns_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT-8: stop without a profile returns rc=2."""
    cfg_file = tmp_path / "cluster.yaml"
    _write_cfg(cfg_file)

    import tctl.workloads.vllm.commands as cmds
    import argparse

    ns = argparse.Namespace(config=str(cfg_file), profile=None)
    rc = cmds._cmd_stop(ns, [])
    assert rc == 2


def test_at8_stop_kill_called_with_correct_session_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT-8: session name format is tctl-vllm-<profile>."""
    killed: list[str] = []

    monkeypatch.setattr("tctl.workloads.vllm.commands._drain_from_lb", lambda p, c: None)
    monkeypatch.setattr("tctl.workloads.vllm.commands._kill_tmux_session", lambda n: killed.append(n))
    monkeypatch.setattr("tctl.workloads.vllm.commands._sweep_local_vllm", lambda: None)

    cfg_file = tmp_path / "cluster.yaml"
    _write_cfg(cfg_file)

    import tctl.workloads.vllm.commands as cmds
    import argparse

    ns = argparse.Namespace(config=str(cfg_file), profile="qwen3-9b")
    rc = cmds._cmd_stop(ns, [])
    assert rc == 0
    assert killed == ["tctl-vllm-qwen3-9b"]


# ---------------------------------------------------------------------------
# Unit tests for individual stop helper functions
# ---------------------------------------------------------------------------


def test_kill_tmux_session_calls_sess_kill_when_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[str] = []

    class FakeSession:
        def __init__(self, name: str) -> None:
            self.name = name

        def exists(self) -> bool:
            return True

        def kill(self, tree: bool = False, grace_s: float = 5.0) -> None:
            killed.append(self.name)

    monkeypatch.setattr("tctl.workloads.vllm.commands.TmuxSession", FakeSession)

    from tctl.workloads.vllm.commands import _kill_tmux_session

    _kill_tmux_session("tctl-vllm-test")
    assert killed == ["tctl-vllm-test"]


def test_kill_tmux_session_noop_when_session_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[str] = []

    class FakeSession:
        def __init__(self, name: str) -> None:
            pass

        def exists(self) -> bool:
            return False

        def kill(self, **kwargs: object) -> None:
            killed.append("should not reach")

    monkeypatch.setattr("tctl.workloads.vllm.commands.TmuxSession", FakeSession)

    from tctl.workloads.vllm.commands import _kill_tmux_session

    _kill_tmux_session("absent-session")
    assert killed == []
