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


# ===========================================================================
# Task 2: pre-scan + confirmation prompt
# ===========================================================================


# AT-6: --dry-run reports + exits 0 without deletion
def test_at6_dry_run_no_deletion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse as _ap

    target = tmp_path / "deep" / "subdir"
    target.mkdir(parents=True)
    (target / "file1.txt").write_text("hello")
    (target / "file2.txt").write_text("world")

    ns = _ap.Namespace()
    rc = run(ns, [str(target), "--dry-run"])

    assert rc == 0
    assert target.exists()  # not deleted
    assert (target / "file1.txt").exists()
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.err
    assert "would delete" in captured.err.lower() or "would" in captured.err.lower()


def test_dry_run_quiet_skips_scan(tmp_path: Path) -> None:
    import argparse as _ap

    target = tmp_path / "deep" / "sub"
    target.mkdir(parents=True)
    (target / "f").write_text("x")

    ns = _ap.Namespace()
    rc = run(ns, [str(target), "--dry-run", "-q"])
    assert rc == 0
    assert target.exists()


# Confirm prompt: EOFError treated as N
def test_confirm_eof_aborts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tctl.commands.fast_rm import _confirm

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise_eof)

    ok = _confirm(
        paths=[Path("/tmp/deep/x")],
        file_count=100,
        total_bytes=12345,
        jobs=4,
        invalid_count=0,
        assume_yes=False,
        quiet=False,
    )
    assert ok is False
    captured = capsys.readouterr()
    assert "no tty" in captured.err.lower() or "aborted" in captured.err.lower()


# Confirm prompt: -y bypasses (no input() call)
def test_confirm_assume_yes_returns_true_without_input(monkeypatch: pytest.MonkeyPatch) -> None:
    from tctl.commands.fast_rm import _confirm

    called: list[str] = []
    monkeypatch.setattr("builtins.input", lambda p="": called.append(p) or "")

    ok = _confirm(
        paths=[Path("/tmp/x")],
        file_count=0,
        total_bytes=0,
        jobs=4,
        invalid_count=0,
        assume_yes=True,
        quiet=False,
    )
    assert ok is True
    assert called == []  # input() never invoked


# Confirm prompt: y/yes/Y/YES all accepted
@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_confirm_accepts_y_variants(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    from tctl.commands.fast_rm import _confirm

    monkeypatch.setattr("builtins.input", lambda p="": answer)
    ok = _confirm([Path("/tmp/x")], 0, 0, 4, 0, False, False)
    assert ok is True


# Confirm prompt: anything else rejects
@pytest.mark.parametrize("answer", ["n", "N", "no", "", "xyz"])
def test_confirm_rejects_non_y(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    from tctl.commands.fast_rm import _confirm

    monkeypatch.setattr("builtins.input", lambda p="": answer)
    ok = _confirm([Path("/tmp/x")], 0, 0, 4, 0, False, False)
    assert ok is False


# _fmt_bytes
@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0B"),
        (1023, "1023B"),
        (1024, "1.0KiB"),
        (1536, "1.5KiB"),
        (1048576, "1.0MiB"),
        (1572864, "1.5MiB"),
        (5_368_709_120, "5.0GiB"),
    ],
)
def test_fmt_bytes(n: int, expected: str) -> None:
    from tctl.commands.fast_rm import _fmt_bytes

    assert _fmt_bytes(n) == expected


# _prescan: real tmp tree
def test_prescan_counts_files_and_bytes(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _prescan

    target = tmp_path / "deep" / "x"
    target.mkdir(parents=True)
    (target / "a").write_text("hello")  # 5 bytes
    (target / "b").write_text("world!")  # 6 bytes
    sub = target / "sub"
    sub.mkdir()
    (sub / "c").write_text("xyz")  # 3 bytes

    file_count, total_bytes = _prescan([target])
    assert file_count == 3
    assert total_bytes >= 14  # du -sb may include dir entries; lenient check


# ===========================================================================
# Task 3: _rename_for_deletion + _delete_one + _pipe_find_xargs_rm
# ===========================================================================


# ---------------------------------------------------------------------------
# _rename_for_deletion: EXDEV fallback
# ---------------------------------------------------------------------------


def test_rename_for_deletion_falls_back_on_exdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import errno as _errno

    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "deep" / "subdir"
    target.mkdir(parents=True)

    def fail_exdev(src: object, dst: object) -> None:
        raise OSError(_errno.EXDEV, "cross-device link")

    monkeypatch.setattr("tctl.commands.fast_rm.os.rename", fail_exdev)

    import logging

    with caplog.at_level(logging.WARNING, logger="tctl.commands.fast_rm"):
        result = _rename_for_deletion(target, "ab12cd")

    assert result == target  # fallback to original
    assert "cross-filesystem" in caplog.text


# ---------------------------------------------------------------------------
# _rename_for_deletion: EEXIST collision fallback
# ---------------------------------------------------------------------------


def test_rename_for_deletion_falls_back_on_eexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import errno as _errno

    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "deep" / "subdir"
    target.mkdir(parents=True)

    def fail_eexist(src: object, dst: object) -> None:
        raise OSError(_errno.EEXIST, "file exists")

    monkeypatch.setattr("tctl.commands.fast_rm.os.rename", fail_eexist)

    import logging

    with caplog.at_level(logging.WARNING, logger="tctl.commands.fast_rm"):
        result = _rename_for_deletion(target, "ab12cd")

    assert result == target
    assert "stale" in caplog.text.lower()


# ---------------------------------------------------------------------------
# _rename_for_deletion: arbitrary OSError re-raised
# ---------------------------------------------------------------------------


def test_rename_for_deletion_reraises_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno as _errno

    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "deep" / "subdir"
    target.mkdir(parents=True)

    def fail_eio(src: object, dst: object) -> None:
        raise OSError(_errno.EIO, "I/O error")

    monkeypatch.setattr("tctl.commands.fast_rm.os.rename", fail_eio)

    with pytest.raises(OSError) as exc_info:
        _rename_for_deletion(target, "ab12cd")
    assert exc_info.value.errno == _errno.EIO


# ---------------------------------------------------------------------------
# _rename_for_deletion: success path
# ---------------------------------------------------------------------------


def test_rename_for_deletion_succeeds(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "deep" / "to_delete"
    target.mkdir(parents=True)
    (target / "marker").write_text("x")

    renamed = _rename_for_deletion(target, "abcdef")

    assert renamed.name.endswith(".deleting-abcdef")
    assert renamed.exists()
    assert not target.exists()
    assert (renamed / "marker").exists()


# ---------------------------------------------------------------------------
# AT-10: rename trick — original path disappears before deletion completes
# ---------------------------------------------------------------------------


def test_at10_rename_trick_path_disappears_immediately(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _delete_one, _rename_for_deletion

    target = tmp_path / "deep" / "instant"
    target.mkdir(parents=True)
    for i in range(10):
        (target / f"file_{i}").write_text("x" * 100)

    # Rename first — original path disappears here.
    renamed = _rename_for_deletion(target, "test1")
    assert not target.exists(), "original must be gone after rename"
    assert renamed.exists(), "renamed path must exist for deletion"

    # Now delete the renamed path.
    ok = _delete_one(renamed, jobs=2)
    assert ok
    assert not renamed.exists()


# ---------------------------------------------------------------------------
# _delete_one: end-to-end on small tree
# ---------------------------------------------------------------------------


def test_delete_one_removes_tree(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _delete_one

    target = tmp_path / "deep" / "tree"
    target.mkdir(parents=True)
    for i in range(5):
        (target / f"f{i}").write_text(str(i))
    sub = target / "sub"
    sub.mkdir()
    for i in range(3):
        (sub / f"g{i}").write_text(str(i))

    ok = _delete_one(target, jobs=2)
    assert ok
    assert not target.exists()


# ---------------------------------------------------------------------------
# _delete_one: symlinks cleaned in phase 2
# ---------------------------------------------------------------------------


def test_delete_one_removes_symlinks(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _delete_one

    target = tmp_path / "deep" / "with_links"
    target.mkdir(parents=True)
    real = tmp_path / "real.txt"
    real.write_text("real")
    (target / "link_to_real").symlink_to(real)
    (target / "file.txt").write_text("regular")

    ok = _delete_one(target, jobs=2)
    assert ok
    assert not target.exists()
    assert real.exists()  # symlink target not affected
