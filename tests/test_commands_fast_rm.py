"""Tests for tctl.commands.fast_rm validation + argparse."""

from __future__ import annotations

from pathlib import Path

import pytest

from tctl.commands.fast_rm import (
    _DANGEROUS_LITERALS,
    _SYSTEM_PATHS,
    _load_list_file,
    _validate_path,
    run,
)


# AT-2: dangerous literal rejected
@pytest.mark.parametrize("bad", sorted(_DANGEROUS_LITERALS))
def test_at2_validate_rejects_dangerous_literals(bad: str) -> None:
    assert _validate_path(bad) is None


# AT-3: system path rejected
@pytest.mark.parametrize("sys_path", sorted(_SYSTEM_PATHS))
def test_at3_validate_rejects_system_path(sys_path: str) -> None:
    assert _validate_path(sys_path) is None


# AT-4: shallow path rejected (<3 segments)
def test_at4_validate_rejects_shallow_path(tmp_path: Path) -> None:
    # /tmp/<n>/ — only 2 segments. But /tmp is in deny list anyway; need to
    # construct a path under a 3+-segment ancestor that is itself 2-segment.
    # Easier: directly construct a path string and verify it's rejected.
    # Note: tmp_path is typically /tmp/pytest-... which is >=3 segments.
    # Build a fake 2-segment dir on a writable mount.
    # Use a real existing /opt dir? Or just check the literal "/a/b".
    # Actually /a/b doesn't exist so it fails on "does not exist" before
    # segment check. Need an existing-but-shallow path.
    # Workaround: test the segment check logic in isolation.
    from tctl.commands.fast_rm import _count_segments

    assert _count_segments("/a") < 3
    assert _count_segments("/a/b") < 3
    assert _count_segments("/a/b/c") >= 3


def test_validate_accepts_real_deep_dir(tmp_path: Path) -> None:
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    # tmp_path is typically /tmp/pytest-of-USER/pytest-NNN/test_NAME0 which
    # already has 4+ segments. Adding x/y gives 6+.
    abs_p = _validate_path(str(deep))
    assert abs_p is not None
    assert abs_p == deep.resolve()


# AT-5: list-file parsing
def test_at5_list_file_parses_with_comments_and_blanks(tmp_path: Path) -> None:
    lf = tmp_path / "list.txt"
    lf.write_text(
        "\n".join(
            [
                "# comment line",
                "/path/one",
                "",
                "  ",
                "/path/two",
                "# another comment",
                "/path/three\r",  # CRLF line ending
                "",
            ]
        )
    )
    paths = _load_list_file(lf)
    assert paths == ["/path/one", "/path/two", "/path/three"]


# Smoke: argparse builds + accepts all flags
def test_subparser_builds_with_all_flags() -> None:
    from tctl.commands.fast_rm import _build_subparser

    parser = _build_subparser()
    parsed = parser.parse_args(
        [
            "/tmp/x",
            "/tmp/y",
            "-j",
            "16",
            "-y",
            "-q",
            "-f",
            "/tmp/list.txt",
            "--dry-run",
            "-d",
        ]
    )
    assert parsed.paths == ["/tmp/x", "/tmp/y"]
    assert parsed.jobs == 16
    assert parsed.yes is True
    assert parsed.quiet is True
    assert parsed.list_file == "/tmp/list.txt"
    assert parsed.dry_run is True
    assert parsed.detach is True


# Smoke: long flags work
def test_subparser_accepts_long_flags() -> None:
    from tctl.commands.fast_rm import _build_subparser

    parser = _build_subparser()
    parsed = parser.parse_args(
        [
            "--jobs",
            "8",
            "--yes",
            "--quiet",
            "--list-file",
            "/tmp/l.txt",
            "--dry-run",
            "--detach",
        ]
    )
    assert parsed.jobs == 8


# Smoke: validation rejects $HOME
def test_validate_rejects_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    fake_home = tmp_path / "myhome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Use the env var by passing the absolute path
    assert _validate_path(str(fake_home)) is None
