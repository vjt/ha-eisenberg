---
name: eisenberg-deploy
description: Full deploy pipeline — test, typecheck, deploy to HAOS, smoke test
---

Run the full deployment pipeline. Abort on any failure — never continue past a red gate.

## Steps

1. **Test suite**: `pytest tests/ -x -q` — abort if any test fails.
2. **Type check**: `pyright eisenberg/ custom_components/` — abort if any new errors.
3. **Lint**: `ruff check eisenberg/ tests/ custom_components/` — abort on errors.
4. **Deploy integration files**: push all `custom_components/eisenberg/*.py` plus
   `manifest.json` and `translations/` to HAOS via SSH pipe:
   ```bash
   for f in custom_components/eisenberg/*.py custom_components/eisenberg/manifest.json; do
     ssh root@homeassistant -p 22222 \
       "cat > /mnt/data/supervisor/homeassistant/custom_components/eisenberg/$(basename $f)" \
       < "$f"
   done
   # Translations
   ssh root@homeassistant -p 22222 \
     "mkdir -p /mnt/data/supervisor/homeassistant/custom_components/eisenberg/translations"
   for f in custom_components/eisenberg/translations/*.json; do
     ssh root@homeassistant -p 22222 \
       "cat > /mnt/data/supervisor/homeassistant/custom_components/eisenberg/translations/$(basename $f)" \
       < "$f"
   done
   ```
5. **Deploy client library**: the `eisenberg/` package is pip-installed inside the HA
   Docker container. Must deploy it separately — the integration imports it at runtime:
   ```bash
   for f in eisenberg/*.py; do
     ssh root@homeassistant -p 22222 \
       "docker exec -i homeassistant sh -c 'cat > /usr/local/lib/python3.14/site-packages/eisenberg/$(basename $f)'" \
       < "$f"
   done
   ```
   **Note**: The Python version path (3.14) may change with HA updates. If deploy fails
   with "No such file or directory", check the actual path with:
   `ssh root@homeassistant -p 22222 "docker exec homeassistant python3 -c \"import eisenberg; print(eisenberg.__file__)\""`

   If the package doesn't exist yet (first deploy), create it:
   ```bash
   ssh root@homeassistant -p 22222 \
     "docker exec homeassistant python3 -c \"import eisenberg\" 2>/dev/null" || \
   ssh root@homeassistant -p 22222 \
     "docker exec homeassistant mkdir -p /usr/local/lib/python3.14/site-packages/eisenberg/"
   ```
6. **Restart HA**: `ssh root@homeassistant -p 22222 "ha core restart"`
7. **Wait**: 30 seconds for HA to fully start.
8. **Log check**: `ssh root@homeassistant -p 22222 "docker logs homeassistant 2>&1 | grep -i eisenberg | tail -30"` — look for ERROR/WARNING.

On failure at any step: stop, show full error, do NOT proceed or auto-fix.
On success: report all gates passed.

**Deploy does NOT release.** Releasing (PyPI + GitHub tag) is a separate manual step.
