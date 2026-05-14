"""Cluster + profile + env merge into a frozen ResolvedConfig."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tctl.config.models import (
        ClusterSection,
        LbHaproxy,
        Model,
        Parallelism,
        Resources,
        Server,
    )


@dataclass(frozen=True)
class ResolvedConfig:
    profile_name: str
    cluster: ClusterSection
    lb: LbHaproxy
    model: Model
    resources: Resources
    parallelism: Parallelism
    server: Server
    vllm_args: dict[str, Any]
    env: dict[str, Any]
    cluster_path: Path
    profile_path: Path


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Merge *b* onto *a* recursively.

    Rules:
    - If both values are dicts, recurse.
    - None in *b* deletes the key from the merged result (lets profiles
      unset cluster-level env vars).
    - Any other value in *b* overwrites *a*.
    """
    out = dict(a)
    for k, v in b.items():
        if v is None:
            # D4: None in b means "delete this key from the merged output"
            out.pop(k, None)
        elif k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_VALID_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")


def _validate_profile_name(profile_name: str) -> None:
    """D8: Reject profile names containing path-traversal characters.

    Allowed: alphanumeric, underscore, hyphen, dot (not leading), slash only
    in the middle (but checked below).  Concretely, any name that doesn't
    match ``[A-Za-z0-9][A-Za-z0-9_.-]*`` is invalid.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", profile_name):
        raise ValueError(
            f"invalid profile name {profile_name!r}; "
            "must be alphanumeric with -/_ (no leading dot, no slash, no ..)"
        )


def resolve(config_path: Path | str, profile: str | None) -> ResolvedConfig:
    # Deferred imports so the module is importable before tctl.config.* exists
    # (Task 3 creates those modules; calling resolve() before Task 3 will
    # raise ImportError at this point, not at module-import time).
    from tctl.config.models import LbHaproxy as _LbHaproxy  # noqa: PLC0415
    from tctl.config.settings import (  # noqa: PLC0415
        load_cluster_file,
        load_profile_file,
        resolve_profile_name,
    )

    cluster_path = Path(config_path).resolve()
    cf = load_cluster_file(cluster_path)
    profile_name = resolve_profile_name(profile, cf)
    if not profile_name:
        raise ValueError(
            "no profile specified — pass --profile, set $TCTL_PROFILE, "
            "or set vllm.default_profile in cluster.yaml"
        )
    # D8: reject path-traversal in profile names
    _validate_profile_name(profile_name)
    profile_path = (cluster_path.parent / "models" / f"{profile_name}.yaml").resolve()
    if not profile_path.exists():
        raise FileNotFoundError(
            f"profile '{profile_name}' not found at {profile_path}; "
            "see `tctl vllm profiles` for available choices"
        )
    pf = load_profile_file(profile_path)
    merged_env = _deep_merge(cf.cluster.env or {}, pf.env or {})
    if not isinstance(cf.haproxy, _LbHaproxy):
        raise TypeError(f"unsupported haproxy.kind {cf.haproxy.kind!r}")
    return ResolvedConfig(
        profile_name=profile_name,
        cluster=cf.cluster,
        lb=cf.haproxy,
        model=pf.model,
        resources=pf.resources,
        parallelism=pf.parallelism,
        server=pf.server,
        vllm_args=dict(pf.vllm_args),
        env=merged_env,
        cluster_path=cluster_path,
        profile_path=profile_path,
    )
