"""Pydantic config model tests — tctl/v1 schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _minimal_haproxy() -> dict:  # type: ignore[type-arg]
    return {
        "kind": "haproxy",
        "host": "127.0.0.1",
        "admin": {"bind_port": 9001},
        "stats": {"bind_port": 9000},
        "pools": [{"name": "default", "bind_port": 8000, "served_model": "*"}],
    }


def _two_pool_haproxy() -> dict:  # type: ignore[type-arg]
    return {
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
    }


def _valid_cluster_dict() -> dict:  # type: ignore[type-arg]
    return {
        "apiVersion": "tctl/v1",
        "cluster": {"venv": "/opt/venv", "state_dir": "/tmp/state"},
        "haproxy": _minimal_haproxy(),
        "vllm": {"default_profile": None},
    }


# ---------------------------------------------------------------------------
# tctl/v1 schema tests
# ---------------------------------------------------------------------------


def test_tctl_cluster_file_accepts_new_shape() -> None:
    from tctl.config.models import ClusterFile, ClusterSection, LbHaproxy, VllmCluster

    cf = ClusterFile(
        apiVersion="tctl/v1",
        cluster=ClusterSection(venv="/venv", state_dir="/tmp/state"),
        haproxy=LbHaproxy(**_minimal_haproxy()),
        vllm=VllmCluster(default_profile=None),
    )
    assert cf.apiVersion == "tctl/v1"
    assert cf.vllm.default_profile is None


def test_tctl_cluster_file_happy_path() -> None:
    from tctl.config.models import ClusterFile, LbHaproxy

    cf = ClusterFile.model_validate(_valid_cluster_dict())
    assert cf.haproxy.kind == "haproxy"
    assert isinstance(cf.haproxy, LbHaproxy)


def test_tctl_cluster_file_rejects_old_lb_key() -> None:
    from tctl.config.models import ClusterFile

    with pytest.raises(ValidationError):
        ClusterFile.model_validate(
            {
                "apiVersion": "tctl/v1",
                "cluster": {"venv": "/venv", "state_dir": "/tmp"},
                "lb": _minimal_haproxy(),  # old key — should be rejected
            }
        )


def test_tctl_cluster_file_rejects_top_level_profile() -> None:
    from tctl.config.models import ClusterFile

    with pytest.raises(ValidationError):
        ClusterFile.model_validate(
            {
                "apiVersion": "tctl/v1",
                "cluster": {"venv": "/venv", "state_dir": "/tmp"},
                "haproxy": _minimal_haproxy(),
                "profile": "foo",  # old top-level field — must be rejected
            }
        )


def test_tctl_cluster_file_rejects_old_api_version() -> None:
    from tctl.config.models import ClusterFile

    with pytest.raises(ValidationError):
        ClusterFile.model_validate(
            {
                "apiVersion": "vctl/v1",  # old literal
                "cluster": {"venv": "/venv", "state_dir": "/tmp"},
                "haproxy": _minimal_haproxy(),
            }
        )


def test_tctl_vllm_cluster_default_profile_none() -> None:
    from tctl.config.models import VllmCluster

    vc = VllmCluster()
    assert vc.default_profile is None


def test_tctl_vllm_cluster_default_profile_roundtrip() -> None:
    from tctl.config.models import VllmCluster

    vc = VllmCluster(default_profile="foo")
    assert vc.default_profile == "foo"


def test_tctl_invalid_bind_port_raises_with_field_path() -> None:
    """int-typed field rejecting a string must name the field path."""
    from tctl.config.models import ClusterFile

    bad = _valid_cluster_dict()
    bad["haproxy"]["admin"] = {"bind_port": "bad-port"}
    with pytest.raises(ValidationError) as exc:
        ClusterFile.model_validate(bad)
    msg = str(exc.value)
    assert "integer" in msg.lower() or "int" in msg.lower()


def test_tctl_strict_section_rejects_unknown_key() -> None:
    from tctl.config.models import ClusterFile

    bad = _valid_cluster_dict()
    bad["haproxy"]["weird_extra"] = 1
    with pytest.raises(ValidationError):
        ClusterFile.model_validate(bad)


def test_tctl_profile_file_happy_path() -> None:
    from tctl.config.models import ProfileFile

    cf = ProfileFile.model_validate(
        {
            "apiVersion": "tctl/v1",
            "kind": "Profile",
            "model": {"name": "Qwen/Qwen3.5-9B"},
            "resources": {"num_gpus": 8, "cuda_visible_devices": "0,1,2,3,4,5,6,7"},
            "parallelism": {"data_parallel": 8, "tensor_parallel": 1, "api_server_count": 8},
            "server": {"http_port": 8000},
            "vllm_args": {"reasoning-parser": "qwen3", "enable-prefix-caching": True},
            "env": {"VLLM_ENGINE_READY_TIMEOUT_S": 1800},
        }
    )
    assert cf.parallelism.data_parallel == 8
    assert cf.vllm_args["reasoning-parser"] == "qwen3"
    assert not hasattr(cf.model, "served_as")


def test_tctl_served_as_now_rejected_as_unknown_field() -> None:
    """the served_as backwards-compat shim is removed."""
    from tctl.config.models import Model

    with pytest.raises(ValidationError):
        Model.model_validate({"name": "Qwen/Qwen3.5-9B", "served_as": "qwen3-9b"})


def test_tctl_parallelism_and_resources_validate() -> None:
    from tctl.config.models import Parallelism, Resources

    Parallelism.model_validate({"data_parallel": 1, "tensor_parallel": 1, "api_server_count": 1})
    Resources.model_validate({"num_gpus": 1, "cuda_visible_devices": "0"})


def test_tctl_lb_with_two_pools_validates() -> None:
    from tctl.config.models import ClusterFile

    cf = ClusterFile.model_validate(
        {
            "apiVersion": "tctl/v1",
            "cluster": {"venv": "/v", "state_dir": "/s"},
            "haproxy": _two_pool_haproxy(),
            "vllm": {"default_profile": None},
        }
    )
    assert len(cf.haproxy.pools) == 2
    assert cf.haproxy.pools[0].name == "a"
    assert cf.haproxy.pools[0].bind_port == 8080
    assert cf.haproxy.pools[1].served_model == "M/B"


def test_tctl_lb_duplicate_bind_port_rejected() -> None:
    from tctl.config.models import ClusterFile

    bad: dict = {  # type: ignore[type-arg]
        "apiVersion": "tctl/v1",
        "cluster": {"venv": "/v", "state_dir": "/s"},
        "haproxy": _two_pool_haproxy(),
        "vllm": {"default_profile": None},
    }
    bad["haproxy"]["pools"][1]["bind_port"] = 8080  # collide
    with pytest.raises(ValidationError, match="bind_port"):
        ClusterFile.model_validate(bad)


def test_tctl_lb_duplicate_pool_name_rejected() -> None:
    from tctl.config.models import ClusterFile

    bad: dict = {  # type: ignore[type-arg]
        "apiVersion": "tctl/v1",
        "cluster": {"venv": "/v", "state_dir": "/s"},
        "haproxy": _two_pool_haproxy(),
        "vllm": {"default_profile": None},
    }
    bad["haproxy"]["pools"][1]["name"] = "a"
    with pytest.raises(ValidationError, match="name"):
        ClusterFile.model_validate(bad)


def test_tctl_lb_duplicate_served_model_rejected() -> None:
    from tctl.config.models import ClusterFile

    bad: dict = {  # type: ignore[type-arg]
        "apiVersion": "tctl/v1",
        "cluster": {"venv": "/v", "state_dir": "/s"},
        "haproxy": _two_pool_haproxy(),
        "vllm": {"default_profile": None},
    }
    bad["haproxy"]["pools"][1]["served_model"] = "M/A"
    with pytest.raises(ValidationError, match="served_model"):
        ClusterFile.model_validate(bad)


def test_tctl_lb_pool_collides_with_admin_port_rejected() -> None:
    from tctl.config.models import ClusterFile

    bad: dict = {  # type: ignore[type-arg]
        "apiVersion": "tctl/v1",
        "cluster": {"venv": "/v", "state_dir": "/s"},
        "haproxy": _two_pool_haproxy(),
        "vllm": {"default_profile": None},
    }
    bad["haproxy"]["pools"][0]["bind_port"] = 9001  # admin port
    with pytest.raises(ValidationError, match="admin|stats"):
        ClusterFile.model_validate(bad)


def test_tctl_pool_name_pure_digits_rejected() -> None:
    """pool names cannot be pure digits."""
    from tctl.config.models import Pool

    with pytest.raises(ValidationError, match="all digits"):
        Pool.model_validate({"name": "8080", "served_model": "X", "bind_port": 9000})


def test_tctl_pool_name_with_letters_or_dashes_accepted() -> None:
    from tctl.config.models import Pool

    Pool.model_validate({"name": "qwen3-5-9b", "served_model": "X", "bind_port": 8080})
    Pool.model_validate({"name": "pool_a", "served_model": "X", "bind_port": 8080})
    Pool.model_validate({"name": "p1", "served_model": "X", "bind_port": 8080})  # mixed OK
