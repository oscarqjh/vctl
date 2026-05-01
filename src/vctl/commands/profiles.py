"""`vctl profiles` — list models/*.yaml or set the active profile."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
from pathlib import Path

from vctl.config.settings import load_cluster_file


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl profiles",
        description=(
            "List available model profiles (models/*.yaml). "
            "Marks the active profile from cluster.yaml with '*'. "
            "Use `set <name>` to switch."
        ),
    )
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.add_parser("list", help="list available profiles (default)")
    s = sub.add_parser("set", help="change cluster.profile in cluster.yaml")
    s.add_argument("name", help="profile name (must match a models/<name>.yaml)")
    return p


def _list_profiles(cluster_path: Path) -> int:
    cf = load_cluster_file(cluster_path)
    models_dir = cluster_path.parent / "models"
    if not models_dir.exists():
        print(f"models/ directory not found next to {cluster_path}", flush=True)
        return 2
    for yml in sorted(models_dir.glob("*.yaml")):
        marker = "*" if yml.stem == cf.profile else " "
        print(f"{marker} {yml.stem}")
    return 0


_BLOCK_SCALAR_RE = re.compile(r"^profile:[ \t]*[|>]", re.MULTILINE)


def _set_profile(cluster_path: Path, name: str) -> int:
    models_dir = cluster_path.parent / "models"
    target = models_dir / f"{name}.yaml"
    if not target.is_file():
        print(f"unknown profile {name!r}: {target} not found", file=sys.stderr)
        available = sorted(p.stem for p in models_dir.glob("*.yaml")) if models_dir.exists() else []
        if available:
            print(f"available: {', '.join(available)}", file=sys.stderr)
        return 3

    # C3: explicit UTF-8 read.
    text = cluster_path.read_text(encoding="utf-8")

    pattern = re.compile(r"^profile:[ \t]*.*$", re.MULTILINE)

    # C2: reject block-scalar header (profile: | or profile: >).
    if _BLOCK_SCALAR_RE.search(text):
        print(
            "block-scalar `profile:` not supported; edit cluster.yaml manually",
            file=sys.stderr,
        )
        return 3

    # C2: reject multiple top-level `profile:` lines.
    matches = pattern.findall(text)
    if len(matches) > 1:
        print(
            f"found {len(matches)} top-level `profile:` keys in {cluster_path}; "
            "edit cluster.yaml manually",
            file=sys.stderr,
        )
        return 3

    if not matches:
        # C8: user error (malformed cluster.yaml) → exit 3
        print(f"no top-level `profile:` key found in {cluster_path}", file=sys.stderr)
        return 3

    new_text = pattern.sub(f"profile: {name}", text, count=1)

    # C3: atomic write — write to a sibling tmp file then os.replace.
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cluster_path.parent,
            delete=False,
            mode="w",
            encoding="utf-8",
            suffix=".tmp",
        ) as fh:
            tmp_path = fh.name
            fh.write(new_text)
        os.replace(tmp_path, cluster_path)
        tmp_path = None  # replaced successfully; no cleanup needed
    except Exception:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise

    cf = load_cluster_file(cluster_path)
    if cf.profile != name:
        print(
            f"wrote {cluster_path} but reload shows profile={cf.profile!r}, expected {name!r}",
            file=sys.stderr,
        )
        return 1
    print(f"active profile: {name}")
    return 0


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    cluster_path = Path(ns.config).resolve()
    verb = parsed.verb or "list"
    if verb == "list":
        return _list_profiles(cluster_path)
    if verb == "set":
        return _set_profile(cluster_path, parsed.name)
    print(f"unknown verb: {verb}", file=sys.stderr)
    return 2
