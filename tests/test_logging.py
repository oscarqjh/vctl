"""Logging configuration tests."""

from __future__ import annotations

import json
import logging

from vctl.logging import configure


def test_pretty_logging_writes_to_stderr(capsys) -> None:
    configure(level="info", fmt="pretty")
    logging.getLogger("vctl.test").info("hello pretty")
    err = capsys.readouterr().err
    assert "hello pretty" in err


def test_json_logging_emits_valid_json(capsys) -> None:
    """AT-13: stderr lines must each parse as JSON with required keys."""
    configure(level="info", fmt="json")
    logging.getLogger("vctl.test").info("hello", extra={"event": "smoke"})
    err = capsys.readouterr().err.strip().splitlines()
    assert err
    for line in err:
        rec = json.loads(line)
        assert {"ts", "level", "logger", "msg"} <= rec.keys()
        if "event" in rec:
            assert rec["event"] == "smoke"


def test_log_level_threshold(capsys) -> None:
    configure(level="warning", fmt="pretty")
    logging.getLogger("vctl.test").info("should not appear")
    logging.getLogger("vctl.test").warning("should appear")
    err = capsys.readouterr().err
    assert "should appear" in err
    assert "should not appear" not in err
