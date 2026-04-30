"""Settings + YAML loader tests."""

from __future__ import annotations

import json
from pathlib import Path

from vctl.config.settings import (
    cluster_json_schema,
    load_cluster_file,
    load_profile_file,
    profile_json_schema,
)

FIX = Path(__file__).parent / "fixtures"


def test_load_cluster_file() -> None:
    cf = load_cluster_file(FIX / "sample_cluster.yaml")
    assert cf.lb.host == "10.0.0.1"
    assert cf.profile == "qwen3-9b"


def test_load_profile_file() -> None:
    pf = load_profile_file(FIX / "sample_profile.yaml")
    assert pf.parallelism.data_parallel == 8


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VCTL_LB__HOST", "10.99.99.99")
    cf = load_cluster_file(FIX / "sample_cluster.yaml")
    assert cf.lb.host == "10.99.99.99"


def test_nested_env_override(monkeypatch) -> None:
    monkeypatch.setenv("VCTL_LB__CLIENT__BIND_PORT", "80")
    cf = load_cluster_file(FIX / "sample_cluster.yaml")
    assert cf.lb.client.bind_port == 80


def test_schema_export_emits_json() -> None:
    schema = json.dumps(cluster_json_schema())
    assert "ClusterFile" in schema or "apiVersion" in schema
    assert "ProfileFile" in json.dumps(profile_json_schema()) or "Profile" in json.dumps(
        profile_json_schema()
    )
