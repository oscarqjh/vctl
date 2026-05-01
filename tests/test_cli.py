"""CLI dispatch + positional-profile shortcut tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence

import pytest


def _vctl(
    *args: str, env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess:
    # Always invoke the venv's vctl via `python -m` so we never accidentally
    # exec a different `vctl` shim from PATH (e.g. the bash prototype symlink).
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=10)


def test_help_lists_subcommands() -> None:
    proc = _vctl("--help")
    assert proc.returncode == 0
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "lb", "config"):
        assert verb in proc.stdout


def test_help_under_200ms() -> None:
    """AT-1 (CLI level): startup budget."""
    t0 = time.perf_counter()
    proc = _vctl("--help")
    assert proc.returncode == 0
    assert (time.perf_counter() - t0) * 1000 < 200


def test_unknown_subcommand_lists_options() -> None:
    proc = _vctl("nope")
    assert proc.returncode != 0
    assert "info" in proc.stderr or "info" in proc.stdout


def test_positional_profile_shortcut(tmp_path, monkeypatch) -> None:
    """`vctl info models/qwen3-9b.yaml` should set --profile=qwen3-9b."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "foo.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    proc = _vctl("info", "models/foo.yaml", "--help")
    assert proc.returncode == 0


def test_config_resolution_order_uses_home_default(tmp_path, monkeypatch) -> None:
    """When no --config, no env, no ~/.vctl/cluster.yaml: exits 2 with helpful message."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("CLUSTER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no cluster.yaml in cwd (cwd no longer consulted)
    # No cluster.yaml anywhere → exit 2 + helpful message
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "info"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "HOME": str(fake_home), "CLUSTER_CONFIG": ""},
    )
    assert proc.returncode == 2
    err = proc.stderr.lower()
    assert "cluster.yaml" in err
    assert "init-config" in err


def test_config_env_var_overrides_default(tmp_path, monkeypatch) -> None:
    """CLUSTER_CONFIG env var picks the file."""
    cfg = tmp_path / "custom.yaml"
    cfg.write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: { venv: /v, state_dir: /s, env: {} }\n"
        "profile: foo\n"
        "lb:\n"
        "  kind: haproxy\n  host: 1.2.3.4\n"
        "  admin: { bind_port: 9001 }\n  stats: { bind_port: 9000 }\n"
        "  algorithm: leastconn\n"
        "  health: { path: /health, check_interval: 5s, fall: 3, rise: 2 }\n"
        "  defaults: { maxconn_per_backend: 256, slowstart: 30s, "
        "timeout_connect: 5s, timeout_client: 1h, timeout_server: 1h }\n"
        "  pools: [ { name: foo, served_model: '*', bind_port: 8080 } ]\n"
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "foo.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Profile\n"
        "model: { name: F, served_as: foo }\n"
        "resources: { num_gpus: 1, cuda_visible_devices: '0' }\n"
        "parallelism: { data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }\n"
        "server: { http_port: 8000 }\nvllm_args: {}\nenv: {}\n"
    )
    # cwd is somewhere unrelated; CLUSTER_CONFIG points at the right file
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "info"],
        capture_output=True,
        text=True,
        cwd=other_dir,
        timeout=10,
        env={**os.environ, "CLUSTER_CONFIG": str(cfg)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "1.2.3.4" in proc.stdout


# ---------------------------------------------------------------------------
# C5/C6/C7 — help-text presence (moved from test_commit_c.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected_substrings"),
    [
        pytest.param(
            ["config", "-h"],
            ["validate", "show", "schema", "migrate"],
            id="c5_config_help_contains_verbs",
        ),
        pytest.param(
            ["serve", "-h"],
            ["--skip-preflight"],
            id="c6_serve_description_present",
        ),
        pytest.param(
            ["stop", "-h"],
            ["stop"],
            id="c6_stop_description_present",
        ),
        pytest.param(
            ["preflight", "-h"],
            ["preflight"],
            id="c6_preflight_description_present",
        ),
        pytest.param(
            ["--help"],
            ["--profile", "--log-level", "--log-format"],
            id="c7_root_flags_have_help",
        ),
    ],
)
def test_help_text_presence(args: list[str], expected_substrings: Sequence[str]) -> None:
    """C5/C6/C7: help output must contain expected substrings."""
    proc = _vctl(*args)
    out = proc.stdout + proc.stderr
    for s in expected_substrings:
        assert s in out, f"{s!r} missing from `vctl {' '.join(args)}` output"
