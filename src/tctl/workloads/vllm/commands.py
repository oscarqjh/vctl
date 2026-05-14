"""tctl vllm — consolidated command dispatcher.

Merges: info, profiles, args_cmd, preflight, serve, stop, rolling_restart.
Public surface: register_all(sub) + _cmd_<verb> handlers + run(ns, argv_rest).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

from tctl.tmux import TmuxSession

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sub-verb registry
# ---------------------------------------------------------------------------

_SERVE_SUB_VERBS = {"status", "restart", "console", "logs"}


def register_all(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register all tctl vllm sub-verbs into *sub*."""
    _register_info(sub)
    _register_profiles(sub)
    _register_args(sub)
    _register_preflight(sub)
    _register_serve(sub)
    _register_stop(sub)
    _register_rolling_restart(sub)


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Top-level dispatcher — called by tctl.workloads.vllm.__init__.run()."""
    p = argparse.ArgumentParser(prog="tctl vllm")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    verb: str = parsed.verb
    return {
        "info": _cmd_info,
        "profiles": _cmd_profiles,
        "args": _cmd_args,
        "preflight": _cmd_preflight,
        "serve": _cmd_serve,
        "stop": _cmd_stop,
        "rolling-restart": _cmd_rolling_restart,
    }[verb](parsed, [])


# ===========================================================================
# info
# ===========================================================================


def _register_info(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser(
        "info",
        prog="tctl vllm info",
        help="print resolved cluster + profile config as a table",
        description="Print the resolved cluster + profile config as a table.",
    )


def _cmd_info(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm info — resolved config table."""
    from tctl.platform import detect_self_ip
    from tctl.resolver import resolve

    try:
        rc = resolve(ns.config, profile=ns.profile)
    except (ValueError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    self_ip = detect_self_ip()
    rows = [
        ("profile", rc.profile_name),
        ("model", rc.model.name),
        ("self_ip", self_ip),
        ("dp / tp", f"{rc.parallelism.data_parallel} / {rc.parallelism.tensor_parallel}"),
        (
            "api_servers",
            str(rc.parallelism.api_server_count)
            if rc.parallelism.api_server_count is not None
            else f"(default: {rc.parallelism.data_parallel})",
        ),
        ("vllm_port", str(rc.server.http_port)),
        ("haproxy.host", rc.lb.host),
    ]
    for pool in rc.lb.pools:
        rows.append(
            (
                f"pool\\[{pool.name}]",
                f"{pool.served_model}  →  http://{rc.lb.host}:{pool.bind_port}",
            )
        )
    rows += [
        ("haproxy.admin", str(rc.lb.admin.bind_port)),
        ("haproxy.stats", str(rc.lb.stats.bind_port)),
        ("venv", rc.cluster.venv),
        ("state_dir", rc.cluster.state_dir),
    ]
    from rich.console import Console
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("key")
    table.add_column("value")
    for k, v in rows:
        table.add_row(k, v)
    Console().print(table)
    return 0


# ===========================================================================
# profiles
# ===========================================================================


def _register_profiles(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "profiles",
        prog="tctl vllm profiles",
        help="list available model profiles or set the active profile",
        description=(
            "List available model profiles (models/*.yaml). "
            "Marks the active profile from cluster.yaml with '*'. "
            "Use `set <name>` to switch."
        ),
    )
    psub = p.add_subparsers(dest="profiles_verb", metavar="VERB")
    psub.add_parser("list", help="list available profiles (default)")
    s = psub.add_parser("set", help="change vllm.default_profile in cluster.yaml")
    s.add_argument("name", help="profile name (must match a models/<name>.yaml)")


def _cmd_profiles(ns: argparse.Namespace, _argv_rest: list[str]) -> int:
    """tctl vllm profiles — list or set the active profile."""

    from tctl.config.settings import load_cluster_file

    cluster_path = Path(ns.config).resolve()
    verb = getattr(ns, "profiles_verb", None) or "list"

    if verb == "list":
        return _profiles_list(cluster_path, load_cluster_file)
    if verb == "set":
        return _profiles_set(cluster_path, ns.name, load_cluster_file)
    print(f"unknown verb: {verb}", file=sys.stderr)
    return 2


def _profiles_list(cluster_path: Path, load_cluster_file: object) -> int:

    cf = load_cluster_file(cluster_path)  # type: ignore[operator]
    # Active profile: vllm.default_profile (v0.9.0 shape)
    active = cf.vllm.default_profile if hasattr(cf, "vllm") else getattr(cf, "profile", None)
    models_dir = cluster_path.parent / "models"
    if not models_dir.exists():
        print(f"models/ directory not found next to {cluster_path}", flush=True)
        return 2
    for yml in sorted(models_dir.glob("*.yaml")):
        marker = "*" if yml.stem == active else " "
        print(f"{marker} {yml.stem}")
    return 0


def _profiles_set(cluster_path: Path, name: str, load_cluster_file: object) -> int:
    import re
    import tempfile

    models_dir = cluster_path.parent / "models"
    target = models_dir / f"{name}.yaml"
    if not target.is_file():
        print(f"unknown profile {name!r}: {target} not found", file=sys.stderr)
        available = sorted(p.stem for p in models_dir.glob("*.yaml")) if models_dir.exists() else []
        if available:
            print(f"available: {', '.join(available)}", file=sys.stderr)
        return 3

    text = cluster_path.read_text(encoding="utf-8")

    # v0.9.0 shape: update vllm.default_profile
    # Pattern matches "  default_profile: <anything>" under a vllm: block
    dp_pattern = re.compile(r"^(\s+default_profile:)[ \t]*.*$", re.MULTILINE)
    if dp_pattern.search(text):
        new_text = dp_pattern.sub(rf"\1 {name}", text, count=1)
    else:
        # Fallback: try legacy `profile:` top-level key
        profile_pattern = re.compile(r"^profile:[ \t]*.*$", re.MULTILINE)
        if not profile_pattern.search(text):
            print(
                f"no `vllm.default_profile` or `profile:` key found in {cluster_path}",
                file=sys.stderr,
            )
            return 3
        new_text = profile_pattern.sub(f"profile: {name}", text, count=1)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cluster_path.parent,
            delete=False,
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
        ) as fh:
            tmp_path = fh.name
            fh.write(new_text)
        os.replace(tmp_path, cluster_path)
        tmp_path = None
    except Exception:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise

    print(f"active profile: {name}")
    return 0


# ===========================================================================
# args
# ===========================================================================


def _register_args(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser(
        "args",
        prog="tctl vllm args",
        help="print the vLLM CLI args for the active profile",
        description=(
            "Print the vLLM CLI args that would be used for the active "
            "profile, one per line. Useful for debugging or piping into a manual "
            "vllm invocation."
        ),
    )


def _cmd_args(ns: argparse.Namespace, _argv_rest: list[str]) -> int:
    """tctl vllm args — resolved vllm serve flags, one per line."""
    from tctl.resolver import resolve

    try:
        rc = resolve(ns.config, profile=ns.profile)
    except (ValueError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out: list[str] = [
        rc.model.name,
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--port={rc.server.http_port}",
    ]
    if rc.parallelism.api_server_count is not None:
        out.append(f"--api-server-count={rc.parallelism.api_server_count}")
    for k, v in rc.vllm_args.items():
        if v is True:
            out.append(f"--{k}")
        elif v is False:
            out.append(f"--no-{k}")
        else:
            out.append(f"--{k}={v}")
    print("\n".join(out))
    return 0


# ===========================================================================
# preflight
# ===========================================================================


def _register_preflight(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "preflight",
        prog="tctl vllm preflight",
        help="sanity checks: GPUs, /dev/shm, venv, lb route, vllm port",
        description=(
            "Run sanity checks before launching a vllm inference server:\n"
            "  gpus       — nvidia-smi present (or num_gpus=0)\n"
            "  shm        — /dev/shm ≥ 8 GB\n"
            "  venv       — cluster.venv path exists\n"
            "  lb_route   — TCP connection to haproxy.host:pool.bind_port succeeds\n"
            "  vllm_port  — server.http_port is free on localhost (no stale vllm)\n"
            "\n"
            "Exit 0 when all checks pass, exit 4 when any check fails.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="Emit results as JSON")


def _cmd_preflight(ns: argparse.Namespace, _argv_rest: list[str]) -> int:
    """tctl vllm preflight — sanity checks."""
    import json
    import shutil
    import socket

    from tctl.resolver import resolve

    try:
        rc = resolve(ns.config, profile=ns.profile)
    except (ValueError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def _check_gpus(num_gpus: int) -> tuple[bool, str]:
        nvidia_smi = shutil.which("nvidia-smi")
        if not nvidia_smi:
            return (num_gpus == 0, "nvidia-smi not found (ok only if num_gpus=0)")
        return (True, "nvidia-smi present")

    def _check_shm() -> tuple[bool, str]:
        try:
            st = os.statvfs("/dev/shm")
            size_gb = st.f_blocks * st.f_frsize / 1e9
            return (size_gb >= 8, f"/dev/shm = {size_gb:.1f} GB")
        except OSError as e:
            return (False, str(e))

    def _check_venv(path: str) -> tuple[bool, str]:
        return (Path(path).exists(), f"venv {path}")

    def _check_lb_route(host: str, port: int) -> tuple[bool, str]:
        try:
            with socket.create_connection((host, port), timeout=2):
                return (True, f"tcp {host}:{port} reachable")
        except OSError as e:
            return (False, f"tcp {host}:{port}: {e}")

    def _check_vllm_port_free(port: int) -> tuple[bool, str]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as e:
                return (
                    False,
                    f"localhost:{port} already in use ({e}); "
                    f"run `tctl vllm stop` or kill the stale process first",
                )
            return (True, f"localhost:{port} free")
        finally:
            sock.close()

    checks = [
        ("gpus", *_check_gpus(rc.resources.num_gpus)),
        ("shm", *_check_shm()),
        ("venv", *_check_venv(rc.cluster.venv)),
        ("lb_route", *_check_lb_route(rc.lb.host, rc.lb.pools[0].bind_port)),
        ("vllm_port", *_check_vllm_port_free(rc.server.http_port)),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "msg": m} for (n, ok, m) in checks]}
    use_json = getattr(ns, "json", False)
    if use_json:
        print(json.dumps(payload, indent=2))
    else:
        for name, ok, msg in checks:
            mark = "OK" if ok else "FAIL"
            print(f"[{mark}] {name}: {msg}")
    return 0 if all(c["ok"] for c in payload["checks"]) else 4


# ===========================================================================
# serve  (sub-verbs: status / restart / console / logs — stop DROPPED)
# ===========================================================================


def _register_serve(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "serve",
        prog="tctl vllm serve",
        help="spawn / manage the vllm tmux session",
        description=(
            "Spawn a vllm inference server in a detached tmux session, attach to LB pool,\n"
            "and return immediately. The vllm process survives SSH disconnect.\n"
            "\n"
            "Signal handling (--foreground mode only):\n"
            "  SIGINT / SIGTERM / SIGHUP — graceful drain → detach → kill subprocess tree\n"
            "\n"
            "Relevant env vars:\n"
            "  TCTL_READY_TIMEOUT      — readiness poll timeout in seconds (default: 1800)\n"
            "  VLLM_ENGINE_READY_TIMEOUT_S — per-profile override (wins over TCTL_READY_TIMEOUT)\n"
            "  LB_DETACH_WAIT          — seconds to wait for in-flight requests to drain\n"
            "  TCTL_KILL_GRACE         — SIGTERM→SIGKILL grace period in seconds\n"
        ),
        epilog=(
            "Sub-commands (run `tctl vllm serve <verb> --help` for details):\n"
            "  status     Show tmux/pid/lb-attached state for the active profile\n"
            "  restart    Stop + start in-place (preserves profile)\n"
            "  console    Attach terminal to live vllm tmux session (Ctrl-B D detaches)\n"
            "  logs       Tail or follow the vllm log file\n"
            "\n"
            "Use `tctl vllm stop` to drain + kill the running vllm.\n"
            "\n"
            "Flags below apply to the default `tctl vllm serve` (start) path only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the preflight checks (GPU, /dev/shm, venv, LB route) before serving",
    )
    p.add_argument(
        "--foreground",
        action="store_true",
        default=False,
        help=(
            "Run vllm as a direct child process. "
            "tctl blocks until vllm exits; SSH disconnect kills vllm. "
            "Signals trigger drain → remove → kill. "
            "Default: detached tmux session."
        ),
    )


def _cmd_serve(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm serve — dispatch to sub-verb or start."""
    # Peel sub-verb before parsing flags
    if argv_rest and argv_rest[0] in _SERVE_SUB_VERBS:
        sub_verb = argv_rest[0]
        rest = argv_rest[1:]
        return {
            "status": _cmd_serve_status,
            "restart": _cmd_serve_restart,
            "console": _cmd_serve_console,
            "logs": _cmd_serve_logs,
        }[sub_verb](ns, rest)

    p = argparse.ArgumentParser(
        prog="tctl vllm serve",
        description="Spawn vllm in detached tmux session and attach to LB pool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--skip-preflight", action="store_true")
    p.add_argument("--foreground", action="store_true", default=False)
    parsed = p.parse_args(argv_rest)

    if parsed.foreground or os.environ.get("TCTL_SERVE_FOREGROUND"):
        return _serve_foreground(ns, parsed)
    return _serve_detached(ns, parsed)


def _serve_detached(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
    """Default start: detached tmux-supervised vllm."""
    from tctl.resolver import resolve
    from tctl.workloads.haproxy.routing import pool_for_model
    from tctl.workloads.vllm.manager import VllmManager

    if not parsed.skip_preflight:
        pf_rc = _cmd_preflight(ns, [])
        if pf_rc != 0:
            _LOG.error("preflight checks failed (exit %d); aborting serve", pf_rc)
            return pf_rc

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl"
    pool_for_model(rc.lb, rc.model.name)  # exit 3 on miss

    vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    try:
        vm.start()
    except RuntimeError as e:
        _LOG.error("%s", e)
        return 4
    return 0


def _cmd_serve_status(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm serve status."""
    from tctl.resolver import resolve
    from tctl.workloads.vllm.manager import VllmManager

    p = argparse.ArgumentParser(
        prog="tctl vllm serve status",
        description=(
            "Show tmux session, pid liveness, vllm readiness, LB-attached state, "
            "and log size for the active profile."
        ),
    )
    p.parse_args(argv_rest)

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl"
    vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    info = vm.status()

    def _yn(v: object) -> str:
        if v is None:
            return "unknown"
        return "yes" if v else "no"

    print(f"profile:     {rc.profile_name}")
    print(f"session:     {info['session_name']}")
    print(f"tmux alive:  {_yn(info['tmux_alive'])}")
    print(f"pid alive:   {_yn(info['pid_alive'])}")
    print(f"vllm ready:  {_yn(info['vllm_ready'])}")
    print(f"lb attached: {_yn(info['lb_attached'])}")
    print(f"pid:         {info['pid'] or '—'}")
    print(f"log size:    {info['log_size']} bytes")
    print(f"log path:    {info['log_path']}")
    if info.get("cross_host"):
        print("note: state files belong to a different host; pid_alive is unknown")
    return 0


def _cmd_serve_restart(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm serve restart."""
    from tctl.resolver import resolve
    from tctl.workloads.vllm.manager import VllmManager

    p = argparse.ArgumentParser(
        prog="tctl vllm serve restart",
        description=(
            "Stop the running vllm and start a fresh instance under the same profile. "
            "Preserves config."
        ),
    )
    p.parse_args(argv_rest)

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl"
    vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    try:
        vm.restart()
    except RuntimeError as e:
        _LOG.error("%s", e)
        return 4
    return 0


def _cmd_serve_console(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm serve console."""
    from tctl.resolver import resolve
    from tctl.workloads.vllm.manager import VllmManager

    p = argparse.ArgumentParser(
        prog="tctl vllm serve console",
        description=(
            "Attach the operator's terminal to the live vllm tmux session. "
            "Ctrl-B D detaches without killing vllm."
        ),
    )
    p.parse_args(argv_rest)

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl"
    vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    try:
        vm.console()
    except RuntimeError as e:
        _LOG.error("%s", e)
        return 4
    return 0


def _cmd_serve_logs(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """tctl vllm serve logs."""
    from tctl.resolver import resolve
    from tctl.workloads.vllm.manager import VllmManager

    p = argparse.ArgumentParser(
        prog="tctl vllm serve logs",
        description=(
            "Print the last N lines of the vllm log, follow new lines with -f, "
            "or prune the log file in-place with --prune (preserves tmux pipe-pane fd)."
        ),
    )
    p.add_argument("-n", type=int, default=50, metavar="N", help="Number of lines (default: 50)")
    p.add_argument("-f", "--follow", action="store_true", help="Stream new lines as written")
    p.add_argument(
        "--prune",
        action="store_true",
        help="Trim log in-place (keeps tmux pipe-pane fd). Mutually exclusive with --follow.",
    )
    p.add_argument(
        "--keep", type=int, default=10000, metavar="N", help="With --prune: keep last N lines"
    )
    p.add_argument(
        "--all", dest="prune_all", action="store_true", help="With --prune: wipe everything"
    )
    parsed = p.parse_args(argv_rest)

    if parsed.prune_all and not parsed.prune:
        p.error("--all requires --prune")
    if parsed.prune and parsed.follow:
        p.error("--prune and --follow are mutually exclusive")

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl"
    vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
    return vm.logs(
        n=parsed.n,
        follow=parsed.follow,
        prune=parsed.prune,
        keep=parsed.keep,
        prune_all=parsed.prune_all,
    )


# ===========================================================================
# Foreground serve helpers (shared between serve and stop sweep)
# ===========================================================================


_READY_TIMEOUT_DEFAULT = 1800


def _resolve_ready_timeout(rc: object) -> float:
    """Return readiness-poll timeout in seconds.

    Precedence (first wins):
    1. ``rc.env["VLLM_ENGINE_READY_TIMEOUT_S"]`` — per-profile override.
    2. ``TCTL_READY_TIMEOUT`` OS environment variable.
    3. Hard default: 1800 s (30 min).
    """
    default = float(_READY_TIMEOUT_DEFAULT)
    env_dict: dict[str, object] = {}
    if hasattr(rc, "env") and isinstance(getattr(rc, "env", None), dict):
        env_dict = rc.env
    raw: object = env_dict.get("VLLM_ENGINE_READY_TIMEOUT_S")
    if raw is None:
        raw = os.environ.get("TCTL_READY_TIMEOUT")
    if raw is not None:
        try:
            return float(int(str(raw)))
        except (ValueError, TypeError):
            _LOG.warning(
                "invalid ready-timeout value %r; falling back to %ss", raw, _READY_TIMEOUT_DEFAULT
            )
    return default


def _wait_for_ready(port: int, timeout: float) -> None:
    import time

    import httpx

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://localhost:{port}/v1/models", timeout=2.0)
            if r.json().get("data"):
                return
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(f"vllm did not become ready on :{port}: {last_err}")


def _wait_for_idle(port: int, timeout: float) -> None:
    import time

    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = httpx.get(f"http://localhost:{port}/metrics", timeout=2.0).text
            running = 0.0
            for line in text.splitlines():
                if line.startswith("vllm:num_requests_running "):
                    running = float(line.split()[1])
            if running <= 0.0:
                return
        except Exception:
            return
        time.sleep(1)


def _kill_tree(pid: int, grace: float = 30.0) -> None:
    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for p in [root, *children]:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs([root, *children], timeout=grace)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()


def _serve_foreground(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
    """Run vllm as direct child process (--foreground mode)."""
    import signal
    import subprocess

    from tctl.platform import detect_self_ip
    from tctl.resolver import resolve
    from tctl.workloads.haproxy.manager import LbManager
    from tctl.workloads.haproxy.routing import pool_for_model
    from tctl.workloads.haproxy.scaling import _do_add, _do_drain, _do_remove
    from tctl.workloads.haproxy.state import BackendState

    if not parsed.skip_preflight:
        pf_rc = _cmd_preflight(ns, [])
        if pf_rc != 0:
            _LOG.error("preflight checks failed (exit %d); aborting serve", pf_rc)
            return pf_rc

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl" / "haproxy"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    pool = pool_for_model(rc.lb, rc.model.name)
    bs = BackendState(state_dir, rc.lb.host, pool=pool.name)

    env = os.environ.copy()
    venv_bin = str(Path(rc.cluster.venv) / "bin")
    env["PATH"] = f"{venv_bin}:{env['PATH']}"
    if rc.resources.cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
    for k, v in rc.env.items():
        if isinstance(v, bool):
            env[k] = "true" if v else "false"
        else:
            env[k] = str(v)

    cmd = [
        "vllm",
        "serve",
        rc.model.name,
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--port={rc.server.http_port}",
    ]
    if rc.parallelism.api_server_count is not None:
        cmd.append(f"--api-server-count={rc.parallelism.api_server_count}")
        if (
            rc.parallelism.api_server_count == 1
            and rc.parallelism.data_parallel > 1
            and rc.vllm_args.get("mm-processor-cache-type") == "shm"
        ):
            _LOG.warning(
                "config will hit vllm shm bug: api_server_count=1 + data_parallel=%d + "
                "mm-processor-cache-type=shm. Remove api_server_count from the profile "
                "(vllm will default to data_parallel), or change mm-processor-cache-type "
                "to 'lru'. Continuing anyway — vllm WILL crash with FileNotFoundError "
                "on shm_open.",
                rc.parallelism.data_parallel,
            )
    for k, v in rc.vllm_args.items():
        if v is True:
            cmd.append(f"--{k}")
        elif v is False:
            cmd.append(f"--no-{k}")
        else:
            cmd.append(f"--{k}={v}")

    _LOG.info("spawning %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env, preexec_fn=os.setpgrp)

    self_ip = detect_self_ip()
    ep = f"{self_ip}:{rc.server.http_port}"
    state = {"attached": False}

    def _shutdown(signum: int, frame: object) -> None:
        if state["attached"]:
            _LOG.info("signal %d received; draining + detaching", signum)
            try:
                _do_drain(ep, mgr, pool_name=pool.name)
                _wait_for_idle(
                    rc.server.http_port,
                    timeout=float(os.environ.get("LB_DETACH_WAIT", "600")),
                )
                _do_remove(ep, mgr, bs, pool_name=pool.name)
            finally:
                grace = float(os.environ.get("TCTL_KILL_GRACE", "30"))
                _kill_tree(proc.pid, grace=grace)
                sys.exit(130)
        else:
            _LOG.info("signal %d received during model load; reaping subprocess", signum)
            grace = float(os.environ.get("TCTL_KILL_GRACE", "30"))
            _kill_tree(proc.pid, grace=grace)
            sys.exit(130)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    try:
        _wait_for_ready(rc.server.http_port, timeout=_resolve_ready_timeout(rc))
    except TimeoutError as e:
        _LOG.error("readiness timed out: %s", e)
        _kill_tree(proc.pid)
        return 4

    attach_rc = _do_add(ep, mgr, bs, pool_name=pool.name)
    if attach_rc != 0:
        _LOG.error("lb attach failed (rc=%d) for %s; shutting down vllm", attach_rc, ep)
        _kill_tree(proc.pid)
        return attach_rc
    state["attached"] = True

    _watchdog_enabled = os.environ.get("TCTL_NO_PPID_WATCHDOG", "0") not in ("1", "true", "yes")
    _watchdog_tick = 0

    import subprocess as _sp

    while True:
        try:
            rc_code = proc.wait(timeout=0.5)
            return rc_code
        except _sp.TimeoutExpired:
            pass

        if _watchdog_enabled:
            _watchdog_tick += 1
            if _watchdog_tick >= 10:
                _watchdog_tick = 0
                if os.getppid() == 1:
                    _LOG.warning(
                        "PPID==1: launching shell appears to have died; "
                        "triggering graceful drain+shutdown"
                    )
                    _shutdown(signal.SIGTERM, None)


# ===========================================================================
# stop  (merged: drain from LB + kill tmux session + sweep process tree)
# ===========================================================================


def _register_stop(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "stop",
        prog="tctl vllm stop",
        help="drain from LB, kill tmux session, sweep local vllm process",
        description=(
            "Drain this host's vllm endpoint(s) from all LB pools,\n"
            "then kill the supervised tmux session, then sweep any remaining\n"
            "local vllm process trees.\n"
            "\n"
            "Env vars:\n"
            "  LB_DETACH_WAIT   — seconds to wait for in-flight requests to drain\n"
            "  TCTL_KILL_GRACE  — SIGTERM→SIGKILL grace period in seconds\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="Emit results as JSON")


def _cmd_stop(ns: argparse.Namespace, _argv_rest: list[str]) -> int:
    """tctl vllm stop — drain from LB, kill tmux session, sweep local process."""
    from tctl.config.settings import load_cluster_file, resolve_profile_name

    cfg = load_cluster_file(ns.config)
    profile = resolve_profile_name(getattr(ns, "profile", None), cfg)
    if not profile:
        print("error: no profile selected (use --profile or $TCTL_PROFILE)", file=sys.stderr)
        return 2
    session_name = f"tctl-vllm-{profile}"

    # Step 1: drain from LB (non-fatal)
    try:
        _drain_from_lb(profile, cfg)
    except Exception as exc:
        _LOG.warning("drain failed (non-fatal): %s", exc)

    # Step 2: kill tmux session
    _kill_tmux_session(session_name)

    # Step 3: process sweep fallback
    _sweep_local_vllm()
    return 0


def _drain_from_lb(profile: str, cfg: object) -> None:
    """Drain this host's vllm endpoint(s) from all LB pools.

    Iterates every registered pool and removes matching self-IP endpoints.
    """

    from tctl.platform import detect_self_ip
    from tctl.workloads.haproxy.manager import LbManager
    from tctl.workloads.haproxy.scaling import _do_drain, _do_remove
    from tctl.workloads.haproxy.state import BackendState

    # cfg is a ClusterFile — access .cluster and .haproxy
    lb = cfg.haproxy  # type: ignore[attr-defined]
    cluster = cfg.cluster  # type: ignore[attr-defined]
    state_dir = Path(cluster.state_dir)
    run_dir = Path.home() / ".tctl" / "haproxy"
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    self_ip = detect_self_ip()

    pool_names = BackendState.list_pools(state_dir, lb.host)
    for pname in pool_names:
        bs = BackendState(state_dir, lb.host, pool=pname)
        matching = [ep for ep in bs.list() if ep.startswith(f"{self_ip}:")]
        for ep in matching:
            drain_rc = _do_drain(ep, mgr, pool_name=pname)
            if drain_rc != 0:
                _LOG.warning("drain failed (rc=%d) for %s in pool %s", drain_rc, ep, pname)
            else:
                port = int(ep.rsplit(":", 1)[1])
                _wait_for_idle(port, timeout=float(os.environ.get("LB_DETACH_WAIT", "600")))
            _do_remove(ep, mgr, bs, pool_name=pname)


def _kill_tmux_session(name: str) -> None:
    """Send C-c then kill the named tmux session if it exists."""
    sess = TmuxSession(name)
    if sess.exists():
        sess.kill(tree=True, grace_s=float(os.environ.get("TCTL_KILL_GRACE", "5")))


def _sweep_local_vllm() -> None:
    """SIGTERM any remaining local vllm process trees."""
    import psutil

    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            if not cmd:
                continue
            if "vllm" not in (cmd[0] if cmd else ""):
                continue
            if "serve" not in cmd:
                continue
            _kill_tree(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


# ===========================================================================
# rolling-restart  (_SESSION_DIR refactored → VllmManager._rolling_restart_session_path)
# ===========================================================================


def _register_rolling_restart(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "rolling-restart",
        prog="tctl vllm rolling-restart",
        help="sequential per-pool endpoint rolling restart",
        description=(
            "Sequential, halt-on-failure rolling restart of every endpoint in a pool.\n"
            "ssh-es to each worker, runs `tctl vllm serve restart`, waits until HAProxy\n"
            "reports UP before moving to the next.\n"
            "\n"
            "State is persisted to ~/.tctl/vllm/rolling-restart/<pool>.json so an\n"
            "interrupted run can be auto-resumed by re-running the same command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pool", required=True, metavar="NAME", help="Target pool name (required).")
    mx = p.add_mutually_exclusive_group()
    mx.add_argument(
        "--fresh",
        action="store_true",
        help="Delete existing session file before starting; force fresh run from all eps.",
    )
    mx.add_argument(
        "--status",
        action="store_true",
        help="Print session file contents (or 'no session in progress'); exit 0.",
    )
    mx.add_argument(
        "--abort",
        action="store_true",
        help="Delete session file if present; exit 0.",
    )
    mx.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would happen without ssh-ing; session file not written or deleted.",
    )
    p.add_argument(
        "--ready-timeout",
        type=int,
        default=60,
        dest="ready_timeout",
        metavar="SECONDS",
        help="Seconds to wait for HAProxy UP after ssh returns 0 (default: 60).",
    )
    p.add_argument(
        "--vllm-timeout",
        type=int,
        default=600,
        dest="vllm_timeout",
        metavar="SECONDS",
        help="Seconds tctl vllm serve restart is allowed to take on the remote (default: 600).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-ep progress lines; print only final summary.",
    )
    p.add_argument(
        "--ssh-user",
        default="",
        dest="ssh_user",
        metavar="USER",
        help="Override ssh username (default: use ssh config / key).",
    )
    p.add_argument(
        "--remote-tctl-path",
        default=None,
        dest="remote_tctl_path",
        metavar="PATH",
        help=(
            "Override remote tctl path (default: bash -lc 'tctl vllm serve restart'). "
            "Use for non-standard installs e.g. /opt/tctl/bin/tctl."
        ),
    )


def _cmd_rolling_restart(ns: argparse.Namespace, _argv_rest: list[str]) -> int:
    """tctl vllm rolling-restart — sequential per-pool rolling restart."""
    import datetime
    import fcntl
    import json
    import subprocess
    import time
    from typing import Literal

    from tctl.resolver import resolve
    from tctl.workloads.haproxy.commands import _fetch_haproxy_stats
    from tctl.workloads.haproxy.manager import LbManager
    from tctl.workloads.haproxy.runtime import lb_admin_client
    from tctl.workloads.haproxy.state import BackendState
    from tctl.workloads.vllm.manager import VllmManager

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".tctl" / "haproxy"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)

    pool_name: str = ns.pool

    # Derive session path via VllmManager._rolling_restart_session_path
    vm_proto = VllmManager.__new__(VllmManager)
    session_path = vm_proto._rolling_restart_session_path(pool_name)

    # Inline _SessionFile using the derived session_path
    class _SessionFile:
        def __init__(self, path: Path) -> None:
            self._path = path
            self._lock_path = path.parent / (path.stem + ".lock")

        def exists(self) -> bool:
            return self._path.exists()

        def read(self) -> dict[str, Any] | None:
            if not self._path.exists():
                return None
            self._lock_path.touch(exist_ok=True)
            with open(self._lock_path, "r+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    raw = self._path.read_text(encoding="utf-8")
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            try:
                return json.loads(raw)  # type: ignore[no-any-return]
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"corrupted session file at {self._path}; use --abort to clear it"
                ) from exc

        def write(self, data: dict[str, Any]) -> None:
            self._lock_path.touch(exist_ok=True)
            with open(self._lock_path, "r+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    tmp_p = Path(str(self._path) + ".tmp")
                    tmp_p.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    os.replace(tmp_p, self._path)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        def delete(self) -> None:
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()

    def _verify_ep_up(ep: str, pool: str, mgr_: LbManager, timeout_s: int) -> bool:
        deadline = time.monotonic() + timeout_s
        pool_section = f"pool_{pool}"
        while time.monotonic() < deadline:
            cli = lb_admin_client(mgr_)
            if cli is None:
                time.sleep(1)
                continue
            stats = _fetch_haproxy_stats(cli)
            for srv_data in stats.get(pool_section, {}).values():
                if srv_data.get("ep") == ep:
                    status = str(srv_data.get("status", ""))
                    if status.startswith("UP"):
                        return True
                    break
            time.sleep(1)
        return False

    def _restart_one_ep(
        ep: str,
        idx: int,
        total: int,
        pool: str,
        mgr_: LbManager,
        ssh_user: str,
        vllm_timeout: int,
        ready_timeout: int,
        dry_run: bool,
        quiet: bool,
        remote_tctl_path: str | None,
    ) -> Literal["ok", "failed"]:
        ep_host = ep.split(":")[0]
        prefix = f"[{idx}/{total}] {ep}"
        if not quiet:
            print(f"{prefix}  draining → restarting...", file=sys.stderr)
        if dry_run:
            print(f"{prefix}  would restart", file=sys.stderr)
            return "ok"
        ssh_target = f"{ssh_user}@{ep_host}" if ssh_user else ep_host
        if remote_tctl_path:
            remote_cmd = f"{remote_tctl_path} vllm serve restart"
        else:
            remote_cmd = "bash -lc 'tctl vllm serve restart'"
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            ssh_target,
            remote_cmd,
        ]
        try:
            result = subprocess.run(argv, timeout=vllm_timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            print(f"{prefix}  timed out after {vllm_timeout}s. HALTING.", file=sys.stderr)
            return "failed"
        if result.returncode != 0:
            snippet = result.stderr.strip()[:200]
            print(
                f"{prefix}  ssh failed (rc={result.returncode}): {snippet}. HALTING.",
                file=sys.stderr,
            )
            return "failed"
        if not quiet:
            print(f"{prefix}  waiting for UP...", file=sys.stderr)
        t0 = time.monotonic()
        if not _verify_ep_up(ep, pool, mgr_, timeout_s=ready_timeout):
            print(f"{prefix}  did not become UP within {ready_timeout}s. HALTING.", file=sys.stderr)
            return "failed"
        elapsed = int(time.monotonic() - t0)
        if not quiet:
            print(f"{prefix}  ready ({elapsed}s)", file=sys.stderr)
        return "ok"

    sf = _SessionFile(session_path)
    dry_run: bool = getattr(ns, "dry_run", False)
    quiet: bool = getattr(ns, "quiet", False)
    ssh_user: str = getattr(ns, "ssh_user", "")
    vllm_timeout: int = getattr(ns, "vllm_timeout", 600)
    ready_timeout: int = getattr(ns, "ready_timeout", 60)
    remote_tctl_path: str | None = getattr(ns, "remote_tctl_path", None)

    # --status
    if getattr(ns, "status", False):
        if sf.exists():
            try:
                data = sf.read()
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(data, indent=2))
        else:
            print(f"no session in progress for pool {pool_name!r}")
        return 0

    # --abort
    if getattr(ns, "abort", False):
        if sf.exists():
            sf.delete()
            print(f"session file for pool {pool_name!r} deleted.", file=sys.stderr)
        else:
            print(f"no session file for pool {pool_name!r}", file=sys.stderr)
        return 0

    # --fresh
    if getattr(ns, "fresh", False):
        sf.delete()

    # Validate pool
    configured = {p.name for p in mgr.lb.pools}
    if pool_name not in configured:
        available = ", ".join(sorted(configured))
        print(f"unknown pool: {pool_name!r}; available: {available}", file=sys.stderr)
        return 3

    # Concurrency guard
    if sf.exists() and not dry_run:
        try:
            data = sf.read()
        except ValueError as exc:
            print(
                f"{exc}  Run `tctl vllm rolling-restart --pool {pool_name} --abort` to clear.",
                file=sys.stderr,
            )
            return 2
        if data is not None and data.get("in_progress"):
            print(
                f"rolling-restart already in progress for pool {pool_name!r} "
                "— kill the other invocation or use --abort",
                file=sys.stderr,
            )
            return 4

    pbs = BackendState(mgr.state_dir, mgr.lb.host, pool=pool_name)
    eps = sorted(pbs.list())
    if not eps:
        print(
            f"pool {pool_name!r} has no registered backends; nothing to restart",
            file=sys.stderr,
        )
        return 0

    # Try resume if session file exists
    try:
        existing = sf.read()
    except ValueError as exc:
        print(
            f"{exc}  Run `tctl vllm rolling-restart --pool {pool_name} --abort` to clear.",
            file=sys.stderr,
        )
        return 2

    if existing is not None:
        # Resume path
        started_at: str = str(existing.get("started_at", ""))
        completed: list[str] = list(existing.get("completed", []))
        failed: list[str] = list(existing.get("failed", []))
        pending: list[str] = list(existing.get("pending", []))
        total_eps = len(completed) + len(failed) + len(pending)
        print(
            f"resuming rolling-restart for pool {pool_name!r}: "
            f"{len(completed)} completed, {len(failed)} failed, {len(pending)} pending "
            f"({total_eps} total)",
            file=sys.stderr,
        )
        if not dry_run:
            sf.write(
                {
                    "pool": pool_name,
                    "started_at": started_at,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                    "in_progress": True,
                }
            )
        to_retry: list[str] = []
        for ep in list(failed):
            is_up = _verify_ep_up(ep, pool_name, mgr, timeout_s=5)
            if is_up:
                print(f"verified: {ep} was fixed externally — moving to completed", file=sys.stderr)
                failed.remove(ep)
                completed.append(ep)
                if not dry_run:
                    sf.write(
                        {
                            "pool": pool_name,
                            "started_at": started_at,
                            "completed": completed,
                            "failed": failed,
                            "pending": pending,
                            "in_progress": True,
                        }
                    )
            else:
                if dry_run or quiet:
                    print(
                        f"{ep} is still DOWN — skipping (--dry-run/--quiet mode)",
                        file=sys.stderr,
                    )
                    failed.remove(ep)
                    completed.append(ep)
                else:
                    print(
                        f"\nep {ep} is still DOWN. Choose:\n"
                        "  (a) skip — mark as completed and continue\n"
                        "  (b) retry — re-attempt restart\n"
                        "  (c) abort — exit now (session file preserved)\n",
                        file=sys.stderr,
                    )
                    if not sys.stdin.isatty():
                        print("  no TTY — defaulting to skip", file=sys.stderr)
                        choice = "a"
                    else:
                        choice = sys.stdin.read(1).strip().lower()
                    if choice == "a":
                        failed.remove(ep)
                        completed.append(ep)
                        if not dry_run:
                            sf.write(
                                {
                                    "pool": pool_name,
                                    "started_at": started_at,
                                    "completed": completed,
                                    "failed": failed,
                                    "pending": pending,
                                    "in_progress": True,
                                }
                            )
                    elif choice == "b":
                        failed.remove(ep)
                        to_retry.append(ep)
                    else:
                        if not dry_run:
                            sf.write(
                                {
                                    "pool": pool_name,
                                    "started_at": started_at,
                                    "completed": completed,
                                    "failed": failed,
                                    "pending": pending,
                                    "in_progress": False,
                                }
                            )
                        print(f"Aborted. Session file preserved at {sf._path}.", file=sys.stderr)
                        return 1

        work_queue = to_retry + pending
        total = len(completed) + len(work_queue)
        start_idx = len(completed) + 1
        for i, ep in enumerate(work_queue):
            idx = start_idx + i
            outcome = _restart_one_ep(
                ep=ep,
                idx=idx,
                total=total,
                pool=pool_name,
                mgr_=mgr,
                ssh_user=ssh_user,
                vllm_timeout=vllm_timeout,
                ready_timeout=ready_timeout,
                dry_run=dry_run,
                quiet=quiet,
                remote_tctl_path=remote_tctl_path,
            )
            if ep in pending:
                pending = [e for e in pending if e != ep]
            if outcome == "ok":
                completed.append(ep)
                if not dry_run:
                    sf.write(
                        {
                            "pool": pool_name,
                            "started_at": started_at,
                            "completed": completed,
                            "failed": failed,
                            "pending": pending,
                            "in_progress": True,
                        }
                    )
            else:
                failed.append(ep)
                if not dry_run:
                    sf.write(
                        {
                            "pool": pool_name,
                            "started_at": started_at,
                            "completed": completed,
                            "failed": failed,
                            "pending": pending,
                            "in_progress": False,
                        }
                    )
                print(
                    f"HALTING after failure on {ep}. "
                    f"Fix ep then re-run `tctl vllm rolling-restart --pool {pool_name}` to resume.",
                    file=sys.stderr,
                )
                return 1
        if not dry_run:
            sf.delete()
        print(
            f"rolling-restart complete: {len(completed)} ep(s) confirmed in pool {pool_name!r}",
            file=sys.stderr,
        )
        return 0

    # Fresh path
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    session: dict[str, Any] = {
        "pool": pool_name,
        "started_at": now_utc,
        "completed": [],
        "failed": [],
        "pending": list(eps),
        "in_progress": True,
    }
    if not dry_run:
        sf.write(session)

    completed_list: list[str] = []
    failed_list: list[str] = []
    pending_list = list(eps)

    for idx, ep in enumerate(eps, start=1):
        outcome = _restart_one_ep(
            ep=ep,
            idx=idx,
            total=len(eps),
            pool=pool_name,
            mgr_=mgr,
            ssh_user=ssh_user,
            vllm_timeout=vllm_timeout,
            ready_timeout=ready_timeout,
            dry_run=dry_run,
            quiet=quiet,
            remote_tctl_path=remote_tctl_path,
        )
        pending_list = [e for e in pending_list if e != ep]
        if outcome == "ok":
            completed_list.append(ep)
            if not dry_run:
                sf.write(
                    {
                        "pool": pool_name,
                        "started_at": now_utc,
                        "completed": completed_list,
                        "failed": [],
                        "pending": pending_list,
                        "in_progress": True,
                    }
                )
        else:
            failed_list.append(ep)
            if not dry_run:
                sf.write(
                    {
                        "pool": pool_name,
                        "started_at": now_utc,
                        "completed": completed_list,
                        "failed": failed_list,
                        "pending": pending_list,
                        "in_progress": False,
                    }
                )
            print(
                f"HALTING after failure on {ep}. "
                f"Fix the ep then re-run `tctl vllm rolling-restart --pool {pool_name}` to resume.",
                file=sys.stderr,
            )
            return 1

    if not dry_run:
        sf.delete()
    print(
        f"rolling-restart complete: {len(completed_list)} ep(s) restarted in pool {pool_name!r}",
        file=sys.stderr,
    )
    return 0


# Type alias for Any used in _SessionFile / rolling restart
from typing import Any  # noqa: E402
