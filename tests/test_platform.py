"""Platform helpers — IP detection, which."""

from __future__ import annotations

from unittest.mock import patch

from vctl.platform import detect_self_ip, which


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
