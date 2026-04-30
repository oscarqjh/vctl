"""Convert old prototype configs into vctl/v1 shape."""

from __future__ import annotations

from typing import Any, Literal

NewDoc = dict[str, Any]


def detect_kind(doc: dict[str, Any]) -> Literal["Cluster", "Profile"]:
    if "model" in doc and "lb" not in doc:
        return "Profile"
    if "lb" in doc:
        return "Cluster"
    raise ValueError("cannot determine kind: doc has neither 'lb' nor 'model'")


def migrate_cluster(old: dict[str, Any]) -> NewDoc:
    if old.get("apiVersion") == "vctl/v1" and old.get("kind") == "Cluster":
        return old
    lb_old = old.get("lb", {}) or {}
    return {
        "apiVersion": "vctl/v1",
        "kind": "Cluster",
        "cluster": {
            "venv": old.get("venv", ""),
            "state_dir": lb_old.get("state_dir", old.get("state_dir", "")),
            "env": old.get("env", {}) or {},
        },
        "profile": old.get("profile", ""),
        "lb": {
            "kind": "haproxy",
            "host": lb_old.get("host", ""),
            "client": {"bind_port": lb_old.get("bind_port", 8080)},
            "admin": {"bind_port": lb_old.get("admin_port", 9001)},
            "stats": {"bind_port": lb_old.get("stats_port", 9000)},
            "algorithm": lb_old.get("algorithm", "leastconn"),
            "health": {
                "path": lb_old.get("health_path", "/health"),
                "check_interval": lb_old.get("check_interval", "5s"),
                "fall": lb_old.get("fall", 3),
                "rise": lb_old.get("rise", 2),
            },
            "defaults": {
                "maxconn_per_backend": lb_old.get("maxconn_per_backend", 256),
                "slowstart": lb_old.get("slowstart", "30s"),
                "timeout_connect": lb_old.get("timeout_connect", "5s"),
                "timeout_client": lb_old.get("timeout_client", "1h"),
                "timeout_server": lb_old.get("timeout_server", "1h"),
            },
        },
    }


def migrate_profile(old: dict[str, Any]) -> NewDoc:
    if old.get("apiVersion") == "vctl/v1" and old.get("kind") == "Profile":
        return old
    model_full = old.get("model", "")
    served_as = model_full.split("/")[-1].lower().replace(".", "-") if model_full else ""
    return {
        "apiVersion": "vctl/v1",
        "kind": "Profile",
        "model": {"name": model_full, "served_as": served_as},
        "resources": {
            "num_gpus": old.get("gpus", 0),
            "cuda_visible_devices": old.get("cuda_visible_devices", ""),
        },
        "parallelism": {
            "data_parallel": old.get("dp_size", 1),
            "tensor_parallel": old.get("tp_size", 1),
            "api_server_count": old.get("api_server_count", 1),
        },
        "server": {"http_port": old.get("http_port", 8000)},
        "vllm_args": old.get("vllm_args", {}) or {},
        "env": old.get("env", {}) or {},
    }


def dump_yaml(doc: NewDoc, *, prefer_ruamel: bool = True) -> str:
    """Best-effort comment-preserving dump (ruamel) with pyyaml fallback."""
    if prefer_ruamel:
        try:
            from io import StringIO

            from ruamel.yaml import YAML

            yamler = YAML()
            yamler.indent(mapping=2, sequence=4, offset=2)
            buf = StringIO()
            yamler.dump(doc, buf)
            return buf.getvalue()
        except ImportError:
            pass
    import yaml as _yaml

    return _yaml.safe_dump(doc, sort_keys=False)
