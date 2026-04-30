"""Golden test for haproxy.cfg rendering."""
from __future__ import annotations

from pathlib import Path

from vctl.config.models import (
    LbAdmin,
    LbClient,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
)
from vctl.lb.render import RuntimePaths, render_haproxy_cfg

FIX = Path(__file__).parent / "fixtures"


def test_golden_haproxy_cfg() -> None:
    lb = LbHaproxy(
        kind="haproxy", host="10.0.0.1",
        client=LbClient(bind_port=8080),
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(path="/health", check_interval="5s", fall=3, rise=2),
        defaults=LbDefaults(maxconn_per_backend=256, slowstart="30s",
                            timeout_connect="5s", timeout_client="1h", timeout_server="1h"),
    )
    paths = RuntimePaths(unix_socket="/tmp/vctl-haproxy.sock", pid_file="/tmp/vctl-haproxy.pid")
    rendered = render_haproxy_cfg(lb, paths, ["10.0.0.5:8000", "10.0.0.6:8000", "10.0.0.7:8000"])
    assert rendered.strip() == (FIX / "expected_haproxy.cfg").read_text().strip()
