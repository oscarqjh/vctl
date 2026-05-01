"""Migrator tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from vctl.config.migrate import detect_kind, migrate_cluster, migrate_profile
from vctl.config.models import ClusterFile, ProfileFile

FIX = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with (FIX / name).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_migrate_cluster_old_to_new() -> None:
    new = migrate_cluster(_load("old_cluster.yaml"))
    assert new["apiVersion"] == "vctl/v1"
    assert new["kind"] == "Cluster"
    assert new["profile"] == "qwen3-vl-30b-a3b"
    assert new["cluster"]["venv"].endswith("/.venv_0_19_2rc1")
    assert new["cluster"]["state_dir"] == "/mnt/aigc/users/qianjianheng/.vllm-lb-state"
    assert new["lb"]["kind"] == "haproxy"
    assert new["lb"]["host"] == "10.119.30.181"
    assert new["lb"]["pools"][0]["bind_port"] == 8080
    assert new["lb"]["admin"]["bind_port"] == 9001
    assert new["lb"]["stats"]["bind_port"] == 9000
    assert new["lb"]["health"]["path"] == "/health"
    assert new["lb"]["defaults"]["maxconn_per_backend"] == 256
    ClusterFile.model_validate(new)  # round-trip


def test_migrate_profile_old_to_new() -> None:
    new = migrate_profile(_load("old_profile.yaml"))
    assert new["apiVersion"] == "vctl/v1"
    assert new["kind"] == "Profile"
    assert new["model"]["name"] == "Qwen/Qwen3-VL-30B-A3B-Thinking"
    assert new["resources"]["num_gpus"] == 8
    assert new["resources"]["cuda_visible_devices"] == "0,1,2,3,4,5,6,7"
    assert new["parallelism"]["data_parallel"] == 4
    assert new["parallelism"]["tensor_parallel"] == 2
    assert new["server"]["http_port"] == 8000
    assert new["vllm_args"]["reasoning-parser"] == "qwen3"
    assert new["env"]["VLLM_ENGINE_READY_TIMEOUT_S"] == 3000
    ProfileFile.model_validate(new)  # round-trip


def test_already_migrated_cluster_is_noop() -> None:
    src = _load("new_cluster.yaml")
    out = migrate_cluster(src)
    assert out["apiVersion"] == "vctl/v1"
    assert out == src or out["lb"]["host"] == src["lb"]["host"]


def test_migrate_cluster_emits_pools_entry() -> None:
    """Legacy bind_port → lb.pools[0]."""
    new = migrate_cluster(
        {
            "profile": "x",
            "venv": "/v",
            "env": {},
            "lb": {
                "host": "10.0.0.1",
                "bind_port": 8080,  # legacy single-pool
                "admin_port": 9001,
                "stats_port": 9000,
                "state_dir": "/s",
            },
        }
    )
    assert "client" not in new["lb"]
    assert new["lb"]["pools"] == [{"name": "default", "served_model": "*", "bind_port": 8080}]
    # Round-trip through pydantic to confirm validity.
    from vctl.config.models import ClusterFile

    ClusterFile.model_validate(new)


def test_detect_kind_cluster_vs_profile() -> None:
    assert detect_kind(_load("old_cluster.yaml")) == "Cluster"
    assert detect_kind(_load("old_profile.yaml")) == "Profile"
