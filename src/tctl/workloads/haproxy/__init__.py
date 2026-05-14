"""tctl haproxy workload — HAProxy load-balancer lifecycle management."""

from __future__ import annotations

import argparse

from tctl.workloads.haproxy.commands import run as _run


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Workload dispatch entry. Called by tctl.cli when token1 == 'haproxy'."""
    return _run(ns, argv_rest)
