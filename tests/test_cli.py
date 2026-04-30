"""CLI dispatch + positional-profile shortcut tests."""
from __future__ import annotations

import subprocess
import sys
import time


def _vctl(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Always invoke the venv's vctl via `python -m` so we never accidentally
    # exec a different `vctl` shim from PATH (e.g. the bash prototype symlink).
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)


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
