"""Resolver tests — cluster + profile merge with env override."""

from __future__ import annotations

from pathlib import Path

import pytest

from vctl.resolver import ResolvedConfig, resolve

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Tmp dir with cluster.yaml + models/<name>.yaml."""
    cluster = tmp_path / "cluster.yaml"
    cluster.write_text((FIX / "sample_cluster.yaml").read_text())
    models = tmp_path / "models"
    models.mkdir()
    (models / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_resolve_default_profile(repo: Path) -> None:
    rc = resolve(repo / "cluster.yaml", profile=None)
    assert isinstance(rc, ResolvedConfig)
    assert rc.profile_name == "qwen3-9b"
    assert rc.model.name == "Qwen/Qwen3.5-9B"
    assert rc.lb.host == "10.0.0.1"


def test_resolve_cli_profile_wins(repo: Path) -> None:
    rc = resolve(repo / "cluster.yaml", profile="qwen3-9b")
    assert rc.profile_name == "qwen3-9b"


def test_resolve_env_override(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCTL_LB__HOST", "10.99.99.99")
    rc = resolve(repo / "cluster.yaml", profile=None)
    assert rc.lb.host == "10.99.99.99"


def test_resolve_env_merges_profile_wins(repo: Path) -> None:
    rc = resolve(repo / "cluster.yaml", profile=None)
    assert rc.env.get("VLLM_ENGINE_READY_TIMEOUT_S") == 1800


def test_resolve_model_profile_alias(repo: Path, monkeypatch) -> None:
    monkeypatch.delenv("VCTL_PROFILE", raising=False)
    monkeypatch.setenv("MODEL_PROFILE", "qwen3-9b")
    rc = resolve(repo / "cluster.yaml", profile=None)
    assert rc.profile_name == "qwen3-9b"


def test_resolved_config_is_frozen(repo: Path) -> None:
    rc = resolve(repo / "cluster.yaml", profile=None)
    with pytest.raises((AttributeError, TypeError)):
        rc.profile_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Task 2 — tctl.resolver importability
# ---------------------------------------------------------------------------


def test_tctl_resolver_importable() -> None:
    from tctl.resolver import resolve as tctl_resolve

    assert callable(tctl_resolve)
