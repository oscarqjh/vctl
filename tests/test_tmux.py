"""Unit tests for TmuxSession (Tasks 1-5) + integration tests."""

from __future__ import annotations

import subprocess

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Task 1 — _validate_tmux_name + TmuxSession.__init__
# ---------------------------------------------------------------------------


def test_validate_tmux_name_valid() -> None:
    from vctl.tmux import _validate_tmux_name

    _validate_tmux_name("vctl-lb")  # no exception
    _validate_tmux_name("vctl.lb")  # dots allowed
    _validate_tmux_name("sess_1")  # underscores allowed


def test_validate_tmux_name_rejects_slash() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad/name")


def test_validate_tmux_name_rejects_empty() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("")


def test_validate_tmux_name_rejects_space() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad name")


# AT-10: TmuxSession raises ValueError on invalid name at __init__ time
@pytest.mark.parametrize("bad_name", ["bad/name", "bad name", "", "has\ttab"])
def test_at10_invalid_name_raises_at_init(bad_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(a) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession

    with pytest.raises(ValueError, match="invalid tmux session name"):
        TmuxSession(bad_name)
    assert not run_calls
