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
    # C-2: map algorithm string to valid HAProxy balance directive
    algo = lb.algorithm
    if algo.startswith("hdr:"):
        balance = f"balance hdr({algo[len('hdr:') :]})"
    elif algo == "random":
        balance = "balance random(2)"
    else:
        balance = f"balance {algo}"

    lines: list[str] = []
    lines += [
        "global",
        "    log stdout format raw local0",
        f"    stats socket {paths.unix_socket} mode 660 level admin expose-fd listeners",
        f"    stats socket ipv4@*:{lb.admin.bind_port} level admin",
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
        f"    {balance}",
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
    ]
    return "\n".join(lines) + "\n"
