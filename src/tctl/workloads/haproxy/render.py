"""Render haproxy.cfg from a typed LB config + per-pool backend list."""

from __future__ import annotations

from dataclasses import dataclass

from tctl.config.models import LbHaproxy, Pool


@dataclass(frozen=True)
class RuntimePaths:
    unix_socket: str
    pid_file: str


def _backend_name(ep: str) -> str:
    """Translate '10.0.0.5:8000' -> 'b_10_0_0_5_8000'."""
    return "b_" + ep.replace(".", "_").replace(":", "_")


def _balance_line(algorithm: str) -> str:
    if algorithm.startswith("hdr:"):
        return f"balance hdr({algorithm[len('hdr:') :]})"
    if algorithm == "random":
        return "balance random(2)"
    return f"balance {algorithm}"


def _render_pool(lb: LbHaproxy, pool: Pool, backends: list[str]) -> list[str]:
    """One frontend + one backend block for the given pool."""
    server_lines: list[str] = []
    for ep in backends:
        server_lines.append(
            f"    server {_backend_name(ep)} {ep} check "
            f"inter {lb.health.check_interval} fall {lb.health.fall} rise {lb.health.rise} "
            f"maxconn {lb.defaults.maxconn_per_backend} slowstart {lb.defaults.slowstart} "
            # v0.4.9: when health check marks a server DOWN, immediately close
            # its existing sessions instead of letting them hang on a dead
            # backend. Combined with `option redispatch` (in defaults), this
            # lets clients of long-blocking requests see a clean error
            # quickly, and haproxy retries on a healthy backend.
            f"on-marked-down shutdown-sessions"
        )
    return [
        f"frontend pool_{pool.name}",
        f"    bind *:{pool.bind_port}",
        f"    default_backend pool_{pool.name}",
        f"backend pool_{pool.name}",
        f"    {_balance_line(lb.algorithm)}",
        f"    option httpchk GET {lb.health.path}",
        *server_lines,
    ]


def render_haproxy_cfg(
    lb: LbHaproxy,
    paths: RuntimePaths,
    backends_by_pool: dict[str, list[str]],
) -> str:
    """Emit haproxy.cfg with one frontend+backend per pool."""
    lines: list[str] = []
    # Global block
    lines += [
        "global",
        "    log stdout format raw local0",
        f"    stats socket {paths.unix_socket} mode 660 level admin expose-fd listeners",
        f"    stats socket ipv4@{lb.admin.bind_addr}:{lb.admin.bind_port} level admin",
        "    stats timeout 30s",
        # Defaults
        "defaults",
        "    mode http",
        "    option httplog",
        "    option dontlognull",
        # v0.4.9: when a backend connection fails (refused, reset, timeout),
        # retry the request on a different backend. Without this, haproxy
        # returns the error to the client and the eval/inference workload
        # sees 5xx until the failed backend is finally marked DOWN by the
        # health check (inter * fall seconds, e.g. 90s with default 30s/3).
        # `option redispatch` is the haproxy idiom for "treat backend
        # failure as retryable, fan out to another backend"; `retries 3`
        # caps how many backends we'll try before giving up.
        "    retries 3",
        "    option redispatch",
        f"    timeout connect {lb.defaults.timeout_connect}",
        f"    timeout client {lb.defaults.timeout_client}",
        f"    timeout server {lb.defaults.timeout_server}",
    ]
    # One frontend+backend per pool
    for pool in lb.pools:
        lines.extend(_render_pool(lb, pool, backends_by_pool.get(pool.name, [])))
    # Stats listener
    lines += [
        "listen stats",
        f"    bind *:{lb.stats.bind_port}",
        "    stats enable",
        "    stats uri /",
        "    stats show-legends",
    ]
    return "\n".join(lines) + "\n"
