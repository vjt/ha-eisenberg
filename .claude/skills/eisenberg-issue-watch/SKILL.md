---
name: eisenberg-issue-watch
description: Poll ha-eisenberg GitHub issues — open ones AND recently-commented closed ones — for reporter replies (esp. debug logs), then analyze and report. Use to (re)start issue monitoring — after /clear, at session start, or when waiting on a reporter's log.
---

Watch the ha-eisenberg issues that are **blocked on a reporter's reply** (usually
a debug log we asked for), detect when the reporter answers, then pull the
attachment and analyze it. Session-cron based: re-arm it every session — a
session cron dies on `/clear` or exit, so the next session must re-invoke this
skill to resume.

## Repo

`vjt/ha-eisenberg`. All `gh` calls target it explicitly:
`gh api repos/vjt/ha-eisenberg/issues/<N>/comments`.

## Step 1 — Build the set from the repo: open issues UNION recent comments

**Two queries, and you need both.** Neither alone sees everything:

```bash
# A — every open issue
gh issue list --repo vjt/ha-eisenberg --state open --json number,title,updatedAt

# B — every issue touched by a recent comment, CLOSED ONES INCLUDED
gh api "repos/vjt/ha-eisenberg/issues/comments?sort=created&direction=desc&per_page=30" \
  --jq '.[] | "\(.created_at) #\(.issue_url|split("/")|last) \(.user.login)"'
```

The union of A and B is the complete set to poll this run.

**Why B exists.** This skill used to poll only open issues, and on 2026-09-19
that went blind: every issue was closed for tracker hygiene, each with a comment
inviting the reporter to *reopen if it still misbehaves*. So the exact signal
being waited for — peteramelang answering four measured questions, laurafabry
saying whether the ffmpeg toggle is on — would arrive as a comment on a **closed**
issue, and query A would never have shown it. GitHub does not reopen an issue
when someone comments on it. An empty open-issue list is therefore not proof
that nothing happened; it is only proof that nothing is open.

**Why A still exists.** B only reaches back 30 comments. A newly opened issue
nobody has commented on yet appears in A and nowhere else, and a long-silent
open issue drops out of B entirely.

**Memory supplies context, not membership.** Read
`project_eisenberg_e2e_status` for what a given issue is blocked on, the exact
question its log must answer, and any per-reporter quirk (e.g. a reporter whose
email-reply attachments GitHub strips, who must upload via the web UI). An issue
that memory says nothing about is not an error — it is a new one to triage.
Never let memory's silence remove an issue from the poll.

## Step 2 — Poll every issue in the set

For each number from Step 1, fetch the last comment and its edit timestamp:

```bash
for iss in <the union from Step 1>; do
  gh api "repos/vjt/ha-eisenberg/issues/$iss/comments" \
    --jq "if length==0 then \"ISSUE$iss: NO COMMENTS\" else (last | \"ISSUE$iss last: \(.user.login) created=\(.created_at) edited=\(.updated_at)\") end"
done
```

Three outcomes, and every issue lands in exactly one:

- **No comments at all** → nobody has answered it yet, including us. A brand-new
  issue → triage it (Step 3, starting from the body rather than a reply).
- **Last author is `vjt`** → still blocked on the reporter. Report it and move on.
- **Last author is anyone else** → they replied. Check it against the watermark
  below before acting.

**The watermark (or you will re-report the same threads forever).** Step 1B
reaches back 30 comments, which on this low-traffic repo is months. It will keep
surfacing settled threads whose last word was the reporter's — #20 and #30 both
end with a reporter signing off happily in July and August. Last-author alone
cannot tell those from a reply we owe an answer to.

So: read `eisenberg-watch-watermark` from memory — the `created_at` of the newest
comment already triaged. A non-`vjt` last comment is **actionable only if its
`created_at` is newer than the watermark**. Anything older is a settled thread;
name it in the report as skipped, do not act on it.

After a run that handled everything it found, update the watermark to the newest
comment `created_at` seen in the sweep, whoever wrote it. A run that leaves
something unhandled must NOT advance it.

We always comment last when handing an issue off, which is what makes the
`vjt`-is-last test mean "blocked". Keep it that way (see Notes). The test works
identically on a closed issue — which is the point of Step 1B.

**If the issue is CLOSED and the reporter has replied: reopen it before working
it.** `gh issue reopen <N> --repo vjt/ha-eisenberg`. A reply on a closed issue is
someone taking up the invitation to reopen; leaving it closed buries the thread
again and the next poll has to rediscover it from the comment sweep.

**Edit-aware cross-check (don't skip):** a reporter who *edits an earlier
comment* to add a log doesn't change who commented last, and an edit does not
appear in the comment sweep either. So compare each issue's `updatedAt` from
Step 1A against its last comment's `created_at`. If `updatedAt` is newer while
`vjt` is still the last author, something changed — an edit, a reaction, a
label. Fetch the full thread with per-comment `updated_at` and look for a
reporter edit before reporting "still blocked".

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
   - **Health tick starvation:** `grep -n "Manually updated eisenberg data"` — that
     string is HA's own `async_set_updated_data` log line. It must NOT appear:
     since 0.4.6 pushes go through `_push_to_entities`, and its return would mean
     the 30-minute tick is being deferred by MQTT traffic again (#35).
   - **Unrouted events (a fix that shipped as a no-op):** `grep -n "Unhandled MQTT topic"`
     — the payload is logged with it, so the topic we failed to route names its own fix.
4. pyaarlo reference for cross-checking Arlo behavior: `~/code/ha/pyaarlo`.
5. **Read their numbers before defending ours.** peteramelang has corrected a
   confident claim of ours with a measurement, and pallemannen was right about a
   path we had "corrected" him on. When a reporter contradicts a conclusion,
   check their sources first — see [[verify-negatives-before-asserting-them]].
6. Report the finding, update `project_eisenberg_e2e_status` and the
   `eisenberg-watch-watermark` in memory, and (if
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
  the cron to build the set itself from BOTH queries in Step 1 — open issues and
  the recent-comment sweep — not to poll a list of numbers baked into the
  prompt.** A prompt carrying a fixed set goes stale the moment an issue is
  opened or closed, and a prompt that enumerates only open issues goes blind the
  moment the tracker is emptied. Per-issue context (what each is waiting for) is
  fine to include as background — just never as the source of *which* issues to
  check, and expect that background to rot: state releases and issue states as
  "as of <date>", so a stale note reads as stale rather than as fact.

Then confirm to the user: the cadence, the cron job id (for `CronDelete`), what
the poll currently shows, and that it dies on `/clear` — the next session
re-runs this skill to resume. If a reporter has **already** replied when you arm
it, handle that reply now (Step 3) before scheduling.

## Notes

- The cosmetic `d/{x}/out/#` wildcard SUBACK refusal is expected on
  base-station accounts and is NOT a bug on its own — per-device
  `allowedMqttTopics` cover what's needed. Don't flag it as the cause of a
  reported symptom without checking the granted per-device topics first.
- Keep `vjt` as the last commenter on every issue you hand off, so the
  last-author heuristic stays reliable.
- **An empty open-issue list means nothing on its own.** Report what the comment
  sweep showed too, or the run has not actually checked anything.
