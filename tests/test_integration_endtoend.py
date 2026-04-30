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
