"""Host primitives — IP detection, `which`."""

from __future__ import annotations

import logging
import shutil
import socket

_LOG = logging.getLogger(__name__)


def detect_self_ip(probe_target: str = "8.8.8.8", probe_port: int = 80) -> str:
    """Return the IP this host would use to reach probe_target.

    Fallback chain (D5):
    1. UDP-connect probe to probe_target — works on any routed interface.
    2. ``socket.gethostbyname(socket.gethostname())`` — works on air-gapped hosts.
    3. ``"127.0.0.1"`` — last resort; logs a WARNING.
    """
    # 1. UDP connect probe
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe_target, probe_port))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # 2. gethostbyname fallback
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        pass

    # 3. Last resort
    _LOG.warning("detect_self_ip: all probes failed; falling back to 127.0.0.1")
    return "127.0.0.1"


def which(binary: str) -> str:
    found = shutil.which(binary)
    if found is None:
        raise FileNotFoundError(f"{binary!r} not on PATH")
    return found
