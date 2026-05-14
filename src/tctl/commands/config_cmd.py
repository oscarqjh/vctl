"""`tctl config <verb>` — validate / show / schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from tctl.config.settings import (
    cluster_json_schema,
    load_cluster_file,
    load_profile_file,
    profile_json_schema,
)
from tctl.config.yaml_source import load_yaml
from tctl.resolver import resolve

_CONFIG_VERB_HELP: dict[str, str] = {
    "validate": "Validate a cluster.yaml or models/<name>.yaml against the schema",
    "show": "Print the fully-resolved config (with env overrides applied)",
    "schema": "Emit the JSON Schema for ClusterFile and ProfileFile",
}


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tctl config",
        description=(
            "Inspect and manage tctl configuration files.\n"
            "\n"
            "Verbs:\n"
            "  validate  — check a YAML file against the schema\n"
            "  show      — print the resolved runtime config (env overrides applied)\n"
            "  schema    — dump the JSON Schema for cluster + profile documents\n"
        ),
    )
    sp = p.add_subparsers(dest="verb", required=True)
    sp.add_parser("schema", help=_CONFIG_VERB_HELP["schema"])
    sp.add_parser("show", help=_CONFIG_VERB_HELP["show"])
    val = sp.add_parser("validate", help=_CONFIG_VERB_HELP["validate"])
    val.add_argument("path")
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    sub = _build_subparser().parse_args(argv_rest)
    if sub.verb == "schema":
        return _do_schema()
    if sub.verb == "validate":
        return _do_validate(sub.path)
    if sub.verb == "show":
        return _do_show(ns)
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
        if raw.get("kind") == "Profile" or ("model" in raw and "haproxy" not in raw):
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
        "apiVersion": "tctl/v1",
        "profile": rc.profile_name,
        "cluster": rc.cluster.model_dump(),
        "haproxy": rc.lb.model_dump(),
        "model": rc.model.model_dump(),
        "resources": rc.resources.model_dump(),
        "parallelism": rc.parallelism.model_dump(),
        "server": rc.server.model_dump(),
        "vllm_args": rc.vllm_args,
        "env": rc.env,
    }
    print(yaml.safe_dump(payload, sort_keys=False))
    return 0
