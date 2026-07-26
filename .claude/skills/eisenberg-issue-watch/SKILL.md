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

## Step 1 — The open issue list IS the set

**Enumerate every open issue and poll all of them. There is no watched
list.** Anything scoped to a remembered set of numbers cannot see an issue
nobody has told you about — which is how several issues sat unnoticed for
days here, opened and invisible until a manual glance caught them. A reopened
issue has the same problem. Start from the repo, never from a list:

```bash
gh issue list --repo vjt/ha-eisenberg --state open --json number,title,updatedAt
```

That output is the complete set to poll this run. Every number in it gets
checked; no number outside it exists.

**Memory supplies context, not membership.** Read
`project_eisenberg_e2e_status` for what a given issue is blocked on, the exact
question its log must answer, and any per-reporter quirk (e.g. a reporter
whose email-reply attachments GitHub strips, who must upload via the web UI).
An open issue that memory says nothing about is not an error — it is a new one
to triage. Never let memory's silence remove an issue from the poll.

## Step 2 — Poll every open issue

For each number from Step 1, fetch the last comment and its edit timestamp:

```bash
for iss in $(gh issue list --repo vjt/ha-eisenberg --state open --json number --jq '.[].number'); do
  gh api "repos/vjt/ha-eisenberg/issues/$iss/comments" \
    --jq "if length==0 then \"ISSUE$iss: NO COMMENTS\" else (last | \"ISSUE$iss last: \(.user.login) created=\(.created_at) edited=\(.updated_at)\") end"
done
```

Three outcomes, and every open issue lands in exactly one:

- **No comments at all** → nobody has answered it yet, including us. A brand-new
  issue → triage it (Step 3, starting from the body rather than a reply).
- **Last author is `vjt`** → still blocked on the reporter. Report it and move on.
- **Last author is anyone else** → they replied. Go to Step 3.

We always comment last when handing an issue off, which is what makes the
`vjt`-is-last test mean "blocked". Keep it that way (see Notes).

**Edit-aware cross-check (don't skip):** a reporter who *edits an earlier
comment* to add a log doesn't change who commented last. So compare each
issue's `updatedAt` from Step 1 against its last comment's `created_at`. If
`updatedAt` is newer while `vjt` is still the last author, something changed —
an edit, a reaction, a label. Fetch the full thread with per-comment
`updated_at` and look for a reporter edit before reporting "still blocked".

Replies have landed in the gap between a poll and the action taken on it, and
immediately after a poll — this cross-check plus the Step 3b re-check are why.

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
   - **Media path:** `grep -niE "feed/live|library/add|mediaUpload|MotionEvent|eisenberg_media"`
   - **Unrouted events (a fix that shipped as a no-op):** `grep -n "Unhandled MQTT topic"`
     — the payload is logged with it, so the topic we failed to route names its own fix.
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
exists because a reporter's reply once landed between a poll and the next
action, and was nearly missed.)

## Step 4 — Re-arm the session cron

Poll on a cadence with `CronCreate` (session-only, in-memory, dies on exit):

- `cron`: `7 */12 * * *` (twice a day at :07 — vjt's chosen cadence; the
  off-minute is deliberate. Adjust only if he asks for something different)
- `recurring: true`
- `prompt`: a self-contained instruction that repeats Steps 1–3. **It must tell
  the cron to enumerate the open issues itself, not to poll a list of numbers
  baked into the prompt.** A prompt carrying a fixed set goes stale the moment
  an issue is opened or closed, and re-creates exactly the blindness Step 1
  exists to remove. Per-issue context (what each is waiting for) is fine to
  include as background — just never as the source of *which* issues to check.

Then confirm to the user: the cadence, the cron job id (for `CronDelete`), what
is currently open, and that it dies on `/clear` — the next session re-runs this
skill to resume. If a reporter has **already** replied when you arm it, handle
that reply now (Step 3) before scheduling.

## Notes

- The cosmetic `d/{x}/out/#` wildcard SUBACK refusal is expected on
  base-station accounts and is NOT a bug on its own — per-device
  `allowedMqttTopics` cover what's needed. Don't flag it as the cause of a
  reported symptom without checking the granted per-device topics first.
- Keep `vjt` as the last commenter on every issue you hand off, so the
  last-author heuristic stays reliable.
