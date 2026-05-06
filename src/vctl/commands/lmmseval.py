"""Hidden helper commands for the lmms-eval workspace."""

from __future__ import annotations

import argparse
import os
import sys

from vctl.tmux import TmuxSession

_TMUX_NAME = "vctl-lmmseval"

_LMMS_ROOT = "/mnt/umm/users/qianjianheng/workspace/lmms-eval"
_VENV = f"{_LMMS_ROOT}/.venv_novllm"
_RUN_LOOP_SH = f"{_LMMS_ROOT}/scripts/run_loop.sh"
_TASK_SH = f"{_LMMS_ROOT}/scripts/osibench_32frame/qwen3vl8binstruct.sh"
_START_IDX = 0
_END_IDX = 5

# Pods have no internet egress to huggingface.co. Force offline mode so
# transformers / huggingface_hub never HEAD the network.
# Note: full os.environ snapshot via TmuxSession means HF_HOME, CUDA_* etc.
# from the operator's shell are propagated automatically — no more prefix
# whitelist needed.
_FORCED_ENV: dict[str, str] = {
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
}


def _build_run_loop_cmd() -> str:
    """Return the shell command line to run in the tmux session.

    All env vars are propagated via TmuxSession's -e flags; this function
    only builds the shell pipeline body (source activate + bash run_loop.sh).
    """
    return f"source {_VENV}/bin/activate && bash {_RUN_LOOP_SH} {_TASK_SH} {_START_IDX} {_END_IDX}"


def _cmd_run_loop(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
        print(
            f"tmux session {_TMUX_NAME!r} already exists. "
            f"attach: tmux attach -t {_TMUX_NAME}  |  kill: vctl lmmseval stop",
            file=sys.stderr,
        )
        return 4
    # Full os.environ snapshot + forced offline vars ensures HF_HOME,
    # TRANSFORMERS_OFFLINE, CUDA_*, etc. are all available in the pane
    # regardless of how old the tmux server's environment cache is (AT-3).
    session_env = {**os.environ, **_FORCED_ENV}
    cmd = _build_run_loop_cmd()
    TmuxSession(_TMUX_NAME, env=session_env).start(cmd)
    print(f"started in tmux session {_TMUX_NAME!r}", file=sys.stderr)
    print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    print(f"  cmd:    {cmd}", file=sys.stderr)
    return 0


def _cmd_stop(_ns: argparse.Namespace) -> int:
    if not TmuxSession(_TMUX_NAME).exists():
        print(f"tmux session {_TMUX_NAME!r} not running", file=sys.stderr)
        return 0
    # tree=True: kill run_loop.sh + accelerate + 8 worker processes.
    TmuxSession(_TMUX_NAME).kill(tree=True)
    print(f"killed tmux session {_TMUX_NAME!r}", file=sys.stderr)
    return 0


def _cmd_status(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
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
