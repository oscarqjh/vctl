"""LB installer tests with mocked which/run."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tctl.workloads.haproxy.installer import ensure_haproxy


@patch("shutil.which", side_effect=lambda b: "/usr/bin/haproxy" if b == "haproxy" else None)
def test_ensure_haproxy_already_on_path(mock_which) -> None:
    assert ensure_haproxy() == "/usr/bin/haproxy"


@patch("shutil.which", side_effect=lambda b: "/usr/bin/mamba" if b == "mamba" else None)
@patch("subprocess.run")
def test_ensure_haproxy_via_mamba(mock_run, mock_which) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    with patch(
        "tctl.workloads.haproxy.installer._post_install_lookup",
        return_value="/opt/conda/bin/haproxy",
    ):
        assert ensure_haproxy() == "/opt/conda/bin/haproxy"
    assert any("mamba" in call.args[0][0] for call in mock_run.call_args_list)


@patch("shutil.which", return_value=None)
def test_ensure_haproxy_falls_through_to_error(mock_which) -> None:
    with (
        patch(
            "tctl.workloads.haproxy.installer._build_from_source",
            side_effect=RuntimeError("no toolchain"),
        ),
        pytest.raises(RuntimeError),
    ):
        ensure_haproxy()
