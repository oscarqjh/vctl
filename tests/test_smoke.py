"""Smoke tests for vctl CLI startup."""

from __future__ import annotations

import subprocess
import sys
import time


def test_help_runs_quickly() -> None:
    """`vctl --help` must complete in <200 ms (AT-1).

    We invoke via `python -m vctl` so we never accidentally measure a
    different `vctl` shim from PATH.
    """
    args = [sys.executable, "-m", "vctl"]
    t0 = time.perf_counter()
    proc = subprocess.run([*args, "--help"], capture_output=True, text=True, timeout=5)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert proc.returncode == 0, proc.stderr
    assert "vctl" in proc.stdout.lower()
    assert elapsed_ms < 400, f"help took {elapsed_ms:.0f} ms (>400 ms budget)"


def test_module_entry_point() -> None:
    """`python -m vctl --help` must work."""
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0


def test_changelog_has_v0_1_0_section() -> None:
    """AT-14: CHANGELOG follows Keep a Changelog with version entries present."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "CHANGELOG.md").read_text()
    assert "## [0.1.0]" in text
    assert "## [0.2.0]" in text
    assert any(h in text for h in ("### Added", "### Changed", "### Fixed"))


def test_pyproject_version_matches_module_version() -> None:
    from pathlib import Path  # noqa: PLC0415

    import tomllib

    repo = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
    pkg_version = pyproject["project"]["version"]
    from vctl import __version__

    assert pkg_version == __version__ == "0.4.6"
