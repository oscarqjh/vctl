"""Golden test for haproxy.cfg rendering."""

from __future__ import annotations

from pathlib import Path

from tctl.config.models import (
    LbAdmin,
    LbClient,
    LbDefaults,
    LbHaproxy,
    LbHealth,
    LbStats,
    Pool,
)
from tctl.workloads.haproxy.render import RuntimePaths, render_haproxy_cfg

FIX = Path(__file__).parent.parent.parent / "fixtures"


def _make_lb(algorithm: str = "leastconn") -> LbHaproxy:
    return LbHaproxy(
        kind="haproxy",
        host="10.0.0.1",
        client=LbClient(bind_port=8080),
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm=algorithm,
        health=LbHealth(path="/health", check_interval="5s", fall=3, rise=2),
        defaults=LbDefaults(
            maxconn_per_backend=256,
            slowstart="30s",
            timeout_connect="5s",
            timeout_client="1h",
            timeout_server="1h",
        ),
    )


def test_golden_haproxy_cfg() -> None:
    lb = LbHaproxy(
        kind="haproxy",
        host="10.0.0.1",
        client=LbClient(bind_port=8080),  # legacy synth → default pool
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(path="/health", check_interval="5s", fall=3, rise=2),
        defaults=LbDefaults(
            maxconn_per_backend=256,
            slowstart="30s",
            timeout_connect="5s",
            timeout_client="1h",
            timeout_server="1h",
        ),
    )
    # legacy synth puts a single "default" pool; backends keyed by that name
    paths = RuntimePaths(unix_socket="/tmp/vctl-haproxy.sock", pid_file="/tmp/vctl-haproxy.pid")
    rendered = render_haproxy_cfg(
        lb,
        paths,
        {"default": ["10.0.0.5:8000", "10.0.0.6:8000", "10.0.0.7:8000"]},
    )
    expected = (FIX / "expected_haproxy.cfg").read_text()
    assert rendered.strip() == expected.strip()


def test_render_two_pools_golden() -> None:
    lb = LbHaproxy(
        kind="haproxy",
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        algorithm="leastconn",
        health=LbHealth(path="/health", check_interval="5s", fall=3, rise=2),
        defaults=LbDefaults(
            maxconn_per_backend=256,
            slowstart="30s",
            timeout_connect="5s",
            timeout_client="1h",
            timeout_server="1h",
        ),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )
    paths = RuntimePaths(unix_socket="/tmp/vctl-haproxy.sock", pid_file="/tmp/vctl-haproxy.pid")
    rendered = render_haproxy_cfg(
        lb,
        paths,
        {
            "a": ["10.0.0.5:8000", "10.0.0.6:8000"],
            "b": ["10.0.0.7:8000"],
        },
    )
    expected = (FIX / "expected_haproxy_two_pools.cfg").read_text()
    assert rendered == expected


def test_algorithm_random() -> None:
    """C-2: 'random' must render as 'balance random(2)'."""
    lb = _make_lb("random")
    paths = RuntimePaths(unix_socket="/tmp/vctl-haproxy.sock", pid_file="/tmp/vctl-haproxy.pid")
    rendered = render_haproxy_cfg(lb, paths, {"default": ["10.0.0.5:8000"]})
    assert "balance random(2)" in rendered
    assert "balance random\n" not in rendered


def test_algorithm_hdr() -> None:
    """C-2: 'hdr:X-Session-Id' must render as 'balance hdr(X-Session-Id)'."""
    lb = _make_lb("hdr:X-Session-Id")
    paths = RuntimePaths(unix_socket="/tmp/vctl-haproxy.sock", pid_file="/tmp/vctl-haproxy.pid")
    rendered = render_haproxy_cfg(lb, paths, {"default": ["10.0.0.5:8000"]})
    assert "balance hdr(X-Session-Id)" in rendered
    assert "balance hdr:X-Session-Id" not in rendered
