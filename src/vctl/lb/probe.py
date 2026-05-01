"""Health-probe helpers for vllm endpoints."""

from __future__ import annotations

import os
import re
from typing import TypedDict

import httpx


class ProbeResult(TypedDict, total=False):
    healthy: bool
    health_code: int
    models_loaded: bool
    num_requests_running: float


class VllmMetrics(TypedDict, total=False):
    running: int | None
    waiting: int | None


def probe_local_vllm(port: int, timeout: float = 2.0) -> ProbeResult:
    """Probe vllm on localhost. Kept for backward compat (attach/detach paths)."""
    return probe_vllm("localhost", port, timeout=timeout)


def probe_vllm(host: str, port: int, timeout: float = 2.0) -> ProbeResult:
    """B9: Probe vllm at an arbitrary host:port.

    Test override hook: VCTL_TEST_PROBE_RESULT={ok,empty,unhealthy}.
    """
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
            h = cli.get(f"http://{host}:{port}/health")
            out["health_code"] = h.status_code
            m = cli.get(f"http://{host}:{port}/v1/models")
            data = m.json().get("data", [])
            out["models_loaded"] = bool(data)
            try:
                metrics = cli.get(f"http://{host}:{port}/metrics").text
                for line in metrics.splitlines():
                    if line.startswith("vllm:num_requests_running "):
                        out["num_requests_running"] = float(line.split()[1])
            except Exception:
                out["num_requests_running"] = 0.0
        out["healthy"] = out.get("health_code") == 200 and out.get("models_loaded", False)
    except Exception:
        pass
    return out


# Regex to strip optional {labels} between metric name and value.
_LABEL_RE = re.compile(r"\{[^}]*\}")


def fetch_vllm_metrics(host: str, port: int, timeout: float = 2.0) -> VllmMetrics:
    """Fetch vLLM Prometheus metrics from ``/metrics``.

    Returns ``{'running': N | None, 'waiting': N | None}``.
    ``None`` on any network / parse error (never raises).

    Parses Prometheus text exposition: looks for lines starting with
    ``vllm:num_requests_running`` and ``vllm:num_requests_waiting``,
    strips optional ``{labels}`` between the metric name and the value,
    then takes the last whitespace-delimited token as a float cast to int.
    Ignores ``# HELP`` / ``# TYPE`` comment lines.

    With ``--data-parallel-size N``, vLLM emits ONE line per dp engine
    (e.g. ``vllm:num_requests_running{engine="0"} 2``). We sum across all
    engine labels so the displayed value matches the actual per-backend
    in-flight count (NOT one engine's slice).
    """
    result: VllmMetrics = {"running": None, "waiting": None}
    try:
        with httpx.Client(timeout=timeout) as cli:
            resp = cli.get(f"http://{host}:{port}/metrics")
            resp.raise_for_status()
            text = resp.text
    except Exception:
        return result

    running_sum = 0
    waiting_sum = 0
    saw_running = False
    saw_waiting = False

    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        # Strip labels block so "vllm:num_requests_running{...} 3.0" parses cleanly.
        stripped = _LABEL_RE.sub("", line)
        tokens = stripped.split()
        if len(tokens) < 2:
            continue
        name = tokens[0]
        try:
            val = int(float(tokens[-1]))
        except (ValueError, IndexError):
            continue
        if name == "vllm:num_requests_running":
            running_sum += val
            saw_running = True
        elif name == "vllm:num_requests_waiting":
            waiting_sum += val
            saw_waiting = True

    if saw_running:
        result["running"] = running_sum
    if saw_waiting:
        result["waiting"] = waiting_sum
    return result
