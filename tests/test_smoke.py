"""Smoke tests for tctl CLI startup."""

from __future__ import annotations

import subprocess
import sys
import time


def test_tctl_help_runs_quickly() -> None:
    """`tctl --help` must complete in <400 ms (AT-startup)."""
    args = [sys.executable, "-m", "tctl"]
    t0 = time.perf_counter()
    proc = subprocess.run([*args, "--help"], capture_output=True, text=True, timeout=5)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert proc.returncode == 0, proc.stderr
    assert "tctl" in proc.stdout.lower() or "vllm" in proc.stdout.lower()
    assert elapsed_ms < 400, f"help took {elapsed_ms:.0f} ms (>400 ms budget)"


def test_tctl_module_entry_point() -> None:
    """`python -m tctl --help` must work."""
    proc = subprocess.run(
        [sys.executable, "-m", "tctl", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0


def test_vctl_module_not_importable() -> None:
    """`python -m vctl` must fail with a non-zero exit (module gone)."""
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode != 0


def test_changelog_has_v0_1_0_section() -> None:
    """AT-14: CHANGELOG follows Keep a Changelog with version entries present."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "docs" / "CHANGELOG.md").read_text()
    assert "## [0.1.0]" in text
    assert "## [0.2.0]" in text
    assert any(h in text for h in ("### Added", "### Changed", "### Fixed"))


def test_pyproject_version_matches_module_version() -> None:
    import sys
    from pathlib import Path  # noqa: PLC0415

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomllib  # type: ignore[no-redef]
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

    repo = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
    pkg_version = pyproject["project"]["version"]
    from tctl import __version__

    assert pkg_version == __version__ == "0.9.1"


def test_tctl_package_importable() -> None:
    import importlib

    tctl = importlib.import_module("tctl")
    assert hasattr(tctl, "__version__")
    assert tctl.__version__ == "0.9.1"


def test_tctl_version_string_format() -> None:
    import tctl

    parts = tctl.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
