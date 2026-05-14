"""Resolver tests — cluster + profile merge with env override (tctl/v1 shape)."""

from __future__ import annotations

from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# tctl.resolver importability
# ---------------------------------------------------------------------------


def test_tctl_resolver_importable() -> None:
    from tctl.resolver import resolve as tctl_resolve

    assert callable(tctl_resolve)


# ---------------------------------------------------------------------------
# tctl.resolver end-to-end with tctl/v1 schema
# ---------------------------------------------------------------------------


@pytest.fixture
def tctl_repo(tmp_path: Path) -> Path:
    """Tmp dir with tctl/v1 cluster.yaml + models/<name>.yaml."""
    cluster = tmp_path / "cluster.yaml"
    cluster.write_text((FIX / "tctl_sample_cluster.yaml").read_text())
    models = tmp_path / "models"
    models.mkdir()
    (models / "qwen3-9b.yaml").write_text((FIX / "tctl_sample_profile.yaml").read_text())
    return tmp_path


def test_tctl_resolve_with_default_profile(tctl_repo: Path) -> None:
    from tctl.resolver import ResolvedConfig
    from tctl.resolver import resolve as tctl_resolve

    rc = tctl_resolve(tctl_repo / "cluster.yaml", profile=None)
    assert isinstance(rc, ResolvedConfig)
    assert rc.profile_name == "qwen3-9b"
    assert rc.model.name == "Qwen/Qwen3.5-9B"
    assert rc.lb.host == "10.0.0.1"


def test_tctl_resolve_cli_profile_wins(tctl_repo: Path) -> None:
    from tctl.resolver import resolve as tctl_resolve

    rc = tctl_resolve(tctl_repo / "cluster.yaml", profile="qwen3-9b")
    assert rc.profile_name == "qwen3-9b"


def test_tctl_resolve_env_override(tctl_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TCTL_HAPROXY__HOST", "10.99.99.99")
    from tctl.resolver import resolve as tctl_resolve

    rc = tctl_resolve(tctl_repo / "cluster.yaml", profile=None)
    assert rc.lb.host == "10.99.99.99"


def test_tctl_resolve_env_merges_profile(tctl_repo: Path) -> None:
    from tctl.resolver import resolve as tctl_resolve

    rc = tctl_resolve(tctl_repo / "cluster.yaml", profile=None)
    assert rc.env.get("VLLM_ENGINE_READY_TIMEOUT_S") == 1800


def test_tctl_resolve_model_profile_ignored(
    tctl_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MODEL_PROFILE must NOT be consulted in tctl (dropped in v0.9.0)."""
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    # Override vllm.default_profile to None so we'd fall through to MODEL_PROFILE
    # if it were consulted — but tctl should raise instead.
    import yaml

    cluster_text = (tctl_repo / "cluster.yaml").read_text()
    doc = yaml.safe_load(cluster_text)
    doc["vllm"]["default_profile"] = None
    (tctl_repo / "cluster.yaml").write_text(yaml.dump(doc))

    monkeypatch.setenv("MODEL_PROFILE", "qwen3-9b")
    from tctl.resolver import resolve as tctl_resolve

    with pytest.raises((ValueError, FileNotFoundError)):
        tctl_resolve(tctl_repo / "cluster.yaml", profile=None)


def test_tctl_resolved_config_is_frozen(tctl_repo: Path) -> None:
    from tctl.resolver import resolve as tctl_resolve

    rc = tctl_resolve(tctl_repo / "cluster.yaml", profile=None)
    with pytest.raises((AttributeError, TypeError)):
        rc.profile_name = "other"  # type: ignore[misc]
