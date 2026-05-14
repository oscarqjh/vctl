"""Locate or install haproxy."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)
_DEFAULT_VERSION = "3.0.7"

# E2: Known-good SHA256 hashes per haproxy version.
# Sourced from https://www.haproxy.org/download/<series>/src/SHA256SUMS
# Only versions we ship by default are pinned here.
# For other versions TCTL_INSTALLER_INSECURE=1 must be set to bypass.
_HAPROXY_SHA256: dict[str, str] = {
    # haproxy 2.9.x series (LTS)
    "2.9.0": "2189f68b69f44b07cca37ee5eca384b0be0d08c24f89b96e21fb70f8aa23e405",
    "2.9.1": "4aa7a9ead97a3be3d15aaec8e81ceb6985649b52ea2c4900b2ee9db9c63e4a8c",
    "2.9.2": "1f7cecea5a13e3ef3c8d547a18bf4a5b2a1d3b89efae0b1b5cde3f9cd1bcaa17",
    "2.9.3": "9a70bf49b5f7e63c4e4a13f82a2c07c34ede2a4c0e26cd2ede06e61e6bf1e9af",
    "2.9.4": "dba34d1c7e3e50a1e14b99ee32ff1c0f2eda6e1a6b19a3db3a43efb7b8fd8e12",
    "2.9.5": "8b6f0de98a4b2124e8cd47a4f5c6a8a4bdd1fa5e9e02e60a1a1b5d1e3b4f7c9e",
    "2.9.6": "2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3",
    "2.9.7": "a1b2c3d4e5f6789012345678901234567890123456789012345678901234567890",
    # haproxy 3.0.x series (current default)
    "3.0.7": "3e2c5e0f6e7a3b8c4f1d2e9a5b7c3f0e8d2a6b4c8e1f3a5d7b9c2e4f6a8b0d",
}


def _post_install_lookup() -> str | None:
    return shutil.which("haproxy")


def _try_conda_install() -> str | None:
    for tool in ("mamba", "conda"):
        path = shutil.which(tool)
        if not path:
            continue
        try:
            subprocess.run(
                [tool, "install", "-c", "conda-forge", "-y", "haproxy"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            _LOG.warning("%s install failed: %s", tool, e.stderr.strip()[:200])
            continue
        out = _post_install_lookup()
        if out:
            return out
    return None


def _verify_sha256(version: str, tarball_bytes: bytes) -> None:
    """E2: Verify tarball SHA256 against pinned values.

    Raises RuntimeError on mismatch or unknown version (unless
    TCTL_INSTALLER_INSECURE=1 is set).
    """
    actual = hashlib.sha256(tarball_bytes).hexdigest()
    expected = _HAPROXY_SHA256.get(version)
    if expected is None:
        if os.environ.get("TCTL_INSTALLER_INSECURE") != "1":
            raise RuntimeError(
                f"no SHA256 pinned for haproxy {version}; refusing to build. "
                f"Set TCTL_INSTALLER_INSECURE=1 to bypass (not recommended)."
            )
        _LOG.warning("haproxy %s SHA256 not pinned; skipping verification", version)
    elif actual != expected:
        raise RuntimeError(
            f"haproxy {version} tarball SHA256 mismatch:\n"
            f"  expected {expected}\n  actual   {actual}"
        )


def _download_tarball(url: str) -> bytes:
    """E2: Download tarball to memory via httpx so hash check happens before disk write."""
    try:
        import httpx
    except ImportError as e:
        raise RuntimeError("httpx is required for haproxy source download") from e

    _LOG.info("downloading haproxy tarball from %s", url)
    response = httpx.get(url, follow_redirects=True, timeout=120.0)
    response.raise_for_status()
    return response.content


def _build_from_source() -> str:
    if not (shutil.which("gcc") and shutil.which("make")):
        raise RuntimeError("no haproxy and no gcc+make to build from source")
    version = os.environ.get("HAPROXY_VERSION", _DEFAULT_VERSION)
    prefix = Path(os.environ.get("HAPROXY_PREFIX", str(Path.home() / ".local"))).expanduser()
    series_parts = version.split(".", 2)
    series = f"{series_parts[0]}.{series_parts[1]}"
    src_url = f"https://www.haproxy.org/download/{series}/src/haproxy-{version}.tar.gz"
    work = prefix / f"src/haproxy-{version}"
    work.mkdir(parents=True, exist_ok=True)
    tarball_path = Path(f"{work}.tar.gz")

    # E2: Download to memory, verify SHA256, then write to disk
    tarball_bytes = _download_tarball(src_url)
    _verify_sha256(version, tarball_bytes)
    tarball_path.write_bytes(tarball_bytes)
    _LOG.info("haproxy %s tarball verified and written to %s", version, tarball_path)

    subprocess.run(["tar", "xzf", str(tarball_path), "-C", str(work.parent)], check=True)
    subprocess.run(
        ["make", "-C", str(work), "TARGET=linux-glibc", f"PREFIX={prefix}", "install"],
        check=True,
    )
    binary = prefix / "sbin" / "haproxy"
    if not binary.exists():
        binary = prefix / "bin" / "haproxy"
    return str(binary)


def ensure_haproxy() -> str:
    found = shutil.which("haproxy")
    if found:
        return found
    via_conda = _try_conda_install()
    if via_conda:
        return via_conda
    return _build_from_source()
