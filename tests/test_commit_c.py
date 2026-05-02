"""Regression tests for Commit C (C1–C12) fixes.

Each test is labelled with its backlog item so failures are easy to correlate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

FIX = Path(__file__).parent / "fixtures"


def _vctl(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=15)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


# ---------------------------------------------------------------------------
# C1 — extra="forbid" at top level
# ---------------------------------------------------------------------------


def test_c1_cluster_extra_key_rejected() -> None:
    """C1: stray top-level key in ClusterFile dict → ValidationError."""
    from pydantic import ValidationError

    from vctl.config.models import ClusterFile

    base = {
        "apiVersion": "vctl/v1",
        "kind": "Cluster",
        "cluster": {"venv": "/v", "state_dir": "/s", "env": {}},
        "profile": "x",
        "lb": {
            "kind": "haproxy",
            "host": "10.0.0.1",
            "client": {"bind_port": 8080},
            "admin": {"bind_port": 9001},
            "stats": {"bind_port": 9000},
        },
        "Profile": "typo-capital-P",  # stray key — was silently ignored before C1
    }
    with pytest.raises(ValidationError, match="extra"):
        ClusterFile.model_validate(base)


def test_c1_profile_extra_key_rejected() -> None:
    """C1: stray top-level key in ProfileFile dict → ValidationError."""
    from pydantic import ValidationError

    from vctl.config.models import ProfileFile

    base = {
        "apiVersion": "vctl/v1",
        "kind": "Profile",
        "model": {"name": "M/A"},
        "resources": {"num_gpus": 1, "cuda_visible_devices": "0"},
        "parallelism": {"data_parallel": 1, "tensor_parallel": 1, "api_server_count": 1},
        "profile": "this-should-not-be-here",  # stray key (only in ClusterFile)
    }
    with pytest.raises(ValidationError, match="extra"):
        ProfileFile.model_validate(base)


# ---------------------------------------------------------------------------
# C2 — profiles set: block-scalar and duplicate profile: lines
# ---------------------------------------------------------------------------


def test_c2_block_scalar_profile_rejected(tmp_path: Path) -> None:
    """C2: `profile: |` (block-scalar header) → exit 3."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: {venv: /v, state_dir: /s, env: {}}\n"
        "profile: |\n  multiline value here\n"
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools:\n    - {name: a, served_model: M/A, bind_port: 8080}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")}
    proc = _vctl("profiles", "set", "a", cwd=tmp_path, env=env)
    assert proc.returncode == 3
    assert "block-scalar" in proc.stderr.lower() or "block" in proc.stderr.lower()


def test_c2_duplicate_profile_key_rejected(tmp_path: Path) -> None:
    """C2: two `profile:` lines → exit 3."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: {venv: /v, state_dir: /s, env: {}}\n"
        "profile: a\n"
        "profile: b\n"  # duplicate — malformed YAML but textually present
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools:\n    - {name: a, served_model: M/A, bind_port: 8080}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")}
    proc = _vctl("profiles", "set", "a", cwd=tmp_path, env=env)
    assert proc.returncode == 3
    assert "profile" in proc.stderr.lower()


# ---------------------------------------------------------------------------
# C3 — profiles set: atomic write (tmp-file + os.replace) + UTF-8
# ---------------------------------------------------------------------------


def test_c3_atomic_write_cleans_up_tmp_on_exception(tmp_path: Path) -> None:
    """C3: if os.replace raises, tmp file must be cleaned up."""
    from vctl.commands.profiles import _set_profile

    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: {venv: /v, state_dir: /s, env: {}}\n"
        "profile: a\n"
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools:\n    - {name: a, served_model: M/A, bind_port: 8080}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    (tmp_path / "models" / "b.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")

    original_count = len(list(tmp_path.glob("*.tmp")))

    with (
        patch("os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        _set_profile(tmp_path / "cluster.yaml", "b")

    # No stray .tmp files left.
    assert len(list(tmp_path.glob("*.tmp"))) == original_count


# ---------------------------------------------------------------------------
# C4 — config migrate: dry-run default, .bak, --force
# ---------------------------------------------------------------------------


def test_c4_migrate_dryrun_leaves_file_unchanged(tmp_path: Path) -> None:
    """C4: migrate without --write → diff on stdout, file untouched."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    proc = _vctl("config", "migrate", str(src))
    assert proc.returncode == 0
    assert src.read_text() == original
    assert not src.with_suffix(".yaml.bak").exists()


def test_c4_migrate_write_creates_bak(tmp_path: Path) -> None:
    """C4: --write creates .bak and rewrites file."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    proc = _vctl("config", "migrate", "--write", str(src))
    assert proc.returncode == 0
    assert "apiVersion: vctl/v1" in src.read_text()
    bak = src.with_suffix(".yaml.bak")
    assert bak.exists()
    assert bak.read_text() == original


def test_c4_migrate_write_refuses_existing_bak(tmp_path: Path) -> None:
    """C4: existing .bak without --force → non-zero exit."""
    src = tmp_path / "cluster.yaml"
    src.write_text((FIX / "old_cluster.yaml").read_text())
    bak = src.with_suffix(".yaml.bak")
    bak.write_text("precious backup\n")
    proc = _vctl("config", "migrate", "--write", str(src))
    assert proc.returncode != 0
    assert bak.read_text() == "precious backup\n"


def test_c4_migrate_write_force_overwrites_bak(tmp_path: Path) -> None:
    """C4: --write --force overwrites existing .bak."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    bak = src.with_suffix(".yaml.bak")
    bak.write_text("old backup\n")
    proc = _vctl("config", "migrate", "--write", "--force", str(src))
    assert proc.returncode == 0
    assert "apiVersion: vctl/v1" in src.read_text()
    assert bak.read_text() == original


# ---------------------------------------------------------------------------
# C6 — serve/stop/preflight descriptions; --skip-preflight wired
# ---------------------------------------------------------------------------


def test_c6_skip_preflight_flag_is_wired(tmp_path: Path) -> None:
    """C6: --skip-preflight must actually skip preflight (not silently ignored).

    We verify by mocking preflight.run and checking it is NOT called when
    --skip-preflight is passed.
    """
    from vctl.commands import preflight as _preflight
    from vctl.commands import serve as _serve

    call_count = {"n": 0}

    def _mock_preflight(ns: argparse.Namespace, argv_rest: list[str]) -> int:
        call_count["n"] += 1
        return 0

    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    ns = argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile=None,
        log_level="info",
        log_format="pretty",
    )

    # With --skip-preflight: preflight.run must NOT be called.
    with (
        patch.object(_preflight, "run", _mock_preflight),
        patch("subprocess.Popen") as mock_popen,
        patch.object(_serve, "_wait_for_ready"),
        patch("vctl.commands.lb_scaling._do_add"),
    ):
        mock_proc = mock_popen.return_value
        mock_proc.wait.return_value = 0
        mock_proc.pid = 99999
        _serve.run(ns, ["--skip-preflight"])

    assert call_count["n"] == 0, (
        "--skip-preflight was set but preflight.run was still called"
    )


# ---------------------------------------------------------------------------
# C8 — exit code alignment
# ---------------------------------------------------------------------------


def test_c8_profiles_no_profile_key_exits_3(tmp_path: Path) -> None:
    """C8: no top-level `profile:` key in cluster.yaml → exit 3."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: {venv: /v, state_dir: /s, env: {}}\n"
        # NOTE: `profile:` key deliberately omitted
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools:\n    - {name: a, served_model: M/A, bind_port: 8080}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")}
    proc = _vctl("profiles", "set", "a", cwd=tmp_path, env=env)
    assert proc.returncode == 3


def test_c8_preflight_failure_exits_4(tmp_path: Path) -> None:
    """C8: any preflight check failure → exit 4."""
    from vctl.commands.preflight import run

    _make_repo(tmp_path)
    ns = argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile=None,
        log_level="info",
        log_format="pretty",
    )
    # Force ALL checks to fail by patching helpers.
    with (
        patch("vctl.commands.preflight._check_gpus", return_value=(False, "no gpu")),
        patch("vctl.commands.preflight._check_shm", return_value=(False, "no shm")),
        patch("vctl.commands.preflight._check_venv", return_value=(False, "no venv")),
        patch("vctl.commands.preflight._check_lb_route", return_value=(False, "no lb")),
    ):
        rc = run(ns, [])
    assert rc == 4


def test_c8_lb_wait_ready_timeout_exits_4(tmp_path: Path) -> None:
    """C8: lb wait-ready timeout → exit 4."""
    _make_repo(tmp_path)
    proc = _vctl(
        "lb",
        "wait-ready",
        "1",
        cwd=tmp_path,
        env={
            **os.environ,
            "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml"),
            "LB_WAIT_TIMEOUT": "1",
            "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        },
    )
    assert proc.returncode == 4


# ---------------------------------------------------------------------------
# C9 — narrow FileNotFoundError catch
# ---------------------------------------------------------------------------


def test_c9_missing_profile_yaml_not_exit_2(tmp_path: Path) -> None:
    """C9: missing profile .yaml (not cluster.yaml) must not return exit 2."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: {venv: /v, state_dir: /s, env: {}}\n"
        "profile: nonexistent\n"
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools:\n    - {name: a, served_model: M/A, bind_port: 8080}\n"
    )
    # No models/ dir at all — profile YAML will not be found.
    env = {**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")}
    proc = _vctl("info", cwd=tmp_path, env=env)
    # Missing profile → FileNotFoundError from resolver; must NOT return 2
    # (exit 2 is reserved for missing cluster.yaml).
    assert proc.returncode != 2


def test_c9_missing_cluster_yaml_exits_2(tmp_path: Path) -> None:
    """C9: missing cluster.yaml → exit 2."""
    env = {
        **os.environ,
        "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml"),
    }
    proc = _vctl("info", cwd=tmp_path, env=env)
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# C10 — lb where multi-pool
# ---------------------------------------------------------------------------


def _make_two_pool_repo(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        f"cluster: {{venv: /v, state_dir: {state_dir}, env: {{}}}}\n"
        "profile: a\n"
        "lb:\n  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  algorithm: leastconn\n"
        "  health: {path: /health, check_interval: 5s, fall: 3, rise: 2}\n"
        "  defaults: {maxconn_per_backend: 256, slowstart: 30s,"
        " timeout_connect: 5s, timeout_client: 1h, timeout_server: 1h}\n"
        "  pools:\n"
        "    - {name: a, served_model: M/A, bind_port: 8080}\n"
        "    - {name: b, served_model: M/B, bind_port: 8081}\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Profile\n"
        "model: {name: M/A}\n"
        "resources: {num_gpus: 1, cuda_visible_devices: '0'}\n"
        "parallelism: {data_parallel: 1, tensor_parallel: 1, api_server_count: 1}\n"
        "server: {http_port: 8000}\nvllm_args: {}\nenv: {}\n"
    )
    return tmp_path


def test_c10_single_pool_where_one_line(tmp_path: Path) -> None:
    """C10: single pool → one line output."""
    repo = _make_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("lb", "where", cwd=repo, env=env)
    assert proc.returncode == 0
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "10.0.0.1:8080" in lines[0]


def test_c10_multi_pool_where_one_line_per_pool(tmp_path: Path) -> None:
    """C10: two pools → two tab-separated lines."""
    repo = _make_two_pool_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("lb", "where", cwd=repo, env=env)
    assert proc.returncode == 0
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert any("a\t10.0.0.1:8080" in ln for ln in lines)
    assert any("b\t10.0.0.1:8081" in ln for ln in lines)


def test_c10_where_pool_filter_match(tmp_path: Path) -> None:
    """C10: --pool <name> prints only matching pool's host:port."""
    repo = _make_two_pool_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("lb", "where", "--pool", "b", cwd=repo, env=env)
    assert proc.returncode == 0
    assert "10.0.0.1:8081" in proc.stdout
    assert "10.0.0.1:8080" not in proc.stdout


def test_c10_where_pool_filter_missing_exits_3(tmp_path: Path) -> None:
    """C10: --pool <name> with unknown pool → exit 3."""
    repo = _make_two_pool_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("lb", "where", "--pool", "zzz", cwd=repo, env=env)
    assert proc.returncode == 3


# ---------------------------------------------------------------------------
# C11 — init-config pre-flight existence sweep
# ---------------------------------------------------------------------------


def test_c11_partial_existence_no_force_exits_2_no_writes(tmp_path: Path) -> None:
    """C11: model profile exists but cluster.yaml doesn't → exit 2, no cluster.yaml created."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3_5-9b.yaml").write_text("existing: true\n")

    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "init-config", "--dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    # cluster.yaml must NOT have been created.
    assert not (tmp_path / "cluster.yaml").exists()


def test_c11_all_exist_no_force_exits_2(tmp_path: Path) -> None:
    """C11: all target files exist, no --force → exit 2."""
    (tmp_path / "cluster.yaml").write_text("existing cluster\n")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3_5-9b.yaml").write_text("existing profile\n")
    (tmp_path / "models" / "qwen3-vl-30b-a3b.yaml").write_text("existing profile 2\n")

    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "init-config", "--dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    # Files must be unchanged.
    assert (tmp_path / "cluster.yaml").read_text() == "existing cluster\n"


def test_c11_force_overwrites_all(tmp_path: Path) -> None:
    """C11: --force proceeds even when all targets exist."""
    (tmp_path / "cluster.yaml").write_text("old\n")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3_5-9b.yaml").write_text("old profile\n")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vctl",
            "init-config",
            "--dir",
            str(tmp_path),
            "--force",
            "--profiles",
            "qwen3_5-9b",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "apiVersion: vctl/v1" in (tmp_path / "cluster.yaml").read_text()


# ---------------------------------------------------------------------------
# C12 — templates: no site-specific paths
# ---------------------------------------------------------------------------


def test_c12_template_no_mnt_umm_paths() -> None:
    """C12: generated template must not contain /mnt/umm or /mnt/aigc."""
    from vctl.commands.templates import (
        CLUSTER_TEMPLATE,
        QWEN3_5_9B_PROFILE,
        QWEN3_VL_30B_A3B_PROFILE,
    )

    for text in (CLUSTER_TEMPLATE, QWEN3_5_9B_PROFILE, QWEN3_VL_30B_A3B_PROFILE):
        assert "/mnt/umm" not in text, "template still contains /mnt/umm"
        assert "/mnt/aigc" not in text, "template still contains /mnt/aigc"


def test_c12_template_contains_edit_me_markers() -> None:
    """C12: template must contain EDIT_ME sentinels for site-specific values."""
    from vctl.commands.templates import CLUSTER_TEMPLATE

    assert "EDIT_ME" in CLUSTER_TEMPLATE
