"""`vctl config <verb>` — validate / show / schema / migrate."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

import yaml

from vctl.config.migrate import detect_kind, dump_yaml, migrate_cluster, migrate_profile
from vctl.config.settings import (
    cluster_json_schema,
    load_cluster_file,
    load_profile_file,
    profile_json_schema,
)
from vctl.config.yaml_source import load_yaml
from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl config")
    sp = p.add_subparsers(dest="verb", required=True)
    sp.add_parser("schema")
    sp.add_parser("show")
    val = sp.add_parser("validate")
    val.add_argument("path")
    mig = sp.add_parser("migrate")
    mig.add_argument("path")
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    sub = _build_subparser().parse_args(argv_rest)
    if sub.verb == "schema":
        return _do_schema()
    if sub.verb == "validate":
        return _do_validate(sub.path)
    if sub.verb == "show":
        return _do_show(ns)
    if sub.verb == "migrate":
        return _do_migrate(sub.path)
    return 2


def _do_schema() -> int:
    print(
        json.dumps(
            {
                "ClusterFile": cluster_json_schema(),
                "ProfileFile": profile_json_schema(),
            },
            indent=2,
        )
    )
    return 0


def _do_validate(path: str) -> int:
    p = Path(path)
    raw = load_yaml(p)
    try:
        if raw.get("kind") == "Profile" or ("model" in raw and "lb" not in raw):
            load_profile_file(p)
        else:
            load_cluster_file(p)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _do_show(ns: argparse.Namespace) -> int:
    rc = resolve(ns.config, profile=ns.profile)
    payload = {
        "apiVersion": "vctl/v1",
        "profile": rc.profile_name,
        "cluster": rc.cluster.model_dump(),
        "lb": rc.lb.model_dump(),
        "model": rc.model.model_dump(),
        "resources": rc.resources.model_dump(),
        "parallelism": rc.parallelism.model_dump(),
        "server": rc.server.model_dump(),
        "vllm_args": rc.vllm_args,
        "env": rc.env,
    }
    print(yaml.safe_dump(payload, sort_keys=False))
    return 0


def _do_migrate(path: str) -> int:
    p = Path(path)
    src = load_yaml(p)
    kind = detect_kind(src)
    new = migrate_cluster(src) if kind == "Cluster" else migrate_profile(src)
    new_text = dump_yaml(new)
    old_text = p.read_text()
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{p}.old",
        tofile=f"{p}.new",
    )
    sys.stderr.writelines(diff)
    p.write_text(new_text)
    return 0
