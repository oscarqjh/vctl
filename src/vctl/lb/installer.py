"""Locate or install haproxy."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)
_DEFAULT_VERSION = "3.0.7"


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
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            _LOG.warning("%s install failed: %s", tool, e.stderr.strip()[:200])
            continue
        out = _post_install_lookup()
        if out:
            return out
    return None


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
    subprocess.run(["curl", "-sSLo", f"{work}.tar.gz", src_url], check=True)
    subprocess.run(["tar", "xzf", f"{work}.tar.gz", "-C", str(work.parent)], check=True)
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
