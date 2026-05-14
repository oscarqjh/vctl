"""Pydantic v2 schema for tctl/v1 config files."""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LOG = logging.getLogger(__name__)

ApiVersion = Literal["tctl/v1"]

# D6: reusable port type alias — ports must be 1-65535
_Port = Annotated[int, Field(ge=1, le=65535)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClusterSection(_Strict):
    venv: Annotated[str, Field(min_length=1)]
    state_dir: Annotated[str, Field(min_length=1)]
    env: dict[str, str | int | float | bool] = Field(default_factory=dict)

    # D7: expand ~ in venv and state_dir
    @field_validator("venv", "state_dir", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            if not v:
                raise ValueError("must not be empty")
            return os.path.expanduser(v)
        return v


class LbClient(_Strict):
    bind_port: _Port


class LbAdmin(_Strict):
    bind_port: _Port
    bind_addr: str = Field(
        default="0.0.0.0",
        description=(
            "IPv4 address to bind the HAProxy admin TCP socket. "
            "Default 0.0.0.0 preserves cross-host tctl scaling behaviour. "
            "Set to 127.0.0.1 to restrict the admin socket to the LB host only — "
            "tighter security but requires running scaling commands on the LB host "
            "(e.g. via SSH/tmux)."
        ),
    )

    # E1: validate bind_addr is a valid IPv4 address
    @field_validator("bind_addr", mode="after")
    @classmethod
    def _valid_ipv4(cls, v: str) -> str:
        try:
            ipaddress.IPv4Address(v)
        except ValueError as exc:
            raise ValueError(f"bind_addr must be a valid IPv4 address, got {v!r}") from exc
        return v


class LbStats(_Strict):
    bind_port: _Port


class LbHealth(_Strict):
    path: str = "/health"
    check_interval: str = "5s"
    fall: int = Field(default=3, ge=1)  # D6
    rise: int = Field(default=2, ge=1)  # D6

    # D6: health path must start with /
    @field_validator("path", mode="after")
    @classmethod
    def _path_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("health path must start with '/'")
        return v


class LbDefaults(_Strict):
    maxconn_per_backend: int = 256
    slowstart: str = "30s"
    timeout_connect: str = "5s"
    timeout_client: str = "1h"
    timeout_server: str = "1h"


class Pool(_Strict):
    name: Annotated[str, Field(min_length=1)]
    served_model: Annotated[str, Field(min_length=1)]  # D9: no empty served_model
    bind_port: _Port  # D6

    @field_validator("name", mode="after")
    @classmethod
    def _name_not_pure_digits(cls, v: str) -> str:
        """v0.4.3: pool names cannot be pure digits.

        The CLI's `--pool` flag accepts either a pool name or a bind_port.
        Disambiguation: if the value parses as int and matches a configured
        bind_port, it's treated as a port; otherwise as a name. Allowing
        purely numeric pool names would create a footgun where the same
        `--pool 8080` could mean different things depending on what other
        pools are configured.
        """
        if v.isdigit():
            raise ValueError(
                f"pool name {v!r} is all digits; pool names must contain at least one "
                "non-digit character (so the CLI's --pool flag can unambiguously "
                "distinguish names from bind_port lookups)"
            )
        return v


class LbPrune(_Strict):
    """Configuration for `tctl haproxy prune` and the auto-watcher bundled in `haproxy start`."""

    enabled: bool = True  # set false to disable auto-watcher on lb start
    threshold: str = "5m"  # minimum DOWN duration before backend is prunable
    watch_interval: str = "30s"  # polling interval for the auto-watcher loop

    @field_validator("threshold", "watch_interval", mode="after")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        from tctl.duration import _parse_duration  # lazy — avoids circular import at module load

        _parse_duration(v)  # raises ValueError on bad input; pydantic converts to ValidationError
        return v


class LbHaproxy(_Strict):
    kind: Literal["haproxy"] = "haproxy"
    host: Annotated[str, Field(min_length=1)]  # D9: no empty host
    client: LbClient | None = None  # legacy single-pool field
    admin: LbAdmin
    stats: LbStats
    algorithm: str = "leastconn"
    health: LbHealth = Field(default_factory=LbHealth)
    defaults: LbDefaults = Field(default_factory=LbDefaults)
    prune: LbPrune = Field(default_factory=LbPrune)
    pools: list[Pool] = Field(default_factory=list)

    @model_validator(mode="after")
    def _synthesize_or_validate_pools(self) -> LbHaproxy:
        if not self.pools:
            if self.client is None:
                raise ValueError("haproxy: must specify either `pools` or `client.bind_port`")
            object.__setattr__(
                self,
                "pools",
                [Pool(name="default", served_model="*", bind_port=self.client.bind_port)],
            )
        names = [p.name for p in self.pools]
        if len(names) != len(set(names)):
            raise ValueError(f"haproxy.pools: duplicate name in {names}")
        ports = [p.bind_port for p in self.pools]
        if len(ports) != len(set(ports)):
            raise ValueError(f"haproxy.pools: duplicate bind_port in {ports}")
        non_wild = [p.served_model for p in self.pools if p.served_model != "*"]
        if len(non_wild) != len(set(non_wild)):
            raise ValueError(f"haproxy.pools: duplicate served_model in {non_wild}")
        if self.admin.bind_port in ports or self.stats.bind_port in ports:
            raise ValueError(
                f"haproxy.pools: bind_port collides with admin/stats "
                f"(admin={self.admin.bind_port}, stats={self.stats.bind_port}, pool_ports={ports})"
            )
        return self


# Tagged union — adding new LB kinds becomes a new class + a new tag.
# NOTE: single-member form; use Union[LbHaproxy, LbOther, ...] when adding kinds.
Lb = Annotated[LbHaproxy, Field(discriminator="kind")]


class Resources(_Strict):
    num_gpus: int = Field(ge=0)  # D6: no negative GPU counts
    cuda_visible_devices: str


class Parallelism(_Strict):
    data_parallel: int = Field(ge=1)  # D6: must be >= 1
    tensor_parallel: int = Field(ge=1)  # D6
    # Optional. When None, tctl omits --api-server-count and lets vllm default
    # (= data_parallel). Setting api_server_count=1 with data_parallel>1 +
    # mm-processor-cache-type=shm triggers vllm 0.19.x bug — see v0.4.13
    # CHANGELOG. Profiles should leave this unset unless deliberately overriding.
    api_server_count: int | None = Field(default=None, ge=1)


class Server(_Strict):
    http_port: _Port = 8000  # D6


class Model(_Strict):
    name: str


class VllmCluster(_Strict):
    """vllm-scoped cluster settings."""

    default_profile: str | None = None


class ClusterFile(BaseModel):
    """Top-level cluster.yaml document (v0.9.0 shape).

    Breaking from v0.8.0: apiVersion tctl/v1, haproxy: (was lb:),
    vllm.default_profile (was top-level profile:).

    Strict at the top level — unknown keys are rejected so typos like
    ``Haproxy:`` (capital) produce a clear error rather than silent data loss.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    apiVersion: ApiVersion  # noqa: N815
    cluster: ClusterSection
    haproxy: LbHaproxy
    vllm: VllmCluster = Field(default_factory=VllmCluster)


class ProfileFile(BaseModel):
    """Top-level models/<name>.yaml document.

    Strict at the top level — unknown keys are rejected.
    ``vllm_args`` and ``env`` are open dicts (any keys allowed inside them).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    apiVersion: ApiVersion  # noqa: N815
    kind: Literal["Profile"]
    model: Model
    resources: Resources
    parallelism: Parallelism
    server: Server = Field(default_factory=Server)
    vllm_args: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str | int | float | bool] = Field(default_factory=dict)  # D12
