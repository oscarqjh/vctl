"""Render haproxy.cfg from a typed LB config + backend list."""

from __future__ import annotations

from dataclasses import dataclass

from vctl.config.models import LbHaproxy


@dataclass(frozen=True)
class RuntimePaths:
    unix_socket: str
    pid_file: str


def _backend_name(ep: str) -> str:
    """Translate '10.0.0.5:8000' -> 'b_10_0_0_5_8000'."""
    return "b_" + ep.replace(".", "_").replace(":", "_")


def render_haproxy_cfg(lb: LbHaproxy, paths: RuntimePaths, backends: list[str]) -> str:
    lines: list[str] = []
    lines += [
        "global",
        "    log /dev/log local0",
        f"    stats socket {paths.unix_socket} mode 660 level admin",
        "    stats timeout 30s",
        "defaults",
        "    mode http",
        "    option httplog",
        "    option dontlognull",
        f"    timeout connect {lb.defaults.timeout_connect}",
        f"    timeout client {lb.defaults.timeout_client}",
        f"    timeout server {lb.defaults.timeout_server}",
        "frontend http-in",
        f"    bind *:{lb.client.bind_port}",
        "    default_backend pool",
        "backend pool",
        f"    balance {lb.algorithm}",
        f"    option httpchk GET {lb.health.path}",
    ]
    for ep in backends:
        lines.append(
            f"    server {_backend_name(ep)} {ep} check "
            f"inter {lb.health.check_interval} fall {lb.health.fall} rise {lb.health.rise} "
            f"maxconn {lb.defaults.maxconn_per_backend} slowstart {lb.defaults.slowstart}"
        )
    lines += [
        "frontend stats",
        f"    bind *:{lb.stats.bind_port}",
        "    stats enable",
        "    stats uri /",
        "frontend admin",
        f"    bind *:{lb.admin.bind_port}",
        "    mode tcp",
        "    default_backend admin_backend",
        "backend admin_backend",
        "    mode tcp",
        f"    server admin {paths.unix_socket}",
    ]
    return "\n".join(lines) + "\n"
