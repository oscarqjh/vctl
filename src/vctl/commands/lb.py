"""`vctl lb <verb>` dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState
from vctl.resolver import resolve
from vctl.tmux import TmuxSession as _TmuxSession  # noqa: F401  (re-export for monkeypatch)
from vctl.tmux import tmux_session_exists as _tmux_session_exists

if TYPE_CHECKING:
    from rich.console import Console

_LOG = logging.getLogger(__name__)


_LB_VERB_HELP: dict[str, str] = {
    "install": "Install haproxy (conda → source-build fallback)",
    "start": "Start haproxy in tmux session vctl-lb",
    "stop": "SIGTERM haproxy (via pidfile) + tear down tmux session",
    "status": "Unified dashboard: process state + per-pool scur/qcur/running/waiting",
    "is-host": "Exit 0 if this pod's IP == lb.host, else exit 1",
    "where": "Print lb.host:bind_port",
    "wait-ready": "Block until ≥N ready in every non-empty pool (and LB front 200)",
    "logs": "Print contents of haproxy.log",
    "config": "Print rendered haproxy.cfg",
    "reload": "Re-render config + haproxy -sf <pid> (graceful, zero-downtime)",
    "auto-add": "Re-register every backend from the state file (post-restart recovery)",
    "add": "Register an endpoint in a pool (idempotent; auto-routes on multi-pool)",
    "remove": "Drop an endpoint from its pool (set maint then del)",
    "drain": "Mark backend as drain (no new traffic, finish in-flight)",
    "attach": "Probe localhost:<port>/v1/models then add self to its pool",
    "detach": "Drain self, wait for in-flight to drain, remove",
    "health": "Probe each registered backend; exit non-zero on any unhealthy",
    "prune": "Remove health-check-failed (DOWN) backends past threshold",
}


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl lb", description="LB lifecycle + scaling control.")
    sp = p.add_subparsers(dest="verb", required=True, metavar="VERB")
    # Verbs with no extra args.
    for verb in (
        "install",
        "stop",
        "status",
        "is-host",
        "logs",
        "config",
        "reload",
        "auto-add",
        "health",
    ):
        sp.add_parser(verb, help=_LB_VERB_HELP[verb])
    de = sp.add_parser("detach", help=_LB_VERB_HELP["detach"])
    de.add_argument(
        "--force",
        action="store_true",
        help=(
            "force-close active sessions before removal "
            "(destructive: drops in-flight requests). "
            "Use when a stuck backend won't drain due to half-open TCP from a crashed vllm."
        ),
    )
    # C10: `where` now accepts an optional --pool filter.
    wh = sp.add_parser("where", help=_LB_VERB_HELP["where"])
    wh.add_argument(
        "--pool",
        default=None,
        help="Print only the matching pool's host:port (exit 3 if not found)",
    )
    # Verbs with args / flags.
    start = sp.add_parser("start", help=_LB_VERB_HELP["start"])
    start.add_argument(
        "--force", action="store_true", help="start even if this pod's IP != lb.host"
    )
    wr = sp.add_parser("wait-ready", help=_LB_VERB_HELP["wait-ready"])
    wr.add_argument(
        "count",
        type=int,
        nargs="?",
        default=1,
        help="minimum ready backends per non-empty pool (default: 1)",
    )
    wr.add_argument("--pool", default=None, help="scope to a single pool")
    add = sp.add_parser("add", help=_LB_VERB_HELP["add"])
    add.add_argument("endpoint", help="ip:port to add")
    add.add_argument(
        "--pool", default=None, help="explicit pool name (default: auto by /v1/models probe)"
    )
    rm = sp.add_parser("remove", help=_LB_VERB_HELP["remove"])
    rm.add_argument("endpoint", help="ip:port to remove")
    dr = sp.add_parser("drain", help=_LB_VERB_HELP["drain"])
    dr.add_argument("endpoint", help="ip:port to drain")
    dr.add_argument("--pool", default=None, help="explicit pool name")
    at = sp.add_parser("attach", help=_LB_VERB_HELP["attach"])
    at.add_argument("port", type=int, nargs="?", help="local vllm port to probe (default: 8000)")
    prune = sp.add_parser("prune", help=_LB_VERB_HELP["prune"])
    prune.add_argument(
        "--pool",
        default=None,
        help="scope to one pool (default: all pools); exit 3 on unknown pool name",
    )
    prune.add_argument(
        "--threshold",
        default=None,
        metavar="DURATION",
        help=(
            "override dead threshold (e.g. 5m, 300s, 2h); "
            "default: cluster.lb.prune.threshold or '5m'"
        ),
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="print candidates without removing; exit 0",
    )
    return p


def _manager(ns: argparse.Namespace) -> tuple[LbManager, BackendState, Path]:
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, rc.lb.host)
    return mgr, bs, state_dir


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    verb = parsed.verb
    mgr, bs, _ = _manager(ns)
    if verb == "where":
        # C10: multi-pool support + optional --pool <name|port> filter.
        pool_ref = getattr(parsed, "pool", None)
        if pool_ref is not None:
            from vctl.lb.routing import resolve_pool_ref

            try:
                match = resolve_pool_ref(mgr.lb, pool_ref)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 3
            print(f"{mgr.lb.host}:{match.bind_port}")
            return 0
        if len(mgr.lb.pools) == 1:
            print(f"{mgr.lb.host}:{mgr.lb.pools[0].bind_port}")
        else:
            for p in mgr.lb.pools:
                print(f"{p.name}\t{mgr.lb.host}:{p.bind_port}")
        return 0
    if verb == "is-host":
        return 0 if mgr.is_host() else 1
    if verb == "config":
        print(mgr.render_config())
        return 0
    if verb == "start":
        try:
            mgr.start(force=parsed.force)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 4
        cfg_override = getattr(ns, "config", None)
        cluster_yaml = (
            Path(cfg_override) if cfg_override else Path.home() / ".vctl" / "cluster.yaml"
        )
        _spawn_watcher_if_enabled(mgr, cluster_yaml)
        return 0
    if verb == "stop":
        _stop_watcher_if_running(mgr)
        mgr.stop()
        return 0
    if verb == "status":
        return _do_info(mgr, bs)
    if verb == "reload":
        mgr.reload()
        return 0
    if verb == "wait-ready":
        return _wait_ready(mgr, parsed.count, pool_filter=parsed.pool)
    if verb == "install":
        from vctl.lb.installer import ensure_haproxy

        print(ensure_haproxy())
        return 0
    if verb == "logs":
        log_path = mgr.run_dir / "haproxy.log"
        if log_path.exists():
            print(log_path.read_text())
        return 0
    if verb == "prune":
        return _do_prune(mgr, parsed)
    # Scaling verbs (Task 17).
    from vctl.commands import lb_scaling

    return lb_scaling.dispatch(verb, parsed, ns, mgr, bs)


def _do_info(mgr: LbManager, bs: BackendState, _console: Console | None = None) -> int:
    """Unified dashboard: LB process panel + per-pool table with scur/qcur/running/waiting.

    Always exits 0 (informational). Use `lb health` for scripting gate.
    ``_console`` is an optional injection point for testing (pass a ``rich.Console``).
    """
    from rich.box import MINIMAL_DOUBLE_HEAD
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from vctl.commands.lb_scaling import _client
    from vctl.lb.probe import fetch_vllm_metrics
    from vctl.lb.prune import _watcher_status

    console: Console = _console if _console is not None else Console(force_terminal=False)

    # ------------------------------------------------------------------
    # 1. LB process panel
    # ------------------------------------------------------------------
    st = mgr.status()
    lb_running = bool(st.get("running"))
    pid = st.get("pid")
    pid_alive = st.get("pid_alive", False)
    admin_reachable = st.get("admin_reachable", False)
    admin_bind = st.get("admin_bind", "?")
    cfg_path = st.get("cfg_path", "?")
    is_local_host = st.get("is_local_host", False)
    stats_url = f"http://{mgr.lb.host}:{mgr.lb.stats.bind_port}"

    admin_status = "(reachable)" if admin_reachable else "(unreachable)"
    ws = _watcher_status(mgr)
    watcher_state = str(ws["state"])
    proc_lines = [
        f"pid {pid}  alive={str(pid_alive).lower()}  admin={admin_bind} {admin_status}",
        f"tmux: {mgr.tmux_name}  is_local_host={str(is_local_host).lower()}",
        f"cfg: {cfg_path}",
        f"stats UI: {stats_url}",
        f"watcher: {watcher_state}",
    ]
    if not lb_running:
        proc_lines.append("[bold red][LB STOPPED][/bold red]")
    console.print(Panel("\n".join(proc_lines), title="LB Process", expand=False))

    # ------------------------------------------------------------------
    # 2. Build live HAProxy stats (scur, qcur, lastchg) if LB is running.
    # ------------------------------------------------------------------
    # haproxy_stats: backend_section -> server_name -> {scur/qcur/lastchg: int, ep: str}
    haproxy_stats: dict[str, dict[str, dict[str, int | str]]] = {}
    live_registry: dict[str, set[str]] | None = None  # backend_section -> set[ep]
    admin_query_failed = False

    if lb_running:
        # HAProxy admin socket closes after each command, so each `show ...`
        # needs its own RuntimeClient. Reusing one across two queries was
        # masking live-registry as "tracked-only" (the second send hit a
        # closed socket and raised, but haproxy_stats had already populated).
        cli1 = _client(mgr)
        if cli1 is None:
            admin_query_failed = True
        else:
            try:
                haproxy_stats = _fetch_haproxy_stats(cli1)
            except Exception as exc:
                _LOG.debug("haproxy show stat raised: %s", exc)
                admin_query_failed = True
            cli2 = _client(mgr)
            if cli2 is not None:
                try:
                    live_registry = _build_live_registry(cli2)
                except Exception as exc:
                    _LOG.debug("haproxy show servers state raised: %s", exc)
                    admin_query_failed = True

    if admin_query_failed:
        console.print(
            "[yellow]WARNING: could not query live haproxy state "
            "— showing state-file entries only[/yellow]"
        )

    # ------------------------------------------------------------------
    # 3. Per-pool tables
    # ------------------------------------------------------------------
    any_drift = False

    for pool in mgr.lb.pools:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pool.name)
        eps = pbs.list()
        url = f"http://{mgr.lb.host}:{pool.bind_port}"
        header = f"pool: {pool.name} → {url}   ({pool.served_model})"
        if not lb_running:
            header += "  [LB STOPPED]"
        console.print(f"\n[bold]{header}[/bold]")

        if not lb_running:
            # Simple listing when LB is stopped — no admin queries.
            if eps:
                table = Table(box=MINIMAL_DOUBLE_HEAD, show_header=True, pad_edge=False)
                table.add_column("Endpoint", overflow="fold")
                table.add_column("Status")
                for ep in eps:
                    table.add_row(ep, "[LB STOPPED]")
                console.print(table)
            else:
                console.print("  (no backends)")
            continue

        # Build per-backend data.
        backend_section = f"pool_{pool.name}"
        live_eps: set[str] = set()
        if live_registry is not None:
            live_eps = live_registry.get(backend_section, set())
        state_set = set(eps)

        # Collect all endpoints to display: state-file + untracked haproxy entries.
        tracked_eps = list(eps)
        untracked_eps = sorted(live_eps - state_set)
        all_eps = tracked_eps + untracked_eps

        if not all_eps:
            console.print("  (no backends)")
            continue

        table = Table(box=MINIMAL_DOUBLE_HEAD, show_header=True, pad_edge=False)
        table.add_column("Endpoint", overflow="fold", no_wrap=False)
        table.add_column("Status")
        table.add_column("scur", justify="right")
        table.add_column("qcur", justify="right")
        table.add_column("running", justify="right")
        table.add_column("waiting", justify="right")
        table.add_column("uptime", justify="right")

        total_scur = 0
        total_qcur = 0
        total_running = 0
        total_waiting = 0

        for ep in all_eps:
            in_state = ep in state_set
            in_haproxy = ep in live_eps

            if admin_query_failed:
                status_str = "⚠ tracked-only"
            elif in_state and in_haproxy:
                status_str = "✓ live"
            elif in_state and not in_haproxy:
                status_str = "⚠ tracked-only"
            elif not in_state and in_haproxy:
                status_str = "⚠ untracked"
                any_drift = True
            else:
                status_str = "?"

            # scur / qcur / lastchg / status from haproxy stats.
            scur_val: int | None = None
            qcur_val: int | None = None
            lastchg_val: int | None = None
            haproxy_status: str = ""
            pool_stats = haproxy_stats.get(backend_section, {})
            for _srv_name, srv_data in pool_stats.items():
                if srv_data.get("ep") == ep:
                    raw_scur = srv_data.get("scur")
                    raw_qcur = srv_data.get("qcur")
                    raw_lastchg = srv_data.get("lastchg")
                    raw_status = srv_data.get("status")
                    scur_val = int(raw_scur) if isinstance(raw_scur, int) else None
                    qcur_val = int(raw_qcur) if isinstance(raw_qcur, int) else None
                    lastchg_val = int(raw_lastchg) if isinstance(raw_lastchg, int) else None
                    haproxy_status = str(raw_status) if raw_status else ""
                    break

            # Override the registered/tracked status with HAProxy's health view
            # when the backend is registered: a registered DOWN backend is more
            # alarming than a "tracked-only" one.
            if status_str == "✓ live" and haproxy_status:
                # status field can be "UP", "DOWN", "MAINT", "DRAIN", "no check",
                # "UP 1/2" (during rise count-up), "DOWN 1/3" (during fall count).
                up = haproxy_status.startswith("UP")
                if not up:
                    # DOWN, MAINT, DRAIN, or "no check" — surface it.
                    short = haproxy_status.split()[0]
                    status_str = f"⚠ {short}"

            scur_str = str(scur_val) if scur_val is not None else "--"
            qcur_str = str(qcur_val) if qcur_val is not None else "--"
            if scur_val is not None:
                total_scur += scur_val
            if qcur_val is not None:
                total_qcur += qcur_val

            # lastchg: seconds since last UP↔DOWN transition (i.e. how long
            # this backend has been continuously in its current state). For a
            # healthy backend that's been up for a while, this is uptime.
            lastchg_str = _format_duration(lastchg_val) if lastchg_val is not None else "--"

            # running / waiting from vllm /metrics.
            host_part, _, port_str = ep.rpartition(":")
            try:
                port_int = int(port_str)
            except ValueError:
                port_int = 0

            metrics = fetch_vllm_metrics(host_part, port_int, timeout=2.0)
            running_val = metrics.get("running")
            waiting_val = metrics.get("waiting")
            running_str = str(running_val) if running_val is not None else "--"
            waiting_str = str(waiting_val) if waiting_val is not None else "--"
            if running_val is not None:
                total_running += running_val
            if waiting_val is not None:
                total_waiting += waiting_val

            table.add_row(ep, status_str, scur_str, qcur_str, running_str, waiting_str, lastchg_str)

        console.print(table)
        console.print(
            f"  totals: scur={total_scur}  qcur={total_qcur}  "
            f"running={total_running}  waiting={total_waiting}"
        )

    # ------------------------------------------------------------------
    # 4. Drift notice (if any untracked endpoints appeared above)
    # ------------------------------------------------------------------
    if any_drift:
        console.print(
            "\n[yellow]Drift detected: endpoints in haproxy not in state file "
            "(marked ⚠ untracked above). "
            "Run `vctl lb add <ep>` to track them.[/yellow]"
        )

    return 0


def _format_duration(seconds: int) -> str:
    """Format seconds as a compact human-readable duration. e.g. 3569 -> '59m'."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def _fetch_haproxy_stats(cli: object) -> dict[str, dict[str, dict[str, int | str]]]:
    """Parse ``show stat csv`` from haproxy admin socket.

    Returns backend_section -> server_name -> {scur, qcur, lastchg, ep, status}.
    Numeric fields are int; ``ep`` and ``status`` are str.
    ``status`` is HAProxy's view: UP, DOWN, MAINT, DRAIN, NOLB, etc.
    Only SERVER rows (svname != BACKEND/FRONTEND) are returned.
    Falls back to empty dict on any error.
    """
    from vctl.lb.runtime import RuntimeClient, _parse_endpoint_from_name

    stats: dict[str, dict[str, dict[str, int | str]]] = {}
    if not isinstance(cli, RuntimeClient):
        return stats

    try:
        raw = cli._send("show stat")  # noqa: SLF001
    except Exception:
        return stats

    # HAProxy CSV header: # pxname,svname,qcur,qmax,scur,...,lastchg,...
    # Column indices are defined by the header line (starts with #).
    col_pxname = 0
    col_svname = 1
    col_qcur = 2
    col_scur = 4
    col_lastchg = 23  # typical haproxy 2.x offset; may vary
    col_status = 17  # typical; may vary
    col_addr = 73  # typical; may vary

    header_cols: list[str] = []

    for line in raw.splitlines():
        if line.startswith("# "):
            header_cols = [c.strip() for c in line[2:].split(",")]
            # Build index map from actual header.
            idx = {c: i for i, c in enumerate(header_cols)}
            col_pxname = idx.get("pxname", 0)
            col_svname = idx.get("svname", 1)
            col_qcur = idx.get("qcur", 2)
            col_scur = idx.get("scur", 4)
            col_lastchg = idx.get("lastchg", 23)
            col_status = idx.get("status", 17)
            col_addr = idx.get("addr", 73)
            continue
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < max(col_pxname, col_svname, col_qcur, col_scur) + 1:
            continue
        svname = parts[col_svname] if len(parts) > col_svname else ""
        if svname in ("BACKEND", "FRONTEND", ""):
            continue
        pxname = parts[col_pxname] if len(parts) > col_pxname else ""

        def _int(s: str) -> int:
            try:
                return int(s)
            except (ValueError, IndexError):
                return 0

        scur = _int(parts[col_scur]) if len(parts) > col_scur else 0
        qcur = _int(parts[col_qcur]) if len(parts) > col_qcur else 0
        lastchg = _int(parts[col_lastchg]) if len(parts) > col_lastchg else 0
        status = parts[col_status].strip() if len(parts) > col_status else ""
        addr_raw = parts[col_addr].strip() if len(parts) > col_addr else ""

        # Decode endpoint from server name (b_<ip>_<port>) or addr column.
        ep = ""
        parsed = _parse_endpoint_from_name(svname)
        if parsed is not None:
            ep = f"{parsed[0]}:{parsed[1]}"
        elif addr_raw and addr_raw not in ("0.0.0.0", "-"):
            # addr column may include port as "ip:port" or just "ip".
            ep = addr_raw.split(" ")[0]  # strip any trailing flags

        stats.setdefault(pxname, {})[svname] = {
            "scur": scur,
            "qcur": qcur,
            "lastchg": lastchg,
            "status": status,
            "ep": ep,
        }

    return stats


def _build_live_registry(cli: object) -> dict[str, set[str]]:
    """Parse ``show servers state`` and return backend_section -> set[endpoint].

    We re-issue the raw command so we can capture the backend column (column 2
    in the output, which RuntimeClient.show_servers_state() discards).
    Falls back to RuntimeClient.show_servers_state() if the raw method is
    unavailable (e.g. _NoOpClient stub in tests).
    """
    from vctl.lb.runtime import RuntimeClient, _parse_endpoint_from_name

    registry: dict[str, set[str]] = {}

    if isinstance(cli, RuntimeClient):
        raw = cli._send("show servers state")  # noqa: SLF001
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            backend_section = parts[1]  # column 1: backend name
            name = parts[3]  # column 3: server name
            srv_addr = parts[4]  # column 4: srv_addr
            parsed = _parse_endpoint_from_name(name)
            if parsed is not None:
                endpoint = f"{parsed[0]}:{parsed[1]}"
            elif srv_addr == "0.0.0.0":
                continue
            else:
                endpoint = srv_addr
            registry.setdefault(backend_section, set()).add(endpoint)
        return registry

    # For any other client type (e.g. test stubs) we have no way to obtain the
    # backend-section column, so we return an empty registry — callers degrade
    # gracefully to tracked-only annotation.
    return registry


def _wait_ready(mgr: LbManager, n: int, pool_filter: str | None = None) -> int:
    """Block until each non-empty target pool has >=n ready backends AND its
    front-port returns 200 on /v1/models. Empty pools are skipped (not blocking).

    LB_WAIT_TIMEOUT (seconds, 0 = forever) env override.

    Note: the `n` threshold is checked per-pool based on the number of registered
    backends in each pool's state file combined with a live HTTP probe of the
    pool's front-port. HAProxy admin-socket per-server state is not inspected.
    """
    import httpx

    timeout_str = os.environ.get("LB_WAIT_TIMEOUT")
    deadline = None
    if timeout_str is not None:
        try:
            t = float(timeout_str)
            if t > 0:
                deadline = time.monotonic() + t
        except ValueError:
            pass

    if pool_filter is not None:
        from vctl.lb.routing import resolve_pool_ref

        try:
            target_pools = [resolve_pool_ref(mgr.lb, pool_filter)]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 3
    else:
        target_pools = list(mgr.lb.pools)

    state_dir = mgr.state_dir if isinstance(mgr.state_dir, Path) else Path(mgr.state_dir)
    last_log = 0.0
    while True:
        all_pools_ok = True
        any_pool_has_backends = False
        ready: list[str] = []
        skipped_empty: list[str] = []
        for p in target_pools:
            pbs = BackendState(state_dir, mgr.lb.host, pool=p.name)
            registered = pbs.list()
            if not registered:
                skipped_empty.append(p.name)
                continue
            any_pool_has_backends = True
            url = f"http://{mgr.lb.host}:{p.bind_port}/v1/models"
            try:
                r = httpx.get(url, timeout=3.0)
                code = r.status_code
            except Exception:
                code = 0
            backends_label = f"{len(registered)}backend{'s' if len(registered) != 1 else ''}"
            ready.append(f"{p.name}={backends_label}/{code or 'ERR'}")
            if code != 200 or len(registered) < n:
                all_pools_ok = False

        if all_pools_ok and any_pool_has_backends:
            msg = f"ready: {', '.join(ready)}"
            if skipped_empty:
                msg += f" (skipped empty: {', '.join(skipped_empty)})"
            print(msg)
            return 0

        if deadline is not None and time.monotonic() > deadline:
            details = ready + [f"{p}=empty" for p in skipped_empty]
            print(
                f"timed out: {', '.join(details)} (after {timeout_str}s)",
                file=sys.stderr,
            )
            return 4  # C8: environment timeout → exit 4

        now = time.monotonic()
        if now - last_log >= 30:
            details = ready + [f"{p}=empty" for p in skipped_empty]
            print(f"waiting... {', '.join(details)}", file=sys.stderr)
            last_log = now
        sleep_s = 2.0
        if deadline is not None:
            sleep_s = min(sleep_s, max(0.0, deadline - time.monotonic()))
        time.sleep(sleep_s)


def _spawn_watcher_if_enabled(mgr: LbManager, cluster_yaml_path: Path) -> None:
    """Spawn vctl-lb-watch watcher session after lb start, if prune.enabled is True."""
    from vctl.lb.prune import _spawn_watcher

    if not mgr.lb.prune.enabled:
        return
    if _tmux_session_exists("vctl-lb-watch"):
        print(
            "watcher already running (session=vctl-lb-watch) — skipping spawn",
            file=sys.stderr,
        )
        return
    _spawn_watcher(mgr, mgr.lb.prune, cluster_yaml_path)
    print("watcher started (session=vctl-lb-watch)", file=sys.stderr)


def _stop_watcher_if_running(mgr: LbManager) -> None:
    """Stop vctl-lb-watch watcher session before lb stop.

    Always calls _stop_watcher regardless of prune.enabled — avoids zombie
    session leaks when the operator toggles enabled=False after a running watcher.
    Prints a message only if a session was actually killed.
    """
    from vctl.lb.prune import _stop_watcher

    if _stop_watcher(mgr):
        print("watcher stopped", file=sys.stderr)


def _do_prune(mgr: LbManager, parsed: argparse.Namespace) -> int:
    """Handle `vctl lb prune`.

    Threshold resolution order:
      1. --threshold flag (if given and valid)
      2. cluster.lb.prune.threshold YAML field
      3. Hardcoded default "5m"

    Pool list:
      - If --pool given: validate against configured pools first; exit 3 if unknown.
      - Otherwise: iterate all configured pools.

    Each candidate calls Reconciler.want_absent (haproxy-first ordering preserved).
    """
    from vctl.duration import _parse_duration
    from vctl.lb.errors import BackendOpFailed, LbUnreachable
    from vctl.lb.prune import _collect_prune_candidates
    from vctl.lb.reconciler import Reconciler

    # Step 1: Resolve threshold string.
    raw_threshold: str = (
        parsed.threshold if parsed.threshold is not None else mgr.lb.prune.threshold
    )

    try:
        threshold_s = _parse_duration(raw_threshold)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Step 2: Determine target pool list with pre-flight validation.
    pool_name_filter: str | None = getattr(parsed, "pool", None)
    if pool_name_filter is not None:
        configured = {p.name for p in mgr.lb.pools}
        if pool_name_filter not in configured:
            available = ", ".join(sorted(configured))
            print(
                f"unknown pool: {pool_name_filter!r}; available: {available}",
                file=sys.stderr,
            )
            return 3
        target_pool_names = [pool_name_filter]
    else:
        target_pool_names = [p.name for p in mgr.lb.pools]

    dry_run: bool = getattr(parsed, "dry_run", False)
    rec = Reconciler(mgr)

    # Step 3-7: Collect candidates and act.
    for pool_name in target_pool_names:
        try:
            candidates = _collect_prune_candidates(mgr, pool_name, threshold_s)
        except LbUnreachable as exc:
            print(f"lb prune: {exc}", file=sys.stderr)
            return 4

        for ep, lastchg_s in candidates:
            duration_str = _format_duration(lastchg_s)
            if dry_run:
                print(
                    f"would prune {ep} from pool {pool_name} (DOWN for {duration_str})",
                    file=sys.stderr,
                )
                continue
            try:
                rec.want_absent(ep, pool_name)
            except BackendOpFailed as exc:
                print(f"lb prune: {exc}", file=sys.stderr)
                return 1
            print(
                f"pruned {ep} from pool {pool_name} (DOWN for {duration_str})",
                file=sys.stderr,
            )

    return 0
