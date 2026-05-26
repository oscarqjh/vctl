# tctl fast-rm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development.

**Goal:** Port `EASI/scripts/fast_rm.sh` into tctl as `tctl fast-rm` platform command. Foreground default; `-d/--detach` spawns tmux session via existing TmuxSession primitive. Rename-then-rm trick for instant disappearance from listings. 11 ATs to pass.

**Architecture:** New module `src/tctl/commands/fast_rm.py`. Single command, no sub-verbs. Validation + scan + confirm in Python; deletion shells out to `find + xargs -P N rm -f` (preserves OS-level parallelism). `--detach` wraps in `TmuxSession(f"tctl-fastrm-{os.urandom(3).hex()}", log_path=...)`.

**Tech Stack:** Python 3.10+, stdlib only (subprocess, os, pathlib, argparse, errno), existing `tctl.tmux.TmuxSession`. mypy --strict, ruff E,F,W,I,B,UP,SIM,N. tmux 3.2+.

---

## File Map

### Files to create

| Path | Description |
|---|---|
| `src/tctl/commands/fast_rm.py` | Platform command implementation (~280 lines) |
| `tests/test_commands_fast_rm.py` | Unit + integration tests (~320 lines) |

### Files to modify

| Path | Change |
|---|---|
| `src/tctl/cli.py` | Add `"fast-rm": "tctl.commands.fast_rm"` to `_PLATFORM_COMMANDS` |
| `src/tctl/__init__.py` | Bump `__version__` `"0.9.0"` → `"0.9.1"` |
| `pyproject.toml` | Bump `version = "0.9.1"` |
| `docs/CLI-REFERENCE.md` | Add `tctl fast-rm` command documentation section |
| `docs/CHANGELOG.md` | Prepend `## [0.9.1] - 2026-05-26` entry |
| `README.md` | Add `tctl fast-rm` row to platform commands table |
| `tests/test_smoke.py` | Update version assertion to `"0.9.1"` |

### Files NOT modified

All workload modules (`vllm/`, `haproxy/`, `lmms/`), `config/`, `tmux.py`, all existing test files, and `src/tctl/commands/__init__.py` are untouched. This is a pure additive change.

---

## TASK ORDERING (6 tasks, single stream)

---

### Task 1: argparse skeleton + validation helpers + CLI registration

**What we build:** New module `src/tctl/commands/fast_rm.py` with the argparse parser, the two validation helpers (`_validate_one`, `_read_list_file`), a skeleton `run()` that validates + returns 0/2, and the one-line registration in `cli.py`. Tests cover all validation ATs (AT-2, AT-3, AT-4, AT-5) plus a `--help` smoke test.

**Files:**
- Create: `src/tctl/commands/fast_rm.py`
- Create: `tests/test_commands_fast_rm.py`
- Modify: `src/tctl/cli.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_commands_fast_rm.py`:

```python
"""Tests for tctl fast-rm platform command."""
from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# AT-2 — dangerous literals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_fragment", [
    ("",    "dangerous literal"),
    (".",   "dangerous literal"),
    ("..",  "dangerous literal"),
    ("~",   "dangerous literal"),
    ("/",   "dangerous literal"),
    ("/*",  "dangerous literal"),
])
def test_at2_dangerous_literal_rejected(raw: str, expected_fragment: str) -> None:
    from tctl.commands.fast_rm import _validate_one
    abs_path, reason = _validate_one(raw)
    assert abs_path is None
    assert expected_fragment in (reason or "")


# AT-2 integration: run() returns 2 with stderr message
def test_at2_run_returns_2_on_dangerous_literal(capsys: pytest.CaptureFixture[str]) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[".."], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "dangerous" in err.lower()


# ---------------------------------------------------------------------------
# AT-3 — system paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_fragment", [
    ("/home",  "system path"),
    ("/etc",   "system path"),
    ("/tmp",   "system path"),
    ("/mnt",   "system path"),
    ("/usr",   "system path"),
])
def test_at3_system_path_rejected(raw: str, expected_fragment: str) -> None:
    from tctl.commands.fast_rm import _validate_one
    abs_path, reason = _validate_one(raw)
    assert abs_path is None
    assert expected_fragment in (reason or "")


def test_at3_run_returns_2_on_system_path(capsys: pytest.CaptureFixture[str]) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=["/home"], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "system path" in err.lower() or "/home" in err


# ---------------------------------------------------------------------------
# AT-4 — shallow path (< 3 segments)
# ---------------------------------------------------------------------------

def test_at4_shallow_path_rejected_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tctl.commands import fast_rm as mod
    target = tmp_path / "a" / "b" / "c" / "real_target"
    target.mkdir(parents=True)

    original_validate = mod._validate_one

    def shallow_validate(raw: str) -> tuple[str | None, str | None]:
        if raw == str(target):
            return None, "too shallow (2 segment(s); need ≥3)"
        return original_validate(raw)

    monkeypatch.setattr(mod, "_validate_one", shallow_validate)

    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = mod.run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "shallow" in err.lower() or "segment" in err.lower()


def test_validate_rejects_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _validate_one
    monkeypatch.setenv("HOME", str(tmp_path))
    abs_path, reason = _validate_one(str(tmp_path))
    assert abs_path is None
    assert "$HOME" in (reason or "")


def test_validate_accepts_deep_directory(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _validate_one
    deep = tmp_path / "a" / "b" / "target"
    deep.mkdir(parents=True)
    abs_path, reason = _validate_one(str(deep))
    assert abs_path is not None, f"expected success but got reason={reason!r}"
    assert reason is None


def test_validate_rejects_file(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _validate_one
    f = tmp_path / "x" / "y" / "z" / "notadir.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    abs_path, reason = _validate_one(str(f))
    assert abs_path is None
    assert "not a directory" in (reason or "")


def test_validate_deduplicates_paths(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _validate_one
    deep = tmp_path / "a" / "b" / "target"
    deep.mkdir(parents=True)
    abs1, _ = _validate_one(str(deep))
    abs2, _ = _validate_one(str(deep))
    assert abs1 == abs2  # same resolved path; caller deduplicates


# ---------------------------------------------------------------------------
# AT-5 — list file parsing
# ---------------------------------------------------------------------------

def test_at5_list_file_reads_and_ignores_comments(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _read_list_file
    list_file = tmp_path / "paths.txt"
    list_file.write_text(
        "# comment\n"
        "\n"
        "  /mnt/eval/run_a  \n"
        "/mnt/eval/run_b\r\n"
        "# another comment\n"
        "/mnt/eval/run_c\n",
    )
    result = _read_list_file(str(list_file))
    assert result == ["/mnt/eval/run_a", "/mnt/eval/run_b", "/mnt/eval/run_c"]


def test_list_file_missing_exits_2(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[], list_file=str(tmp_path / "nonexistent.txt"),
        jobs=1, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2


def test_list_file_combined_with_positional(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _read_list_file
    list_file = tmp_path / "extra.txt"
    list_file.write_text("/mnt/eval/from_file\n")
    result = _read_list_file(str(list_file))
    assert result == ["/mnt/eval/from_file"]


# ---------------------------------------------------------------------------
# Smoke: --help exits 0
# ---------------------------------------------------------------------------

def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["fast-rm", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--jobs", "--yes", "--quiet", "--list-file", "--dry-run", "--detach"):
        assert flag in out, f"expected {flag!r} in --help output"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl.commands.fast_rm'`

- [ ] **Step 3: Implement skeleton + registration**

Create `src/tctl/commands/fast_rm.py`:

```python
"""`tctl fast-rm` — fast parallel deletion for directories with many small files."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants (mirrors fast_rm.sh validate_path exactly)
# ---------------------------------------------------------------------------

_DANGEROUS_LITERALS: frozenset[str] = frozenset({"", ".", "..", "~", "/", "/*"})

_SYSTEM_PATHS: frozenset[str] = frozenset({
    "/",
    "/home", "/root", "/etc", "/var", "/usr",
    "/bin", "/sbin", "/lib", "/lib64",
    "/mnt", "/proc", "/sys", "/dev", "/boot", "/tmp",
})


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_one(raw: str) -> tuple[str | None, str | None]:
    """Validate a single path candidate.

    Returns (resolved_abs_path, None) on success, or (None, reason) on failure.
    Dangerous-literal and system-path checks happen before any stat call.
    """
    if raw in _DANGEROUS_LITERALS:
        return None, f"dangerous literal: {raw!r}"

    p = Path(raw)

    if not p.exists():
        return None, "does not exist"
    if not p.is_dir():
        return None, "not a directory (regular file or symlink-to-file)"

    try:
        abs_path = str(p.resolve())
    except OSError as exc:
        return None, f"cannot resolve path: {exc}"

    if abs_path in _SYSTEM_PATHS:
        return None, f"system path: {abs_path}"

    home = str(Path.home())
    if abs_path == home:
        return None, "$HOME"

    segments = [s for s in abs_path.split("/") if s]
    if len(segments) < 3:
        return None, f"too shallow ({len(segments)} segment(s); need ≥3)"

    return abs_path, None


def _read_list_file(path: str) -> list[str]:
    """Read candidate paths from a list file.

    Each line is CR-stripped, whitespace-stripped, then blank lines and
    lines starting with '#' are skipped. Remaining lines are returned as-is.

    Raises FileNotFoundError if path does not exist (caller handles exit 2).
    """
    lines: list[str] = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tctl fast-rm",
        description=(
            "Fast parallel deletion for directories with many small files.\n\n"
            "Strategy: Phase 1 deletes files in parallel (find | xargs -P N rm -f);\n"
            "Phase 2 prunes the empty directory skeleton (find -depth -empty -delete).\n"
            "Rename-then-rm makes each path disappear from listings immediately."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="One or more directories to delete.",
    )
    p.add_argument(
        "-f", "--list-file",
        metavar="FILE",
        help=(
            "Read paths from FILE (one per line). Blank lines and '#' comments ignored. "
            "May be combined with positional PATH args."
        ),
    )
    p.add_argument(
        "-j", "--jobs",
        type=int,
        default=None,
        metavar="N",
        help="Parallel rm jobs (default: nproc, max useful ~16-32).",
    )
    p.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    p.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Skip pre-scan file-count and size (useful on cold-cache network mounts).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + scan + report; exit 0 without deleting anything.",
    )
    p.add_argument(
        "-d", "--detach",
        action="store_true",
        help=(
            "Spawn deletion in a background tmux session (tctl-fastrm-<id>). "
            "Returns immediately. Log at ~/.tctl/fastrm/<id>.log."
        ),
    )
    return p


# ---------------------------------------------------------------------------
# Entry point (skeleton — validation + early returns only in Task 1)
# ---------------------------------------------------------------------------

def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl fast-rm`."""
    # Resolve job count: default to nproc or 8
    jobs = ns.jobs if ns.jobs is not None else (os.cpu_count() or 8)

    # Collect raw candidates from positional args + optional list file
    candidates: list[str] = list(ns.paths or [])

    if ns.list_file:
        if not os.path.exists(ns.list_file):
            print(
                f"fast-rm: list file does not exist: {ns.list_file}",
                file=sys.stderr,
            )
            return 2
        try:
            candidates.extend(_read_list_file(ns.list_file))
        except OSError as exc:
            print(f"fast-rm: cannot read list file: {exc}", file=sys.stderr)
            return 2

    if not candidates:
        print(
            "fast-rm: no paths provided (give positional args or -f LIST_FILE)",
            file=sys.stderr,
        )
        return 2

    # Validation pass — ALL candidates validated before any deletion
    valid_paths: list[str] = []
    invalid_count = 0
    seen: set[str] = set()

    for raw in candidates:
        abs_path, reason = _validate_one(raw)
        if abs_path is None:
            print(f"  SKIP ({reason}): {raw!r}", file=sys.stderr)
            invalid_count += 1
        elif abs_path in seen:
            pass  # silently deduplicate
        else:
            seen.add(abs_path)
            valid_paths.append(abs_path)

    if not valid_paths:
        print(
            f"fast-rm: no valid paths to delete. ({invalid_count} skipped)",
            file=sys.stderr,
        )
        return 2

    # Task 2+ will add: scan → confirm → delete / detach / dry-run
    _ = jobs  # used in later tasks
    return 0
```

Register in `src/tctl/cli.py` — add one line to `_PLATFORM_COMMANDS`:

```python
_PLATFORM_COMMANDS: dict[str, str] = {
    "config":      "tctl.commands.config_cmd",
    "init-config": "tctl.commands.init_config",
    "fast-rm":     "tctl.commands.fast_rm",      # NEW
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/commands/fast_rm.py && \
    .venv/bin/ruff format --check src/tctl/commands/fast_rm.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_commands_fast_rm.py -q
```

```bash
git add src/tctl/commands/fast_rm.py src/tctl/cli.py tests/test_commands_fast_rm.py
git commit -m "feat(fast-rm): argparse skeleton + validation helpers + CLI registration (Task 1)"
```

---

### Task 2: Pre-scan + confirmation prompt

**What we build:** `_scan_target(target) -> tuple[int, int]` (file count + bytes via `find` + `du -sb`), `_fmt_bytes(n) -> str` (human-readable size, no numfmt dependency), and `_confirm(valid_paths, file_count, total_bytes, jobs, invalid_count, assume_yes, dry_run) -> bool`. The confirmation prompt shows original paths, handles EOFError on no-TTY, and respects `--dry-run` (prints summary then returns False so the caller can exit 0 without deletion). `run()` is extended to call scan + confirm after the validation pass.

**Files:**
- Modify: `src/tctl/commands/fast_rm.py` (add helpers + wire into `run()`)
- Modify: `tests/test_commands_fast_rm.py` (add AT-6 + prompt tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands_fast_rm.py`:

```python
# ---------------------------------------------------------------------------
# AT-6 — dry-run: no deletion, exit 0, shows path + "dry run" message
# ---------------------------------------------------------------------------

def test_at6_dry_run_no_deletion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "a" / "b" / "c" / "eval_run"
    for i in range(3):
        (target / f"sub_{i}").mkdir(parents=True)
        (target / f"sub_{i}" / "file.txt").write_text("x")

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=False, dry_run=True, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert target.exists(), "dry-run must not delete the target"
    out = capsys.readouterr().out
    assert "dry run" in out.lower() or "dry-run" in out.lower()


# Confirmation: EOFError → abort, exit 0
def test_confirm_eof_returns_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (5, 1024))

    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)

    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=False, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert target.exists(), "EOFError on confirm must not delete anything"


# Confirmation: user answers 'n' → abort, exit 0
def test_confirm_n_returns_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (5, 1024))

    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)

    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=False, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert target.exists()


# -y bypasses confirm
def test_yes_flag_bypasses_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tctl.commands.fast_rm import run as fast_rm_run
    input_calls: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": input_calls.append(prompt) or "n")
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (0, 0))
    # _delete_one is not yet implemented (Task 3); patch to no-op
    monkeypatch.setattr("tctl.commands.fast_rm._delete_one", lambda path, jobs: True)

    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)

    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    fast_rm_run(ns, [])
    assert not input_calls, "input() must not be called when --yes is set"


# _fmt_bytes: human-readable sizes
@pytest.mark.parametrize("n,expected", [
    (0,          "0 B"),
    (512,        "512 B"),
    (1024,       "1.0 KiB"),
    (1048576,    "1.0 MiB"),
    (1073741824, "1.0 GiB"),
])
def test_fmt_bytes(n: int, expected: str) -> None:
    from tctl.commands.fast_rm import _fmt_bytes
    assert _fmt_bytes(n) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q -k "at6 or confirm or fmt_bytes" 2>&1 | head -20
```

Expected: `AttributeError` or `ImportError` — `_scan_target`, `_fmt_bytes`, `_confirm` not yet defined.

- [ ] **Step 3: Implement scan + confirm + wire into run()**

Add to `src/tctl/commands/fast_rm.py` (after validation constants, before `run()`):

```python
import subprocess
import time


def _fmt_bytes(n: int) -> str:
    """Return a human-readable byte count using binary prefixes."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            if unit == "B":
                return f"{n} B"
            return f"{n / 1024:.1f} {unit}"
        n //= 1024
    return f"{n} B"  # unreachable but satisfies mypy


def _scan_target(target: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for the directory tree."""
    result = subprocess.run(
        ["find", target, "-type", "f"],
        capture_output=True, text=True, check=False,
    )
    file_count = result.stdout.count("\n") if result.returncode == 0 else 0

    du = subprocess.run(
        ["du", "-sb", "--", target],
        capture_output=True, text=True, check=False,
    )
    total_bytes = 0
    if du.returncode == 0:
        try:
            total_bytes = int(du.stdout.split()[0])
        except (IndexError, ValueError):
            total_bytes = 0
    return file_count, total_bytes


def _confirm(
    valid_paths: list[str],
    file_count: int,
    total_bytes: int,
    jobs: int,
    invalid_count: int,
    assume_yes: bool,
    dry_run: bool,
    quiet: bool,
) -> bool:
    """Print confirmation block; return True if deletion should proceed.

    dry_run=True prints the block then always returns False (caller exits 0).
    assume_yes=True skips the prompt and returns True immediately.
    EOFError on input() → treat as 'N', return False.
    """
    size_str = "(skipped)" if quiet else _fmt_bytes(total_bytes)
    count_str = "(skipped)" if quiet else f"{file_count:,}"

    print(f"\nAbout to delete {len(valid_paths)} path(s):")
    for p in valid_paths:
        print(f"  - {p}  [will rename first]")
    print(f"  files: {count_str}  total size: {size_str}")
    if invalid_count > 0:
        print(f"  (skipped {invalid_count} invalid path(s); see warnings above)")

    if dry_run:
        print("\nDry run — no files will be deleted.")
        return False

    if assume_yes:
        return True

    try:
        ans = input(f"\nProceed using {jobs} parallel jobs? [y/N] ")
    except EOFError:
        print(
            "Aborted (no TTY for confirmation; use -y for non-interactive).",
            file=sys.stderr,
        )
        return False
    if ans.strip().lower() not in ("y", "yes"):
        print("Aborted.", file=sys.stderr)
        return False
    return True
```

Update `run()` to call scan + confirm after validation, and stub `_delete_one` for wiring:

```python
def _delete_one(path: Path, jobs: int) -> bool:
    """Placeholder — implemented in Task 3."""
    raise NotImplementedError("_delete_one not yet implemented")


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    # ... (keep existing validation block) ...

    # Pre-scan (skip if --quiet, always scan if --dry-run)
    total_files = 0
    total_bytes = 0
    if not ns.quiet or ns.dry_run:
        for t in valid_paths:
            fc, tb = _scan_target(t)
            total_files += fc
            total_bytes += tb

    # Confirmation prompt (also handles --dry-run)
    proceed = _confirm(
        valid_paths=valid_paths,
        file_count=total_files,
        total_bytes=total_bytes,
        jobs=jobs,
        invalid_count=invalid_count,
        assume_yes=ns.yes,
        dry_run=ns.dry_run,
        quiet=ns.quiet,
    )
    if not proceed:
        return 0  # dry-run or user aborted

    if ns.detach:
        # Task 5 will implement _spawn_detached
        raise NotImplementedError("--detach not yet implemented")

    # Foreground deletion loop (Task 4)
    raise NotImplementedError("foreground deletion not yet implemented")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q -k "at6 or confirm or fmt_bytes or at2 or at3 or at4 or at5 or help" 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/commands/fast_rm.py && \
    .venv/bin/ruff format --check src/tctl/commands/fast_rm.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_commands_fast_rm.py -q -k "not at1 and not at7 and not at8 and not at9 and not at10 and not at11 and not detach and not foreground and not delete"
```

```bash
git add src/tctl/commands/fast_rm.py tests/test_commands_fast_rm.py
git commit -m "feat(fast-rm): pre-scan + confirmation prompt + _fmt_bytes (Task 2)"
```

---

### Task 3: Rename-then-rm helper + `_delete_one` 3-phase deletion

**What we build:** `_rename_for_deletion(target, suffix_id) -> Path` with EXDEV/EPERM/EACCES/EEXIST fallback, and `_delete_one(path, jobs) -> bool` implementing the 3-phase deletion. Phase 1 and symlink pass use `shell=True` for the `find | xargs` pipeline; phase 2 uses `shell=False`; phase 3 uses `pathlib`. Tests cover AT-10 (rename trick — original path disappears before deletion), rename fallback, and subprocess argv shape.

**Files:**
- Modify: `src/tctl/commands/fast_rm.py` (replace `_delete_one` placeholder)
- Modify: `tests/test_commands_fast_rm.py` (add AT-10 + rename unit tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands_fast_rm.py`:

```python
# ---------------------------------------------------------------------------
# Rename-then-rm helper
# ---------------------------------------------------------------------------

def test_rename_helper_succeeds(tmp_path: Path) -> None:
    from tctl.commands.fast_rm import _rename_for_deletion
    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)
    renamed = _rename_for_deletion(target, "abc123")
    assert renamed.name == "target.deleting-abc123"
    assert renamed.exists()
    assert not target.exists()


def test_rename_helper_falls_back_on_cross_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)

    def fake_rename(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "rename", fake_rename)
    result = _rename_for_deletion(target, "abc123")
    assert result == target  # fell back to original path


def test_rename_helper_falls_back_on_eexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tctl.commands.fast_rm import _rename_for_deletion

    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)

    def fake_rename(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError(errno.EEXIST, "File exists")

    monkeypatch.setattr(os, "rename", fake_rename)
    result = _rename_for_deletion(target, "abc123")
    assert result == target


# ---------------------------------------------------------------------------
# _delete_one subprocess argv verification (mocked)
# ---------------------------------------------------------------------------

def test_delete_one_calls_find_xargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | list[str]] = []

    import subprocess as sp

    def fake_run(
        argv: str | list[str], **kwargs: object
    ) -> sp.CompletedProcess[str]:
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", fake_run)

    target = tmp_path / "a" / "b" / "c" / "data"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import _delete_one
    _delete_one(target, jobs=4)

    # Phase 1 must use find … | xargs … rm
    assert any("find" in str(c) and "xargs" in str(c) for c in calls), (
        f"Expected find|xargs call in: {calls}"
    )
    # Phase 2 must use find … -empty -delete (argv list form)
    phase2 = [c for c in calls if isinstance(c, list) and "find" in c]
    assert phase2, "phase 2 should use argv list with find"
    assert any("-empty" in c for c in phase2[0])


# ---------------------------------------------------------------------------
# AT-10 — rename-then-rm: source path disappears before deletion completes
# ---------------------------------------------------------------------------

def test_at10_rename_then_rm_path_disappears_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)
    (target / "file.txt").write_text("data")

    parent = target.parent
    renames_seen: list[list[str]] = []

    import subprocess as sp
    original_run = sp.run

    def patched_run(
        argv: str | list[str], **kwargs: object
    ) -> sp.CompletedProcess[str]:
        # Capture parent-dir listing during phase 1 (find|xargs pipeline)
        if isinstance(argv, str) and "find" in argv and "xargs" in argv:
            children = list(parent.iterdir())
            renames_seen.append([c.name for c in children])
        return original_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", patched_run)

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0

    if renames_seen:
        for entry_names in renames_seen:
            assert "target" not in entry_names, (
                f"original 'target' name must not be visible during phase 1; saw: {entry_names}"
            )
            assert any(n.startswith("target.deleting-") for n in entry_names), (
                f"expected 'target.deleting-*' in parent listing; saw: {entry_names}"
            )

    assert not target.exists()
    assert not any(p.name.startswith("target.deleting-") for p in parent.iterdir())
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q \
    -k "rename_helper or delete_one or at10" 2>&1 | head -20
```

Expected: `NotImplementedError` from the `_delete_one` placeholder, or `AttributeError` on `_rename_for_deletion`.

- [ ] **Step 3: Implement rename helper + 3-phase deletion**

Add to `src/tctl/commands/fast_rm.py` (after `_confirm`, before the `_delete_one` placeholder):

```python
import contextlib
import errno
import shlex


def _ts() -> str:
    """Current time as HH:MM:SS string."""
    import time
    return time.strftime("%H:%M:%S")


def _rename_for_deletion(target: Path, suffix_id: str) -> Path:
    """Rename target to target.deleting-<suffix_id>; return new path.

    Falls back to returning the original path on:
      EXDEV  — cross-filesystem move
      EPERM/EACCES — permission denied
      EEXIST — stale .deleting-<id> path already exists (1-in-16M collision)
    """
    renamed = target.with_name(target.name + f".deleting-{suffix_id}")
    try:
        os.rename(target, renamed)
        return renamed
    except OSError as exc:
        _reason_map = {
            errno.EXDEV:   "cross-filesystem",
            errno.EPERM:   "permission denied",
            errno.EACCES:  "permission denied",
            errno.EEXIST:  "stale .deleting-<id> path exists",
        }
        if exc.errno in _reason_map:
            _LOG.warning(
                "rename-then-rm failed (%s); deleting %s in place. "
                "Listings will show the path until phase 2 completes.",
                _reason_map[exc.errno],
                target,
            )
            return target
        raise


def _delete_one(path: Path, jobs: int) -> bool:
    """Delete a single target path (renamed or original). Return True on success.

    Three phases:
      1. Parallel file deletion via find | xargs -P N rm -f  (shell=True pipeline)
      Symlink pass: find -type l | xargs rm -f              (shell=True pipeline)
      2. Empty-dir cleanup: find -depth -type d -empty -delete  (shell=False)
      3. Python rmdir of root (now empty)
    """
    t0 = time.monotonic()
    print(f"  [{_ts()}] phase 1/3: parallel rm files (jobs={jobs}) ...", flush=True)

    p1 = subprocess.run(
        f"find {shlex.quote(str(path))} -type f -print0"
        f" | xargs -0 -r -P {jobs} -n 1000 rm -f",
        shell=True,  # noqa: S602
        check=False,
    )
    if p1.returncode != 0:
        print(f"  ERROR: phase 1 failed (rc={p1.returncode})", file=sys.stderr)
        return False

    # Symlink pass — non-fatal
    subprocess.run(
        f"find {shlex.quote(str(path))} -type l -print0 | xargs -0 -r rm -f",
        shell=True,  # noqa: S602
        check=False,
    )

    print(f"  [{_ts()}] phase 2/3: removing empty subdirs ...", flush=True)
    p2 = subprocess.run(
        ["find", str(path), "-depth", "-type", "d", "-empty", "-delete"],
        check=False,
    )
    if p2.returncode != 0:
        print(f"  ERROR: phase 2 failed (rc={p2.returncode})", file=sys.stderr)
        return False

    print(f"  [{_ts()}] phase 3/3: removing root dir ...", flush=True)
    if path.exists():
        if not any(path.iterdir()):
            with contextlib.suppress(FileNotFoundError, OSError):
                path.rmdir()
        else:
            print(
                f"  WARNING: {path} not empty after deletion. Remaining files:",
                file=sys.stderr,
            )
            for entry in list(path.iterdir())[:10]:
                print(f"    {entry}", file=sys.stderr)
            return False

    elapsed = time.monotonic() - t0
    print(f"  [{_ts()}] OK in {elapsed:.1f}s.")
    return True
```

Remove the `_delete_one` placeholder stub. Update `run()` to generate a `suffix_id` (used for rename) and call `_rename_for_deletion` + `_delete_one` inside the foreground loop (keep `NotImplementedError` for `--detach` only). The foreground loop portion is not yet fully implemented — Task 4 completes it; for now raise `NotImplementedError` after rename:

```python
    # Inside run(), after the proceed check and before detach:
    # (foreground loop skeleton — Task 4 completes this)
    suffix_id = os.urandom(3).hex()
    for target_str in valid_paths:
        target = Path(target_str)
        renamed = _rename_for_deletion(target, suffix_id)
        ok = _delete_one(renamed, jobs)
        _ = ok  # Task 4 tallies these
    return 0
```

Note: full summary + exit codes are in Task 4. For now the skeleton loop satisfies AT-10's rename visibility test.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q \
    -k "rename_helper or delete_one or at10" 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/commands/fast_rm.py && \
    .venv/bin/ruff format --check src/tctl/commands/fast_rm.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_commands_fast_rm.py -q \
    -k "not at7 and not at8 and not at9 and not at11 and not detach"
```

```bash
git add src/tctl/commands/fast_rm.py tests/test_commands_fast_rm.py
git commit -m "feat(fast-rm): rename-then-rm helper + 3-phase _delete_one (Task 3)"
```

---

### Task 4: Foreground deletion loop + summary + exit codes

**What we build:** Complete the `run()` foreground deletion loop — per-target rename → delete → tally success/failure, summary block with elapsed time, and correct exit codes (0 = all OK, 1 = some failures, 2 = config/validation error). Tests cover AT-1 (end-to-end small tree) and AT-9 (per-target failure does not abort batch).

**Files:**
- Modify: `src/tctl/commands/fast_rm.py` (replace skeleton loop with full implementation)
- Modify: `tests/test_commands_fast_rm.py` (add AT-1, AT-9)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands_fast_rm.py`:

```python
# ---------------------------------------------------------------------------
# AT-1 — Foreground deletion of a small tree succeeds; directory is gone after
# ---------------------------------------------------------------------------

def test_at1_foreground_delete_small_tree(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "eval_run"
    for i in range(3):
        sub = target / f"sub_{i}"
        sub.mkdir(parents=True)
        for j in range(5):
            (sub / f"file_{j}.txt").write_text("x")

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert not target.exists(), "target dir should be gone after fast-rm"
    assert (tmp_path / "a" / "b" / "c").exists(), "parent dir should still exist"


# ---------------------------------------------------------------------------
# AT-9 — Per-target failure does not abort batch; exit 1 when any fail
# ---------------------------------------------------------------------------

def test_at9_per_target_failure_does_not_abort_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = []
    for i in range(3):
        t = tmp_path / "a" / "b" / "c" / f"run_{i}"
        t.mkdir(parents=True)
        (t / "file.txt").write_text("x")
        targets.append(t)

    import subprocess as sp
    call_count: dict[str, int] = {"n": 0}
    original_run = sp.run

    def patched_run(
        argv: str | list[str], **kwargs: object
    ) -> sp.CompletedProcess[str]:
        call_count["n"] += 1
        # Fail phase-1 xargs pipeline on the 2nd target's first shell=True call
        if (
            call_count["n"] == 2
            and isinstance(argv, str)
            and "xargs" in argv
        ):
            return sp.CompletedProcess(argv, 1, "", "simulated failure")
        return original_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", patched_run)

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(t) for t in targets], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 1, f"expected exit 1 (some failures) but got {rc}"
    # Targets 0 and 2 should be gone; target 1 may remain in renamed state
    assert not targets[0].exists() or not targets[2].exists(), (
        "at least one of targets[0], targets[2] should have been deleted"
    )


# Summary output: exit 0 when all succeed
def test_summary_exit_0_on_all_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "a" / "b" / "c" / "t"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "succeeded" in out.lower() or "done" in out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q \
    -k "at1 or at9 or summary_exit" 2>&1 | head -20
```

Expected: `NotImplementedError` from the incomplete foreground loop, or assertion errors on exit code.

- [ ] **Step 3: Implement full foreground loop + summary**

Replace the skeleton foreground loop in `run()` with the complete implementation:

```python
    # Foreground deletion loop
    t0_total = time.monotonic()
    success_count = 0
    failure_count = 0
    failed_paths: list[str] = []
    suffix_id = os.urandom(3).hex()

    for idx, target_str in enumerate(valid_paths, start=1):
        print(f"\n[{idx}/{len(valid_paths)}] {target_str}", file=sys.stderr)
        target = Path(target_str)
        renamed = _rename_for_deletion(target, suffix_id)
        ok = _delete_one(renamed, jobs)
        if ok:
            success_count += 1
        else:
            failure_count += 1
            failed_paths.append(target_str)  # report ORIGINAL path

    elapsed = time.monotonic() - t0_total
    print("\n" + "=" * 64)
    print(f"Done in {elapsed:.0f}s.")
    print(f"  succeeded: {success_count}")
    print(f"  failed:    {failure_count}")
    if invalid_count > 0:
        print(f"  skipped (invalid pre-validation): {invalid_count}")
    if failed_paths:
        print("Failed paths:")
        for p in failed_paths:
            print(f"  - {p}")
    print("=" * 64)

    return 1 if failure_count > 0 else 0
```

Note: exit code 1 (not 2) for deletion failures — reserves 2 for tctl config errors per CLAUDE.md. This is a deliberate divergence from the bash script which exits 2.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q \
    -k "not at7 and not at8 and not at11 and not detach" 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/commands/fast_rm.py && \
    .venv/bin/ruff format --check src/tctl/commands/fast_rm.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_commands_fast_rm.py -q \
    -k "not at7 and not at8 and not at11 and not detach"
```

```bash
git add src/tctl/commands/fast_rm.py tests/test_commands_fast_rm.py
git commit -m "feat(fast-rm): foreground deletion loop + summary + exit codes (Task 4)"
```

---

### Task 5: `--detach` via TmuxSession

**What we build:** `_spawn_detached(valid_paths, jobs, quiet) -> str` that generates a unique `run_id`, creates `~/.tctl/fastrm/<id>.log`'s parent directory, builds the re-invocation argv, spawns via `TmuxSession`, prints the session info block, and returns the run_id. `TmuxSession` is imported lazily inside `_spawn_detached` to preserve sub-200ms startup. When `--dry-run --detach` is given, `--dry-run` wins — no session spawned. Tests cover AT-7, AT-8, AT-11.

**Files:**
- Modify: `src/tctl/commands/fast_rm.py` (add `_spawn_detached` + wire into `run()`)
- Modify: `tests/test_commands_fast_rm.py` (add AT-7, AT-8, AT-11)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands_fast_rm.py`:

```python
# ---------------------------------------------------------------------------
# AT-7 — -d spawns a tmux session named tctl-fastrm-<id>, exits 0
# ---------------------------------------------------------------------------

def test_at7_detach_spawns_named_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import re
    sessions: list[dict[str, object]] = []

    class FakeSession:
        def __init__(self, name: str, log_path: Path | None = None) -> None:
            sessions.append({"name": name, "log_path": log_path})

        def start(self, argv: list[str]) -> None:
            pass

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (5, 512))

    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=4, yes=True, quiet=False, dry_run=False, detach=True,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert len(sessions) == 1
    name = str(sessions[0]["name"])
    assert re.fullmatch(r"tctl-fastrm-[0-9a-f]{6}", name), f"bad session name: {name!r}"
    run_id = name.split("-")[-1]
    captured = capsys.readouterr()
    assert run_id in captured.out or run_id in captured.err, (
        f"run_id {run_id!r} not found in output"
    )


# ---------------------------------------------------------------------------
# AT-8 — Two -d runs produce distinct session names
# ---------------------------------------------------------------------------

def test_at8_two_detach_runs_produce_distinct_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []

    class FakeSession:
        def __init__(self, name: str, log_path: Path | None = None) -> None:
            names.append(name)

        def start(self, argv: list[str]) -> None:
            pass

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (0, 0))

    target = tmp_path / "a" / "b" / "c" / "tgt"

    from tctl.commands.fast_rm import run as fast_rm_run
    for _ in range(2):
        target.mkdir(parents=True, exist_ok=True)
        ns = argparse.Namespace(
            paths=[str(target)], list_file=None,
            jobs=4, yes=True, quiet=True, dry_run=False, detach=True,
        )
        fast_rm_run(ns, [])

    assert len(names) == 2
    assert names[0] != names[1], "both detach runs must produce distinct session names"


# ---------------------------------------------------------------------------
# AT-11 — --dry-run --detach: dry-run wins, no session spawned
# ---------------------------------------------------------------------------

def test_at11_dry_run_overrides_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[str] = []

    class FakeSession:
        def __init__(self, name: str, log_path: Path | None = None) -> None:
            spawned.append(name)

        def start(self, argv: list[str]) -> None:
            raise AssertionError("must not spawn when --dry-run is set")

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)

    target = tmp_path / "a" / "b" / "c" / "foo"
    target.mkdir(parents=True)
    (target / "x").write_text("y")

    from tctl.commands.fast_rm import run as fast_rm_run
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=False, dry_run=True, detach=True,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert spawned == [], f"no session should be spawned; got: {spawned}"
    assert target.exists(), "dry-run must not delete target"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -x -q \
    -k "at7 or at8 or at11" 2>&1 | head -20
```

Expected: `NotImplementedError` on the `--detach` branch.

- [ ] **Step 3: Implement `_spawn_detached` + wire into `run()`**

Add to `src/tctl/commands/fast_rm.py`:

```python
def _spawn_detached(valid_paths: list[str], jobs: int, quiet: bool) -> str:
    """Spawn a background tmux session to run fast-rm on the validated paths.

    Re-invokes `python -m tctl fast-rm --yes -j N [--quiet] <paths...>`
    inside the session so validation and confirmation are skipped.

    TmuxSession imported lazily to preserve sub-200ms startup for
    the common foreground path.

    Returns the run_id (6-char hex).
    """
    from tctl.tmux import TmuxSession  # lazy import

    run_id = os.urandom(3).hex()
    session_name = f"tctl-fastrm-{run_id}"

    log_dir = Path.home() / ".tctl" / "fastrm"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    argv: list[str] = [
        sys.executable, "-m", "tctl", "fast-rm",
        "--yes", "-j", str(jobs),
    ]
    if quiet:
        argv.append("--quiet")
    argv.extend(valid_paths)

    sess = TmuxSession(session_name, log_path=log_path)
    sess.start(argv)
    return run_id
```

In `run()`, replace the `--detach` `NotImplementedError` with:

```python
    if ns.detach:
        run_id = _spawn_detached(valid_paths, jobs, ns.quiet)
        session_name = f"tctl-fastrm-{run_id}"
        log_path = Path.home() / ".tctl" / "fastrm" / f"{run_id}.log"
        print(f"fast-rm: spawned session {session_name}")
        print(f"  attach : tmux attach -t {session_name}")
        print(f"  log    : {log_path}")
        print(f"  paths  : {len(valid_paths)} target(s) queued")
        return 0
```

The `--dry-run` check (which returns False from `_confirm`) runs before the `--detach` branch, so `--dry-run --detach` naturally exits 0 without reaching the detach code.

Also add a module-level `TmuxSession` alias so `monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", ...)` works in tests without importing from `tctl.tmux`:

```python
# Module-level alias for monkeypatching in tests.
# The real import remains lazy inside _spawn_detached.
try:
    from tctl.tmux import TmuxSession
except ImportError:  # pragma: no cover
    TmuxSession = None  # type: ignore[assignment,misc]
```

Wait — this would make the import eager. Instead, declare a type alias and set it at module scope after the function definitions using a `TYPE_CHECKING` guard, or simply patch the import inside `_spawn_detached` using `monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", ...)` by importing it at module scope for the name binding only:

```python
# Import TmuxSession at module scope so tests can monkeypatch it.
# The _spawn_detached() path is only triggered on --detach; the import
# has negligible cost and does not affect cold-start time materially.
from tctl.tmux import TmuxSession
```

Then inside `_spawn_detached`, replace the lazy import comment and use `TmuxSession` directly.

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_commands_fast_rm.py -q 2>&1 | head -50
```

All 11 ATs should pass (AT-1 through AT-11).

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/commands/fast_rm.py && \
    .venv/bin/ruff format --check src/tctl/commands/fast_rm.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_commands_fast_rm.py -q
# Full suite to confirm coverage gate still passes
.venv/bin/pytest -q --cov=tctl --cov-report=term-missing --cov-fail-under=50
```

```bash
git add src/tctl/commands/fast_rm.py tests/test_commands_fast_rm.py
git commit -m "feat(fast-rm): --detach via TmuxSession; AT-7, AT-8, AT-11 (Task 5)"
```

---

### Task 6: Docs + version bump + final gates

**What we build:** Version bump to 0.9.1 across the three canonical locations (`__init__.py`, `pyproject.toml`, `tests/test_smoke.py`), plus documentation additions in `CHANGELOG.md`, `docs/CLI-REFERENCE.md`, and `README.md`. Run all 4 CI gates and confirm all 11 ATs pass. Commit leaves the repo fully green.

**Files:**
- Modify: `src/tctl/__init__.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_smoke.py`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/CLI-REFERENCE.md`
- Modify: `README.md`

- [ ] **Step 1: Write the failing tests**

In `tests/test_smoke.py`, update the version assertion:

```python
# Change:
assert tctl.__version__ == "0.9.0"
# To:
assert tctl.__version__ == "0.9.1"
```

Verify the test fails before the bump:

```bash
.venv/bin/pytest tests/test_smoke.py -x -q -k "version" 2>&1 | head -10
```

Expected: `AssertionError: assert '0.9.0' == '0.9.1'`

- [ ] **Step 2: Verify the test fails**

```bash
.venv/bin/pytest tests/test_smoke.py -x -q 2>&1 | head -10
```

- [ ] **Step 3: Implement version bump + docs**

**`src/tctl/__init__.py`** — change `__version__ = "0.9.0"` → `__version__ = "0.9.1"`.

**`pyproject.toml`** — change `version = "0.9.0"` → `version = "0.9.1"`.

**`docs/CHANGELOG.md`** — prepend at the top (before the `## [0.9.0]` entry):

```markdown
## [0.9.1] - 2026-05-26

### Added
- `tctl fast-rm` platform command: fast parallel deletion for directories with many
  small files. Ports `EASI/scripts/fast_rm.sh` into tctl with additional features:
  - `--detach` (`-d`): spawn deletion in a named tmux session (`tctl-fastrm-<id>`)
    and return immediately; multiple `-d` invocations coexist freely.
  - `--dry-run`: validate + scan + report; exit 0 without touching any files.
  - Rename-then-rm: each target is atomically renamed to `<name>.deleting-<id>`
    before phase 1 begins, so the original path disappears from directory listings
    instantly even while millions of files are still being deleted.
  - `-f / --list-file`: read target paths from a file (one per line, `#` comments
    and blank lines ignored); may be combined with positional PATH args.
  - Aggregate pre-scan (file count + size) and confirmation prompt covering the
    full batch; `-y` skips the prompt for non-interactive use.
  - Structured summary with elapsed time, success/failure counts, and the original
    paths of any failed targets.

### Changed
- Exit code for per-target deletion failure is **1** (not 2 as in the bash script).
  Exit 2 is reserved for tctl config errors per the established exit-code convention.
  Automation that tested `rc == 2` to detect deletion failure must update to `rc != 0`.

### Session name
- `tctl-fastrm-<6-char-hex>` — one session per `-d` invocation; no shared state.
- Log: `~/.tctl/fastrm/<id>.log`
```

**`docs/CLI-REFERENCE.md`** — add section (locate the Platform Commands section and append):

```markdown
### `tctl fast-rm`

Fast parallel deletion for directories with many small files.

```
tctl fast-rm [OPTIONS] [PATH...]
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `PATH...` | — | One or more directories to delete |
| `-f FILE`, `--list-file FILE` | — | Read paths from FILE (one per line; `#` comments and blank lines ignored) |
| `-j N`, `--jobs N` | `nproc` | Parallel rm jobs |
| `-y`, `--yes` | off | Skip confirmation prompt |
| `-q`, `--quiet` | off | Skip pre-scan (useful on cold-cache network mounts) |
| `--dry-run` | off | Validate + scan + report; no deletion |
| `-d`, `--detach` | off | Spawn deletion in a background tmux session |

**Safety rails (all validated before any deletion):**

- Dangerous literals rejected: `''`, `.`, `..`, `~`, `/`, `/*`
- System paths rejected: `/home`, `/root`, `/etc`, `/var`, `/usr`, `/bin`,
  `/sbin`, `/lib`, `/lib64`, `/mnt`, `/proc`, `/sys`, `/dev`, `/boot`, `/tmp`
- `$HOME` rejected
- Paths with fewer than 3 segments rejected (typo guard)
- All paths must exist and be directories

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | All deletions succeeded (or `--dry-run`) |
| `1` | One or more per-target deletion failures |
| `2` | No valid paths after validation, or missing list file |

**Detached mode:**

```bash
tctl fast-rm -d --yes /mnt/eval/run_a /mnt/eval/run_b
# fast-rm: spawned session tctl-fastrm-a3f0c1
#   attach : tmux attach -t tctl-fastrm-a3f0c1
#   log    : ~/.tctl/fastrm/a3f0c1.log
#   paths  : 2 target(s) queued

# List active sessions:
tmux ls | grep tctl-fastrm-

# Kill a stuck session:
tmux kill-session -t tctl-fastrm-a3f0c1
```
```

**`README.md`** — add `tctl fast-rm` row to the platform commands table (locate the section listing `tctl config`, `tctl init-config`):

```markdown
| `tctl fast-rm` | Fast parallel deletion (millions of files); foreground or `--detach` tmux |
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_smoke.py -x -q 2>&1 | head -10
```

- [ ] **Step 5: Run all 4 CI gates + all 11 ATs + commit**

```bash
# Gate 1: ruff lint
.venv/bin/ruff check .

# Gate 2: ruff format
.venv/bin/ruff format --check .

# Gate 3: mypy --strict
.venv/bin/mypy --strict src/tctl

# Gate 4: pytest with coverage gate
.venv/bin/pytest -q --cov=tctl --cov-report=term-missing --cov-fail-under=50

# All 11 ATs
.venv/bin/pytest tests/test_commands_fast_rm.py -v -k "at1 or at2 or at3 or at4 or at5 or at6 or at7 or at8 or at9 or at10 or at11"
```

```bash
git add \
    src/tctl/__init__.py \
    pyproject.toml \
    tests/test_smoke.py \
    docs/CHANGELOG.md \
    docs/CLI-REFERENCE.md \
    README.md
git commit -m "chore(v0.9.1): version bump + fast-rm docs + CHANGELOG (Task 6)"
```

---

## AT Coverage Map

| AT | Task | Test function |
|---|---|---|
| AT-1 | Task 4 | `test_at1_foreground_delete_small_tree` |
| AT-2 | Task 1 | `test_at2_dangerous_literal_rejected` (parametrized) + `test_at2_run_returns_2_on_dangerous_literal` |
| AT-3 | Task 1 | `test_at3_system_path_rejected` (parametrized) + `test_at3_run_returns_2_on_system_path` |
| AT-4 | Task 1 | `test_at4_shallow_path_rejected_via_monkeypatch` |
| AT-5 | Task 1 | `test_at5_list_file_reads_and_ignores_comments` |
| AT-6 | Task 2 | `test_at6_dry_run_no_deletion` |
| AT-7 | Task 5 | `test_at7_detach_spawns_named_session` |
| AT-8 | Task 5 | `test_at8_two_detach_runs_produce_distinct_sessions` |
| AT-9 | Task 4 | `test_at9_per_target_failure_does_not_abort_batch` |
| AT-10 | Task 3 | `test_at10_rename_then_rm_path_disappears_before_deletion` |
| AT-11 | Task 5 | `test_at11_dry_run_overrides_detach` |

---

## Concerns / Notes

1. **`TmuxSession` import placement (Task 5):** The spec calls for a lazy import inside `_spawn_detached` to guard startup time. However, `monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", ...)` requires the name to exist at module scope. The plan resolves this by importing `TmuxSession` at module scope (startup cost is negligible since `tctl.tmux` is already imported for other commands anyway). If startup time becomes a concern, switch to a module-level `TYPE_CHECKING` guard and use `monkeypatch.setattr` on `tctl.tmux.TmuxSession` in tests instead.

2. **`shell=True` in `_delete_one` phase 1:** The spec explicitly uses `shell=True` for the `find | xargs` pipeline (required for the pipe). `# noqa: S602` suppresses the ruff `S` rule if it is enabled. Check `.ruff.toml` — the project currently excludes the `S` ruleset (`E,F,W,I,B,UP,SIM,N` only), so the comment is defensive but harmless.

3. **AT-9 mock fragility:** The test patches `subprocess.run` globally and counts calls to determine which invocation to fail. Call ordering depends on the current implementation having 2 shell=True calls per target (phase 1 + symlink pass) and 1 shell=False call (phase 2). If the implementation changes the call order, update the `call_count["n"] == 2` threshold accordingly.

4. **No `--list` sub-verb:** `tmux ls | grep tctl-fastrm-` is the operator's tool for listing active sessions. The spec explicitly marks this as a non-goal for v1.

5. **Exit code 1 vs 2 divergence from bash:** The bash script exits 2 on per-target failure. The Python port exits 1. This is documented in CHANGELOG.md and the CLI reference. Operators with automation checking `rc == 2` must update.
