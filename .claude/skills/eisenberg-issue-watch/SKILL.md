---
name: eisenberg-issue-watch
description: Poll open ha-eisenberg GitHub issues for reporter replies (esp. debug logs), then analyze and report. Use to (re)start issue monitoring — after /clear, at session start, or when waiting on a reporter's log.
---

Watch the open ha-eisenberg issues that are **blocked on a reporter's reply**
(usually a debug log we asked for), detect when the reporter answers, then pull
the attachment and analyze it. Session-cron based: re-arm it every session — a
session cron dies on `/clear` or exit, so the next session must re-invoke this
skill to resume.

## Repo

`vjt/ha-eisenberg`. All `gh` calls target it explicitly:
`gh api repos/vjt/ha-eisenberg/issues/<N>/comments`.

## Step 1 — Load state, discover what's actually open

The authoritative list of blocked issues + who we're waiting on + the exact
question each log must answer lives in **memory**: read
`project_eisenberg_e2e_status` (the "WATCHED SET" / "Open threads" sections).
Do NOT hardcode issue numbers here — they change. As of this skill's last
edit (2026-07-17) the watched set is **#19, #20, #22** (#15, #21, #23 all
closed — released 0.3.11); e.g.:

- **#19** — reporter `mwebm` — awaiting a **motion-event** debug log to settle
  whether media archival needs a feed/live parse fix or a move to
  `library/add` / `mediaUploadNotification`. See [[arlo-mqtt-event-payloads]].
- **#20** — reporter `scottdiprose-code` — awaiting **battery info** (camera
  model, power type, whether `sensor.<cam>_battery` exists / its state,
  integration+HA versions, startup log).
- **#22** — reporter `blackside17` — awaiting **where connect fails** (config
  flow / MFA / running), exact error, HA install type, outbound 443/8084.

Cross-check against live state before arming — an issue may have been closed:
```bash
gh issue list --repo vjt/ha-eisenberg --state open --json number,title,labels
```
Only watch issues that are (a) open and (b) blocked on a reporter, per memory.

## Step 2 — Poll each watched issue

For each watched issue, the signal is: **the last comment author is the
reporter, not `vjt`** (we always comment last when we hand off). Poll the last
comment AND its edit timestamp:

```bash
for iss in 19 20 22; do
  gh api "repos/vjt/ha-eisenberg/issues/$iss/comments" \
    --jq "last | \"ISSUE$iss last: \(.user.login) created=\(.created_at) edited=\(.updated_at)\""
done
```

- Last author `vjt` → still blocked, no reply. Report "still blocked, no
  reporter reply" for that issue and move on.
- Last author is the reporter → **they replied**. Go to Step 3.

**Edit-aware cross-check (don't skip):** the last-author heuristic misses a
reporter who *edits an earlier comment* to add a log/confirmation — the edit
doesn't change who commented last. So also diff each issue's `updatedAt`
against its last-comment `created_at`:

```bash
gh issue list --repo vjt/ha-eisenberg --state open --json number,updatedAt
```

If `updatedAt` is newer than the last-comment `created_at` (and the last
author is `vjt`), a comment was edited (or a reaction/label changed) — fetch
the full thread with per-comment `updated_at` and check for a reporter edit
before reporting "still blocked". (A #23 reply once landed in the gap between
poll and action, and #15/#23 replies once arrived just after a poll — this
cross-check plus the Step 3b re-check are why.)

## Step 3 — On a reporter reply: fetch, download, analyze

1. Fetch the full comment body:
   ```bash
   gh api "repos/vjt/ha-eisenberg/issues/<N>/comments" \
     --jq '.[] | select(.user.login=="<reporter>") | .created_at, .body'
   ```
2. If it links a log attachment (`https://github.com/user-attachments/...`),
   download it to the scratchpad and analyze:
   ```bash
   curl -sL "<attachment-url>" -o "$SCRATCH/<issue>.log"
   ```
3. Analyze against the issue's specific question (from memory). Useful greps:
   - **Device enumeration / duplicate IDs:** `grep -nE "device id=|already exists"`
   - **SUBACK coverage:** `grep -niE "SUBACK|refused|granted|topic filter"`
   - **Mode / location routing:** `grep -niE "gatewayDeviceId|sharedLocation|not in gateway|set_active_mode|activeMode"`
   - **Media path (#19):** `grep -niE "feed/live|library/add|mediaUpload|MotionEvent|eisenberg_media"`
4. pyaarlo reference for cross-checking Arlo behavior: `~/code/ha/pyaarlo`.
5. Report the finding, update `project_eisenberg_e2e_status` in memory, and (if
   it changes the fix plan) proceed per the user's direction. Do NOT auto-code a
   fix — surface the analysis first.

## Step 3b — Re-check for fresh updates BEFORE posting anything (MANDATORY)

A poll result goes stale the instant you start acting on it — a reporter can
reply in the gap between the poll and your comment, and the last-author
heuristic will have already moved on. **Immediately before you post any comment,
close, reopen, or otherwise hand off an issue**, re-fetch its latest comment and
confirm nothing new landed since the poll you're acting on:

```bash
gh api "repos/vjt/ha-eisenberg/issues/<N>/comments" \
  --jq "last | \"\(.user.login) @ \(.created_at)\""
```

- Unchanged from the poll you analyzed → safe to post.
- A **newer** comment appeared (especially from the reporter) → **STOP**, read
  it, re-run Step 3 against it, and fold it in *before* writing anything. Never
  post a comment built on a snapshot you already know is superseded.

This applies to cron fires too: the state can move between the cron's poll and
its comment. Re-check at **comment time**, not just at poll time. (This rule
exists because a #23 reply from `HippoGlouton` landed between a poll and the
next action and was nearly missed.)

## Step 4 — Re-arm the session cron

Poll on a cadence with `CronCreate` (session-only, in-memory, dies on exit):

- `cron`: `7 */4 * * *` (every 4h at :07 — off-minute on purpose; adjust if the
  user asked for a different cadence)
- `recurring: true`
- `prompt`: a self-contained instruction that repeats Steps 2–3 for the watched
  issues — check last-comment author, and on a reporter reply download+analyze
  the log and report; else report "still blocked".

Then confirm to the user: which issues are watched, the cadence, the cron job
id (for `CronDelete`), and that it dies on `/clear` — the next session re-runs
this skill to resume. If a reporter has **already** replied when you arm it,
handle that reply now (Step 3) before scheduling.

## Notes

- The cosmetic `d/{x}/out/#` wildcard SUBACK refusal is expected on
  base-station accounts and is NOT a bug on its own — per-device
  `allowedMqttTopics` cover what's needed. Don't flag it as the cause of a
  reported symptom without checking the granted per-device topics first.
- Keep `vjt` as the last commenter on every issue you hand off, so the
  last-author heuristic stays reliable.
