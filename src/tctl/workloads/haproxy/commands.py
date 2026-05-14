"""`tctl haproxy <verb>` dispatch — HAProxy load-balancer lifecycle + scaling."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from tctl.resolver import resolve
from tctl.tmux import TmuxSession as _TmuxSession  # noqa: F401  (re-export for monkeypatch)
from tctl.tmux import tmux_session_exists as _tmux_session_exists
from tctl.workloads.haproxy.manager import LbManager
from tctl.workloads.haproxy.state import BackendState

if TYPE_CHECKING:
    from rich.console import Console

_LOG = logging.getLogger(__name__)


_HAPROXY_VERB_HELP: dict[str, str] = {
    "start": "Start haproxy in tmux session tctl-haproxy",
    "stop": "SIGTERM haproxy (via pidfile) + tear down tmux session",
    "status": "Unified dashboard: process state + per-pool scur/qcur/running/waiting",
    "reload": "Re-render config + haproxy -sf <pid> (graceful, zero-downtime)",
    "logs": "Print contents of haproxy.log",
    "config": "Print rendered haproxy.cfg",
    "health": "Probe each registered backend; exit non-zero on any unhealthy",
    "add": "Register an endpoint in a pool (idempotent; auto-routes on multi-pool)",
    "remove": "Drop an endpoint from its pool (set maint then del)",
    "drain": "Mark backend as drain (no new traffic, finish in-flight)",
    "scaling": "Scaling sub-commands: attach / detach / auto-add",
    "prune": "Remove health-check-failed (DOWN) backends past threshold",
}

_SCALING_VERB_HELP: dict[str, str] = {
    "attach": "Probe localhost:<port>/v1/models then add self to its pool",
    "detach": "Drain self, wait for in-flight to drain, remove",
    "auto-add": "Re-register every backend from the state file (post-restart recovery)",
}


# ---------------------------------------------------------------------------
# Subparser registration (called by __init__.py dispatcher)
# ---------------------------------------------------------------------------


def _register_start(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("start", help=_HAPROXY_VERB_HELP["start"])
    p.add_argument(
        "--force", action="store_true", help="start even if this pod's IP != haproxy.host"
    )


def _register_stop(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("stop", help=_HAPROXY_VERB_HELP["stop"])


def _register_status(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("status", help=_HAPROXY_VERB_HELP["status"])


def _register_reload(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("reload", help=_HAPROXY_VERB_HELP["reload"])


def _register_logs(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("logs", help=_HAPROXY_VERB_HELP["logs"])


def _register_config(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("config", help=_HAPROXY_VERB_HELP["config"])


def _register_health(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("health", help=_HAPROXY_VERB_HELP["health"])


def _register_add(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("add", help=_HAPROXY_VERB_HELP["add"])
    p.add_argument("endpoint", help="ip:port to add")
    p.add_argument(
        "--pool", default=None, help="explicit pool name (default: auto by /v1/models probe)"
    )


def _register_remove(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("remove", help=_HAPROXY_VERB_HELP["remove"])
    p.add_argument("endpoint", help="ip:port to remove")


def _register_drain(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("drain", help=_HAPROXY_VERB_HELP["drain"])
    p.add_argument("endpoint", help="ip:port to drain")
    p.add_argument("--pool", default=None, help="explicit pool name")


def _register_scaling(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("scaling", help=_HAPROXY_VERB_HELP["scaling"])
    sc = p.add_subparsers(dest="scaling_verb", required=True, metavar="SCALING_VERB")

    at = sc.add_parser("attach", help=_SCALING_VERB_HELP["attach"])
    at.add_argument(
        "port",
        type=int,
        nargs="?",
        default=None,
        help="local vllm port to probe (default: 8000)",
    )

    de = sc.add_parser("detach", help=_SCALING_VERB_HELP["detach"])
    de.add_argument(
        "--force",
        action="store_true",
        help=(
            "force-close active sessions before removal "
            "(destructive: drops in-flight requests). "
            "Use when a stuck backend won't drain due to half-open TCP from a crashed vllm."
        ),
    )

    sc.add_parser("auto-add", help=_SCALING_VERB_HELP["auto-add"])


def _register_prune(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("prune", help=_HAPROXY_VERB_HELP["prune"])
    p.add_argument(
        "--pool",
        default=None,
        help="scope to one pool (default: all pools); exit 3 on unknown pool name",
    )
    p.add_argument(
        "--threshold",
        default=None,
        metavar="DURATION",
        help=(
            "override dead threshold (e.g. 5m, 300s, 2h); "
            "default: cluster.haproxy.prune.threshold or '5m'"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print candidates without removing; exit 0",
    )


def register_all(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register all haproxy sub-verb parsers into *sub*.

    Called by ``tctl.workloads.haproxy.__init__.run`` after building the top-level
    ``tctl haproxy`` parser.
    """
    _register_start(sub)
    _register_stop(sub)
    _register_status(sub)
    _register_reload(sub)
    _register_logs(sub)
    _register_config(sub)
    _register_health(sub)
    _register_add(sub)
    _register_remove(sub)
    _register_drain(sub)
    _register_scaling(sub)
    _register_prune(sub)


def _build_subparser() -> argparse.ArgumentParser:
    """Build and return the top-level ``tctl haproxy`` argument parser.

    Exposed for testing (AT-3: verify all sub-verbs appear in --help).
    """
    p = argparse.ArgumentParser(
        prog="tctl haproxy", description="HAProxy lifecycle + scaling control."
    )
    sub = p.add_subparsers(dest="verb", required=True, metavar="VERB")
    register_all(sub)
    return p


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _manager(ns: argparse.Namespace) -> tuple[LbManager, BackendState, Path]:
    rc = resolve(ns.config, profile=getattr(ns, "profile", None))
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl" / "haproxy"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, rc.lb.host)
    return mgr, bs, state_dir


def _spawn_watcher_if_enabled(mgr: LbManager, cluster_yaml_path: Path) -> None:
    """Spawn tctl-haproxy-watch watcher session after haproxy start, if prune.enabled is True."""
    from tctl.workloads.haproxy.prune import _spawn_watcher

    if not mgr.lb.prune.enabled:
        return
    if _tmux_session_exists("tctl-haproxy-watch"):
        print(
            "watcher already running (session=tctl-haproxy-watch) — skipping spawn",
            file=sys.stderr,
        )
        return
    _spawn_watcher(mgr, mgr.lb.prune, cluster_yaml_path)
    print("watcher started (session=tctl-haproxy-watch)", file=sys.stderr)


def _stop_watcher_if_running(mgr: LbManager) -> None:
    """Stop tctl-haproxy-watch watcher session before haproxy stop.

    Always calls _stop_watcher regardless of prune.enabled — avoids zombie
    session leaks when the operator toggles enabled=False after a running watcher.
    Prints a message only if a session was actually killed.
    """
    from tctl.workloads.haproxy.prune import _stop_watcher

    if _stop_watcher(mgr):
        print("watcher stopped", file=sys.stderr)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Top-level entry point: parse argv_rest and dispatch to ``_cmd_*`` handler."""
    parsed = _build_subparser().parse_args(argv_rest, namespace=ns)
    verb: str = parsed.verb
    _dispatch: dict[str, Callable[[argparse.Namespace], int]] = {
        "start": _cmd_start,
        "stop": _cmd_stop,
        "status": _cmd_status,
        "reload": _cmd_reload,
        "logs": _cmd_logs,
        "config": _cmd_config,
        "health": _cmd_health,
        "add": _cmd_add,
        "remove": _cmd_remove,
        "drain": _cmd_drain,
        "scaling": _cmd_scaling,
        "prune": _cmd_prune,
    }
    return _dispatch[verb](parsed)


def _cmd_start(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    try:
        mgr.start(force=getattr(ns, "force", False))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 4
    cfg_override = getattr(ns, "config", None)
    cluster_yaml = Path(cfg_override) if cfg_override else Path.home() / ".tctl" / "cluster.yaml"
    _spawn_watcher_if_enabled(mgr, cluster_yaml)
    return 0


def _cmd_stop(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    _stop_watcher_if_running(mgr)
    mgr.stop()
    return 0


def _cmd_status(ns: argparse.Namespace) -> int:
    mgr, bs, _state_dir = _manager(ns)
    return _do_info(mgr, bs)


def _cmd_reload(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    mgr.reload()
    return 0


def _cmd_logs(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    log_path = mgr.run_dir / "haproxy.log"
    if log_path.exists():
        print(log_path.read_text())
    return 0


def _cmd_config(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    print(mgr.render_config())
    return 0


def _cmd_health(ns: argparse.Namespace) -> int:
    mgr, bs, _state_dir = _manager(ns)
    return _do_health(mgr, bs)


def _cmd_add(ns: argparse.Namespace) -> int:
    mgr, bs, _state_dir = _manager(ns)
    from tctl.workloads.haproxy import scaling as _sc

    return _sc._do_add_cli(ns.endpoint, mgr, bs, getattr(ns, "pool", None))


def _cmd_remove(ns: argparse.Namespace) -> int:
    mgr, bs, _state_dir = _manager(ns)
    from tctl.workloads.haproxy import scaling as _sc

    return _sc._do_remove_cli(ns.endpoint, mgr, bs)


def _cmd_drain(ns: argparse.Namespace) -> int:
    mgr, bs, _state_dir = _manager(ns)
    from tctl.workloads.haproxy import scaling as _sc

    return _sc._do_drain(ns.endpoint, mgr, pool_name=getattr(ns, "pool", None))


def _cmd_scaling(ns: argparse.Namespace) -> int:
    """Dispatch to scaling sub-verbs: attach / detach / auto-add."""
    mgr, bs, _state_dir = _manager(ns)
    from tctl.workloads.haproxy import scaling as _sc

    sv: str = ns.scaling_verb
    if sv == "attach":
        port = getattr(ns, "port", None) or 8000
        return _sc._do_attach(port, mgr, bs)
    if sv == "detach":
        return _sc._do_detach(mgr, bs, force=getattr(ns, "force", False))
    if sv == "auto-add":
        return _sc._do_auto_add(mgr, bs)
    print(f"unknown scaling verb: {sv}", file=sys.stderr)
    return 2


def _cmd_prune(ns: argparse.Namespace) -> int:
    mgr, _bs, _state_dir = _manager(ns)
    return _do_prune(mgr, ns)


# ---------------------------------------------------------------------------
# Implementation: status / health / prune / haproxy-stats
# ---------------------------------------------------------------------------


def _do_info(mgr: LbManager, bs: BackendState, _console: Console | None = None) -> int:
    """Unified dashboard: LB process panel + per-pool table with scur/qcur/running/waiting.

    Always exits 0 (informational). Use `haproxy health` for scripting gate.
    ``_console`` is an optional injection point for testing (pass a ``rich.Console``).
    """
    from rich.box import MINIMAL_DOUBLE_HEAD
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from tctl.workloads.haproxy.probe import fetch_vllm_metrics
    from tctl.workloads.haproxy.prune import _watcher_status
    from tctl.workloads.haproxy.scaling import _client

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
            "Run `tctl haproxy add <ep>` to track them.[/yellow]"
        )

    return 0


def _do_health(mgr: LbManager, bs: BackendState) -> int:
    from tctl.workloads.haproxy.probe import probe_vllm

    unhealthy = 0
    total = 0
    use_color = sys.stdout.isatty()
    green = "\x1b[32m" if use_color else ""
    red = "\x1b[31m" if use_color else ""
    dim = "\x1b[2m" if use_color else ""
    reset = "\x1b[0m" if use_color else ""

    pools = list(mgr.lb.pools)
    for i, pool in enumerate(pools):
        if i > 0:
            print()  # blank line between pools
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pool.name)
        eps = pbs.list()
        url = f"http://{mgr.lb.host}:{pool.bind_port}"
        header = f"pool: {pool.name} ({pool.served_model})  {dim}→ {url}{reset}"
        if not eps:
            print(f"{header}  {dim}— (no backends){reset}")
            continue
        print(header)
        # Aligned columns: endpoint | status | /health | model | running
        col_w = max(len(ep) for ep in eps)
        for ep in eps:
            # B9: probe the actual backend host, not localhost.
            host = ep.split(":")[0]
            port = int(ep.rsplit(":", 1)[1])
            probe = probe_vllm(host, port)
            ok = probe.get("healthy", False)
            color = green if ok else red
            marker = "OK  " if ok else "FAIL"
            health = probe.get("health_code", "?")
            loaded = "yes" if probe.get("models_loaded") else "no"
            running = probe.get("num_requests_running", 0.0)
            print(
                f"  {color}{marker}{reset}  {ep:<{col_w}}  "
                f"/health={health}  loaded={loaded}  running={running:.0f}"
            )
            total += 1
            if not ok:
                unhealthy += 1
    # B10: print summary + return 1 if any unhealthy (not the count).
    print(f"unhealthy: {unhealthy} of {total}")
    return 1 if unhealthy else 0


def _do_prune(mgr: LbManager, parsed: argparse.Namespace) -> int:
    """Handle `tctl haproxy prune`.

    Threshold resolution order:
      1. --threshold flag (if given and valid)
      2. cluster.haproxy.prune.threshold YAML field
      3. Hardcoded default "5m"

    Pool list:
      - If --pool given: validate against configured pools first; exit 3 if unknown.
      - Otherwise: iterate all configured pools.

    Each candidate calls Reconciler.want_absent (haproxy-first ordering preserved).
    """
    from tctl.duration import _parse_duration
    from tctl.workloads.haproxy.errors import BackendOpFailed, LbUnreachable
    from tctl.workloads.haproxy.prune import _collect_prune_candidates
    from tctl.workloads.haproxy.reconciler import Reconciler

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
            print(f"haproxy prune: {exc}", file=sys.stderr)
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
                print(f"haproxy prune: {exc}", file=sys.stderr)
                return 1
            print(
                f"pruned {ep} from pool {pool_name} (DOWN for {duration_str})",
                file=sys.stderr,
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
    from tctl.workloads.haproxy.runtime import RuntimeClient, _parse_endpoint_from_name

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
        parsed_ep = _parse_endpoint_from_name(svname)
        if parsed_ep is not None:
            ep = f"{parsed_ep[0]}:{parsed_ep[1]}"
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
    Falls back to empty dict if the raw method is unavailable.
    """
    from tctl.workloads.haproxy.runtime import RuntimeClient, _parse_endpoint_from_name

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
            parsed_ep = _parse_endpoint_from_name(name)
            if parsed_ep is not None:
                endpoint = f"{parsed_ep[0]}:{parsed_ep[1]}"
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
