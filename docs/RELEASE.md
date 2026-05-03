# Release Runbook

Steps to cut a new `vctl` release.

## 1. Verify CI is green on main

All checks (ruff, mypy, pytest, coverage ≥50%) must pass on the `main` branch before tagging.

```bash
gh run list --branch main --limit 5
```

## 2. Update CHANGELOG.md

If there are entries under `## [Unreleased]`, move them into a new dated section:

```markdown
## [0.1.0] - 2026-05-01
```

Leave `## [Unreleased]` as an empty placeholder above the new section.

## 3. Verify version consistency

Both files must agree and match the intended release tag:

```bash
grep '^version' pyproject.toml          # project.version = "0.1.0"
grep '__version__' src/vctl/__init__.py  # __version__ = "0.1.0"
```

Or run the smoke test directly:

```bash
uv run pytest tests/test_smoke.py::test_pyproject_version_matches_module_version -v
```

## 4. Tag and push

```bash
git tag v0.1.0
git push --tags
```

The `release.yml` GitHub Actions workflow triggers on `v*` tags and builds the distribution.

## 5. Smoke test the published release

After the workflow completes:

```bash
uv tool install --reinstall "git+https://github.com/oscarqjh/vctl.git@v0.1.0"
vctl --help
vctl --version   # must print 0.1.0
```
