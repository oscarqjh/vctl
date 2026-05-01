"""Argparse root with lazy command imports for fast startup."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from vctl import __version__

_LOG = logging.getLogger(__name__)

_COMMANDS: dict[str, str] = {
    "info": "vctl.commands.info",
    "profiles": "vctl.commands.profiles",
    "args": "vctl.commands.args_cmd",
    "preflight": "vctl.commands.preflight",
    "serve": "vctl.commands.serve",
    "stop": "vctl.commands.stop",
    "lb": "vctl.commands.lb",
    "config": "vctl.commands.config_cmd",
    "init-config": "vctl.commands.init_config",
}

_PROFILE_AWARE = {"info", "serve", "args", "preflight", "stop"}

# Sentinel: distinguishes "user supplied --config" from "no --config given".
_CONFIG_SENTINEL = "<auto>"

# Default home location for `uv tool install`-ed users.
_CONFIG_DEFAULT_HOME = Path.home() / "vctl-cfg" / "cluster.yaml"


def _resolve_config_path(cli_arg: str | None) -> str:
    """Pick which cluster.yaml to load.

    Order:
      1. --config flag
      2. CLUSTER_CONFIG env var
      3. ./cluster.yaml (cwd, back-compat)
      4. ~/vctl-cfg/cluster.yaml (default for `uv tool install`-ed users)
    Returns the chosen path even if it doesn't exist; let the loader raise
    a clear FileNotFoundError naming candidates that were tried.
    """
    if cli_arg:
        return cli_arg
    env = os.environ.get("CLUSTER_CONFIG")
    if env:
        return env
    cwd_cfg = Path.cwd() / "cluster.yaml"
    if cwd_cfg.is_file():
        return str(cwd_cfg)
    return str(_CONFIG_DEFAULT_HOME)


def _config_path_error_message() -> str:
    return (
        "could not find cluster.yaml. Searched (in order):\n"
        f"  --config flag (not provided)\n"
        f"  CLUSTER_CONFIG env (={os.environ.get('CLUSTER_CONFIG', '<unset>')})\n"
        f"  {Path.cwd() / 'cluster.yaml'}\n"
        f"  {_CONFIG_DEFAULT_HOME}\n"
        f"\n"
        f"Run `vctl init-config --dir ~/vctl-cfg` to bootstrap one."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl", description="Typed CLI for vLLM fleet.")
    p.add_argument("-V", "--version", action="version", version=f"vctl {__version__}")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.add_argument("--log-format", default="pretty", choices=["pretty", "json"])
    p.add_argument(
        "--config",
        default=_CONFIG_SENTINEL,
        help=(
            "path to cluster.yaml "
            "(default: $CLUSTER_CONFIG, ./cluster.yaml, or ~/vctl-cfg/cluster.yaml)"
        ),
    )
    p.add_argument("--profile", default=None)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name in _COMMANDS:
        # add_help=False so `vctl <cmd> --help` passes --help through to the
        # command module's own subparser (which knows its real verbs/flags).
        # Otherwise argparse prints an empty shallow help here and never
        # reaches the actual command's argparse.
        sp = sub.add_parser(
            name, help=f"see `vctl {name} --help`", add_help=False
        )
        sp.set_defaults(_subname=name)
    return p


def _hoist_positional_profile(argv: list[str]) -> list[str]:
    """If first non-flag arg after a profile-aware subcommand is `models/<x>.yaml`,
    rewrite it to `--profile <x>`."""
    out = list(argv)
    i = 0
    while i < len(out) and out[i].startswith("-"):
        if out[i] in ("--log-level", "--log-format", "--config", "--profile"):
            i += 2
            continue
        i += 1
    if i >= len(out) or out[i] not in _PROFILE_AWARE:
        return out
    sub_idx = i
    j = sub_idx + 1
    while j < len(out) and out[j].startswith("-"):
        j += 1
    if j < len(out):
        token = out[j]
        if token.startswith("models/") and token.endswith(".yaml"):
            stem = token[len("models/") : -len(".yaml")]
            return out[: sub_idx + 1] + ["--profile", stem] + out[sub_idx + 1 : j] + out[j + 1 :]
    return out


def _dispatch(name: str, argv_rest: list[str], ns: argparse.Namespace) -> int:
    mod = importlib.import_module(_COMMANDS[name])
    handler: Callable[[argparse.Namespace, list[str]], int] = mod.run
    return handler(ns, argv_rest)


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    raw = _hoist_positional_profile(raw)

    parser = build_parser()
    ns, rest = parser.parse_known_args(raw)

    from vctl.logging import configure as _configure_logging

    _configure_logging(level=ns.log_level, fmt=ns.log_format)

    # Resolve default config path before dispatch so commands can rely on
    # ns.config being a usable string.
    ns.config = _resolve_config_path(None if ns.config == _CONFIG_SENTINEL else ns.config)

    try:
        return _dispatch(ns.command, rest, ns)
    except FileNotFoundError as e:
        # If the missing file is the cluster.yaml we just resolved, augment
        # the error message with the search list.
        msg = str(e)
        if "cluster.yaml" in msg:
            _LOG.error("%s\n\n%s", msg, _config_path_error_message())
        else:
            _LOG.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130
