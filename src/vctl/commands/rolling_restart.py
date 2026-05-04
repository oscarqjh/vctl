"""``vctl rolling-restart`` — sequential per-pool endpoint restart."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import subprocess  # noqa: F401  (module-level for monkeypatching in later tasks)
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vctl.commands.lb import _fetch_haproxy_stats
from vctl.lb.runtime import lb_admin_client
from vctl.lb.state import BackendState

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager

_SESSION_DIR: Path = Path.home() / ".vctl" / "lb" / "rolling-restart"


def _session_path(pool: str) -> Path:
    """Return the JSON session file path for *pool*."""
    return _SESSION_DIR / f"{pool}.json"


class _SessionFile:
    """Atomic, fcntl.flock-protected session file for a single pool.

    Mirrors BackendState._locked() from vctl.lb.state — holds an exclusive
    flock on a sibling <pool>.lock file for every read and write so two
    concurrent invocations for the same pool never race.
    """

    def __init__(self, pool: str) -> None:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._path: Path = _SESSION_DIR / f"{pool}.json"
        self._lock_path: Path = _SESSION_DIR / f"{pool}.lock"

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> dict[str, Any] | None:
        """Return parsed JSON dict, or None if file absent.

        Raises ValueError on JSON decode error (corrupted file).
        """
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
        """Atomically write *data* as JSON (via .tmp + os.replace)."""
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                tmp_path = Path(str(self._path) + ".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp_path, self._path)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def delete(self) -> None:
        """Remove the session file if present; no-op if absent."""
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()


def _verify_ep_up(
    ep: str,
    pool_name: str,
    mgr: LbManager,
    timeout_s: int,
) -> bool:
    """Poll HAProxy stats until *ep* in pool_<pool_name> reports status starting 'UP'.

    Opens a fresh lb_admin_client per iteration (HAProxy admin socket closes after
    each response — see CLAUDE.md gotcha). Returns True on first UP hit; False if
    deadline expires without seeing UP. A None client (LB unreachable) is treated as
    non-fatal: we sleep and retry until the deadline.
    """
    deadline = time.monotonic() + timeout_s
    pool_section = f"pool_{pool_name}"
    while time.monotonic() < deadline:
        cli = lb_admin_client(mgr)
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
    pool_name: str,
    mgr: LbManager,
    ssh_user: str,
    vllm_timeout: int,
    ready_timeout: int,
    dry_run: bool,
    quiet: bool,
    remote_vctl_path: str | None,
) -> Literal["ok", "failed"]:
    """Restart a single endpoint via ssh and verify it returns UP in HAProxy.

    Returns "ok" on success, "failed" on ssh error, ssh timeout, or health-check
    timeout. All progress + error output goes to stderr (stdout stays clean).
    """
    ep_host = ep.split(":")[0]
    prefix = f"[{idx}/{total}] {ep}"

    if not quiet:
        print(f"{prefix}  draining → restarting...", file=sys.stderr)

    if dry_run:
        print(f"{prefix}  would restart", file=sys.stderr)
        return "ok"

    # Build ssh target and remote command.
    ssh_target = f"{ssh_user}@{ep_host}" if ssh_user else ep_host
    if remote_vctl_path:
        remote_cmd = f"{remote_vctl_path} serve restart"
    else:
        remote_cmd = "bash -lc 'vctl serve restart'"

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
        result = subprocess.run(
            argv,
            timeout=vllm_timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(
            f"{prefix}  timed out after {vllm_timeout}s. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    if result.returncode != 0:
        stderr_snippet = result.stderr.strip()[:200]
        print(
            f"{prefix}  ssh failed (rc={result.returncode}): {stderr_snippet}. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    if not quiet:
        print(f"{prefix}  waiting for UP...", file=sys.stderr)

    t0 = time.monotonic()
    if not _verify_ep_up(ep, pool_name, mgr, timeout_s=ready_timeout):
        print(
            f"{prefix}  did not become UP within {ready_timeout}s. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    elapsed = int(time.monotonic() - t0)
    if not quiet:
        print(f"{prefix}  ready ({elapsed}s)", file=sys.stderr)
    return "ok"


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl rolling-restart",
        description=(
            "Sequential, halt-on-failure rolling restart of every endpoint in a pool.\n"
            "ssh-es to each worker, runs `vctl serve restart`, waits until HAProxy "
            "reports UP before moving to the next.\n"
            "\n"
            "State is persisted to ~/.vctl/lb/rolling-restart/<pool>.json so an "
            "interrupted run can be auto-resumed by re-running the same command."
        ),
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
        help="Seconds vctl serve restart is allowed to take on the remote (default: 600).",
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
        "--remote-vctl-path",
        default=None,
        dest="remote_vctl_path",
        metavar="PATH",
        help=(
            "Override remote vctl path (default: bash -lc 'vctl serve restart'). "
            "Use for non-standard installs e.g. /opt/vctl/bin/vctl."
        ),
    )
    return p


def _run_fresh(parsed: argparse.Namespace, mgr: LbManager) -> int:
    """Execute a fresh rolling restart from all eps in the pool."""
    pool_name: str = parsed.pool
    dry_run: bool = getattr(parsed, "dry_run", False)
    quiet: bool = getattr(parsed, "quiet", False)
    ssh_user: str = getattr(parsed, "ssh_user", "")
    vllm_timeout: int = getattr(parsed, "vllm_timeout", 600)
    ready_timeout: int = getattr(parsed, "ready_timeout", 60)
    remote_vctl_path: str | None = getattr(parsed, "remote_vctl_path", None)

    # Validate pool exists in config.
    configured = {p.name for p in mgr.lb.pools}
    if pool_name not in configured:
        available = ", ".join(sorted(configured))
        print(
            f"unknown pool: {pool_name!r}; available: {available}",
            file=sys.stderr,
        )
        return 3

    sf = _SessionFile(pool_name)

    # Concurrency guard: refuse if another invocation is already in_progress.
    # Checked before backend enumeration so the guard fires even with empty backends.
    if sf.exists() and not dry_run:
        try:
            data = sf.read()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if data is not None and data.get("in_progress"):
            print(
                f"rolling-restart already in progress for pool {pool_name!r} "
                "— kill the other invocation or use --abort",
                file=sys.stderr,
            )
            return 4

    # Enumerate endpoints from state file.
    pbs = BackendState(mgr.state_dir, mgr.lb.host, pool=pool_name)
    eps = sorted(pbs.list())
    if not eps:
        print(
            f"pool {pool_name!r} has no registered backends; nothing to restart",
            file=sys.stderr,
        )
        return 0

    # Build initial session.
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

    completed: list[str] = []
    failed: list[str] = []
    pending = list(eps)

    for idx, ep in enumerate(eps, start=1):
        outcome = _restart_one_ep(
            ep=ep,
            idx=idx,
            total=len(eps),
            pool_name=pool_name,
            mgr=mgr,
            ssh_user=ssh_user,
            vllm_timeout=vllm_timeout,
            ready_timeout=ready_timeout,
            dry_run=dry_run,
            quiet=quiet,
            remote_vctl_path=remote_vctl_path,
        )
        pending = [e for e in pending if e != ep]
        if outcome == "ok":
            completed.append(ep)
            if not dry_run:
                sf.write(
                    {
                        "pool": pool_name,
                        "started_at": now_utc,
                        "completed": completed,
                        "failed": [],
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
                        "started_at": now_utc,
                        "completed": completed,
                        "failed": failed,
                        "pending": pending,
                        "in_progress": False,
                    }
                )
            print(
                f"HALTING after failure on {ep}. "
                f"Fix the ep then re-run `vctl rolling-restart --pool {pool_name}` to resume.",
                file=sys.stderr,
            )
            return 1

    # Full success.
    if not dry_run:
        sf.delete()
    print(
        f"rolling-restart complete: {len(completed)} ep(s) restarted in pool {pool_name!r}",
        file=sys.stderr,
    )
    return 0


def _run_resume(
    parsed: argparse.Namespace,
    mgr: LbManager,
    sf: _SessionFile,
    session: dict[str, Any],
) -> int:
    """Resume an interrupted rolling restart from a persisted session file.

    For each ep in `failed`:
      - Quick HAProxy probe (5s window): if UP → mark completed, log, continue.
      - Still DOWN → prompt operator: (a) skip, (b) retry, (c) abort.
    Then continue sequentially with `pending` eps via the same restart loop.
    """
    pool_name: str = str(session.get("pool", parsed.pool))
    started_at: str = str(session.get("started_at", ""))
    dry_run: bool = getattr(parsed, "dry_run", False)
    quiet: bool = getattr(parsed, "quiet", False)
    ssh_user: str = getattr(parsed, "ssh_user", "")
    vllm_timeout: int = getattr(parsed, "vllm_timeout", 600)
    ready_timeout: int = getattr(parsed, "ready_timeout", 60)
    remote_vctl_path: str | None = getattr(parsed, "remote_vctl_path", None)

    completed: list[str] = list(session.get("completed", []))
    failed: list[str] = list(session.get("failed", []))
    pending: list[str] = list(session.get("pending", []))

    total_eps = len(completed) + len(failed) + len(pending)
    print(
        f"resuming rolling-restart for pool {pool_name!r}: "
        f"{len(completed)} completed, {len(failed)} failed, {len(pending)} pending "
        f"({total_eps} total)",
        file=sys.stderr,
    )

    # Mark in_progress=True now that we're resuming.
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

    # Step 1: resolve the failed list.
    to_retry: list[str] = []
    for ep in list(failed):
        is_up = _verify_ep_up(ep, pool_name, mgr, timeout_s=5)
        if is_up:
            print(
                f"verified: {ep} was fixed externally — moving to completed",
                file=sys.stderr,
            )
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
            # DOWN/MAINT/other — prompt unless --dry-run / --quiet.
            if dry_run or quiet:
                # Non-interactive: default to skip.
                print(
                    f"{ep} is still DOWN — skipping (--dry-run/--quiet mode)",
                    file=sys.stderr,
                )
                failed.remove(ep)
                completed.append(ep)
            else:
                print(
                    f"\nep {ep} is still DOWN. Choose:\n"
                    f"  (a) skip — mark as completed and continue\n"
                    f"  (b) retry — re-attempt restart\n"
                    f"  (c) abort — exit now (session file preserved)\n",
                    file=sys.stderr,
                )
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
                    # abort (c or anything else)
                    print(
                        f"Aborted. Session file preserved at {sf._path}.",
                        file=sys.stderr,
                    )
                    return 1

    # Build work queue: retried eps first, then remaining pending.
    work_queue = to_retry + pending
    total = len(completed) + len(work_queue)
    start_idx = len(completed) + 1

    for i, ep in enumerate(work_queue):
        idx = start_idx + i
        outcome = _restart_one_ep(
            ep=ep,
            idx=idx,
            total=total,
            pool_name=pool_name,
            mgr=mgr,
            ssh_user=ssh_user,
            vllm_timeout=vllm_timeout,
            ready_timeout=ready_timeout,
            dry_run=dry_run,
            quiet=quiet,
            remote_vctl_path=remote_vctl_path,
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
                f"Fix the ep then re-run `vctl rolling-restart --pool {pool_name}` to resume.",
                file=sys.stderr,
            )
            return 1

    # Full success.
    if not dry_run:
        sf.delete()
    print(
        f"rolling-restart complete: {len(completed)} ep(s) confirmed in pool {pool_name!r}",
        file=sys.stderr,
    )
    return 0


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point dispatched by cli._dispatch."""
    parsed = _build_subparser().parse_args(argv_rest)

    # Resolve LbManager from cluster config.
    from vctl.lb.manager import LbManager
    from vctl.resolver import resolve

    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)

    pool_name: str = parsed.pool
    sf = _SessionFile(pool_name)

    # --status: print and exit.
    if parsed.status:
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

    # --abort: delete and exit.
    if parsed.abort:
        if sf.exists():
            sf.delete()
            print(f"session file for pool {pool_name!r} deleted.", file=sys.stderr)
        else:
            print(f"no session file for pool {pool_name!r}", file=sys.stderr)
        return 0

    # --fresh: delete existing session before starting.
    if parsed.fresh:
        sf.delete()

    # Dispatch: resume if session present, fresh otherwise.
    try:
        existing = sf.read()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if existing is not None and not parsed.dry_run:
        return _run_resume(parsed, mgr, sf, existing)
    return _run_fresh(parsed, mgr)
