"""stop command stub — replaced by Task 14."""

from __future__ import annotations

import argparse
import sys


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    print("stop: not yet implemented", file=sys.stderr)
    return 1
