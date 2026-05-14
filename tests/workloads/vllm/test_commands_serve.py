"""tests/workloads/vllm/test_commands_serve.py — tctl vllm serve tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# _resolve_ready_timeout
# ---------------------------------------------------------------------------


def test_resolve_ready_timeout_from_rc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """VLLM_ENGINE_READY_TIMEOUT_S in rc.env takes precedence over OS env."""
    from tctl.workloads.vllm.commands import _resolve_ready_timeout

    monkeypatch.delenv("TCTL_READY_TIMEOUT", raising=False)
    monkeypatch.delenv("VLLM_ENGINE_READY_TIMEOUT_S", raising=False)

    rc = MagicMock()
    rc.env = {"VLLM_ENGINE_READY_TIMEOUT_S": "1800"}
    assert _resolve_ready_timeout(rc) == 1800.0


def test_resolve_ready_timeout_from_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCTL_READY_TIMEOUT OS env used when rc.env has no override."""
    from tctl.workloads.vllm.commands import _resolve_ready_timeout

    monkeypatch.setenv("TCTL_READY_TIMEOUT", "600")
    monkeypatch.delenv("VLLM_ENGINE_READY_TIMEOUT_S", raising=False)

    rc = MagicMock()
    rc.env = {}
    assert _resolve_ready_timeout(rc) == 600.0


def test_resolve_ready_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default is 1800s when no env overrides are set."""
    from tctl.workloads.vllm.commands import _resolve_ready_timeout

    monkeypatch.delenv("TCTL_READY_TIMEOUT", raising=False)
    monkeypatch.delenv("VLLM_ENGINE_READY_TIMEOUT_S", raising=False)

    rc = MagicMock()
    rc.env = {}
    assert _resolve_ready_timeout(rc) == 1800.0


def test_resolve_ready_timeout_rc_env_wins_over_os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc.env wins over TCTL_READY_TIMEOUT."""
    from tctl.workloads.vllm.commands import _resolve_ready_timeout

    monkeypatch.setenv("TCTL_READY_TIMEOUT", "300")
    rc = MagicMock()
    rc.env = {"VLLM_ENGINE_READY_TIMEOUT_S": "900"}
    assert _resolve_ready_timeout(rc) == 900.0


def test_resolve_ready_timeout_bad_value_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid timeout value logs a warning and falls back to 1800."""
    from tctl.workloads.vllm.commands import _resolve_ready_timeout

    monkeypatch.delenv("TCTL_READY_TIMEOUT", raising=False)
    rc = MagicMock()
    rc.env = {"VLLM_ENGINE_READY_TIMEOUT_S": "not-a-number"}
    result = _resolve_ready_timeout(rc)
    assert result == 1800.0


# ---------------------------------------------------------------------------
# serve dispatch: sub-verbs
# ---------------------------------------------------------------------------


def test_serve_sub_verb_status_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve status argv dispatches to _cmd_serve_status."""
    import tctl.workloads.vllm.commands as cmds

    dispatched: list[str] = []

    def fake_status(ns: argparse.Namespace, rest: list[str]) -> int:
        dispatched.append("status")
        return 0

    monkeypatch.setattr(cmds, "_cmd_serve_status", fake_status)
    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    rc = cmds._cmd_serve(ns, ["status"])
    assert rc == 0
    assert dispatched == ["status"]


def test_serve_sub_verb_restart_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve restart argv dispatches to _cmd_serve_restart."""
    import tctl.workloads.vllm.commands as cmds

    dispatched: list[str] = []

    def fake_restart(ns: argparse.Namespace, rest: list[str]) -> int:
        dispatched.append("restart")
        return 0

    monkeypatch.setattr(cmds, "_cmd_serve_restart", fake_restart)
    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    rc = cmds._cmd_serve(ns, ["restart"])
    assert rc == 0
    assert dispatched == ["restart"]


def test_serve_no_stop_sub_verb(tmp_path: Path) -> None:
    """'stop' must NOT be a serve sub-verb (merged into tctl vllm stop)."""
    from tctl.workloads.vllm.commands import _SERVE_SUB_VERBS

    assert "stop" not in _SERVE_SUB_VERBS, "stop must be a top-level verb, not serve sub-verb"


def test_serve_has_correct_sub_verbs() -> None:
    """serve sub-verbs are exactly: status, restart, console, logs."""
    from tctl.workloads.vllm.commands import _SERVE_SUB_VERBS

    assert {"status", "restart", "console", "logs"} == _SERVE_SUB_VERBS


# ---------------------------------------------------------------------------
# detached start
# ---------------------------------------------------------------------------


def test_detached_start_calls_vllm_manager_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_serve_detached() instantiates VllmManager and calls start()."""
    import tctl.workloads.vllm.commands as cmds
    import tctl.workloads.vllm.manager as vm_mod
    from tests.workloads.vllm.test_manager import _make_rc

    rc_obj = _make_rc()
    start_called: list[bool] = []

    class FakeVllmManager:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def start(self) -> None:
            start_called.append(True)

    monkeypatch.setattr(vm_mod, "VllmManager", FakeVllmManager)

    # Patch resolve at the source module level (lazily imported inside function)
    monkeypatch.setattr("tctl.resolver.resolve", lambda *a, **kw: rc_obj)
    monkeypatch.setattr(
        "tctl.workloads.haproxy.routing.pool_for_model",
        lambda lb, model: MagicMock(name="default"),
    )

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    rc_code = cmds._serve_detached(ns, argparse.Namespace(skip_preflight=True, foreground=False))

    assert start_called, "VllmManager.start() must be called for detached start"
    assert rc_code == 0


# ---------------------------------------------------------------------------
# serve logs
# ---------------------------------------------------------------------------


def test_serve_logs_prune_calls_vm_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve logs --prune calls vm.logs(prune=True)."""
    import tctl.workloads.vllm.commands as cmds
    import tctl.workloads.vllm.manager as vm_mod
    from tests.workloads.vllm.test_manager import _make_rc

    rc_obj = _make_rc()
    logs_kwargs: list[dict[str, object]] = []

    class FakeVllmManager:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def logs(self, **kwargs: object) -> int:
            logs_kwargs.append(dict(kwargs))
            return 0

    monkeypatch.setattr(vm_mod, "VllmManager", FakeVllmManager)
    monkeypatch.setattr("tctl.resolver.resolve", lambda *a, **kw: rc_obj)

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    rc = cmds._cmd_serve_logs(ns, ["--prune"])

    assert rc == 0
    assert logs_kwargs, "vm.logs must be called"
    assert logs_kwargs[0].get("prune") is True


def test_serve_logs_all_without_prune_rejected(tmp_path: Path) -> None:
    """serve logs --all without --prune → exit 2."""
    import tctl.workloads.vllm.commands as cmds

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    with pytest.raises(SystemExit) as exc_info:
        cmds._cmd_serve_logs(ns, ["--all"])
    assert exc_info.value.code == 2


def test_serve_logs_prune_with_follow_rejected(tmp_path: Path) -> None:
    """serve logs --prune --follow → exit 2."""
    import tctl.workloads.vllm.commands as cmds

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile="qwen3-9b")
    with pytest.raises(SystemExit) as exc_info:
        cmds._cmd_serve_logs(ns, ["--prune", "--follow"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# register_all: verb count
# ---------------------------------------------------------------------------


def test_register_all_registers_seven_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    """register_all must register exactly 7 verbs."""
    import tctl.workloads.vllm.commands as cmds

    p = argparse.ArgumentParser(prog="tctl vllm")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    cmds.register_all(sub)
    # Each registered verb should appear in --help
    with pytest.raises(SystemExit):
        p.parse_args(["--help"])
    out = capsys.readouterr().out
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "rolling-restart"):
        assert verb in out, f"verb {verb!r} missing from help"
