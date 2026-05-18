"""
Spike: compare network reachability + env between current shell and a fresh
tmux session.

Goal: prove whether `vctl lmmseval run-loop` failures are caused by env-gap
between operator shell and tmux server (cached env from when first started).

Usage:
  .venv/bin/python scripts/spike_tmux_network.py [--target URL]

Defaults probe https://huggingface.co/. Output goes to stderr (live) and to
/tmp/vctl-spike-tmux-net.out (full diff).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_PROBE_NAME = "vctl-spike-tmux-net"
_OUT_TMUX = Path("/tmp/vctl-spike-tmux-net.tmux.out")
_OUT_SHELL = Path("/tmp/vctl-spike-tmux-net.shell.out")
_OUT_DIFF = Path("/tmp/vctl-spike-tmux-net.diff.out")

_LMMS_VENV = "/mnt/umm/users/qianjianheng/workspace/lmms-eval/.venv_novllm"

_FAILING_URL = "https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct/resolve/main/processor_config.json"


def _probe_script(target: str) -> str:
    """Bash one-liner producing a deterministic, env+net-probe report."""
    return rf"""
set +e
{{
  echo '=== whoami ==='
  whoami
  echo
  echo '=== uname -a ==='
  uname -a
  echo
  echo '=== full env (sorted) ==='
  env | sort
  echo
  echo '=== /etc/resolv.conf ==='
  cat /etc/resolv.conf 2>&1 | head -10
  echo
  echo '=== getent hosts huggingface.co ==='
  getent hosts huggingface.co 2>&1
  echo
  echo '=== ip route to huggingface.co (first resolved A record) ==='
  IP4=$(getent ahostsv4 huggingface.co | awk 'NR==1 {{print $1}}')
  echo "resolved-v4=$IP4"
  ip route get "$IP4" 2>&1
  echo
  echo '=== curl -4 HEAD {target} ==='
  curl -4 -v -sI --max-time 10 {target} 2>&1 | head -30
  echo "curl4-exit=$?"
  echo
  echo '=== curl -6 HEAD {target} (IPv6 only — many internal pods reject) ==='
  curl -6 -v -sI --max-time 10 {target} 2>&1 | head -20
  echo "curl6-exit=$?"
  echo
  echo '=== HEAD the actual failing URL ==='
  curl -v -sI --max-time 10 {_FAILING_URL} 2>&1 | head -30
  echo "curl-failing-exit=$?"
  echo
  echo '=== same probe via lmms-eval venv python (httpx) ==='
  source {_LMMS_VENV}/bin/activate 2>&1
  python3 -c "
import httpx, traceback, sys
try:
    r = httpx.head('{_FAILING_URL}', timeout=10, follow_redirects=False)
    print('httpx OK:', r.status_code, dict(r.headers).get('location'))
except Exception:
    traceback.print_exc()
    sys.exit(1)
" 2>&1
  echo "httpx-exit=$?"
}}
"""


def _run_in_tmux(target: str) -> str:
    # kill stale session
    subprocess.run(["tmux", "kill-session", "-t", _PROBE_NAME], check=False)
    # write probe to tempfile so we don't wrestle shell quoting
    probe_path = Path("/tmp/vctl-spike-probe.sh")
    probe_path.write_text(_probe_script(target))
    cmd = f"bash {probe_path} > {_OUT_TMUX} 2>&1; echo SPIKE_DONE >> {_OUT_TMUX}"
    subprocess.run(["tmux", "new-session", "-d", "-s", _PROBE_NAME, cmd], check=True)
    print(f"[spike] tmux session {_PROBE_NAME!r} spawned, waiting...", file=sys.stderr)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _OUT_TMUX.exists() and "SPIKE_DONE" in _OUT_TMUX.read_text():
            break
        time.sleep(1)
    subprocess.run(["tmux", "kill-session", "-t", _PROBE_NAME], check=False)
    return _OUT_TMUX.read_text()


def _run_in_shell(target: str) -> str:
    probe_path = Path("/tmp/vctl-spike-probe.sh")
    probe_path.write_text(_probe_script(target))
    res = subprocess.run(
        ["bash", str(probe_path)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    return res.stdout + ("\n--- stderr ---\n" + res.stderr if res.stderr else "")


def _env_diff(tmux_out: str, shell_out: str) -> str:
    """Extract env block from each report, return shell-only and tmux-only keys."""

    def env_block(text: str) -> dict[str, str]:
        in_block = False
        env: dict[str, str] = {}
        for line in text.splitlines():
            if line.startswith("=== full env"):
                in_block = True
                continue
            if line.startswith("==="):
                in_block = False
                continue
            if in_block and "=" in line:
                k, _, v = line.partition("=")
                env[k] = v
        return env

    e_tmux = env_block(tmux_out)
    e_shell = env_block(shell_out)
    only_shell = sorted(set(e_shell) - set(e_tmux))
    only_tmux = sorted(set(e_tmux) - set(e_shell))
    differing = sorted(k for k in set(e_shell) & set(e_tmux) if e_shell[k] != e_tmux[k])
    lines = ["=== env keys ONLY in shell (missing in tmux) ==="]
    for k in only_shell:
        lines.append(f"  {k}={e_shell[k]}")
    lines.append("")
    lines.append("=== env keys ONLY in tmux (extra in tmux) ===")
    for k in only_tmux:
        lines.append(f"  {k}={e_tmux[k]}")
    lines.append("")
    lines.append("=== env keys with DIFFERING values ===")
    for k in differing:
        lines.append(f"  {k}:")
        lines.append(f"    shell={e_shell[k]}")
        lines.append(f"    tmux= {e_tmux[k]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default="https://huggingface.co/", help="URL to HEAD")
    args = ap.parse_args()

    print(f"[spike] target={args.target}", file=sys.stderr)
    print("[spike] running probe in tmux...", file=sys.stderr)
    tmux_out = _run_in_tmux(args.target)
    print("[spike] running probe in current shell...", file=sys.stderr)
    shell_out = _run_in_shell(args.target)
    _OUT_SHELL.write_text(shell_out)

    diff = _env_diff(tmux_out, shell_out)
    _OUT_DIFF.write_text(diff)

    print("---", file=sys.stderr)
    print(f"[spike] tmux  output: {_OUT_TMUX}", file=sys.stderr)
    print(f"[spike] shell output: {_OUT_SHELL}", file=sys.stderr)
    print(f"[spike] env diff:     {_OUT_DIFF}", file=sys.stderr)
    print("---", file=sys.stderr)
    print(diff)

    # quick verdict on net status
    print("---", file=sys.stderr)
    for label, out in [("tmux", tmux_out), ("shell", shell_out)]:
        for line in out.splitlines():
            if line.startswith("curl-exit="):
                print(f"[spike] {label} curl-exit: {line.split('=')[1]}", file=sys.stderr)
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
