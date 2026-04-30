"""YAML-backed settings with VCTL_* env override and JSON Schema export."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from vctl.config.models import ClusterFile, ProfileFile
from vctl.config.yaml_source import load_yaml

ENV_PREFIX = "VCTL_"
ENV_DELIM = "__"


def _apply_env_overrides(
    base: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Overlay VCTL_* env vars with __ as nested key delimiter."""
    env = environ if environ is not None else os.environ
    out = dict(base)
    for raw_key, raw_val in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        key = raw_key[len(ENV_PREFIX) :].lower()
        parts = key.split(ENV_DELIM)
        if not parts or parts == [""]:
            continue
        cursor = out
        for piece in parts[:-1]:
            nxt = cursor.get(piece)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[piece] = nxt
            cursor = nxt
        cursor[parts[-1]] = _coerce_scalar(raw_val)
    return out


def _coerce_scalar(s: str) -> Any:
    low = s.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def load_cluster_file(path: Path | str) -> ClusterFile:
    raw = load_yaml(Path(path))
    merged = _apply_env_overrides(raw)
    return ClusterFile.model_validate(merged)


def load_profile_file(path: Path | str) -> ProfileFile:
    raw = load_yaml(Path(path))
    merged = _apply_env_overrides(raw)
    return ProfileFile.model_validate(merged)


def cluster_json_schema() -> dict[str, Any]:
    return ClusterFile.model_json_schema()


def profile_json_schema() -> dict[str, Any]:
    return ProfileFile.model_json_schema()


def resolve_profile_name(cli_value: str | None, cluster_default: str) -> str:
    """Profile selection precedence: CLI > VCTL_PROFILE > MODEL_PROFILE > cluster.profile."""
    if cli_value:
        return cli_value
    if os.environ.get("VCTL_PROFILE"):
        return os.environ["VCTL_PROFILE"]
    if os.environ.get("MODEL_PROFILE"):
        return os.environ["MODEL_PROFILE"]
    return cluster_default
