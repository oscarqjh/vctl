"""tctl lmms workload — lmms-eval job management (hidden from top-level --help)."""

from __future__ import annotations

import argparse

_VERBS: dict[str, str] = {
    "run-loop": "_cmd_run_loop",
    "stop": "_cmd_stop",
    "status": "_cmd_status",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl lmms <verb>`."""
    from tctl.workloads.lmms import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl lmms")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    fn_name = _VERBS[parsed.verb]
    return getattr(_cmds, fn_name)(parsed, [])  # type: ignore[no-any-return]
