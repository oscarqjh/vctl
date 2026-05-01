"""End-to-end integration test: real haproxy in tmpdir."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


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
    mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    BackendState(tmp_path / "state", "127.0.0.1").add("127.0.0.1:18999")
    mgr.start(force=True)
    try:
        time.sleep(1)
        cli = RuntimeClient.for_unix(str(mgr.sock_path))
        rows = cli.show_servers_state()
        assert any("18999" in r.endpoint for r in rows)
    finally:
        mgr.stop()


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

    lb = LbHaproxy(
        kind="haproxy", host="127.0.0.1",
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

    mgr = LbManager(lb, state_dir=state_dir, run_dir=tmp_path / "run")
    mgr.start(force=True)
    try:
        time.sleep(1)
        cli = RuntimeClient.for_unix(str(mgr.sock_path))
        rows = cli.show_servers_state()
        endpoints = {r.endpoint for r in rows}
        assert "127.0.0.1:18999" in endpoints
        assert "127.0.0.1:18998" in endpoints
    finally:
        mgr.stop()
