"""Tests for the tctl package skeleton (Task 1)."""

from __future__ import annotations


def test_tctl_version() -> None:
    import tctl

    assert tctl.__version__ == "0.9.1"


def test_tctl_workloads_importable() -> None:
    import importlib

    importlib.import_module("tctl.workloads.haproxy")
    importlib.import_module("tctl.workloads.lmms")
    importlib.import_module("tctl.workloads.vllm")


def test_tctl_commands_importable() -> None:
    import importlib

    importlib.import_module("tctl.commands")


def test_tctl_config_importable() -> None:
    import importlib

    importlib.import_module("tctl.config")


def test_tctl_py_typed_present() -> None:
    from pathlib import Path

    pkg_dir = Path(__import__("tctl").__file__).resolve().parent  # type: ignore[arg-type]
    assert (pkg_dir / "py.typed").exists()
