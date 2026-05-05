"""
Smoke spike: exercise the real ssh layer of `_restart_one_ep` against a real
worker pod, using `/bin/true` as the remote command so no vllm work happens.

Goal: prove that
  - the ssh argv assembled by _restart_one_ep is accepted by /usr/bin/ssh
  - BatchMode + ConnectTimeout=5 + StrictHostKeyChecking=accept-new behave
  - the cross-host wire returns rc=0 within wall-clock expectations

What this skips: HAProxy verification (stubbed to True). Run with `-v` for full
ssh output.

Usage:
  .venv/bin/python scripts/spike_rolling_restart_ssh.py <worker_ip> [--user U] [-v]

Example:
  .venv/bin/python scripts/spike_rolling_restart_ssh.py 10.119.30.182
  .venv/bin/python scripts/spike_rolling_restart_ssh.py 10.119.30.182 --user qianjianheng -v
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import vctl.commands.rolling_restart as rr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("worker_ip", help="IP of a real worker pod reachable via ssh")
    ap.add_argument("--user", default="", help="ssh user (default: current user)")
    ap.add_argument("--port", default=8000, type=int, help="ep port (any value; not dialed)")
    ap.add_argument("-v", "--verbose", action="store_true", help="dump full subprocess output")
    args = ap.parse_args()

    ep = f"{args.worker_ip}:{args.port}"

    # Stub _verify_ep_up so we isolate the ssh layer.
    rr._verify_ep_up = lambda *a, **kw: True  # type: ignore[assignment]

    # Stub LbManager (only consumed by _verify_ep_up which is now a no-op).
    mgr = MagicMock()

    # Trace subprocess to capture the actual argv that would have been launched.
    real_run = rr.subprocess.run
    captured_argv: list[list[str]] = []

    def traced_run(argv: list[str], **kwargs: object) -> object:
        captured_argv.append(argv)
        if args.verbose:
            print(f"[spike] subprocess.run argv = {argv}", file=sys.stderr)
        return real_run(argv, **kwargs)

    rr.subprocess.run = traced_run  # type: ignore[assignment]

    print(f"[spike] target ep={ep} user={args.user or '(current)'}", file=sys.stderr)
    print("[spike] remote command will be: /bin/true (no-op)", file=sys.stderr)
    t0 = time.monotonic()

    result = rr._restart_one_ep(
        ep=ep,
        idx=1,
        total=1,
        pool_name="spike",
        mgr=mgr,
        ssh_user=args.user,
        vllm_timeout=15,
        ready_timeout=5,
        dry_run=False,
        quiet=False,
        remote_vctl_path="/bin/true",
    )

    elapsed = time.monotonic() - t0
    print(f"[spike] result = {result!r}  elapsed={elapsed:.2f}s", file=sys.stderr)
    print(f"[spike] argv built = {captured_argv[0] if captured_argv else 'NONE'}", file=sys.stderr)

    if result != "ok":
        print("[spike] FAILED — ssh wire did not return rc=0. See stderr above.", file=sys.stderr)
        return 1

    print("[spike] OK — ssh wire works end-to-end", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    sys.exit(main())
