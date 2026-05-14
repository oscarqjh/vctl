"""tests/workloads/vllm/test_commands_rolling_restart.py — rolling-restart tests."""

from __future__ import annotations

import argparse as _argparse
import json as _json
import subprocess as _subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lb_mgr(tmp_path: Path) -> object:
    from tctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from tctl.workloads.haproxy.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


# ---------------------------------------------------------------------------
# Session path uses VllmManager._rolling_restart_session_path
# ---------------------------------------------------------------------------


def test_session_path_via_manager_method(tmp_path: Path) -> None:
    """Rolling-restart session path is derived from VllmManager._rolling_restart_session_path."""
    from tctl.workloads.vllm.manager import VllmManager

    vm = VllmManager.__new__(VllmManager)
    # Use a tmp_path-based state_dir to avoid creating ~/.tctl dirs in test
    p = vm._rolling_restart_session_path("mypool", state_dir=tmp_path / "rr")
    assert p.name == "mypool.json"
    assert p.parent == tmp_path / "rr"


def test_rolling_restart_uses_manager_session_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_cmd_rolling_restart uses VllmManager._rolling_restart_session_path not _SESSION_DIR."""
    import inspect

    from tctl.workloads.vllm import commands as cmds_mod

    src = inspect.getsource(cmds_mod._cmd_rolling_restart)
    assert "_SESSION_DIR" not in src, "_SESSION_DIR must not appear in tctl rolling-restart"
    assert "_rolling_restart_session_path" in src


# ---------------------------------------------------------------------------
# _verify_ep_up (exercised through the inline closure)
# ---------------------------------------------------------------------------


def test_rolling_restart_dry_run_no_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must not ssh; session file not written."""
    from tctl.workloads.haproxy.state import BackendState
    from tctl.workloads.vllm import commands as cmds

    mgr = _make_lb_mgr(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Pre-populate pool with one endpoint
    bs = BackendState(state_dir, "10.0.0.1", pool="mypool")
    bs.add("10.0.0.2:8000")

    ssh_calls: list[list[str]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> _subprocess.CompletedProcess[str]:
        ssh_calls.append(argv)
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import tctl.workloads.haproxy.manager as lbm

    monkeypatch.setattr(lbm, "LbManager", lambda *a, **kw: mgr)

    from tctl.config.models import ClusterSection, LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool, VllmCluster
    from tctl.config.models import ClusterFile

    mock_rc = type("RC", (), {
        "cluster": ClusterSection(venv="/v", state_dir=str(state_dir)),
        "lb": mgr.lb,
        "profile_name": "test",
    })()

    monkeypatch.setattr("tctl.resolver.resolve", lambda *a, **kw: mock_rc)

    # Patch _cmd_rolling_restart's subprocess import to spy on ssh
    import subprocess as subprocess_mod
    monkeypatch.setattr(subprocess_mod, "run", _fake_run)

    ns = _argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile="test",
        pool="mypool",
        dry_run=True,
        status=False,
        abort=False,
        fresh=False,
        quiet=False,
        ssh_user="",
        vllm_timeout=600,
        ready_timeout=60,
        remote_tctl_path=None,
    )

    # Use a tmp rolling-restart path
    from tctl.workloads.vllm.manager import VllmManager
    vm = VllmManager.__new__(VllmManager)
    session_path = vm._rolling_restart_session_path("mypool", state_dir=tmp_path / "rr")
    assert not session_path.exists(), "session file must not exist before dry-run"

    rc = cmds._cmd_rolling_restart(ns, [])
    assert rc == 0
    assert ssh_calls == [], "no SSH calls in dry-run"
    assert not session_path.exists(), "session file must not be created in dry-run"


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


def test_rolling_restart_importable() -> None:
    from tctl.workloads.vllm.commands import _cmd_rolling_restart, _register_rolling_restart

    assert callable(_cmd_rolling_restart)
    assert callable(_register_rolling_restart)


def test_rolling_restart_remote_cmd_uses_tctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote command uses 'tctl vllm serve restart', not 'vctl serve restart'."""
    import inspect

    from tctl.workloads.vllm import commands as cmds_mod

    src = inspect.getsource(cmds_mod._cmd_rolling_restart)
    assert "tctl vllm serve restart" in src
    assert "vctl serve restart" not in src
