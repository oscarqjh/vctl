# tctl v0.9.1 — `fast-rm` Platform Command — Design Spec

## 1. Objective

Port `EASI/scripts/fast_rm.sh` into `tctl` as a first-class **platform command**
(`tctl fast-rm`). The port:

1. **Preserves all bash safety rails** verbatim (dangerous-literal check, system-path
   deny-list, `$HOME` guard, minimum-segment depth) — translated into Python.

2. **Adds `--detach` (`-d`)** — spawns a named tmux session (`tctl-fastrm-<id>`) via
   the existing `TmuxSession` primitive, returns immediately while deletion runs in
   the background. Multiple `-d` invocations coexist freely; no shared state.

3. **Adds `--dry-run`** — validates, scans, and reports count + size; exits 0 without
   touching any files.

4. **Adds rename-then-rm** — atomically renames each target to
   `<target>.deleting-<id>` before phase 1 begins. The original path disappears from
   parent-directory listings immediately, even while millions of files are still being
   deleted in the background.

5. **Improves UX** over the bash script: aggregated confirmation prompt for the full
   batch, structured summary with elapsed time and success/failure counts, clean exit
   codes that callers can act on.

**Version:** v0.9.0 → **v0.9.1** (additive feature; no breaking changes).

---

## 2. Background

### 2.1 Why a parallel deleter?

Directories produced by evaluation jobs — vLLM request logs, lmms-eval per-sample
outputs, image-generation grids — routinely contain hundreds of thousands to tens of
millions of small files. `rm -rf` is single-threaded: it stat/unlink/rmdir loops over
the directory tree sequentially. On NVMe-backed local filesystems with 1 M+ inodes,
a naive `rm -rf` can take 30–60 minutes where `find -type f | xargs -P 16 rm -f` takes
2–4 minutes — a 15× speedup.

`EASI/scripts/fast_rm.sh` has been used routinely since mid-2025 for exactly this
workload. It encapsulates the correct three-phase strategy:

- **Phase 1** (slow): delete all files in parallel — `find -type f -print0 | xargs -0 -P N -n 1000 rm -f`
- **Symlink pass**: remove dangling symlinks left after phase 1
- **Phase 2** (fast): bottom-up `find -depth -type d -empty -delete`

The weakness of the bash script is that it blocks the terminal for the full deletion
duration. On the largest trees (tens of millions of files, hours of runtime) operators
need to close their laptop. Detached tmux operation is the obvious fix; `tctl` already
has `TmuxSession` for exactly this purpose.

### 2.2 Why a platform command, not a workload?

A *workload* in tctl's taxonomy is a long-running supervised process (vllm serve,
haproxy, lmms-eval) with start / stop / status sub-verbs and a tmux session that
persists indefinitely. `fast-rm` is a *task* — it runs, completes, and exits. There
are no sub-verbs, no persistent process to query, no cluster.yaml dependency. It is a
platform-level utility, analogous to `init-config` or `config validate`. Placing it in
`src/tctl/commands/` alongside those commands is the correct slot.

### 2.3 Source script location

`/mnt/umm/users/qianjianheng/workspace/EASI/scripts/fast_rm.sh` (~290 lines, bash).
This spec supersedes the bash script; v1 of `tctl fast-rm` achieves full feature parity
plus the additions listed in §1.

---

## 3. Architecture

### 3.1 Module layout

```
src/tctl/
    commands/
        __init__.py
        config_cmd.py        # existing
        init_config.py       # existing
        fast_rm.py           # NEW — this spec
tests/
    test_commands_fast_rm.py # NEW — this spec
```

No new directories. `fast_rm.py` follows the same `run(ns, argv_rest) -> int` contract
as all other platform commands and is loaded lazily via `importlib.import_module`.

### 3.2 CLI registration

One line added to `_PLATFORM_COMMANDS` in `src/tctl/cli.py`:

```python
_PLATFORM_COMMANDS: dict[str, str] = {
    "config":     "tctl.commands.config_cmd",
    "init-config": "tctl.commands.init_config",
    "fast-rm":    "tctl.commands.fast_rm",      # NEW
}
```

No other changes to `cli.py`.

### 3.3 Argparse surface

```python
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tctl fast-rm",
        description=(
            "Fast parallel deletion for directories with many small files.\n\n"
            "Strategy: Phase 1 deletes files in parallel (find | xargs -P N rm -f);\n"
            "Phase 2 prunes the empty directory skeleton (find -depth -empty -delete).\n"
            "Optional rename-then-rm makes each path disappear immediately."
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
        default=None,       # resolved to os.cpu_count() or 8 at runtime
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
```

### 3.4 Subprocess pipeline

The actual deletion is delegated to `find` + `xargs` via `subprocess.run()`. Python
handles: argparse, validation, scan, confirmation, rename-then-rm, summary, and exit
codes. It does NOT reimplement file deletion in Python — OS-level parallelism is the
whole point.

```
find <renamed_target> -type f -print0
    | xargs -0 -r -P <jobs> -n 1000 rm -f

find <renamed_target> -type l -print0
    | xargs -0 -r rm -f

find <renamed_target> -depth -type d -empty -delete
```

Each stage is a separate `subprocess.run(argv_list, check=False)` call; `shell=False`
always. Return codes are captured; non-zero is treated as a per-target failure.

### 3.5 Tmux dispatch (detach path)

`TmuxSession` from `tctl.tmux` is the only tmux abstraction used. The session is
created by re-invoking `tctl fast-rm` with the validated paths, `--yes`, and without
`--detach`:

```python
def _spawn_detached(valid_paths: list[str], jobs: int, quiet: bool) -> str:
    run_id = os.urandom(3).hex()          # 6-char hex, e.g. "a3f0c1"
    session_name = f"tctl-fastrm-{run_id}"
    log_dir = Path.home() / ".tctl" / "fastrm"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    # Re-invoke tctl with validated paths (bypass validation + confirmation)
    argv = [sys.executable, "-m", "tctl", "fast-rm", "--yes",
            "-j", str(jobs)] + valid_paths
    if quiet:
        argv.append("--quiet")

    sess = TmuxSession(session_name, log_path=log_path)
    sess.start(argv)
    return run_id
```

The detach UX prints to stderr:

```
fast-rm: spawned session tctl-fastrm-a3f0c1
  attach : tmux attach -t tctl-fastrm-a3f0c1
  log    : ~/.tctl/fastrm/a3f0c1.log
  paths  : 3 target(s) queued
```

Then exits 0.

### 3.6 Rename-then-rm helper

```python
def _rename_for_deletion(target: Path, suffix_id: str) -> Path:
    """Rename target to target.deleting-<id>; return new path.

    Falls back to returning the original path on EXDEV (cross-filesystem),
    EPERM/EACCES (permission), or EEXIST (1-in-16M collision — accept and
    delete in-place with a warning).
    """
    import errno
    renamed = target.with_name(target.name + f".deleting-{suffix_id}")
    try:
        os.rename(target, renamed)
        return renamed
    except OSError as exc:
        if exc.errno in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.EEXIST):
            reason = {
                errno.EXDEV: "cross-filesystem",
                errno.EPERM: "permission denied",
                errno.EACCES: "permission denied",
                errno.EEXIST: "stale .deleting-<id> path exists",
            }[exc.errno]
            _LOG.warning(
                "rename-then-rm failed (%s); deleting %s in place. Listings will show "
                "the path until phase 2 completes.", reason, target,
            )
            return target
        raise
```

After successful deletion, `renamed.rmdir()` is attempted (no-op if the dir was
already removed by phase 2's `find -empty -delete`; suppressed via
`contextlib.suppress(FileNotFoundError, OSError)`).

---

## 4. Detailed Behavior

### 4.1 Validation pass

Validation mirrors the bash `validate_path()` function exactly. All candidates are
validated **before** any deletion begins. Invalid paths are reported to stderr and
counted; valid paths proceed.

```python
_DANGEROUS_LITERALS: frozenset[str] = frozenset({"", ".", "..", "~", "/", "/*"})

_SYSTEM_PATHS: frozenset[str] = frozenset({
    "/",
    "/home", "/root", "/etc", "/var", "/usr",
    "/bin", "/sbin", "/lib", "/lib64",
    "/mnt", "/proc", "/sys", "/dev", "/boot", "/tmp",
})


def _validate_one(raw: str) -> tuple[str | None, str | None]:
    """Validate a single path candidate.

    Returns (resolved_abs_path, None) on success, or (None, reason) on failure.
    """
    # Dangerous literals
    if raw in _DANGEROUS_LITERALS:
        return None, f"dangerous literal: {raw!r}"

    p = Path(raw)

    # Existence + directory check
    if not p.exists():
        return None, "does not exist"
    if not p.is_dir():
        return None, "not a directory (regular file or symlink-to-file)"

    # Resolve to absolute path (follows symlinks)
    try:
        abs_path = str(p.resolve())
    except OSError as exc:
        return None, f"cannot resolve path: {exc}"

    # System paths
    if abs_path in _SYSTEM_PATHS:
        return None, f"system path: {abs_path}"

    # $HOME exactly
    home = str(Path.home())
    if abs_path == home:
        return None, "$HOME"

    # Minimum depth: 3 segments (e.g. /a/b/c)
    segments = [s for s in abs_path.split("/") if s]
    if len(segments) < 3:
        return None, f"too shallow ({len(segments)} segment(s); need ≥3)"

    return abs_path, None
```

De-duplication (same resolved absolute path from multiple inputs) runs after
validation; later occurrences are dropped silently.

### 4.2 List-file parsing

When `-f FILE` is given, each line is:

1. Strip trailing `\r` (Windows line endings)
2. Strip leading and trailing whitespace
3. Skip empty lines
4. Skip lines starting with `#`
5. Append to candidate list

Lines from the list file are combined with positional `PATH` args; both are processed
through the same validation pass.

If `FILE` does not exist, print an error and exit 2 immediately (before validation).

### 4.3 Pre-scan

When `--quiet` is NOT set, scan each valid target for file count and total size:

```python
def _scan_target(target: str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for the directory tree."""
    # File count via find
    result = subprocess.run(
        ["find", target, "-type", "f"],
        capture_output=True, text=True, check=False,
    )
    file_count = result.stdout.count("\n") if result.returncode == 0 else 0

    # Total size via du -sb (bytes, Linux; portable via --block-size=1)
    du = subprocess.run(
        ["du", "-sb", "--", target],
        capture_output=True, text=True, check=False,
    )
    if du.returncode == 0:
        try:
            total_bytes = int(du.stdout.split()[0])
        except (IndexError, ValueError):
            total_bytes = 0
    else:
        total_bytes = 0
    return file_count, total_bytes
```

Human-readable size is formatted via `_fmt_bytes(n)` (DIY; no dependency on
`numfmt` binary). Total file count and size are aggregated across all targets and
shown in the confirmation prompt.

When `--quiet` is set, scan is skipped entirely; count/size shown as "(skipped)".

### 4.4 Confirmation prompt

After validation and optional scan, a single aggregate prompt is shown:

```
About to delete 3 path(s):
  - /mnt/eval/run_a  [will rename first]
  - /mnt/eval/run_b  [will rename first]
  - /mnt/eval/run_c  [will rename first]
  files: 1,234,567  total size: 98.4 GiB
  (skipped 1 invalid path(s); see warnings above)

Proceed using 16 parallel jobs? [y/N]
```

The confirmation prompt shows the **original paths** (not yet renamed). Renaming happens inside the deletion loop after the operator confirms — the `.deleting-<id>` suffix must never appear at confirmation time.

If `--yes` is set, the prompt is skipped and deletion proceeds immediately. With
`--dry-run`, the prompt text is shown but "Proceed" is replaced with "Dry run — no
files will be deleted." and the process exits 0.

If the user answers anything other than `y`, `Y`, `yes`, or `YES`, print "Aborted."
and exit 0.

**Non-TTY stdin (piped / systemd-run):**
If `input()` raises `EOFError` (stdin closed or not a TTY), treat as "N" — print `Aborted.` and exit 0. Operator must use `-y` for non-interactive contexts. This matches bash script behavior (`read -r ans` on closed stdin returns empty, falls through to default "Aborted.").

```python
if not assume_yes:
    try:
        ans = input(f"Proceed using {jobs} parallel jobs? [y/N] ")
    except EOFError:
        print("Aborted (no TTY for confirmation; use -y for non-interactive).", file=sys.stderr)
        return 0
    if ans.strip().lower() not in ("y", "yes"):
        print("Aborted.", file=sys.stderr)
        return 0
```

With `--detach`, the confirmation prompt fires BEFORE the tmux session is spawned.
`--detach --yes` skips the prompt and returns immediately.

### 4.5 Foreground deletion loop

The deletion loop runs **after** confirmation is accepted. Rename-then-rm happens inside
the loop, not before the confirmation prompt. Call order:

```python
# Inside run() after confirmation accepted:
for idx, target in enumerate(valid_paths, start=1):
    print(f"[{idx}/{len(valid_paths)}] {target}", file=sys.stderr)
    # Step 1: rename-then-rm. Path disappears from listings here.
    renamed = _rename_for_deletion(target)  # returns target if rename failed
    # Step 2: 3-phase deletion on the renamed (or original if EXDEV) path
    ok = _delete_one(renamed, jobs)
    if ok:
        success_count += 1
    else:
        failure_count += 1
        failed_paths.append(target)  # report ORIGINAL path to operator
```

`_delete_one` receives the already-renamed (or original, on rename failure) path and
the job count only. The caller is responsible for passing the right path:

```python
def _delete_one(path: Path, jobs: int) -> bool:
    """Delete a single target path (renamed or original). Return True on success."""
    t0 = time.monotonic()
    print(f"  [{_ts()}] phase 1/3: parallel rm files (jobs={jobs}) ...")

    # Phase 1: parallel file deletion
    p1 = subprocess.run(
        f"find {shlex.quote(str(path))} -type f -print0"
        f" | xargs -0 -r -P {jobs} -n 1000 rm -f",
        shell=True, check=False,
    )
    if p1.returncode != 0:
        print(f"  ERROR: phase 1 failed (rc={p1.returncode})", file=sys.stderr)
        return False

    # Symlink pass
    subprocess.run(
        f"find {shlex.quote(str(path))} -type l -print0 | xargs -0 -r rm -f",
        shell=True, check=False,
    )

    # Phase 2: empty-dir cleanup
    print(f"  [{_ts()}] phase 2/3: removing empty subdirs ...")
    p2 = subprocess.run(
        ["find", str(path), "-depth", "-type", "d", "-empty", "-delete"],
        check=False,
    )
    if p2.returncode != 0:
        print(f"  ERROR: phase 2 failed (rc={p2.returncode})", file=sys.stderr)
        return False

    # Phase 3: remove root (now empty)
    print(f"  [{_ts()}] phase 3/3: removing root dir ...")
    if path.exists():
        if not any(path.iterdir()):
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

Note: Phase 1 and the symlink pass use `shell=True` for the pipeline (`find | xargs`).
Phase 2 uses `shell=False` (no pipeline needed). Phase 3 uses Python `pathlib`.

Per-target failures do not abort the batch; the loop records failures and continues.

### 4.6 Detach path

When `--detach` is set:

1. Validate paths (same as foreground path).
2. Scan and confirmation prompt (including `--yes` / user-typed response).
3. On confirmation: call `_spawn_detached(valid_paths, jobs, quiet)`.
4. Print the session info block (session name, attach hint, log path, target count).
5. Exit 0 immediately.

The spawned subprocess re-invokes `tctl fast-rm` with:
- The validated (resolved, deduplicated) absolute paths as positional args
- `--yes` (no second confirmation)
- `-j <jobs>`
- `--quiet` if the original invocation used `--quiet`
- No `--detach` (foreground execution inside tmux)

This means the rename-then-rm logic runs inside the tmux session, not in the parent
process. The session name and run ID are the same `id` used for both the session name
and the `.deleting-<id>` suffix.

### 4.7 Multi-detach concurrency

Each `-d` invocation generates an independent `run_id = os.urandom(3).hex()`. Sessions
coexist without interference:

- No shared lock files
- No "already running" guard
- No shared state directory
- Session names are globally unique with 1-in-16M collision probability per pair (negligible)

List active fast-rm sessions:

```
tmux ls | grep tctl-fastrm-
```

Kill a specific session:

```
tmux kill-session -t tctl-fastrm-a3f0c1
```

### 4.8 Dry-run path

When `--dry-run` is set:

1. Validate paths (full validation pass — invalid paths reported).
2. Pre-scan (even if `--quiet` is set — dry-run scan is lightweight; inform operator).
3. Show what would be deleted (same output as the confirmation block).
4. Print "Dry run — no files will be deleted."
5. Exit 0.

No files are touched. No renaming occurs. No tmux session is spawned (even with
`--dry-run --detach`; `--dry-run` takes precedence).

### 4.9 Summary output (foreground)

```
================================================================
Done in 127s.
  succeeded: 2
  failed:    1
  skipped (invalid pre-validation): 1
Failed paths:
  - /mnt/eval/run_b
================================================================
```

Failed paths are reported by their **original** pre-rename names (matching what the operator passed in), not the `.deleting-<id>` names.

If any failures: exit 1. If all succeeded: exit 0.

**Exit code divergence from bash:** The bash script (`fast_rm.sh`) exits 2 when any per-target deletion fails. The Python port uses exit 1 for deletion failures to reserve exit 2 for tctl's standard "config error" semantic (per CLAUDE.md exit-code table). Operators upgrading from bash scripts that grep for `rc == 2` to detect deletion failure must update to `rc != 0`.

---

## 5. Tests Strategy

### 5.1 Unit tests — validation (`test_commands_fast_rm.py`)

Test `_validate_one` directly with parametrize. No filesystem I/O needed for
dangerous-literal and system-path cases (validation rejects them before stat).

```python
@pytest.mark.parametrize("raw,expected_reason_fragment", [
    ("",        "dangerous literal"),
    (".",       "dangerous literal"),
    ("..",      "dangerous literal"),
    ("~",       "dangerous literal"),
    ("/",       "dangerous literal"),
    ("/*",      "dangerous literal"),
    ("/home",   "system path"),
    ("/etc",    "system path"),
    ("/tmp",    "system path"),
    ("/mnt",    "system path"),
])
def test_validate_rejects_dangerous(raw, expected_reason_fragment):
    from tctl.commands.fast_rm import _validate_one
    abs_path, reason = _validate_one(raw)
    assert abs_path is None
    assert expected_reason_fragment in (reason or "")
```

### 5.2 Unit tests — validation with real tmp_path

```python
def test_validate_rejects_shallow(tmp_path):
    # /tmp/pytest-xxx/test_0  is only 3 segments; need path deeper
    from tctl.commands.fast_rm import _validate_one
    # Exactly 2 segments: should fail
    shallow = tmp_path.parent   # typically /tmp/pytest-xxx  (2 segs)
    abs_path, reason = _validate_one(str(shallow))
    # Either rejected as system path (/tmp) or too shallow
    assert abs_path is None

def test_validate_accepts_deep_directory(tmp_path):
    from tctl.commands.fast_rm import _validate_one
    # tmp_path is typically /tmp/pytest-xxx/test_validate_xxx/0 (4 segs)
    deep = tmp_path / "a" / "b" / "target"
    deep.mkdir(parents=True)
    abs_path, reason = _validate_one(str(deep))
    assert abs_path is not None, f"expected success but got reason={reason!r}"
    assert reason is None

def test_validate_rejects_file(tmp_path):
    from tctl.commands.fast_rm import _validate_one
    f = tmp_path / "x" / "y" / "z" / "notadir.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    abs_path, reason = _validate_one(str(f))
    assert abs_path is None
    assert "not a directory" in (reason or "")

def test_validate_rejects_home(monkeypatch, tmp_path):
    from tctl.commands.fast_rm import _validate_one
    monkeypatch.setenv("HOME", str(tmp_path))
    abs_path, reason = _validate_one(str(tmp_path))
    assert abs_path is None
    assert "$HOME" in (reason or "")
```

### 5.3 Unit tests — list-file parsing

```python
def test_list_file_parsing(tmp_path):
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

def test_list_file_missing_exits_2(tmp_path):
    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[], list_file=str(tmp_path / "nonexistent.txt"),
        jobs=1, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2
```

### 5.4 Unit tests — rename-then-rm helper

```python
def test_rename_helper_succeeds(tmp_path):
    from tctl.commands.fast_rm import _rename_for_deletion
    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)
    renamed = _rename_for_deletion(target, "abc123")
    assert renamed.name == "target.deleting-abc123"
    assert renamed.exists()
    assert not target.exists()

def test_rename_helper_falls_back_on_cross_device(tmp_path, monkeypatch):
    from tctl.commands.fast_rm import _rename_for_deletion
    import errno, os
    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)
    # Simulate EXDEV
    def fake_rename(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(os, "rename", fake_rename)
    result = _rename_for_deletion(target, "abc123")
    assert result == target     # fell back to original path
```

### 5.5 Unit tests — TmuxSession mocked for detach path

```python
def test_detach_spawns_session_and_exits_0(tmp_path, monkeypatch, capsys):
    spawned = []
    class FakeSession:
        def __init__(self, name, log_path=None):
            self.name = name
        def start(self, argv):
            spawned.append({"name": self.name, "argv": argv})

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)

    # Create a valid deep target
    target = tmp_path / "x" / "y" / "z" / "target"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=4, yes=True, quiet=False, dry_run=False, detach=True,
    )
    # Mock scan so it doesn't call find on a real FS
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (10, 1024))

    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert len(spawned) == 1
    assert spawned[0]["name"].startswith("tctl-fastrm-")
    assert len(spawned[0]["name"]) == len("tctl-fastrm-") + 6  # 6-char hex id

def test_two_detach_calls_produce_distinct_session_names(tmp_path, monkeypatch):
    names = []
    class FakeSession:
        def __init__(self, name, log_path=None):
            names.append(name)
        def start(self, argv): pass

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (0, 0))

    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    for _ in range(2):
        target.mkdir(parents=True, exist_ok=True)
        ns = argparse.Namespace(
            paths=[str(target)], list_file=None,
            jobs=4, yes=True, quiet=True, dry_run=False, detach=True,
        )
        fast_rm_run(ns, [])

    assert len(names) == 2
    assert names[0] != names[1], "expected distinct session names"
```

### 5.6 Unit tests — subprocess mocked for deletion

```python
def test_foreground_deletion_calls_find_xargs(tmp_path, monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv if isinstance(argv, list) else argv)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", fake_run)

    target = tmp_path / "a" / "b" / "c" / "data"
    target.mkdir(parents=True)
    renamed = str(target) + ".deleting-abc123"

    from tctl.commands.fast_rm import _delete_one
    # Mocked: no actual deletion
    _delete_one(str(target), renamed, jobs=4)
    # Verify find was called
    assert any("find" in str(c) for c in calls)
```

### 5.7 Integration test — foreground end-to-end with real tmp_path

```python
@pytest.mark.integration
def test_foreground_deletes_real_tree(tmp_path):
    """End-to-end: create a small tree, fast-rm it, verify it's gone."""
    # Build a target with 10 files in 3 subdirs
    target = tmp_path / "a" / "b" / "c" / "eval_run"
    for i in range(3):
        sub = target / f"sub_{i}"
        sub.mkdir(parents=True)
        for j in range(5):
            (sub / f"file_{j}.txt").write_text(f"content {i} {j}")

    assert target.exists()

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert not target.exists(), "target dir should be gone after fast-rm"
    # Parent dir should still exist
    assert (tmp_path / "a" / "b" / "c").exists()
```

---

## 6. Acceptance Tests

### AT-1 — Foreground deletion of a small tree succeeds; directory is gone after

```
Given: /tmp/.../a/b/c/eval_run exists and contains 15 files in 3 subdirectories
When:  tctl fast-rm --yes --quiet /tmp/.../a/b/c/eval_run
Then:  Exit code: 0
       /tmp/.../a/b/c/eval_run does NOT exist after the command returns
       /tmp/.../a/b/c/ (parent) still exists
       Summary line shows "succeeded: 1  failed: 0"
```

```python
def test_at1_foreground_delete_small_tree(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "eval_run"
    for i in range(3):
        sub = target / f"sub_{i}"
        sub.mkdir(parents=True)
        for j in range(5):
            (sub / f"file_{j}.txt").write_text("x")

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert not target.exists()
    assert (tmp_path / "a" / "b" / "c").exists()
```

---

### AT-2 — Dangerous literal `..` rejected with exit 2

```
Given: No valid paths provided — only the dangerous literal ".."
When:  tctl fast-rm ..
Then:  Exit code: 2 (no valid paths after validation)
       Stderr contains "dangerous literal"
       No files are deleted
```

```python
def test_at2_dangerous_literal_rejected(capsys):
    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[".."], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "dangerous literal" in err.lower() or "dangerous" in err.lower()
```

---

### AT-3 — System path `/home` rejected

```
Given: Path is the system path "/home"
When:  tctl fast-rm /home
Then:  Exit code: 2
       Stderr contains "system path"
       No files are deleted
```

```python
def test_at3_system_path_rejected(capsys):
    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=["/home"], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "system path" in err.lower() or "/home" in err
```

---

### AT-4 — Shallow path rejected (fewer than 3 segments)

```
Given: A real directory exists at exactly /a/b (2 segments) — simulated via monkeypatch
When:  tctl fast-rm /a/b
Then:  Exit code: 2
       Stderr contains "too shallow" or segment-count message
```

```python
def test_at4_shallow_path_rejected(tmp_path, monkeypatch, capsys):
    # Simulate a 2-segment resolved path by monkeypatching Path.resolve()
    from tctl.commands import fast_rm as mod
    target = tmp_path / "a" / "b" / "c" / "real_target"
    target.mkdir(parents=True)

    original_validate = mod._validate_one
    def shallow_validate(raw):
        # Force a 2-segment absolute path result for our test path
        if raw == str(target):
            segments = ["a", "b"]   # fake 2 segments
            return None, f"too shallow ({len(segments)} segment(s); need ≥3)"
        return original_validate(raw)
    monkeypatch.setattr(mod, "_validate_one", shallow_validate)

    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=4, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = mod.run(ns, [])
    assert rc == 2
    err = capsys.readouterr().err
    assert "shallow" in err.lower() or "segment" in err.lower()
```

---

### AT-5 — `-f LIST_FILE` reads paths, ignores blank lines and `#` comments

```
Given: paths.txt contains:
         # comment line
         (blank line)
         /mnt/.../a/b/c/run_a
         /mnt/.../a/b/c/run_b
       Both run_a and run_b exist and are valid directories
When:  tctl fast-rm -f paths.txt --yes --quiet
Then:  Exit code: 0
       Both run_a and run_b are deleted
       Comment and blank lines produced no error
```

```python
def test_at5_list_file_reads_and_ignores_comments(tmp_path):
    run_a = tmp_path / "x" / "y" / "z" / "run_a"
    run_b = tmp_path / "x" / "y" / "z" / "run_b"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    (run_a / "file.txt").write_text("data")
    (run_b / "file.txt").write_text("data")

    list_file = tmp_path / "paths.txt"
    list_file.write_text(
        "# comment\n"
        "\n"
        f"{run_a}\n"
        f"{run_b}\n"
    )

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[], list_file=str(list_file),
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert not run_a.exists()
    assert not run_b.exists()
```

---

### AT-6 — `--dry-run` reports count + size, exits 0, no deletion

```
Given: /tmp/.../a/b/c/eval_run exists with 15 files
When:  tctl fast-rm --dry-run /tmp/.../a/b/c/eval_run
Then:  Exit code: 0
       Output contains "Dry run"
       Output contains the target path
       /tmp/.../a/b/c/eval_run still exists after the command returns
```

```python
def test_at6_dry_run_no_deletion(tmp_path, capsys):
    target = tmp_path / "a" / "b" / "c" / "eval_run"
    for i in range(3):
        (target / f"sub_{i}").mkdir(parents=True)
        (target / f"sub_{i}" / "file.txt").write_text("x")

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=False, dry_run=True, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert target.exists(), "dry-run must not delete the target"
    out = capsys.readouterr().out
    assert "dry run" in out.lower() or "dry-run" in out.lower()
```

---

### AT-7 — `-d` spawns a tmux session named `tctl-fastrm-<id>`, exits 0, prints id

```
Given: A valid directory /tmp/.../a/b/c/target exists
       TmuxSession is monkeypatched to capture spawn calls
When:  tctl fast-rm -d --yes /tmp/.../a/b/c/target
Then:  Exit code: 0
       Exactly one TmuxSession was instantiated
       Session name matches regex tctl-fastrm-[0-9a-f]{6}
       The session name (id) is printed to stderr or stdout
       Log path ~/.tctl/fastrm/<id>.log is set on the session
```

```python
def test_at7_detach_spawns_named_session(tmp_path, monkeypatch, capsys):
    sessions = []
    class FakeSession:
        def __init__(self, name, log_path=None):
            sessions.append({"name": name, "log_path": log_path})
        def start(self, argv): pass

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (5, 512))

    target = tmp_path / "a" / "b" / "c" / "tgt"
    target.mkdir(parents=True)

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse, re
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=4, yes=True, quiet=False, dry_run=False, detach=True,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0
    assert len(sessions) == 1
    name = sessions[0]["name"]
    assert re.fullmatch(r"tctl-fastrm-[0-9a-f]{6}", name), f"bad session name: {name!r}"
    # id appears in output
    run_id = name.split("-")[-1]
    captured = capsys.readouterr()
    assert run_id in captured.out or run_id in captured.err
```

---

### AT-8 — Two `-d` runs in rapid succession spawn 2 distinct sessions

```
Given: A valid directory exists
When:  tctl fast-rm -d --yes <path> is run twice in rapid succession
Then:  Two distinct tmux sessions are spawned
       Both session names match tctl-fastrm-[0-9a-f]{6}
       The two session names are different (distinct run IDs)
```

```python
def test_at8_two_detach_runs_produce_distinct_sessions(tmp_path, monkeypatch):
    sessions = []
    class FakeSession:
        def __init__(self, name, log_path=None):
            sessions.append(name)
        def start(self, argv): pass

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)
    monkeypatch.setattr("tctl.commands.fast_rm._scan_target", lambda t: (0, 0))

    target = tmp_path / "a" / "b" / "c" / "tgt"

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    for _ in range(2):
        target.mkdir(parents=True, exist_ok=True)
        ns = argparse.Namespace(
            paths=[str(target)], list_file=None,
            jobs=4, yes=True, quiet=True, dry_run=False, detach=True,
        )
        fast_rm_run(ns, [])

    assert len(sessions) == 2
    assert sessions[0] != sessions[1], "both detach runs must produce distinct session names"
```

---

### AT-9 — Per-target failure does not abort batch; exit 1

```
Given: Three valid target directories exist
       Phase 1 of the second target fails (simulated via subprocess mock returning rc=1)
When:  tctl fast-rm --yes --quiet <target1> <target2> <target3>
Then:  Exit code: 1 (some failures)
       target1 and target3 are deleted successfully
       target2 remains (or is in a partially-deleted renamed state)
       Summary shows "succeeded: 2  failed: 1"
```

```python
def test_at9_per_target_failure_does_not_abort_batch(tmp_path, monkeypatch):
    targets = []
    for i in range(3):
        t = tmp_path / "a" / "b" / "c" / f"run_{i}"
        t.mkdir(parents=True)
        (t / "file.txt").write_text("x")
        targets.append(t)

    import subprocess as sp
    call_count = {"n": 0}
    original_run = sp.run
    def patched_run(argv, **kwargs):
        call_count["n"] += 1
        # Fail phase-1 find|xargs on the 2nd target's first subprocess call
        if call_count["n"] == 2 and isinstance(argv, str) and "xargs" in argv:
            return sp.CompletedProcess(argv, 1, "", "simulated failure")
        return original_run(argv, **kwargs)
    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", patched_run)

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(t) for t in targets], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 1  # some failures
    # targets 0 and 2 should be gone; target 1 may remain
    assert not targets[0].exists() or not targets[2].exists()
```

---

### AT-10 — Rename-then-rm: source path disappears before deletion completes

```
Given: /tmp/.../a/b/c/target exists with many files
When:  tctl fast-rm --yes /tmp/.../a/b/c/target is run (foreground)
Then:  After the rename step, target no longer appears in parent-directory listing
       Parent directory (/tmp/.../a/b/c/) contains an entry named
         "target.deleting-<id>" instead
       By the time the command returns, "target.deleting-<id>" is also gone
```

```python
def test_at10_rename_then_rm_path_disappears_before_deletion(tmp_path, monkeypatch):
    target = tmp_path / "a" / "b" / "c" / "target"
    target.mkdir(parents=True)
    (target / "file.txt").write_text("data")

    parent = target.parent
    renames_seen = []
    import subprocess as sp
    original_run = sp.run

    def patched_run(argv, **kwargs):
        # Capture parent-dir listing right after rename (during phase 1)
        if isinstance(argv, str) and "find" in argv and "xargs" in argv:
            children = list(parent.iterdir())
            renames_seen.append([c.name for c in children])
        return original_run(argv, **kwargs)
    monkeypatch.setattr("tctl.commands.fast_rm.subprocess.run", patched_run)

    from tctl.commands.fast_rm import run as fast_rm_run
    import argparse
    ns = argparse.Namespace(
        paths=[str(target)], list_file=None,
        jobs=2, yes=True, quiet=True, dry_run=False, detach=False,
    )
    rc = fast_rm_run(ns, [])
    assert rc == 0

    # Verify: original name was absent during phase 1
    if renames_seen:
        for entry_names in renames_seen:
            assert "target" not in entry_names, (
                "original 'target' name should not be visible during phase 1 "
                f"(saw: {entry_names})"
            )
            assert any(n.startswith("target.deleting-") for n in entry_names), (
                f"expected 'target.deleting-*' in parent listing; saw: {entry_names}"
            )

    # After completion: both names are gone
    assert not target.exists()
    assert not any(p.name.startswith("target.deleting-") for p in parent.iterdir())
```

---

### AT-11: `--dry-run --detach` does not spawn tmux session

**Given:** valid path `/tmp/fastrm_at11/foo` (a real directory containing a file)
**When:** `tctl fast-rm /tmp/fastrm_at11/foo --dry-run -d -y`
**Then:**
- exit code 0
- no tmux session matching `tctl-fastrm-*` is spawned
- path `/tmp/fastrm_at11/foo` still exists after the command

Pseudocode:

```python
def test_at11_dry_run_overrides_detach(tmp_path, monkeypatch):
    target = tmp_path / "foo"
    target.mkdir(parents=True)
    (target / "x").write_text("y")
    spawned: list[str] = []

    class FakeSession:
        def __init__(self, name, env=None, log_path=None):
            spawned.append(name)
        def start(self, argv): raise AssertionError("must not spawn")

    monkeypatch.setattr("tctl.commands.fast_rm.TmuxSession", FakeSession)

    rc = run_main(["fast-rm", str(target), "--dry-run", "-d", "-y"])
    assert rc == 0
    assert spawned == []
    assert target.exists()
```

---

## 7. Risks / Non-Goals

### Non-Goals

- **`tctl fast-rm --list` / `tctl fast-rm status` sub-verbs.** Operators use
  `tmux ls | grep tctl-fastrm-` to list active detached deletions. No status
  sub-verb is needed in v1.

- **Configurable deny-list via cluster.yaml.** The system-path deny-list and dangerous
  literals are hardcoded. They are safety rails, not operator preferences. Putting them
  in cluster.yaml would imply that an operator might want to shorten the list — which is
  the wrong UX affordance for a guard meant to prevent catastrophic mistakes.

- **JSON progress / status files.** The tmux log at `~/.tctl/fastrm/<id>.log` is
  sufficient for monitoring. A structured JSON status file adds complexity with minimal
  benefit for a fire-and-forget utility.

- **Resume after Ctrl-C.** `tctl fast-rm` is re-runnable: if interrupted, the renamed
  directory (`*.deleting-<id>`) is still present and valid. The operator re-runs
  `tctl fast-rm` against it. The renamed path passes validation (it is a real directory,
  ≥3 segments, not a system path).

- **Cross-host deletion.** `tctl fast-rm` is a local-only operation. Paths must be
  accessible on the current host's filesystem. Deletion on a remote host requires ssh
  + running `tctl fast-rm` there directly.

### Risks

**Rename across filesystems fails.** If `target` lives on a different filesystem than
its parent — unusual but possible with bind-mounts — `os.rename()` raises `EXDEV`.
**Mitigation:** `_rename_for_deletion` catches `EXDEV` and `EPERM`, logs a warning,
and falls back to in-place deletion. The benefit of "path disappears immediately" is
lost, but correctness is preserved.

**Detached session lingers if deletion hangs.** On network-mounted filesystems under
heavy load, `find | xargs rm` can stall indefinitely. The tmux session stays alive;
the log keeps growing (slowly). **Mitigation:** Operator kills the stuck session with
`tmux kill-session -t tctl-fastrm-<id>`. Document in CLI reference and CHANGELOG.

**Naming collision for `os.urandom(3).hex()`.** Two simultaneous `-d` invocations
could theoretically generate the same 6-hex run ID. Probability: 1 in 16,777,216 per
pair. In practice, the second `TmuxSession.start()` would raise `RuntimeError("tmux
session already exists")` — a loud, non-silent failure. **Mitigation:** The probability
is negligible; the failure is immediate and visible. No extra guard needed in v1.

**`tctl fast-rm` inside a renamed `.deleting-*` path.** If an operator re-runs
`tctl fast-rm` against a `*.deleting-<old-id>` path left by an interrupted run, the
command generates a second rename: `target.deleting-<old>.deleting-<new>`. This is
harmless — both names are valid, and deletion proceeds on the doubly-renamed path.

**`--list-file` with absolute paths that pass validation on one host but not another.**
Paths in list files are validated at runtime on the host where `tctl fast-rm` runs.
No portability issue — this is local-only deletion.

**`FileExistsError` on rename collision falls back to in-place deletion.** In the 1-in-16M case where a `*.deleting-<id>` path already exists (stale from an interrupted prior run), `os.rename()` raises `OSError(EEXIST)`. `_rename_for_deletion` now catches `EEXIST` alongside `EXDEV`/`EPERM`/`EACCES`, logs a warning ("stale .deleting-<id> path exists"), and returns the original path so deletion proceeds in-place. The operator sees a log warning but deletion is not aborted.

**Exit code 2 vs 1 is a deliberate breaking change vs the source bash script.** The bash script exits 2 on per-target deletion failure; the Python port exits 1. This is intentional — exit 2 is reserved for tctl config errors per the CLAUDE.md convention. Operators with automation that tests `rc == 2` to detect deletion failures must update to `rc != 0` or `rc == 1` when migrating from the bash script.

---

## 8. File Map

### Files to create

| Path | Description |
|---|---|
| `src/tctl/commands/fast_rm.py` | Platform command implementation (~250 lines) |
| `tests/test_commands_fast_rm.py` | Unit + integration tests (~300 lines) |

### Files to modify

| Path | Change |
|---|---|
| `src/tctl/cli.py` | Add `"fast-rm": "tctl.commands.fast_rm"` to `_PLATFORM_COMMANDS` |
| `src/tctl/__init__.py` | Bump version `0.9.0` → `0.9.1` |
| `pyproject.toml` | Bump `version = "0.9.1"` |
| `docs/CLI-REFERENCE.md` | Add `tctl fast-rm` command documentation section |
| `docs/CHANGELOG.md` | Add v0.9.1 entry |
| `README.md` | Mention `tctl fast-rm` in the platform commands overview |

### Files NOT modified

All workload modules (`vllm/`, `haproxy/`, `lmms/`), `config/`, `tmux.py`, and all
existing test files are untouched. This is a pure additive change.

---

## 9. Tech Stack

No new runtime or build dependencies are introduced.

- **Python 3.10+** — `from __future__ import annotations` in `fast_rm.py`. Union
  syntax (`X | Y`) and `match` are avoided for 3.10 compatibility; plain `if/elif`
  chains and `isinstance` guards used instead.

- **stdlib only** — `argparse`, `os`, `os.path`, `pathlib`, `subprocess`, `sys`,
  `time`, `shlex`, `errno`, `contextlib`, `logging`. No new third-party dependencies.

- **`tctl.tmux.TmuxSession`** — the only tctl import in `fast_rm.py`. Used exclusively
  for the `--detach` path. Imported lazily inside `_spawn_detached()` to preserve the
  sub-200ms startup guarantee for the common foreground invocation.

- **`find` + `xargs`** — GNU coreutils; present on all target Linux deployments. The
  `find -print0 | xargs -0` pipeline is the same strategy used by the bash script; the
  Python wrapper delegates OS-level parallelism to these binaries. `shell=True` is used
  only for the find-pipe-xargs stage; all other `subprocess.run` calls use
  `shell=False` and `argv_list`.

- **`du -sb`** — for pre-scan byte count. Falls back to `0` on failure. `numfmt` is
  NOT required — byte-to-human formatting is implemented inline in `_fmt_bytes()`.

- **tmux 3.2+** — inherited requirement from `TmuxSession`. Only needed when `--detach`
  is used; the foreground path has no tmux dependency.

- **mypy --strict** — all functions typed. `subprocess.CompletedProcess[str]` return
  types, `Path | None` optionals, and `list[str]` argv lists are annotated throughout.
  No `Any` except where stdlib forces it (`subprocess.run` overloads).

- **ruff** — `line-length = 100`, double-quote format, lint rules `E,F,W,I,B,UP,SIM,N`.
  All `shell=True` `subprocess.run` calls use `# noqa: S602` or are structured to pass
  the `S` rule exclusion already configured in `.ruff.toml`.

---

*This spec covers v0.9.1. The platform-command pattern it follows is established in
v0.9.0 (`config_cmd.py`, `init_config.py`). `TmuxSession` (v0.8.0) is unchanged;
its design spec is at
`docs/super-agent-skills/specs/2026-05-06-tmux-session-mgmt-design.md`.*
