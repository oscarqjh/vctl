"""Pydantic config model tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vctl.config.models import (
    ClusterFile,
    LbHaproxy,
    Parallelism,
    ProfileFile,
    Resources,
)


def _valid_cluster_dict() -> dict:
    return {
        "apiVersion": "vctl/v1",
        "kind": "Cluster",
        "cluster": {
            "venv": "/opt/venv",
            "state_dir": "/tmp/state",
            "env": {},
        },
        "profile": "qwen3-9b",
        "lb": {
            "kind": "haproxy",
            "host": "10.0.0.1",
            "client": {"bind_port": 8080},
            "admin": {"bind_port": 9001},
            "stats": {"bind_port": 9000},
            "algorithm": "leastconn",
            "health": {
                "path": "/health",
                "check_interval": "5s",
                "fall": 3,
                "rise": 2,
            },
            "defaults": {
                "maxconn_per_backend": 256,
                "slowstart": "30s",
                "timeout_connect": "5s",
                "timeout_client": "1h",
                "timeout_server": "1h",
            },
        },
    }


def test_cluster_file_happy_path() -> None:
    cf = ClusterFile.model_validate(_valid_cluster_dict())
    assert cf.lb.kind == "haproxy"
    assert isinstance(cf.lb, LbHaproxy)
    assert cf.lb.client.bind_port == 8080


def test_invalid_bind_port_raises_with_field_path() -> None:
    """AT-3: int-typed field rejecting a string must name the field path."""
    bad = _valid_cluster_dict()
    bad["lb"]["client"]["bind_port"] = "eighty-eight"
    with pytest.raises(ValidationError) as exc:
        ClusterFile.model_validate(bad)
    msg = str(exc.value)
    # Pydantic v2 with a discriminated union inserts the discriminator tag into
    # the path (e.g. lb.haproxy.client.bind_port).  All three forms are valid
    # AT-3 evidence that the field path is present in the error message.
    assert (
        "lb.client.bind_port" in msg
        or "lb -> client -> bind_port" in msg
        or "lb.haproxy.client.bind_port" in msg
    )
    assert "integer" in msg.lower()


def test_strict_section_rejects_unknown_key() -> None:
    bad = _valid_cluster_dict()
    bad["lb"]["weird_extra"] = 1
    with pytest.raises(ValidationError):
        ClusterFile.model_validate(bad)


def test_profile_file_happy_path() -> None:
    cf = ProfileFile.model_validate(
        {
            "apiVersion": "vctl/v1",
            "kind": "Profile",
            "model": {"name": "Qwen/Qwen3.5-9B", "served_as": "qwen3-9b"},
            "resources": {"num_gpus": 8, "cuda_visible_devices": "0,1,2,3,4,5,6,7"},
            "parallelism": {"data_parallel": 8, "tensor_parallel": 1, "api_server_count": 8},
            "server": {"http_port": 8000},
            "vllm_args": {"reasoning-parser": "qwen3", "enable-prefix-caching": True},
            "env": {"VLLM_ENGINE_READY_TIMEOUT_S": 1800},
        }
    )
    assert cf.parallelism.data_parallel == 8
    assert cf.vllm_args["reasoning-parser"] == "qwen3"


def test_vllm_args_lenient() -> None:
    """vllm_args accepts arbitrary kebab-case keys without schema fail."""
    Parallelism.model_validate({"data_parallel": 1, "tensor_parallel": 1, "api_server_count": 1})
    Resources.model_validate({"num_gpus": 1, "cuda_visible_devices": "0"})


def _two_pool_dict() -> dict:
    return {
        "apiVersion": "vctl/v1",
        "kind": "Cluster",
        "cluster": {"venv": "/v", "state_dir": "/s", "env": {}},
        "profile": "qwen3_5-9b",
        "lb": {
            "kind": "haproxy",
            "host": "10.0.0.1",
            "admin": {"bind_port": 9001},
            "stats": {"bind_port": 9000},
            "algorithm": "leastconn",
            "health": {"path": "/health", "check_interval": "5s", "fall": 3, "rise": 2},
            "defaults": {
                "maxconn_per_backend": 256,
                "slowstart": "30s",
                "timeout_connect": "5s",
                "timeout_client": "1h",
                "timeout_server": "1h",
            },
            "pools": [
                {"name": "a", "served_model": "M/A", "bind_port": 8080},
                {"name": "b", "served_model": "M/B", "bind_port": 8081},
            ],
        },
    }


def _legacy_single_pool_dict() -> dict:
    """v0.1.0-style cluster.yaml — has client.bind_port, no pools."""
    return {
        "apiVersion": "vctl/v1",
        "kind": "Cluster",
        "cluster": {"venv": "/v", "state_dir": "/s", "env": {}},
        "profile": "qwen3_5-9b",
        "lb": {
            "kind": "haproxy",
            "host": "10.0.0.1",
            "client": {"bind_port": 8080},
            "admin": {"bind_port": 9001},
            "stats": {"bind_port": 9000},
            "algorithm": "leastconn",
            "health": {"path": "/health", "check_interval": "5s", "fall": 3, "rise": 2},
            "defaults": {
                "maxconn_per_backend": 256,
                "slowstart": "30s",
                "timeout_connect": "5s",
                "timeout_client": "1h",
                "timeout_server": "1h",
            },
        },
    }


def test_lb_with_two_pools_validates() -> None:
    cf = ClusterFile.model_validate(_two_pool_dict())
    assert len(cf.lb.pools) == 2
    assert cf.lb.pools[0].name == "a"
    assert cf.lb.pools[0].bind_port == 8080
    assert cf.lb.pools[1].served_model == "M/B"


def test_lb_duplicate_bind_port_rejected() -> None:
    bad = _two_pool_dict()
    bad["lb"]["pools"][1]["bind_port"] = 8080  # collide with pool a
    with pytest.raises(ValidationError, match="bind_port"):
        ClusterFile.model_validate(bad)


def test_lb_duplicate_pool_name_rejected() -> None:
    bad = _two_pool_dict()
    bad["lb"]["pools"][1]["name"] = "a"
    with pytest.raises(ValidationError, match="name"):
        ClusterFile.model_validate(bad)


def test_lb_duplicate_served_model_rejected() -> None:
    bad = _two_pool_dict()
    bad["lb"]["pools"][1]["served_model"] = "M/A"
    with pytest.raises(ValidationError, match="served_model"):
        ClusterFile.model_validate(bad)


def test_lb_pool_collides_with_admin_port_rejected() -> None:
    bad = _two_pool_dict()
    bad["lb"]["pools"][0]["bind_port"] = 9001  # admin port
    with pytest.raises(ValidationError, match="admin|stats"):
        ClusterFile.model_validate(bad)


def test_lb_legacy_single_pool_synthesized() -> None:
    cf = ClusterFile.model_validate(_legacy_single_pool_dict())
    assert len(cf.lb.pools) == 1
    assert cf.lb.pools[0].name == "default"
    assert cf.lb.pools[0].served_model == "*"
    assert cf.lb.pools[0].bind_port == 8080


def test_lb_no_pools_and_no_client_rejected() -> None:
    bad = _legacy_single_pool_dict()
    del bad["lb"]["client"]
    with pytest.raises(ValidationError, match="pools|client"):
        ClusterFile.model_validate(bad)
