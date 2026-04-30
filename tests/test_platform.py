"""Platform helpers — IP detection, tmux, which."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from vctl.platform import (
    detect_self_ip,
    tmux_kill,
    tmux_run_detached,
    tmux_session_exists,
    which,
)


def test_detect_self_ip_returns_string() -> None:
    ip = detect_self_ip()
    assert isinstance(ip, str)
    assert ip.count(".") == 3 or ":" in ip


@patch("shutil.which", return_value="/usr/bin/haproxy")
def test_which_returns_path(mock_which) -> None:
    assert which("haproxy") == "/usr/bin/haproxy"


@patch("shutil.which", return_value=None)
def test_which_raises_when_missing(mock_which) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        which("definitely-not-on-path-zzzz")


@patch("subprocess.run")
def test_tmux_session_exists_true(mock_run) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    assert tmux_session_exists("lb") is True


@patch("subprocess.run")
def test_tmux_session_exists_false(mock_run) -> None:
    mock_run.return_value = MagicMock(returncode=1)
    assert tmux_session_exists("lb") is False


@patch("subprocess.run")
def test_tmux_run_detached_invokes_new_session(mock_run) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    tmux_run_detached("lb", "haproxy -f /tmp/h.cfg")
    args = mock_run.call_args[0][0]
    assert args[:3] == ["tmux", "new-session", "-d"]
    assert "lb" in args


@patch("subprocess.run")
def test_tmux_kill(mock_run) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    tmux_kill("lb")
    assert mock_run.called
