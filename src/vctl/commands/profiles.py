"""`vctl profiles` — list models/*.yaml with default marker."""

from __future__ import annotations

import argparse
from pathlib import Path

from vctl.config.settings import load_cluster_file


def _build_subparser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="vctl profiles",
        description="List available model profiles (models/*.yaml). "
        "Marks the default profile from cluster.yaml with '*'.",
    )


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    _build_subparser().parse_args(argv_rest)
    cluster_path = Path(ns.config).resolve()
    cf = load_cluster_file(cluster_path)
    models_dir = cluster_path.parent / "models"
    if not models_dir.exists():
        print(f"models/ directory not found next to {cluster_path}", flush=True)
        return 2
    for yml in sorted(models_dir.glob("*.yaml")):
        marker = "*" if yml.stem == cf.profile else " "
        print(f"{marker} {yml.stem}")
    return 0
