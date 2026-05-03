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

    requested = [p.strip() for p in parsed.profiles.split(",") if p.strip()]
    unknown = [p for p in requested if p not in PROFILE_TEMPLATES]
    if unknown:
        print(f"unknown profile(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(sorted(PROFILE_TEMPLATES))}", file=sys.stderr)
        return 3

    # C11: pre-flight existence sweep — check ALL targets before writing anything.
    cluster_yaml = target_dir / "cluster.yaml"
    models_dir = target_dir / "models"
    target_paths: list[Path] = [cluster_yaml] + [models_dir / f"{name}.yaml" for name in requested]
    if not parsed.force:
        existing = [p for p in target_paths if p.exists()]
        if existing:
            print("refusing to overwrite existing file(s) (pass --force):", file=sys.stderr)
            for p in existing:
                print(f"  {p}", file=sys.stderr)
            return 2

    # All-clear (or --force): proceed with writes.
    target_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    cluster_yaml.write_text(CLUSTER_TEMPLATE)
    created.append(cluster_yaml)

    for name in requested:
        path = models_dir / f"{name}.yaml"
        path.write_text(PROFILE_TEMPLATES[name])
        created.append(path)

    for p in created:
        print(p)
    return 0
