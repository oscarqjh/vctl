"""Pool-routing helpers: model id → Pool. Used by serve / lb add / attach."""

from __future__ import annotations

import re
import sys

import httpx

from vctl.config.models import LbHaproxy, Pool

# IPv4:port only. Tighter than the previous "any string" contract — rejects
# newlines, spaces, slashes, and other characters that could inject into the
# HAProxy admin socket protocol when interpolated into commands like
# `add server <backend>/<name> <ep> check`.
_EP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}")


def pool_for_model(lb: LbHaproxy, served_model: str) -> Pool:
    """Pick the pool whose served_model matches.

    Resolution order:
      1. Exact match (one) → return it.
      2. Multiple exact matches → ambiguous → exit 3.
      3. No exact match, one wildcard ("*") pool → return it.
      4. Otherwise → exit 3 with available pools listed.
    """
    exact = [p for p in lb.pools if p.served_model == served_model]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        print(
            f"ambiguous pool for {served_model!r}: "
            f"matched {[p.name for p in exact]} — config has duplicate served_model",
            file=sys.stderr,
        )
        sys.exit(3)
    wildcards = [p for p in lb.pools if p.served_model == "*"]
    if len(wildcards) == 1:
        return wildcards[0]
    available = ", ".join(f"{p.served_model} ({p.name})" for p in lb.pools)
    print(
        f"no pool serves {served_model!r}. Available: {available}. "
        f"Add a pools entry to cluster.yaml and run `vctl lb reload`.",
        file=sys.stderr,
    )
    sys.exit(3)


def pool_for_endpoint(lb: LbHaproxy, ep: str, *, timeout: float = 3.0) -> Pool:
    """Probe ep/v1/models, route by the model it returns.

    Exit 1 if model not loaded; exit 3 if no pool serves it.
    """
    url = f"http://{ep}/v1/models"
    try:
        r = httpx.get(url, timeout=timeout)
        data = r.json().get("data") or []
    except Exception as e:
        print(f"probe of {url} failed: {e}", file=sys.stderr)
        sys.exit(3)
    if not data:
        print(f"{ep}: model not loaded (/v1/models returned empty data)", file=sys.stderr)
        sys.exit(1)
    return pool_for_model(lb, data[0]["id"])


def _name_for(ep: str) -> str:
    """Derive HAProxy server name from endpoint: 'b_' + dots/colons replaced with underscores.

    Validates that ep is a well-formed `ip:port` string before deriving the name,
    rejecting characters that could inject into the HAProxy admin protocol
    (whitespace, newline, slash, etc.). Raises ValueError on invalid input.
    """
    if not _EP_RE.fullmatch(ep):
        raise ValueError(
            f"invalid endpoint {ep!r}; expected IPv4:port (e.g. '10.0.0.5:8000')"
        )
    return "b_" + ep.replace(".", "_").replace(":", "_")
