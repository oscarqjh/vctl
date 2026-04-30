"""`vctl args` — resolved vllm serve flags, one per line."""

from __future__ import annotations

import argparse

from vctl.resolver import resolve


def _bool_flag(value: object) -> str:
    return "true" if value else "false"


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    rc = resolve(ns.config, profile=ns.profile)
    out: list[str] = [
        rc.model.name,
        f"--served-model-name={rc.model.served_as}",
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--api-server-count={rc.parallelism.api_server_count}",
        f"--port={rc.server.http_port}",
    ]
    for k, v in rc.vllm_args.items():
        if isinstance(v, bool):
            out.append(f"--{k}={_bool_flag(v)}")
        else:
            out.append(f"--{k}={v}")
    print("\n".join(out))
    return 0
