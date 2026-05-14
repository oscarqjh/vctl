"""tctl CLI — two-level dispatch: workloads + platform commands."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from tctl import __version__

_LOG = logging.getLogger(__name__)

# Workload sub-trees: name → (module_path, hidden_from_help)
_WORKLOADS: dict[str, tuple[str, bool]] = {
    "vllm": ("tctl.workloads.vllm", False),
    "haproxy": ("tctl.workloads.haproxy", False),
    "lmms": ("tctl.workloads.lmms", True),
}

# Platform commands (no workload namespace)
_PLATFORM_COMMANDS: dict[str, str] = {
    "config": "tctl.commands.config_cmd",
    "init-config": "tctl.commands.init_config",
}

# Profile-aware verbs per workload: positional models/*.yaml → --profile
_PROFILE_AWARE: dict[str, set[str]] = {
    "vllm": {"info", "args", "preflight", "serve", "stop"},
    # haproxy / lmms have no profile-aware verbs
}

_TCTL_HOME = Path.home() / ".tctl"
_CONFIG_DEFAULT_HOME = _TCTL_HOME / "cluster.yaml"
_CONFIG_SENTINEL = "<auto>"


def _resolve_config_path(cli_arg: str | None) -> str:
    """Pick which cluster.yaml to load.

    Order (only three sources, no implicit fallbacks):
      1. --config flag
      2. CLUSTER_CONFIG env var
      3. ~/.tctl/cluster.yaml (canonical default)

    Returns the chosen path even if it doesn't exist; let the loader raise
    a clear FileNotFoundError naming candidates that were tried.
    """
    if cli_arg:
        return cli_arg
    env = os.environ.get("CLUSTER_CONFIG")
    if env:
        return env
    return str(_CONFIG_DEFAULT_HOME)


def _config_path_error_message() -> str:
    return (
        "could not find cluster.yaml. Searched (in order):\n"
        f"  --config flag (not provided)\n"
        f"  CLUSTER_CONFIG env (={os.environ.get('CLUSTER_CONFIG', '<unset>')})\n"
        f"  {_CONFIG_DEFAULT_HOME}\n"
        f"\n"
        f"Run `tctl init-config` to bootstrap one at {_CONFIG_DEFAULT_HOME.parent}/."
    )


def _hoist_positional_profile(argv: list[str]) -> list[str]:
    """Rewrite `tctl <workload> <verb> models/<x>.yaml` → `tctl <workload> <verb> --profile <x>`.

    Two-token form: workload token consumed first, then verb token, then
    the positional is rewritten only when (workload, verb) is in _PROFILE_AWARE.
    """
    out = list(argv)
    # Skip leading global flags (including value-consuming ones)
    i = 0
    while i < len(out) and out[i].startswith("-"):
        if out[i] in ("--log-level", "--log-format", "--config", "--profile"):
            i += 2
            continue
        i += 1
    # Consume workload token
    if i >= len(out) or out[i] not in _WORKLOADS:
        return out
    workload = out[i]
    workload_idx = i
    # Consume verb token (skip flags between workload and verb)
    j = workload_idx + 1
    while j < len(out) and out[j].startswith("-"):
        j += 1
    if j >= len(out):
        return out
    verb = out[j]
    if verb not in _PROFILE_AWARE.get(workload, set()):
        return out
    # Look for models/*.yaml positional after verb (skip flags)
    k = j + 1
    while k < len(out) and out[k].startswith("-"):
        k += 1
    if k < len(out):
        token = out[k]
        if token.startswith("models/") and token.endswith(".yaml"):
            stem = token[len("models/") : -len(".yaml")]
            return out[: j + 1] + ["--profile", stem] + out[j + 1 : k] + out[k + 1 :]
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tctl",
        description="Tmux controller for multi-workload GPU fleets.",
    )
    p.add_argument("-V", "--version", action="version", version=f"tctl {__version__}")
    p.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Logging verbosity (default: info)",
    )
    p.add_argument(
        "--log-format",
        default="pretty",
        choices=["pretty", "json"],
        help="Log output format: 'pretty' (human-readable) or 'json' (structured, for log shippers)",  # noqa: E501
    )
    p.add_argument(
        "--config",
        default=_CONFIG_SENTINEL,
        help="path to cluster.yaml (default: $CLUSTER_CONFIG → ~/.tctl/cluster.yaml)",
    )
    p.add_argument(
        "--profile",
        default=None,
        help=(
            "Profile name to activate. "
            "Overrides cluster.vllm.default_profile and the TCTL_PROFILE env var."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    # Register visible workloads
    for name, (_, hidden) in _WORKLOADS.items():
        if not hidden:
            sp = sub.add_parser(name, help=f"see `tctl {name} --help`", add_help=False)
            sp.set_defaults(_subname=name)
    # Register platform commands
    for name in _PLATFORM_COMMANDS:
        sp = sub.add_parser(name, help=f"see `tctl {name} --help`", add_help=False)
        sp.set_defaults(_subname=name)
    return p


def _dispatch(name: str, argv_rest: list[str], ns: argparse.Namespace) -> int:
    if name in _WORKLOADS:
        mod_path, _ = _WORKLOADS[name]
    elif name in _PLATFORM_COMMANDS:
        mod_path = _PLATFORM_COMMANDS[name]
    else:
        print(f"tctl: unknown command {name!r}", file=sys.stderr)
        return 2
    mod = importlib.import_module(mod_path)
    handler: Callable[[argparse.Namespace, list[str]], int] = mod.run
    return handler(ns, argv_rest)


def _missing_path_is_config(exc: FileNotFoundError, config_path: Path | None = None) -> bool:
    """Return True if *exc* is about the cluster.yaml / tctl config being missing."""
    # Fast path: check message content
    msg = str(exc)
    if "cluster.yaml" in msg or ".tctl" in msg:
        return True
    if config_path is None:
        return False
    # Method 1: filename attribute (most reliable).
    if exc.filename is not None:
        try:
            if Path(exc.filename).resolve() == config_path.resolve():
                return True
        except Exception:
            pass
    # Method 2: parse from message string.
    try:
        quoted = msg.rsplit(": ", 1)[-1].strip("'\"")
        if quoted and Path(quoted).resolve() == config_path.resolve():
            return True
    except Exception:
        pass
    return False


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    raw = _hoist_positional_profile(raw)

    # Hidden workloads bypass global argparse — they are not listed in
    # `tctl --help` and dispatch directly without global flag processing.
    for i, tok in enumerate(raw):
        if tok.startswith("-"):
            continue
        if tok in _WORKLOADS and _WORKLOADS[tok][1]:  # hidden=True
            from tctl.logging import configure as _configure_logging

            _configure_logging()
            ns = argparse.Namespace(command=tok, config=str(_CONFIG_DEFAULT_HOME), profile=None)
            return _dispatch(tok, raw[i + 1 :], ns)
        break

    parser = build_parser()
    ns, rest = parser.parse_known_args(raw)

    from tctl.logging import configure as _configure_logging

    _configure_logging(level=ns.log_level, fmt=ns.log_format)

    # Resolve default config path before dispatch so commands can rely on
    # ns.config being a usable string.
    ns.config = _resolve_config_path(None if ns.config == _CONFIG_SENTINEL else ns.config)

    try:
        return _dispatch(ns.command, rest, ns)
    except FileNotFoundError as e:
        config_path = Path(ns.config).resolve()
        if _missing_path_is_config(e, config_path):
            msg = str(e)
            if "cluster.yaml" in str(config_path):
                _LOG.error("%s\n\n%s", msg, _config_path_error_message())
            else:
                _LOG.error("%s", msg)
            return 2
        _LOG.error("%s", e)
        raise
    except KeyboardInterrupt:
        return 130
