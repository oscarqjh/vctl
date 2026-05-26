"""`tctl fast-rm` — fast parallel deletion for directories with many small files."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants (mirrors fast_rm.sh validate_path exactly)
# ---------------------------------------------------------------------------

_DANGEROUS_LITERALS: frozenset[str] = frozenset({"", ".", "..", "~", "/", "/*"})

_SYSTEM_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/home",
        "/root",
        "/etc",
        "/var",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/mnt",
        "/proc",
        "/sys",
        "/dev",
        "/boot",
        "/tmp",
    }
)

_MIN_SEGMENTS = 3


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _count_segments(abs_path: str) -> int:
    """Count the number of non-empty path segments in an absolute path.

    Examples:
        /a       -> 1
        /a/b     -> 2
        /a/b/c   -> 3
    """
    return len([s for s in abs_path.split("/") if s])


def _validate_path(p: str) -> Path | None:
    """Validate a single path candidate. Returns absolute Path or None (reason logged to stderr).

    Rejects:
    - Dangerous literals: "", ".", "..", "~", "/", "/*"
    - Paths that don't exist
    - Paths that aren't directories
    - Paths that can't be resolved to absolute
    - System paths: /, /home, /root, /etc, /var, /usr, /bin, /sbin, /lib, /lib64,
                    /mnt, /proc, /sys, /dev, /boot, /tmp
    - $HOME exactly
    - Paths with <3 segments (e.g. /a/b)
    """
    if p in _DANGEROUS_LITERALS:
        print(f"  SKIP (dangerous literal): {p!r}", file=sys.stderr)
        return None

    candidate = Path(p)

    if not candidate.exists():
        print(f"  SKIP (does not exist): {p!r}", file=sys.stderr)
        return None

    if not candidate.is_dir():
        print(f"  SKIP (not a directory): {p!r}", file=sys.stderr)
        return None

    try:
        abs_path = candidate.resolve()
    except OSError as exc:
        print(f"  SKIP (cannot resolve path: {exc}): {p!r}", file=sys.stderr)
        return None

    abs_str = str(abs_path)

    if abs_str in _SYSTEM_PATHS:
        print(f"  SKIP (system path): {p!r}", file=sys.stderr)
        return None

    home_str = os.environ.get("HOME", "")
    if home_str:
        try:
            home_resolved = str(Path(home_str).resolve())
        except OSError:
            home_resolved = ""
        if abs_str == home_resolved:
            print(f"  SKIP ($HOME): {p!r}", file=sys.stderr)
            return None

    if _count_segments(abs_str) < _MIN_SEGMENTS:
        seg = _count_segments(abs_str)
        print(
            f"  SKIP (too shallow: {seg} segment(s); need >={_MIN_SEGMENTS}): {p!r}",
            file=sys.stderr,
        )
        return None

    return abs_path


def _load_list_file(path: Path) -> list[str]:
    """Read paths from a list file. One per line. Strips CR + leading/trailing whitespace.

    Skips blank lines + lines starting with '#'. Returns paths in order.
    Raises FileNotFoundError if path doesn't exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"list file not found: {path}")

    lines: list[str] = []
    with path.open() as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Pre-scan helpers
# ---------------------------------------------------------------------------


def _fmt_bytes(n: int) -> str:
    """Format bytes as IEC suffix (e.g. 1234567 → '1.2MiB').

    Uses binary prefixes (1 KiB = 1024 bytes).  No space between number
    and unit, matching the task-2 spec.  Sub-KiB amounts are printed as
    plain integer bytes (e.g. '1023B').
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    if n < 1024:
        return f"{n}B"
    value: float = float(n)
    for unit in units[1:]:
        value /= 1024.0
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}"
    return f"{n}B"  # unreachable — satisfies mypy


def _scan_target(target: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for a single directory tree.

    Shells out to ``find -type f`` (count newlines) and ``du -sb`` (parse
    first column).  Both calls use ``check=False``; errors are logged to
    stderr and the failing metric is returned as 0 (best-effort).
    """
    find_result = subprocess.run(
        ["find", str(target), "-type", "f"],
        capture_output=True,
        text=True,
        check=False,
    )
    if find_result.returncode == 0:
        file_count = find_result.stdout.count("\n")
    else:
        _LOG.warning(
            "find failed for %s (rc=%d): %s",
            target,
            find_result.returncode,
            find_result.stderr.strip(),
        )
        file_count = 0

    du_result = subprocess.run(
        ["du", "-sb", "--", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    total_bytes = 0
    if du_result.returncode == 0:
        try:
            total_bytes = int(du_result.stdout.split()[0])
        except (IndexError, ValueError) as exc:
            _LOG.warning("du output parse error for %s: %s", target, exc)
    else:
        _LOG.warning(
            "du failed for %s (rc=%d): %s",
            target,
            du_result.returncode,
            du_result.stderr.strip(),
        )

    return file_count, total_bytes


def _prescan(paths: list[Path]) -> tuple[int, int]:
    """Return (total_files, total_bytes) across all paths.

    Best-effort — errors are logged to stderr and the failing path
    contributes 0 to both totals.  Calls ``_scan_target`` per path.
    """
    total_files = 0
    total_bytes = 0
    for p in paths:
        fc, tb = _scan_target(p)
        total_files += fc
        total_bytes += tb
    return total_files, total_bytes


def _confirm(
    paths: list[Path],
    file_count: int,
    total_bytes: int,
    jobs: int,
    invalid_count: int,
    assume_yes: bool,
    quiet: bool,
) -> bool:
    """Print a summary block and ask for y/N confirmation.

    Returns True if the user accepts (or ``assume_yes`` is set).

    Spec §4.4 + IMPORTANT-4 fix:
    - If ``quiet``, file count and size are shown as ``(skipped)``.
    - If ``assume_yes``, skip the prompt entirely and return True.
    - ``EOFError`` from ``input()`` is treated as 'N': prints an
      explanatory message to stderr and returns False.
    - Accepts y / Y / yes / YES.  Anything else → print 'Aborted.' and
      return False.
    """
    print(f"About to delete {len(paths)} path(s):")
    for p in paths:
        print(f"  - {p}")
    if quiet:
        count_str = "(skipped)"
        size_str = "(skipped)"
    else:
        count_str = str(file_count)
        size_str = _fmt_bytes(total_bytes)
    print(f"  files: {count_str}, total size: {size_str}")
    if invalid_count > 0:
        print(f"  (skipped {invalid_count} invalid path(s); see warnings above)")

    if assume_yes:
        return True

    try:
        ans = input(f"Proceed using {jobs} parallel jobs? [y/N] ")
    except EOFError:
        print(
            "Aborted (no TTY for confirmation; use -y for non-interactive).",
            file=sys.stderr,
        )
        return False

    if ans.strip().lower() in ("y", "yes"):
        return True

    print("Aborted.", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_subparser() -> argparse.ArgumentParser:
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
        "-f",
        "--list-file",
        metavar="FILE",
        default=None,
        help=(
            "Read paths from FILE (one per line). "
            "Blank lines and '#' comments ignored. "
            "May be combined with positional PATH args."
        ),
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 8,
        metavar="N",
        help="Parallel rm jobs (default: nproc, max useful ~16-32).",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Skip pre-scan file-count and size (useful on cold-cache network mounts).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + scan + report; exit 0 without deleting anything.",
    )
    p.add_argument(
        "-d",
        "--detach",
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
    parser = _build_subparser()
    parsed = parser.parse_args(argv_rest)

    # Collect candidates: positional + list_file
    candidates: list[str] = list(parsed.paths)
    if parsed.list_file:
        list_path = Path(parsed.list_file)
        if not list_path.is_file():
            print(
                f"ERROR: list file does not exist: {list_path}",
                file=sys.stderr,
            )
            return 2
        candidates.extend(_load_list_file(list_path))

    if not candidates:
        print(
            "ERROR: no paths provided (give positional args or -f LIST_FILE)",
            file=sys.stderr,
        )
        return 2

    # Validate all candidates
    valid: list[Path] = []
    invalid_count = 0
    for p in candidates:
        abs_path = _validate_path(p)
        if abs_path is not None:
            valid.append(abs_path)
        else:
            invalid_count += 1

    # Dedupe
    seen: set[Path] = set()
    valid_unique: list[Path] = []
    for vp in valid:
        if vp not in seen:
            seen.add(vp)
            valid_unique.append(vp)
    valid = valid_unique

    if not valid:
        print(
            f"No valid paths to delete. ({invalid_count} skipped)",
            file=sys.stderr,
        )
        return 2

    # ------------------------------------------------------------------
    # Pre-scan (Part A / Part D wiring)
    # ------------------------------------------------------------------
    file_count = 0
    total_bytes = 0
    if not parsed.quiet:
        print(f"Scanning {len(valid)} path(s) ...", file=sys.stderr)
        file_count, total_bytes = _prescan(valid)

    # ------------------------------------------------------------------
    # Dry-run path (exits before confirmation prompt)
    # ------------------------------------------------------------------
    if parsed.dry_run:
        if parsed.quiet:
            size_str = "(skipped)"
        elif total_bytes > 0:
            size_str = _fmt_bytes(total_bytes)
        else:
            size_str = "0B"
        print(f"\nDRY RUN: would delete {len(valid)} path(s):", file=sys.stderr)
        for vp in valid:
            print(f"  - {vp}", file=sys.stderr)
        print(f"  files: {file_count}, total size: {size_str}", file=sys.stderr)
        if invalid_count > 0:
            print(
                f"  (skipped {invalid_count} invalid path(s); see warnings above)",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------
    # Confirmation prompt (Part C / Part D wiring)
    # ------------------------------------------------------------------
    if not _confirm(
        paths=valid,
        file_count=file_count,
        total_bytes=total_bytes,
        jobs=parsed.jobs,
        invalid_count=invalid_count,
        assume_yes=parsed.yes,
        quiet=parsed.quiet,
    ):
        return 0  # user aborted; not an error

    # ------------------------------------------------------------------
    # Task 3-5 will add deletion loop + --detach
    # ------------------------------------------------------------------
    print(
        f"VALIDATED + CONFIRMED: {len(valid)} path(s) ready (Tasks 3-5 pending)",
        file=sys.stderr,
    )
    return 0
