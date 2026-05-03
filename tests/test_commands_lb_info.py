"""Comprehensive tests for `vctl lb info` unified dashboard (v0.2.4)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vctl.commands import lb as lb_cmd
from vctl.commands import lb_scaling
from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mgr(tmp_path: Path, pools: list[Pool] | None = None) -> tuple[LbManager, BackendState]:
    if pools is None:
        pools = [Pool(name="default", served_model="M/Default", bind_port=8080)]
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=pools,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1")
    return mgr, bs


def _running_status() -> dict[str, object]:
    return {
        "running": True,
        "pid": 12345,
        "pid_alive": True,
        "admin_reachable": True,
        "tmux_managed": True,
        "cfg_path": "/tmp/haproxy.cfg",
        "admin_bind": "0.0.0.0:9001",
        "is_local_host": True,
    }


def _stopped_status() -> dict[str, object]:
    return {
        "running": False,
        "pid": None,
        "pid_alive": False,
        "admin_reachable": False,
        "tmux_managed": False,
        "cfg_path": "/tmp/haproxy.cfg",
        "admin_bind": "0.0.0.0:9001",
        "is_local_host": True,
    }


def _capture_info(
    mgr: LbManager,
    bs: BackendState,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: dict[str, object],
    live_registry: dict[str, set[str]] | None = None,
    haproxy_stats: dict[str, dict[str, dict[str, int]]] | None = None,
    client: object | None = MagicMock(),
    vllm_metrics: dict[str, int | None] | None = None,
) -> tuple[int, str]:
    """Run _do_info with mocked dependencies; return (rc, output_text)."""
    from rich.console import Console

    buf = io.StringIO()
    fake_console = Console(file=buf, force_terminal=False)

    monkeypatch.setattr(mgr, "status", lambda: status)

    if client is not None:
        monkeypatch.setattr(lb_scaling, "_client", lambda m: client)
    else:
        monkeypatch.setattr(lb_scaling, "_client", lambda m: None)

    if live_registry is not None:
        monkeypatch.setattr(lb_cmd, "_build_live_registry", lambda cli: live_registry)
    else:
        monkeypatch.setattr(lb_cmd, "_build_live_registry", lambda cli: {})

    if haproxy_stats is not None:
        monkeypatch.setattr(lb_cmd, "_fetch_haproxy_stats", lambda cli: haproxy_stats)
    else:
        monkeypatch.setattr(lb_cmd, "_fetch_haproxy_stats", lambda cli: {})

    if vllm_metrics is not None:
        monkeypatch.setattr(
            "vctl.lb.probe.fetch_vllm_metrics",
            lambda h, p, **kw: vllm_metrics,
        )
    else:
        monkeypatch.setattr(
            "vctl.lb.probe.fetch_vllm_metrics",
            lambda h, p, **kw: {"running": None, "waiting": None},
        )

    rc = lb_cmd._do_info(mgr, bs, _console=fake_console)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path: 1 pool, 2 backends, haproxy stats + vllm metrics
# ---------------------------------------------------------------------------


def test_info_happy_path_two_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: 2 live backends with scur/qcur/running/waiting."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")
    pbs.add("10.1.2.6:8000")

    registry = {"pool_default": {"10.1.2.5:8000", "10.1.2.6:8000"}}
    stats: dict[str, dict[str, dict[str, int]]] = {
        "pool_default": {
            "b_10_1_2_5_8000": {"scur": 2, "qcur": 0, "lastchg": 3, "ep": "10.1.2.5:8000"},
            "b_10_1_2_6_8000": {"scur": 1, "qcur": 1, "lastchg": 5, "ep": "10.1.2.6:8000"},
        }
    }

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry=registry,
        haproxy_stats=stats,
        vllm_metrics={"running": 3, "waiting": 1},
    )

    assert rc == 0
    assert "10.1.2.5:8000" in out
    assert "10.1.2.6:8000" in out
    assert "✓ live" in out
    # totals line should appear
    assert "totals:" in out


def test_info_shows_pool_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool header includes pool name and bind URL."""
    mgr, bs = _make_mgr(
        tmp_path,
        pools=[
            Pool(name="qwen3", served_model="Qwen/Qwen3-9B", bind_port=8080),
        ],
    )

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_stopped_status(),
    )

    assert rc == 0
    assert "qwen3" in out
    assert "8080" in out


def test_info_exit_always_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lb info always exits 0 regardless of backend health."""
    mgr, bs = _make_mgr(tmp_path)

    rc, _ = _capture_info(mgr, bs, monkeypatch, status=_stopped_status())
    assert rc == 0

    rc2, _ = _capture_info(mgr, bs, monkeypatch, status=_running_status())
    assert rc2 == 0


# ---------------------------------------------------------------------------
# Drift: untracked endpoint
# ---------------------------------------------------------------------------


def test_info_drift_untracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drift: state file has ep A, haproxy has A + B (B is untracked)."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")

    registry = {"pool_default": {"10.1.2.5:8000", "10.1.2.9:8000"}}  # 9 is untracked

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry=registry,
    )

    assert rc == 0
    assert "10.1.2.9:8000" in out
    assert "untracked" in out
    assert "⚠" in out
    assert "Drift" in out


# ---------------------------------------------------------------------------
# LB stopped: compact panel, no admin queries
# ---------------------------------------------------------------------------


def test_info_lb_stopped_shows_annotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When LB stopped, process panel shows [LB STOPPED] and no admin calls made."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")

    spy_client = MagicMock()
    monkeypatch.setattr(lb_scaling, "_client", lambda m: spy_client)

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_stopped_status(),
        client=None,  # prevent _client from being called via _capture_info
    )

    assert rc == 0
    assert "LB STOPPED" in out
    assert "10.1.2.5:8000" in out
    # _client must NOT be called when LB is stopped.
    spy_client.assert_not_called()


def test_info_lb_stopped_pid_alive_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Process panel shows pid_alive=false when LB stopped."""
    mgr, bs = _make_mgr(tmp_path)

    rc, out = _capture_info(mgr, bs, monkeypatch, status=_stopped_status())

    assert rc == 0
    assert "alive=false" in out


# ---------------------------------------------------------------------------
# Admin socket unreachable mid-call
# ---------------------------------------------------------------------------


def test_info_admin_unreachable_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When admin socket unreachable, WARNING printed and state-file entries shown."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        client=None,  # _client returns None → admin unreachable
    )

    assert rc == 0
    assert "WARNING" in out
    assert "10.1.2.5:8000" in out


# ---------------------------------------------------------------------------
# vLLM /metrics unreachable: show -- for running/waiting
# ---------------------------------------------------------------------------


def test_info_vllm_metrics_unreachable_shows_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When vllm /metrics unreachable, running/waiting show '--'."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")

    registry = {"pool_default": {"10.1.2.5:8000"}}

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry=registry,
        vllm_metrics={"running": None, "waiting": None},
    )

    assert rc == 0
    # "--" appears in running/waiting columns for unreachable metrics.
    assert "--" in out


def test_info_vllm_metrics_with_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When vllm /metrics returns data, values appear in output."""
    mgr, bs = _make_mgr(tmp_path)
    pbs = BackendState(mgr.state_dir, "10.0.0.1", pool="default")
    pbs.add("10.1.2.5:8000")

    registry = {"pool_default": {"10.1.2.5:8000"}}

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry=registry,
        vllm_metrics={"running": 7, "waiting": 3},
    )

    assert rc == 0
    assert "7" in out
    assert "3" in out


# ---------------------------------------------------------------------------
# Multi-pool
# ---------------------------------------------------------------------------


def test_info_multi_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multi-pool: both pools shown with their endpoints."""
    pools = [
        Pool(name="a", served_model="M/A", bind_port=8080),
        Pool(name="b", served_model="M/B", bind_port=8081),
    ]
    mgr, bs = _make_mgr(tmp_path, pools=pools)
    BackendState(mgr.state_dir, "10.0.0.1", pool="a").add("10.1.2.5:8000")
    BackendState(mgr.state_dir, "10.0.0.1", pool="b").add("10.1.2.7:8000")

    registry = {
        "pool_a": {"10.1.2.5:8000"},
        "pool_b": {"10.1.2.7:8000"},
    }

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry=registry,
    )

    assert rc == 0
    assert "pool: a" in out
    assert "pool: b" in out
    assert "10.1.2.5:8000" in out
    assert "10.1.2.7:8000" in out


def test_info_empty_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool with no backends shows (no backends) placeholder."""
    mgr, bs = _make_mgr(tmp_path)

    rc, out = _capture_info(
        mgr,
        bs,
        monkeypatch,
        status=_running_status(),
        live_registry={},
    )

    assert rc == 0
    assert "(no backends)" in out


# ---------------------------------------------------------------------------
# fetch_vllm_metrics unit tests
# ---------------------------------------------------------------------------


def test_fetch_vllm_metrics_parses_plain() -> None:
    """Parse plain metrics without label blocks."""
    from unittest.mock import MagicMock

    from vctl.lb.probe import fetch_vllm_metrics

    body = (
        "# HELP vllm:num_requests_running Running requests\n"
        "# TYPE vllm:num_requests_running gauge\n"
        "vllm:num_requests_running 5.0\n"
        "vllm:num_requests_waiting 2.0\n"
        "some_other_metric 99\n"
    )

    mock_resp = MagicMock()
    mock_resp.text = body
    mock_resp.raise_for_status = lambda: None

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_vllm_metrics("10.0.0.1", 8000, timeout=1.0)

    assert result["running"] == 5
    assert result["waiting"] == 2


def test_fetch_vllm_metrics_parses_with_labels() -> None:
    """Labels block stripped before value parsing."""
    from unittest.mock import MagicMock

    from vctl.lb.probe import fetch_vllm_metrics

    body = (
        'vllm:num_requests_running{model="gpt2",version="1"} 3.0\n'
        'vllm:num_requests_waiting{model="gpt2"} 1.0\n'
    )

    mock_resp = MagicMock()
    mock_resp.text = body
    mock_resp.raise_for_status = lambda: None

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_vllm_metrics("10.0.0.1", 8000, timeout=1.0)

    assert result["running"] == 3
    assert result["waiting"] == 1


def test_fetch_vllm_metrics_network_error_returns_none() -> None:
    """Network errors return None for both fields, don't raise."""

    import httpx

    from vctl.lb.probe import fetch_vllm_metrics

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("refused")
        mock_client_cls.return_value = mock_client

        result = fetch_vllm_metrics("10.0.0.1", 8000, timeout=1.0)

    assert result["running"] is None
    assert result["waiting"] is None


def test_fetch_vllm_metrics_http_error_returns_none() -> None:
    """HTTP 500 returns None for both fields."""
    from unittest.mock import MagicMock

    import httpx

    from vctl.lb.probe import fetch_vllm_metrics

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_vllm_metrics("10.0.0.1", 8000, timeout=1.0)

    assert result["running"] is None
    assert result["waiting"] is None


def test_fetch_vllm_metrics_missing_metrics() -> None:
    """If metrics not present in response, return None (not zero)."""
    from unittest.mock import MagicMock

    from vctl.lb.probe import fetch_vllm_metrics

    body = "some_other_metric 99\n"
    mock_resp = MagicMock()
    mock_resp.text = body
    mock_resp.raise_for_status = lambda: None

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_vllm_metrics("10.0.0.1", 8000, timeout=1.0)

    assert result["running"] is None
    assert result["waiting"] is None


# ---------------------------------------------------------------------------
# CLI subprocess test: lb info exits 0
# ---------------------------------------------------------------------------


def test_lb_info_subprocess_exits_zero(tmp_path: Path) -> None:
    """Subprocess test: `vctl lb info` exits 0 with a minimal config."""
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    state = tmp_path / "state"
    state.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "lb", "info"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml"),
            "VCTL_CLUSTER__STATE_DIR": str(state),
        },
        timeout=15,
    )
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Verify removed verbs are gone
# ---------------------------------------------------------------------------


def test_lb_status_verb_removed(tmp_path: Path) -> None:
    """lb status verb has been removed (BREAKING in v0.2.4)."""
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "lb", "status"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")},
        timeout=10,
    )
    assert proc.returncode != 0


def test_lb_stats_verb_removed(tmp_path: Path) -> None:
    """lb stats verb has been removed (BREAKING in v0.2.4)."""
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "lb", "stats"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")},
        timeout=10,
    )
    assert proc.returncode != 0


def test_lb_list_verb_removed(tmp_path: Path) -> None:
    """lb list verb has been removed (BREAKING in v0.2.4)."""
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "lb", "list"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")},
        timeout=10,
    )
    assert proc.returncode != 0
