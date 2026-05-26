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

    # Task 2 will add pre-scan + confirm
    # Task 3-4 will add deletion loop
    # Task 5 will add --detach

    if parsed.dry_run:
        print(
            f"DRY RUN: would delete {len(valid)} path(s); {invalid_count} skipped.",
            file=sys.stderr,
        )
        for vp in valid:
            print(f"  - {vp}", file=sys.stderr)
        return 0

    # Placeholder: deletion not yet implemented (Tasks 2-5)
    print(
        f"VALIDATED: {len(valid)} path(s) ready (Tasks 2-5 pending)",
        file=sys.stderr,
    )
    return 0
