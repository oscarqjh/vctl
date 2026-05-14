"""tests/workloads/vllm/test_commands_readonly.py — info / args / preflight / profiles unit tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CLUSTER_YAML = """\
apiVersion: tctl/v1
cluster:
  venv: /venv
  state_dir: /tmp/state
haproxy:
  kind: haproxy
  host: 10.0.0.1
  admin: {bind_port: 9001}
  stats: {bind_port: 9000}
  pools:
    - {name: default, bind_port: 8000, served_model: '*'}
vllm:
  default_profile: qwen3-9b
"""

_PROFILE_YAML = """\
apiVersion: tctl/v1
kind: Profile
model:
  name: Qwen/Qwen3.5-9B
resources:
  num_gpus: 1
  cuda_visible_devices: "0"
parallelism:
  data_parallel: 1
  tensor_parallel: 1
server:
  http_port: 8000
vllm_args: {}
env: {}
"""


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text(_CLUSTER_YAML)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text(_PROFILE_YAML)
    return tmp_path


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_cmd_info_importable() -> None:
    from tctl.workloads.vllm.commands import _cmd_info

    assert callable(_cmd_info)


def test_cmd_info_returns_0_with_valid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(config=str(repo / "cluster.yaml"), profile="qwen3-9b")
    rc = cmds._cmd_info(ns, [])
    assert rc == 0


def test_cmd_info_no_profile_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """info without a resolvable profile returns rc=2."""
    # Write cluster.yaml with no default_profile
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: tctl/v1\n"
        "cluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_model: '*'}]\n"
        "vllm:\n  default_profile: null\n"
    )
    (tmp_path / "models").mkdir()

    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile=None)
    rc = cmds._cmd_info(ns, [])
    assert rc == 2


# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------


def test_cmd_args_importable() -> None:
    from tctl.workloads.vllm.commands import _cmd_args

    assert callable(_cmd_args)


def test_cmd_args_emits_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_repo(tmp_path)
    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(config=str(repo / "cluster.yaml"), profile="qwen3-9b")
    rc = cmds._cmd_args(ns, [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Qwen/Qwen3.5-9B" in out
    assert "--data-parallel-size" in out or "data-parallel-size" in out


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_cmd_preflight_importable() -> None:
    from tctl.workloads.vllm.commands import _cmd_preflight

    assert callable(_cmd_preflight)


def test_cmd_preflight_no_profile_returns_2(
    tmp_path: Path,
) -> None:
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: tctl/v1\n"
        "cluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_model: '*'}]\n"
        "vllm:\n  default_profile: null\n"
    )
    (tmp_path / "models").mkdir()

    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile=None, json=False)
    rc = cmds._cmd_preflight(ns, [])
    assert rc == 2


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def test_cmd_profiles_importable() -> None:
    from tctl.workloads.vllm.commands import _cmd_profiles

    assert callable(_cmd_profiles)


def test_cmd_profiles_list_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_repo(tmp_path)
    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(
        config=str(repo / "cluster.yaml"),
        profile=None,
        profiles_verb=None,
    )
    rc = cmds._cmd_profiles(ns, [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "qwen3-9b" in out


def test_cmd_profiles_set_changes_default_profile(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    # Add a second profile
    (repo / "models" / "other.yaml").write_text(_PROFILE_YAML)

    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(
        config=str(repo / "cluster.yaml"),
        profile=None,
        profiles_verb="set",
        name="other",
    )
    rc = cmds._cmd_profiles(ns, [])
    assert rc == 0


def test_cmd_profiles_set_unknown_profile_returns_3(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    from tctl.workloads.vllm import commands as cmds

    ns = argparse.Namespace(
        config=str(repo / "cluster.yaml"),
        profile=None,
        profiles_verb="set",
        name="nonexistent",
    )
    rc = cmds._cmd_profiles(ns, [])
    assert rc == 3


# ---------------------------------------------------------------------------
# register_all: importability and sub-parser count
# ---------------------------------------------------------------------------


def test_register_all_importable() -> None:
    from tctl.workloads.vllm.commands import register_all

    assert callable(register_all)


def test_all_verbs_registered(capsys: pytest.CaptureFixture[str]) -> None:
    """register_all must register 7 verbs."""
    from tctl.workloads.vllm.commands import register_all

    p = argparse.ArgumentParser(prog="tctl vllm")
    sub = p.add_subparsers(dest="verb")
    register_all(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["--help"])
    out = capsys.readouterr().out
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "rolling-restart"):
        assert verb in out, f"{verb!r} missing from tctl vllm --help"
