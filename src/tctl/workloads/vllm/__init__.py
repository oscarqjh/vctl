"""tctl vllm workload — vLLM inference worker management."""

from __future__ import annotations

import argparse

from tctl.workloads.vllm.commands import run as _run


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Workload dispatch entry."""
    return _run(ns, argv_rest)
