"""Hidden helper commands for the lmms-eval workspace."""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from vctl.platform import tmux_kill, tmux_run_detached, tmux_session_exists

_TMUX_NAME = "vctl-lmmseval"

_LMMS_ROOT = "/mnt/umm/users/qianjianheng/workspace/lmms-eval"
_VENV = f"{_LMMS_ROOT}/.venv_novllm"
_RUN_LOOP_SH = f"{_LMMS_ROOT}/scripts/run_loop.sh"
_TASK_SH = f"{_LMMS_ROOT}/scripts/osibench_32frame/qwen3vl8binstruct.sh"
_START_IDX = 0
_END_IDX = 5

# Env var prefixes propagated from current shell into the tmux session.
# tmux server caches env from when first started, so vars set in the current
# shell (e.g. HF_HOME) are missing inside `tmux new-session`. Mirror the
# Phase 1 vllm fix (PATH override) — explicitly export at cmd entry.
_ENV_PROPAGATE_PREFIXES = (
    "HF_",
    "HUGGINGFACE_",
    "HUGGING_FACE_",
    "TRANSFORMERS_",
    "ACCELERATE_",
    "TORCH_",
    "CUDA_",
    "NCCL_",
    "OMP_",
)


def _build_env_exports() -> str:
    """Return shell `export K=V; ...` lines for every env var matching
    `_ENV_PROPAGATE_PREFIXES`, with values shlex-quoted."""
    pairs = [
        (k, v)
        for k, v in os.environ.items()
        if any(k.startswith(p) for p in _ENV_PROPAGATE_PREFIXES)
    ]
    pairs.sort()
    return " ".join(f"export {k}={shlex.quote(v)};" for k, v in pairs)


def _build_run_loop_cmd() -> str:
    exports = _build_env_exports()
    body = f"source {_VENV}/bin/activate && bash {_RUN_LOOP_SH} {_TASK_SH} {_START_IDX} {_END_IDX}"
    return f"{exports} {body}" if exports else body


def _cmd_run_loop(_ns: argparse.Namespace) -> int:
    if tmux_session_exists(_TMUX_NAME):
        print(
            f"tmux session {_TMUX_NAME!r} already exists. "
            f"attach: tmux attach -t {_TMUX_NAME}  |  kill: vctl lmmseval stop",
            file=sys.stderr,
        )
        return 4
    cmd = _build_run_loop_cmd()
    tmux_run_detached(_TMUX_NAME, cmd)
    print(f"started in tmux session {_TMUX_NAME!r}", file=sys.stderr)
    print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    print(f"  cmd:    {cmd}", file=sys.stderr)
    return 0


def _cmd_stop(_ns: argparse.Namespace) -> int:
    if not tmux_session_exists(_TMUX_NAME):
        print(f"tmux session {_TMUX_NAME!r} not running", file=sys.stderr)
        return 0
    tmux_kill(_TMUX_NAME)
    print(f"killed tmux session {_TMUX_NAME!r}", file=sys.stderr)
    return 0


def _cmd_status(_ns: argparse.Namespace) -> int:
    if tmux_session_exists(_TMUX_NAME):
        print(f"tmux session {_TMUX_NAME!r}: running", file=sys.stderr)
        print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    else:
        print(f"tmux session {_TMUX_NAME!r}: not running", file=sys.stderr)
    return 0


def run(_ns: argparse.Namespace, argv_rest: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="vctl lmmseval",
        description="Hidden helper commands for the lmms-eval workspace.",
    )
    sub = p.add_subparsers(dest="verb", required=True, metavar="VERB")

    sp_run = sub.add_parser(
        "run-loop",
        help=f"start {_RUN_LOOP_SH} in detached tmux session (with venv activated)",
    )
    sp_run.set_defaults(_handler=_cmd_run_loop)

    sp_stop = sub.add_parser("stop", help=f"kill the {_TMUX_NAME!r} tmux session")
    sp_stop.set_defaults(_handler=_cmd_stop)

    sp_status = sub.add_parser("status", help=f"show whether {_TMUX_NAME!r} is running")
    sp_status.set_defaults(_handler=_cmd_status)

    parsed = p.parse_args(argv_rest)
    handler = parsed._handler
    return int(handler(parsed))
