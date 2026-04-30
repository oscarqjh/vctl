"""Pydantic v2 schema for vctl/v1 config files."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ApiVersion = Literal["vctl/v1"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClusterSection(_Strict):
    venv: str
    state_dir: str
    env: dict[str, Any] = Field(default_factory=dict)


class LbClient(_Strict):
    bind_port: int


class LbAdmin(_Strict):
    bind_port: int


class LbStats(_Strict):
    bind_port: int


class LbHealth(_Strict):
    path: str = "/health"
    check_interval: str = "5s"
    fall: int = 3
    rise: int = 2


class LbDefaults(_Strict):
    maxconn_per_backend: int = 256
    slowstart: str = "30s"
    timeout_connect: str = "5s"
    timeout_client: str = "1h"
    timeout_server: str = "1h"


class LbHaproxy(_Strict):
    kind: Literal["haproxy"] = "haproxy"
    host: str
    client: LbClient
    admin: LbAdmin
    stats: LbStats
    algorithm: str = "leastconn"
    health: LbHealth = Field(default_factory=LbHealth)
    defaults: LbDefaults = Field(default_factory=LbDefaults)


# Tagged union — adding new LB kinds becomes a new class + a new tag.
# NOTE: single-member form; use Union[LbHaproxy, LbOther, ...] when adding kinds.
Lb = Annotated[LbHaproxy, Field(discriminator="kind")]


class Resources(_Strict):
    num_gpus: int
    cuda_visible_devices: str


class Parallelism(_Strict):
    data_parallel: int
    tensor_parallel: int
    api_server_count: int


class Server(_Strict):
    http_port: int = 8000


class Model(_Strict):
    name: str
    served_as: str


class ClusterFile(BaseModel):
    """Top-level cluster.yaml document. Lenient at the top level."""

    model_config = ConfigDict(extra="ignore")
    apiVersion: ApiVersion  # noqa: N815
    kind: Literal["Cluster"]
    cluster: ClusterSection
    profile: str
    lb: Lb


class ProfileFile(BaseModel):
    """Top-level models/<name>.yaml document. Lenient on vllm_args + env."""

    model_config = ConfigDict(extra="ignore")
    apiVersion: ApiVersion  # noqa: N815
    kind: Literal["Profile"]
    model: Model
    resources: Resources
    parallelism: Parallelism
    server: Server = Field(default_factory=Server)
    vllm_args: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, Any] = Field(default_factory=dict)
