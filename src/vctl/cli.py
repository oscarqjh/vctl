"""Argparse root. Real subcommand wiring is added in later tasks."""

from __future__ import annotations

import argparse
import sys

from vctl import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl", description="Typed CLI for vLLM fleet.")
    p.add_argument("-V", "--version", action="version", version=f"vctl {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv if argv is not None else sys.argv[1:])
    return 0
