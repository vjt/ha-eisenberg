---
name: eisenberg-release
description: Bump version, publish to PyPI, create GitHub release
---

Release a new version. Takes a version argument (e.g. `/eisenberg-release 0.2.0`).
Abort on any failure.

## Pre-flight checks

1. **Working tree clean?** `git status --porcelain` must be empty. If not, abort — commit or stash first.
2. **Tests pass?** `pytest tests/ -x -q` — abort on failure.
3. **Type check?** `pyright eisenberg/ custom_components/` — abort on errors.
4. **Lint?** `ruff check eisenberg/ tests/ custom_components/` — abort on errors.
5. **On master?** Must be on `master` branch. Abort otherwise.
6. **CHANGELOG.md updated?** Check that the new version has an entry. If not, ask the user what changed and add one.

## Version bump

Three files to update — all three MUST match:

1. `pyproject.toml` — `version = "X.Y.Z"` (line 3)
2. `custom_components/eisenberg/manifest.json` — `"version": "X.Y.Z"`
3. `custom_components/eisenberg/manifest.json` — `"requirements": ["pyeisenberg>=X.Y.Z"]`

After editing, verify all three match:
```bash
grep '"version"' custom_components/eisenberg/manifest.json
grep '^version' pyproject.toml
grep 'pyeisenberg>=' custom_components/eisenberg/manifest.json
```

## Commit and tag

```bash
git add pyproject.toml custom_components/eisenberg/manifest.json CHANGELOG.md
git commit -m "chore: bump to X.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary from CHANGELOG>"
git push origin master --tags
```

**Ask the user for confirmation before pushing.**

## Publish to PyPI

```bash
rm -rf dist/
python3 -m build
source .env
TWINE_USERNAME=__token__ TWINE_PASSWORD="$PYPI_TOKEN" \
  python3 -m twine upload dist/*
```

Verify the upload succeeded — check for the `View at:` URL in twine output.

## GitHub release

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --notes "$(sed -n '/^## X\.Y\.Z/,/^## [0-9]/p' CHANGELOG.md | head -n -1)" \
  dist/*
```

If the sed extraction fails, ask the user for release notes.

## Post-release

1. Verify: `gh release view vX.Y.Z`
2. Report: version, PyPI URL, GitHub release URL
3. Remind: "HACS picks up the new tag automatically. Users update at their own pace."

**Release does NOT deploy.** The user's own HA instance gets the update via HACS, not via this skill.
