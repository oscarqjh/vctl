"""Scaling verbs stub — replaced by Task 17."""

from __future__ import annotations

import argparse
import sys

from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState


def dispatch(
    verb: str, parsed: argparse.Namespace, ns: argparse.Namespace, mgr: LbManager, bs: BackendState
) -> int:
    print(f"lb {verb}: not yet implemented (Task 17)", file=sys.stderr)
    return 1
