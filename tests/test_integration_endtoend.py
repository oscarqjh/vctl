"""End-to-end integration test: real haproxy in tmpdir."""

from __future__ import annotations

import contextlib
import shutil
import signal
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# F3: belt-and-suspenders cleanup helper — kills any haproxy whose cmdline
# contains the given cfg_path.  Never matches production paths.
# ---------------------------------------------------------------------------


def _force_cleanup_haproxy_for_cfg(cfg_path: Path) -> None:
    """SIGKILL any haproxy process whose cmdline contains ``-f <cfg_path>``.

    Used as a teardown safety net when :meth:`LbManager.stop` itself may
    have failed partway through.  Deliberately matches only on the specific
    *cfg_path* so it can never reach a production haproxy running against
    ``~/.vctl/lb/haproxy.cfg``.
    """
    try:
        import psutil
    except ImportError:
        return

    target = str(cfg_path)
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            cmd = proc.info.get("cmdline") or []
            if "haproxy" not in name and not any("haproxy" in p for p in cmd[:1]):
                continue
            for i, tok in enumerate(cmd):
                if tok == "-f" and i + 1 < len(cmd) and cmd[i + 1] == target:
                    proc.send_signal(signal.SIGKILL)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="haproxy not on PATH")
def test_haproxy_register_drain_remove_cycle(tmp_path: Path) -> None:
    from vctl.config.models import (
        LbAdmin,
        LbClient,
        LbDefaults,
        LbHaproxy,
        LbHealth,
        LbStats,
    )
    from vctl.lb.manager import LbManager
    from vctl.lb.runtime import RuntimeClient
    from vctl.lb.state import BackendState

    # F2: unique tmux session name so this test never collides with a live LB
    # or with a parallel test worker.
    tmux_name = f"vctl-lb-test-{uuid.uuid4().hex[:8]}"

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
    )
    mgr = LbManager(
        lb,
        state_dir=tmp_path / "state",
        run_dir=tmp_path / "run",
        tmux_name=tmux_name,
    )
    BackendState(tmp_path / "state", "127.0.0.1").add("127.0.0.1:18999")
    # F3: wrap in try/finally so spawned haproxy is always cleaned up
    try:
        mgr.start(force=True)
        time.sleep(1)
        cli = RuntimeClient.for_unix(str(mgr.sock_path))
        rows = cli.show_servers_state()
        assert any("18999" in r.endpoint for r in rows)
    finally:
        with contextlib.suppress(Exception):
            mgr.stop()
        # Belt-and-suspenders: kill any haproxy still holding our cfg file
        _force_cleanup_haproxy_for_cfg(mgr.cfg_path)


@pytest.mark.skipif(shutil.which("haproxy") is None, reason="haproxy not on PATH")
def test_haproxy_two_pools_with_distinct_backends(tmp_path: Path) -> None:
    from vctl.config.models import (
        LbAdmin,
        LbDefaults,
        LbHaproxy,
        LbHealth,
        LbStats,
        Pool,
    )
    from vctl.lb.manager import LbManager
    from vctl.lb.runtime import RuntimeClient
    from vctl.lb.state import BackendState

    # F2: unique tmux session name so this test never collides with a live LB
    # or with a parallel test worker.
    tmux_name = f"vctl-lb-test-{uuid.uuid4().hex[:8]}"

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
        algorithm="leastconn",
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=18080),
            Pool(name="b", served_model="M/B", bind_port=18081),
        ],
    )
    state_dir = tmp_path / "state"
    BackendState(state_dir, "127.0.0.1", pool="a").add("127.0.0.1:18999")
    BackendState(state_dir, "127.0.0.1", pool="b").add("127.0.0.1:18998")

    mgr = LbManager(
        lb,
        state_dir=state_dir,
        run_dir=tmp_path / "run",
        tmux_name=tmux_name,
    )
    # F3: wrap in try/finally so spawned haproxy is always cleaned up
    try:
        mgr.start(force=True)
        time.sleep(1)
        cli = RuntimeClient.for_unix(str(mgr.sock_path))
        rows = cli.show_servers_state()
        endpoints = {r.endpoint for r in rows}
        assert "127.0.0.1:18999" in endpoints
        assert "127.0.0.1:18998" in endpoints
    finally:
        with contextlib.suppress(Exception):
            mgr.stop()
        # Belt-and-suspenders: kill any haproxy still holding our cfg file
        _force_cleanup_haproxy_for_cfg(mgr.cfg_path)
