"""Argparse root with lazy command imports for fast startup."""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from collections.abc import Callable

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
}

_PROFILE_AWARE = {"info", "serve", "args", "preflight", "stop"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl", description="Typed CLI for vLLM fleet.")
    p.add_argument("-V", "--version", action="version", version=f"vctl {__version__}")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.add_argument("--log-format", default="pretty", choices=["pretty", "json"])
    p.add_argument("--config", default="cluster.yaml")
    p.add_argument("--profile", default=None)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name in _COMMANDS:
        sp = sub.add_parser(name, help=f"see `vctl {name} --help`")
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

    try:
        return _dispatch(ns.command, rest, ns)
    except FileNotFoundError as e:
        _LOG.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 130
