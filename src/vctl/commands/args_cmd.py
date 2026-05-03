"""`vctl args` — resolved vllm serve flags, one per line."""

from __future__ import annotations

import argparse

from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="vctl args",
        description="Print the vLLM CLI args that would be used for the active "
        "profile, one per line. Useful for debugging or piping into a manual "
        "vllm invocation.",
    )


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    # Don't include --served-model-name; vllm defaults to model.name. Mirrors
    # serve.py emission.
    out: list[str] = [
        rc.model.name,
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--port={rc.server.http_port}",
    ]
    if rc.parallelism.api_server_count is not None:
        out.append(f"--api-server-count={rc.parallelism.api_server_count}")
    for k, v in rc.vllm_args.items():
        # vLLM uses BooleanOptionalAction: bare `--flag` / `--no-flag`,
        # not `--flag=true`. Match serve.py emission.
        if v is True:
            out.append(f"--{k}")
        elif v is False:
            out.append(f"--no-{k}")
        else:
            out.append(f"--{k}={v}")
    print("\n".join(out))
    return 0
