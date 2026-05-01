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

_CONFIG_VERB_HELP: dict[str, str] = {
    "validate": "Validate a cluster.yaml or models/<name>.yaml against the schema",
    "show": "Print the fully-resolved config (with env overrides applied)",
    "schema": "Emit the JSON Schema for ClusterFile and ProfileFile",
    "migrate": "Upgrade an old-format config to vctl/v1 (default: dry-run diff only)",
}


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl config",
        description=(
            "Inspect and manage vctl configuration files.\n"
            "\n"
            "Verbs:\n"
            "  validate  — check a YAML file against the schema\n"
            "  show      — print the resolved runtime config (env overrides applied)\n"
            "  schema    — dump the JSON Schema for cluster + profile documents\n"
            "  migrate   — upgrade old-format configs to vctl/v1 (dry-run by default)\n"
        ),
    )
    sp = p.add_subparsers(dest="verb", required=True)
    sp.add_parser("schema", help=_CONFIG_VERB_HELP["schema"])
    sp.add_parser("show", help=_CONFIG_VERB_HELP["show"])
    val = sp.add_parser("validate", help=_CONFIG_VERB_HELP["validate"])
    val.add_argument("path")
    mig = sp.add_parser("migrate", help=_CONFIG_VERB_HELP["migrate"])
    mig.add_argument("path")
    mig.add_argument(
        "--write",
        action="store_true",
        default=False,
        help=(
            "Write the migrated YAML back to <path> "
            "(default: print diff to stdout and exit 0 without writing)"
        ),
    )
    mig.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite an existing <path>.bak backup file",
    )
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
        return _do_migrate(sub.path, write=sub.write, force=sub.force)
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


def _do_migrate(path: str, write: bool = False, force: bool = False) -> int:
    """Migrate *path* to the current schema version.

    Default (``--write`` not given): print a unified diff to stdout and exit 0
    without touching the file.

    With ``--write``: validate the migrated output round-trips through the
    schema, write a ``.bak`` backup beside the file, then overwrite.
    ``--force`` overwrites an existing ``.bak``.
    """
    p = Path(path)
    old_text = p.read_text()
    src = load_yaml(p)
    kind = detect_kind(src)
    new = migrate_cluster(src) if kind == "Cluster" else migrate_profile(src)
    new_text = dump_yaml(new)

    diff = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{p}.old",
            tofile=f"{p}.new",
        )
    )

    if not write:
        # Dry-run: print diff to stdout and exit 0.
        sys.stdout.writelines(diff)
        return 0

    # Validate the migrated text before writing.
    try:
        parsed_new = yaml.safe_load(new_text)
        if kind == "Cluster":
            from vctl.config.models import ClusterFile

            ClusterFile.model_validate(parsed_new)
        else:
            from vctl.config.models import ProfileFile

            ProfileFile.model_validate(parsed_new)
    except Exception as exc:
        print(
            f"migration produced invalid output, refusing to write: {exc}",
            file=sys.stderr,
        )
        return 1

    # Write .bak before clobber.
    bak = p.with_suffix(".yaml.bak")
    if bak.exists() and not force:
        print(
            f"{bak} already exists; pass --force to overwrite or move it first",
            file=sys.stderr,
        )
        return 1
    bak.write_text(old_text)

    p.write_text(new_text)
    return 0
