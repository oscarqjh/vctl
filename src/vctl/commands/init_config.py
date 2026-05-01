"""`vctl init-config` — scaffold cluster.yaml + models/*.yaml from canonical templates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vctl.commands.templates import CLUSTER_TEMPLATE, PROFILE_TEMPLATES

_DEFAULT_TARGET_DIR = str(Path.home() / ".vctl")


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl init-config")
    p.add_argument(
        "--dir",
        default=_DEFAULT_TARGET_DIR,
        help=f"target directory (default: {_DEFAULT_TARGET_DIR})",
    )
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument(
        "--profiles",
        default="qwen3_5-9b,qwen3-vl-30b-a3b",
        help="comma-separated profile names to scaffold",
    )
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    target_dir = Path(parsed.dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    requested = [p.strip() for p in parsed.profiles.split(",") if p.strip()]
    unknown = [p for p in requested if p not in PROFILE_TEMPLATES]
    if unknown:
        print(f"unknown profile(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(sorted(PROFILE_TEMPLATES))}", file=sys.stderr)
        return 3

    created: list[Path] = []
    cluster_yaml = target_dir / "cluster.yaml"
    if cluster_yaml.exists() and not parsed.force:
        print(f"refusing to overwrite {cluster_yaml} (pass --force)", file=sys.stderr)
        return 2
    cluster_yaml.write_text(CLUSTER_TEMPLATE)
    created.append(cluster_yaml)

    models_dir = target_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for name in requested:
        path = models_dir / f"{name}.yaml"
        if path.exists() and not parsed.force:
            print(f"refusing to overwrite {path} (pass --force)", file=sys.stderr)
            return 2
        path.write_text(PROFILE_TEMPLATES[name])
        created.append(path)

    for p in created:
        print(p)
    return 0
