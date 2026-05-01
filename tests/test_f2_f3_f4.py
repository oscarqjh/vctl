"""Unit tests for F2 (tmux_name param), F3 (teardown helpers), F4 (status UX)."""

from __future__ import annotations

import argparse
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vctl.commands.lb import run as lb_run
from vctl.config.models import (
    LbAdmin,
    LbClient,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
)
from vctl.lb.manager import LbManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_mgr(
    tmp_path: Path, host: str = "10.0.0.1", tmux_name: str = "vctl-lb"
) -> LbManager:
    return LbManager(
        _lb(host),
        state_dir=tmp_path / "state",
        run_dir=tmp_path / "run",
        tmux_name=tmux_name,
    )


# ---------------------------------------------------------------------------
# F2: tmux_name parameter
# ---------------------------------------------------------------------------


class TestF2TmuxNameParameter:
    def test_default_tmux_name_is_vctl_lb(self, tmp_path: Path) -> None:
        """F2: default tmux_name is 'vctl-lb'."""
        mgr = LbManager(_lb(), state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        assert mgr.tmux_name == "vctl-lb"

    def test_custom_tmux_name_stored(self, tmp_path: Path) -> None:
        """F2: custom tmux_name is stored on manager."""
        mgr = _make_mgr(tmp_path, tmux_name="vctl-lb-test-abc")
        assert mgr.tmux_name == "vctl-lb-test-abc"

    def test_invalid_tmux_name_raises_value_error(self, tmp_path: Path) -> None:
        """F2: tmux_name with a space raises ValueError (E3 regex)."""
        with pytest.raises(ValueError, match="invalid tmux session name"):
            _make_mgr(tmp_path, tmux_name="bad name")

    def test_invalid_tmux_name_slash_raises(self, tmp_path: Path) -> None:
        """F2: tmux_name with a slash raises ValueError."""
        with pytest.raises(ValueError, match="invalid tmux session name"):
            _make_mgr(tmp_path, tmux_name="bad/name")

    @patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
    @patch("vctl.lb.manager.tmux_run_detached_argv")
    @patch("vctl.lb.manager.ensure_haproxy", return_value="/usr/bin/haproxy")
    def test_start_uses_custom_tmux_name(
        self, mock_haproxy: MagicMock, mock_tmux: MagicMock, mock_ip: MagicMock, tmp_path: Path
    ) -> None:
        """F2: start() calls tmux_run_detached_argv with self.tmux_name."""
        mgr = _make_mgr(tmp_path, tmux_name="vctl-lb-test-abc")
        mgr.start(force=True)
        assert mock_tmux.called
        call_args = mock_tmux.call_args
        assert call_args[0][0] == "vctl-lb-test-abc"

    @patch("vctl.lb.manager.tmux_kill")
    def test_stop_uses_custom_tmux_name(
        self, mock_kill: MagicMock, tmp_path: Path
    ) -> None:
        """F2: stop() calls tmux_kill with self.tmux_name."""
        mgr = _make_mgr(tmp_path, tmux_name="vctl-lb-test-xyz")
        mgr.stop()
        mock_kill.assert_called_once_with("vctl-lb-test-xyz")

    @patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
    @patch("vctl.lb.manager.tmux_session_exists", return_value=True)
    @patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
    def test_status_uses_custom_tmux_name(
        self,
        mock_conn: MagicMock,
        mock_tmux_exists: MagicMock,
        mock_ip: MagicMock,
        tmp_path: Path,
    ) -> None:
        """F2: status() checks tmux_session_exists with self.tmux_name."""
        mgr = _make_mgr(tmp_path, tmux_name="vctl-lb-test-zzz")
        st = mgr.status()
        mock_tmux_exists.assert_called_once_with("vctl-lb-test-zzz")
        assert st["tmux_managed"] is True

    def test_module_constant_still_exists(self) -> None:
        """F2: _TMUX_NAME module constant is still 'vctl-lb' for production callers."""
        from vctl.lb.manager import _TMUX_NAME

        assert _TMUX_NAME == "vctl-lb"


# ---------------------------------------------------------------------------
# F3: force-cleanup helper
# ---------------------------------------------------------------------------


class TestF3ForceCleanup:
    def test_force_cleanup_kills_matching_process(self, tmp_path: Path) -> None:
        """F3: _force_cleanup_haproxy_for_cfg kills proc with matching cfg in cmdline."""
        from tests.test_integration_endtoend import _force_cleanup_haproxy_for_cfg

        cfg_path = tmp_path / "haproxy.cfg"
        killed: list[int] = []

        fake_proc = MagicMock()
        fake_proc.info = {
            "name": "haproxy",
            "cmdline": ["haproxy", "-f", str(cfg_path)],
        }

        def fake_send_signal(sig: int) -> None:
            killed.append(sig)

        fake_proc.send_signal = fake_send_signal

        with patch("psutil.process_iter", return_value=[fake_proc]):
            _force_cleanup_haproxy_for_cfg(cfg_path)

        assert signal.SIGKILL in killed

    def test_force_cleanup_ignores_different_cfg(self, tmp_path: Path) -> None:
        """F3: _force_cleanup_haproxy_for_cfg ignores process with different cfg."""
        from tests.test_integration_endtoend import _force_cleanup_haproxy_for_cfg

        cfg_path = tmp_path / "haproxy.cfg"
        other_cfg = tmp_path / "other.cfg"
        killed: list[int] = []

        fake_proc = MagicMock()
        fake_proc.info = {
            "name": "haproxy",
            "cmdline": ["haproxy", "-f", str(other_cfg)],
        }

        def fake_send_signal(sig: int) -> None:
            killed.append(sig)

        fake_proc.send_signal = fake_send_signal

        with patch("psutil.process_iter", return_value=[fake_proc]):
            _force_cleanup_haproxy_for_cfg(cfg_path)

        assert not killed, "must not kill haproxy with a different cfg path"

    def test_force_cleanup_ignores_non_haproxy(self, tmp_path: Path) -> None:
        """F3: _force_cleanup_haproxy_for_cfg ignores processes not named haproxy."""
        from tests.test_integration_endtoend import _force_cleanup_haproxy_for_cfg

        cfg_path = tmp_path / "haproxy.cfg"
        killed: list[int] = []

        fake_proc = MagicMock()
        fake_proc.info = {
            "name": "python",
            "cmdline": ["python", "-f", str(cfg_path)],
        }

        def fake_send_signal(sig: int) -> None:
            killed.append(sig)

        fake_proc.send_signal = fake_send_signal

        with patch("psutil.process_iter", return_value=[fake_proc]):
            _force_cleanup_haproxy_for_cfg(cfg_path)

        assert not killed, "must not kill non-haproxy processes"

    def test_force_cleanup_suppresses_no_such_process(self, tmp_path: Path) -> None:
        """F3: _force_cleanup_haproxy_for_cfg swallows NoSuchProcess gracefully."""
        import psutil

        from tests.test_integration_endtoend import _force_cleanup_haproxy_for_cfg

        cfg_path = tmp_path / "haproxy.cfg"

        fake_proc = MagicMock()
        fake_proc.info = {
            "name": "haproxy",
            "cmdline": ["haproxy", "-f", str(cfg_path)],
        }
        fake_proc.send_signal.side_effect = psutil.NoSuchProcess(pid=9999)

        with patch("psutil.process_iter", return_value=[fake_proc]):
            # Must not raise
            _force_cleanup_haproxy_for_cfg(cfg_path)


# ---------------------------------------------------------------------------
# F4: status() is_local_host + CLI output
# ---------------------------------------------------------------------------


class TestF4StatusIsLocalHost:
    @patch("vctl.lb.manager.tmux_session_exists", return_value=False)
    @patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
    @patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
    def test_status_is_local_host_true_when_ip_matches(
        self,
        mock_ip: MagicMock,
        mock_conn: MagicMock,
        mock_tmux: MagicMock,
        tmp_path: Path,
    ) -> None:
        """F4: is_local_host=True when detect_self_ip() == lb.host."""
        mgr = _make_mgr(tmp_path, host="10.0.0.1")
        st = mgr.status()
        assert st["is_local_host"] is True

    @patch("vctl.lb.manager.tmux_session_exists", return_value=False)
    @patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
    @patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.99")
    def test_status_is_local_host_false_when_ip_differs(
        self,
        mock_ip: MagicMock,
        mock_conn: MagicMock,
        mock_tmux: MagicMock,
        tmp_path: Path,
    ) -> None:
        """F4: is_local_host=False when detect_self_ip() != lb.host."""
        mgr = _make_mgr(tmp_path, host="10.0.0.1")
        st = mgr.status()
        assert st["is_local_host"] is False


class TestF4StatusCliOutput:
    """F4: CLI `vctl lb status` output format depends on is_local_host."""

    def _make_status_dict(self, is_local_host: bool) -> dict[str, object]:
        return {
            "running": False,
            "pid": None,
            "pid_alive": False,
            "admin_reachable": False,
            "tmux_managed": False,
            "cfg_path": "/tmp/haproxy.cfg",
            "admin_bind": "0.0.0.0:9001",
            "is_local_host": is_local_host,
        }

    def test_local_host_prints_all_fields(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        """F4: when is_local_host=True, all fields are printed."""
        status_dict = self._make_status_dict(is_local_host=True)
        ns = argparse.Namespace(config=None, profile=None)

        with patch("vctl.commands.lb._manager") as mock_mgr_fn:
            mock_mgr = MagicMock()
            mock_mgr.status.return_value = status_dict
            mock_mgr.lb.host = "10.0.0.1"
            mock_mgr.lb.admin.bind_port = 9001
            mock_mgr_fn.return_value = (mock_mgr, MagicMock(), MagicMock())
            rc = lb_run(ns, ["status"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "pid:" in captured.out
        assert "pid_alive:" in captured.out
        assert "tmux_managed:" in captured.out

    def test_remote_host_prints_compact_line(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        """F4: when is_local_host=False, compact 'remote LB' line is printed."""
        status_dict = self._make_status_dict(is_local_host=False)
        ns = argparse.Namespace(config=None, profile=None)

        with patch("vctl.commands.lb._manager") as mock_mgr_fn:
            mock_mgr = MagicMock()
            mock_mgr.status.return_value = status_dict
            mock_mgr.lb.host = "10.0.0.1"
            mock_mgr.lb.admin.bind_port = 9001
            mock_mgr_fn.return_value = (mock_mgr, MagicMock(), MagicMock())
            rc = lb_run(ns, ["status"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "remote LB" in captured.out
        assert "10.0.0.1:9001" in captured.out
        assert "pid:" not in captured.out
        assert "pid_alive:" not in captured.out
        assert "pidfile/tmux are local-only" in captured.out

    def test_remote_host_shows_admin_reachable(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        """F4: remote-LB line includes admin_reachable value."""
        status_dict = self._make_status_dict(is_local_host=False)
        status_dict["admin_reachable"] = True
        ns = argparse.Namespace(config=None, profile=None)

        with patch("vctl.commands.lb._manager") as mock_mgr_fn:
            mock_mgr = MagicMock()
            mock_mgr.status.return_value = status_dict
            mock_mgr.lb.host = "10.0.0.1"
            mock_mgr.lb.admin.bind_port = 9001
            mock_mgr_fn.return_value = (mock_mgr, MagicMock(), MagicMock())
            lb_run(ns, ["status"])

        captured = capsys.readouterr()
        assert "admin_reachable=True" in captured.out
