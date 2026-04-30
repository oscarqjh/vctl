"""Health-probe helpers for vllm endpoints."""

from __future__ import annotations

import os
from typing import TypedDict

import httpx


class ProbeResult(TypedDict, total=False):
    healthy: bool
    health_code: int
    models_loaded: bool
    num_requests_running: float


def probe_local_vllm(port: int, timeout: float = 2.0) -> ProbeResult:
    """Test override hook: VCTL_TEST_PROBE_RESULT={ok,empty,unhealthy}."""
    sentinel = os.environ.get("VCTL_TEST_PROBE_RESULT")
    if sentinel == "ok":
        return {
            "healthy": True,
            "health_code": 200,
            "models_loaded": True,
            "num_requests_running": 0.0,
        }
    if sentinel == "empty":
        return {
            "healthy": False,
            "health_code": 200,
            "models_loaded": False,
            "num_requests_running": 0.0,
        }
    if sentinel == "unhealthy":
        return {
            "healthy": False,
            "health_code": 503,
            "models_loaded": False,
            "num_requests_running": 0.0,
        }
    out: ProbeResult = {"healthy": False}
    try:
        with httpx.Client(timeout=timeout) as cli:
            h = cli.get(f"http://localhost:{port}/health")
            out["health_code"] = h.status_code
            m = cli.get(f"http://localhost:{port}/v1/models")
            data = m.json().get("data", [])
            out["models_loaded"] = bool(data)
            try:
                metrics = cli.get(f"http://localhost:{port}/metrics").text
                for line in metrics.splitlines():
                    if line.startswith("vllm:num_requests_running "):
                        out["num_requests_running"] = float(line.split()[1])
            except Exception:
                out["num_requests_running"] = 0.0
        out["healthy"] = out.get("health_code") == 200 and out.get("models_loaded", False)
    except Exception:
        pass
    return out
