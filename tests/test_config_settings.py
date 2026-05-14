"""Settings + YAML loader tests — tctl/v1 schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# tctl config settings tests
# ---------------------------------------------------------------------------


def _minimal_tctl_cluster_file():  # type: ignore[return]
    from tctl.config.models import ClusterFile, ClusterSection, LbHaproxy, VllmCluster

    return ClusterFile(
        apiVersion="tctl/v1",
        cluster=ClusterSection(venv="/venv", state_dir="/tmp/state"),
        haproxy=LbHaproxy(
            kind="haproxy",
            host="127.0.0.1",
            admin={"bind_port": 9001},
            stats={"bind_port": 9000},
            pools=[{"name": "default", "bind_port": 8000, "served_model": "*"}],
        ),
        vllm=VllmCluster(default_profile=None),
    )


def test_tctl_profile_env_var_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TCTL_PROFILE", "myprofile")
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name

    assert resolve_profile_name(None, _minimal_tctl_cluster_file()) == "myprofile"


def test_tctl_model_profile_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROFILE", "oldprofile")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name

    assert resolve_profile_name(None, _minimal_tctl_cluster_file()) is None


def test_tctl_cluster_default_profile_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name

    cf = _minimal_tctl_cluster_file()
    object.__setattr__(cf.vllm, "default_profile", "cluster-default")
    assert resolve_profile_name(None, cf) == "cluster-default"


def test_tctl_env_prefix_applies() -> None:
    from tctl.config.settings import _apply_env_overrides

    env = {"TCTL_CLUSTER__VENV": "/new/venv"}
    result = _apply_env_overrides(
        {"cluster": {"venv": "/old"}},
        environ=env,
        allowed_toplevel=frozenset({"cluster", "haproxy", "vllm"}),
    )
    assert result["cluster"]["venv"] == "/new/venv"


def test_tctl_vctl_prefix_not_applied() -> None:
    from tctl.config.settings import _apply_env_overrides

    env = {"VCTL_CLUSTER__VENV": "/should-be-ignored"}
    result = _apply_env_overrides(
        {"cluster": {"venv": "/original"}},
        environ=env,
        allowed_toplevel=frozenset({"cluster", "haproxy", "vllm"}),
    )
    assert result["cluster"]["venv"] == "/original"


def test_tctl_load_cluster_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from tctl.config.settings import load_cluster_file as tctl_load_cluster_file

    cf = tctl_load_cluster_file(FIX / "tctl_sample_cluster.yaml")
    assert cf.haproxy.host == "10.0.0.1"
    assert cf.vllm.default_profile == "qwen3-9b"


def test_tctl_env_override_haproxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TCTL_HAPROXY__HOST", "10.99.99.99")
    from tctl.config.settings import load_cluster_file as tctl_load_cluster_file

    cf = tctl_load_cluster_file(FIX / "tctl_sample_cluster.yaml")
    assert cf.haproxy.host == "10.99.99.99"


def test_tctl_schema_export_emits_json() -> None:
    from tctl.config.settings import cluster_json_schema, profile_json_schema

    schema = json.dumps(cluster_json_schema())
    assert "ClusterFile" in schema or "apiVersion" in schema
    assert "ProfileFile" in json.dumps(profile_json_schema()) or "Profile" in json.dumps(
        profile_json_schema()
    )
