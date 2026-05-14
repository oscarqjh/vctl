"""Tests for tctl.workloads.lmms.commands (Task 8 / AT-4 coverage)."""

from __future__ import annotations

import argparse

import pytest


def test_lmms_commands_importable() -> None:
    from tctl.workloads.lmms.commands import register_all

    assert callable(register_all)


def test_lmms_session_name_uses_tctl_prefix() -> None:
    import inspect

    from tctl.workloads.lmms import commands as _cmds

    src = inspect.getsource(_cmds)
    assert "tctl-lmms" in src
    assert "vctl-lmmseval" not in src


def test_lms_tmux_session_name() -> None:
    from tctl.workloads.lmms.commands import _TMUX_NAME

    assert _TMUX_NAME == "tctl-lmms"


def test_register_all_adds_expected_subcommands() -> None:
    from tctl.workloads.lmms.commands import register_all

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="verb")
    register_all(sub)
    # parse each verb to confirm it is registered
    for verb in ("run-loop", "stop", "status"):
        ns = p.parse_args([verb])
        assert ns.verb == verb


# AT-4 (partial — full AT-4 requires cli.py from Task 9)
def test_at4_lmms_run_loop_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    from tctl.workloads.lmms import commands as _cmds

    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, rest: called.append(1) or 0)

    from tctl.workloads.lmms import run

    ns = argparse.Namespace()
    rc = run(ns, ["run-loop"])
    assert rc == 0
    assert called, "lmms run-loop was not dispatched"


def test_at4_lmms_stop_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    from tctl.workloads.lmms import commands as _cmds

    monkeypatch.setattr(_cmds, "_cmd_stop", lambda ns, rest: called.append(1) or 0)

    from tctl.workloads.lmms import run

    ns = argparse.Namespace()
    rc = run(ns, ["stop"])
    assert rc == 0
    assert called, "lmms stop was not dispatched"


def test_at4_lmms_status_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    from tctl.workloads.lmms import commands as _cmds

    monkeypatch.setattr(_cmds, "_cmd_status", lambda ns, rest: called.append(1) or 0)

    from tctl.workloads.lmms import run

    ns = argparse.Namespace()
    rc = run(ns, ["status"])
    assert rc == 0
    assert called, "lmms status was not dispatched"
