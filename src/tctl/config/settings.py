"""YAML-backed settings with TCTL_* env override and JSON Schema export."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tctl.config.models import ClusterFile, ProfileFile
from tctl.config.yaml_source import load_yaml

ENV_PREFIX = "TCTL_"
ENV_DELIM = "__"


# Top-level field names for each document type (lower-cased).
# Only TCTL_* env vars whose first segment matches a known field are applied.
# This prevents test-sentinel env vars (e.g. TCTL_TEST_NO_SOCKET) from
# polluting the document dict and causing extra="forbid" validation errors.
_CLUSTER_TOPLEVEL: frozenset[str] = frozenset({"apiversion", "cluster", "haproxy", "vllm"})
_PROFILE_TOPLEVEL: frozenset[str] = frozenset(
    {"apiversion", "kind", "model", "resources", "parallelism", "server", "vllm_args", "env"}
)


def _apply_env_overrides(
    base: dict[str, Any],
    environ: dict[str, str] | None = None,
    allowed_toplevel: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Overlay TCTL_* env vars with __ as nested key delimiter.

    When *allowed_toplevel* is provided, only vars whose first key segment
    appears in that set are applied — unknown top-level segments are ignored.
    This prevents test-sentinel env vars such as ``TCTL_TEST_NO_SOCKET`` from
    polluting the cluster dict and causing ``extra="forbid"`` validation errors.
    """
    env = environ if environ is not None else os.environ
    out = dict(base)
    for raw_key, raw_val in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        key = raw_key[len(ENV_PREFIX) :].lower()
        parts = key.split(ENV_DELIM)
        if not parts or parts == [""]:
            continue
        # D2: reject empty key segments (e.g. TCTL_HAPROXY__=8080, TCTL___HOST=x)
        if any(p == "" for p in parts):
            raise ValueError(f"TCTL env var {raw_key!r} has empty key segment")
        if allowed_toplevel is not None and parts[0] not in allowed_toplevel:
            continue
        cursor = out
        for piece in parts[:-1]:
            nxt = cursor.get(piece)
            if not isinstance(nxt, dict):
                # D1: if the YAML already set this as a non-mapping leaf, hard error
                if nxt is not None:
                    raise ValueError(
                        f"TCTL env var {raw_key!r} tries to descend into non-mapping at"
                        f" {piece!r} (existing value is a {type(nxt).__name__})"
                    )
                nxt = {}
                cursor[piece] = nxt
            cursor = nxt
        cursor[parts[-1]] = _coerce_scalar(raw_val)
    return out


def _coerce_scalar(s: str) -> Any:
    low = s.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    # D3: strict patterns — reject nan, inf, scientific notation, hex, etc.
    if re.fullmatch(r"-?\d+", s.strip()):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s.strip()):
        return float(s)
    return s


def load_cluster_file(path: Path | str) -> ClusterFile:
    raw = load_yaml(Path(path))
    merged = _apply_env_overrides(raw, allowed_toplevel=_CLUSTER_TOPLEVEL)
    return ClusterFile.model_validate(merged)


def load_profile_file(path: Path | str) -> ProfileFile:
    raw = load_yaml(Path(path))
    merged = _apply_env_overrides(raw, allowed_toplevel=_PROFILE_TOPLEVEL)
    return ProfileFile.model_validate(merged)


def cluster_json_schema() -> dict[str, Any]:
    return ClusterFile.model_json_schema()


def profile_json_schema() -> dict[str, Any]:
    return ProfileFile.model_json_schema()


def resolve_profile_name(cli_value: str | None, cluster: ClusterFile) -> str | None:
    """Profile selection: CLI > $TCTL_PROFILE > cluster.vllm.default_profile.

    MODEL_PROFILE is intentionally NOT consulted (dropped in v0.9.0).
    """
    if cli_value:
        return cli_value
    env = os.environ.get("TCTL_PROFILE")
    if env:
        return env
    return cluster.vllm.default_profile
