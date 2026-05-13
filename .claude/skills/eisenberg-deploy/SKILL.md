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

5. **Deploy integration files** (HAOS host filesystem, single SSH, tar pipe):
   ```bash
   ssh root@homeassistant -p 22222 \
     "mkdir -p /mnt/data/supervisor/homeassistant/custom_components/eisenberg/translations"
   tar -cf - -C custom_components/eisenberg . | \
     ssh root@homeassistant -p 22222 \
       "tar -xf - -C /mnt/data/supervisor/homeassistant/custom_components/eisenberg/"
   ```

6. **Deploy client library** (inside HA Docker container, single SSH, tar pipe):
   ```bash
   tar -cf - -C eisenberg . | \
     ssh root@homeassistant -p 22222 \
       "docker exec -i homeassistant tar -xf - -C $PY_DIR/"
   ```

7. **Verify checksums** — catches any silent corruption from the tar pipes:
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
   If any mismatch: abort, do NOT restart HA. Investigate the tar pipe first.

8. **Restart HA**: `ssh root@homeassistant -p 22222 "ha core restart"`

9. **Wait for HA core to be ready** — poll, don't sleep blindly:
   ```bash
   until ssh root@homeassistant -p 22222 "ha core info" >/dev/null 2>&1; do sleep 3; done
   ```

10. **Log check** — explicitly grep for ERROR lines AFTER the restart marker:
    ```bash
    ssh root@homeassistant -p 22222 \
      "docker logs homeassistant --since 2m 2>&1 | grep -iE 'eisenberg.*(error|traceback|importerror)' | tail -30"
    ```
    Empty output = clean. Any line shown = investigate before declaring success.

On failure at any step: stop, show full error, do NOT proceed or auto-fix.
On success: report all gates passed.

**Deploy does NOT release.** Releasing (PyPI + GitHub tag) is a separate manual step.
