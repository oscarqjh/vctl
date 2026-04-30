"""LB lifecycle (start/stop/status) with self-IP guard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vctl.config.models import (
    LbAdmin,
    LbClient,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
)
from vctl.lb.manager import LbManager


def _lb(host: str = "10.0.0.1") -> LbHaproxy:
    return LbHaproxy(
        kind="haproxy",
        host=host,
        client=LbClient(bind_port=8080),
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
    )


@patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.2")
def test_start_refuses_on_wrong_host(mock_ip, tmp_path: Path) -> None:
    """AT-8 part 1."""
    mgr = LbManager(_lb(), state_dir=tmp_path, run_dir=tmp_path)
    with pytest.raises(RuntimeError) as exc:
        mgr.start(force=False)
    assert "10.0.0.1" in str(exc.value) and "10.0.0.2" in str(exc.value)


@patch("vctl.lb.manager.tmux_run_detached")
@patch("vctl.lb.manager.ensure_haproxy", return_value="/usr/bin/haproxy")
@patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.2")
def test_start_with_force_proceeds(mock_ip, mock_ens, mock_tmux, tmp_path: Path) -> None:
    """AT-8 part 2."""
    mgr = LbManager(_lb(), state_dir=tmp_path, run_dir=tmp_path)
    mgr.start(force=True)
    assert mock_tmux.called
    cfg = (tmp_path / "haproxy.cfg").read_text()
    assert "frontend http-in" in cfg


@patch("vctl.lb.manager.tmux_kill")
def test_stop(mock_kill, tmp_path: Path) -> None:
    mgr = LbManager(_lb(), state_dir=tmp_path, run_dir=tmp_path)
    mgr.stop()
    assert mock_kill.called
