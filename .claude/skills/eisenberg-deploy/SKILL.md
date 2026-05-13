---
name: eisenberg-deploy
description: Full deploy pipeline — test, typecheck, deploy to HAOS, smoke test
---

Run the full deployment pipeline. Abort on any failure — never continue past a red gate.

## Why this is tar-based, not per-file SSH

Earlier versions of this skill did `for f in *.py; do ssh ... < $f; done`. That
pattern silently dropped files: when ssh's `ControlMaster` reuses a single TCP
connection across iterations, stdin pipes can interleave or close early, and
some `docker exec -i` writes complete with truncated content. The loop exits 0
on every iteration so the failure is invisible until HA throws ImportError on
restart.

The fix: stream the whole tree through a single SSH connection with `tar`, then
verify every deployed file's sha256 against the local copy. One connection,
atomic extract, explicit verification — no silent drops.

## Why we restart twice

The HA Supervisor's component loader calls `pip install pyeisenberg>=X.Y.Z`
on every integration startup, even when the requirement is already satisfied
— pip re-writes the package files from its wheel cache (PyPI's published
version), overwriting our manually-deployed development code. So a single
restart leaves the container running the previous PyPI build, not the dev
copy.

The robust dance: restart once (let HA finish its pip-install settle),
re-deploy the library on top (now nothing else will touch it), then restart
again so HA picks up the dev code. Two restarts, one source of truth.

## Steps

1. **Test suite**: `pytest tests/ -x -q` — abort if any test fails.
2. **Type check**: `pyright eisenberg/ custom_components/` — abort on any new error.
3. **Lint**: `ruff check eisenberg/ tests/ custom_components/` — abort on errors.

4. **Resolve container library path** (Python version changes across HA updates):
   ```bash
   PY_DIR=$(ssh root@homeassistant -p 22222 \
     "docker exec homeassistant python3 -c 'import eisenberg, os; print(os.path.dirname(eisenberg.__file__))'")
   echo "Container eisenberg path: $PY_DIR"
   ```
   If this fails because the package doesn't exist yet (first deploy), create it:
   ```bash
   ssh root@homeassistant -p 22222 \
     "docker exec homeassistant sh -c 'mkdir -p /usr/local/lib/python*/site-packages/eisenberg'"
   ```
   Then re-run the resolve step.

5. **Deploy integration files** (HAOS host filesystem — safe from HA's pip,
   so we do these once before the first restart):
   ```bash
   ssh root@homeassistant -p 22222 \
     "mkdir -p /mnt/data/supervisor/homeassistant/custom_components/eisenberg/translations"
   tar -cf - -C custom_components/eisenberg . | \
     ssh root@homeassistant -p 22222 \
       "tar -xf - -C /mnt/data/supervisor/homeassistant/custom_components/eisenberg/"
   ```

6. **First restart** so HA picks up the new manifest and lets pip-install
   settle on its own copy of the library before we overwrite it:
   ```bash
   ssh root@homeassistant -p 22222 "ha core restart"
   until ssh root@homeassistant -p 22222 "ha core info" >/dev/null 2>&1; do sleep 3; done
   ```

7. **Deploy client library** (single SSH, tar pipe — HA's pip step is done
   so nothing else will touch these files now):
   ```bash
   tar -cf - -C eisenberg . | \
     ssh root@homeassistant -p 22222 \
       "docker exec -i homeassistant tar -xf - -C $PY_DIR/"
   ```

8. **Verify checksums** — catches both tar-pipe corruption and any race
   with pip's reinstall:
   ```bash
   set -e
   # Integration
   for f in custom_components/eisenberg/*.py custom_components/eisenberg/manifest.json; do
     name=$(basename "$f")
     local=$(sha256sum "$f" | awk '{print $1}')
     remote=$(ssh root@homeassistant -p 22222 \
       "sha256sum /mnt/data/supervisor/homeassistant/custom_components/eisenberg/$name | awk '{print \$1}'")
     [ "$local" = "$remote" ] || { echo "MISMATCH: $name"; exit 1; }
   done
   # Library
   for f in eisenberg/*.py; do
     name=$(basename "$f")
     local=$(sha256sum "$f" | awk '{print $1}')
     remote=$(ssh root@homeassistant -p 22222 \
       "docker exec homeassistant sha256sum $PY_DIR/$name | awk '{print \$1}'")
     [ "$local" = "$remote" ] || { echo "MISMATCH: $name"; exit 1; }
   done
   echo "All checksums match"
   ```
   If any mismatch: abort and investigate. A mismatch on the library after
   the first restart usually means HA's pip step hadn't actually finished
   when `ha core info` returned ready — wait longer and re-deploy.

9. **Second restart** so HA loads the dev library now sitting on disk:
   ```bash
   ssh root@homeassistant -p 22222 "ha core restart"
   until ssh root@homeassistant -p 22222 "ha core info" >/dev/null 2>&1; do sleep 3; done
   ```

10. **Log check** — explicitly grep for ERROR lines AFTER the second restart:
    ```bash
    ssh root@homeassistant -p 22222 \
      "docker logs homeassistant --since 2m 2>&1 | grep -iE 'eisenberg.*(error|traceback|importerror)' | tail -30"
    ```
    Empty output = clean. Any line shown = investigate before declaring success.

On failure at any step: stop, show full error, do NOT proceed or auto-fix.
On success: report all gates passed.

**Deploy does NOT release.** Releasing (PyPI + GitHub tag) is a separate manual step.
