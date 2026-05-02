"""`vctl info` — resolved config table."""

from __future__ import annotations

import argparse

from vctl.platform import detect_self_ip
from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="vctl info",
        description="Print the resolved cluster + profile config as a table.",
    )


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    self_ip = detect_self_ip()
    rows = [
        ("profile", rc.profile_name),
        ("model", rc.model.name),
        ("self_ip", self_ip),
        ("dp / tp", f"{rc.parallelism.data_parallel} / {rc.parallelism.tensor_parallel}"),
        ("api_servers", str(rc.parallelism.api_server_count)),
        ("vllm_port", str(rc.server.http_port)),
        ("lb.host", rc.lb.host),
    ]
    # Per-pool URL rows (escape brackets so Rich doesn't treat them as markup)
    for pool in rc.lb.pools:
        rows.append(
            (
                f"pool\\[{pool.name}]",
                f"{pool.served_model}  →  http://{rc.lb.host}:{pool.bind_port}",
            )
        )
    rows += [
        ("lb.admin", str(rc.lb.admin.bind_port)),
        ("lb.stats", str(rc.lb.stats.bind_port)),
        ("venv", rc.cluster.venv),
        ("state_dir", rc.cluster.state_dir),
    ]
    from rich.console import Console
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("key")
    table.add_column("value")
    for k, v in rows:
        table.add_row(k, v)
    Console().print(table)
    return 0
