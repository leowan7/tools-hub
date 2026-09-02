# QC round 1 — PR #175, deploy-drift guard

Independent review. Reviewer did not write the change.
Branch `fix/detect-stale-deploy`, HEAD `c43ea2d`, base `origin/main` = `9432824`.
Files under review: `.github/workflows/synthetic-smoke.yml`, `ALERTING.md`.

## VERDICT

**DO NOT SHIP** as written.

The guard is aimed at a real, verified gap and the shape of the idea is right.
But it was executed against ten scenarios and **three of them come out wrong**,
including one **false PASS** — production running code that was never merged is
certified `OK`. Two more are false alarms that fire on ordinary, currently-enabled
workflows (a merge-commit PR, and the manual dispatch the workflow's own comment
advertises). A guard that certifies false is worse than no guard, and this repo
already carries seventeen recorded instances of that failure mode.

The good news: **one line closes all three.** Replacing

```sh
EXPECTED=$(git rev-list -1 --before="$GRACE" HEAD)
```

with a `--first-parent origin/main` walk plus one extra `merge-base --is-ancestor`
in the other direction was implemented and executed against the same ten
scenarios: **9/9 correct**, including the three that currently fail. This is a
half-day of edits, not a redesign.

---

## Findings

### 1. HIGH — FALSE PASS: production running an unmerged commit is certified `OK`

**Where:** `.github/workflows/synthetic-smoke.yml:129`

```sh
if git merge-base --is-ancestor "$EXPECTED" "$DEPLOYED"; then
  echo "OK: production at ${DEPLOYED:0:7} ..."
  exit 0
fi
```

**What is wrong.** The test is one-directional. It asks only "is production at or
ahead of the bar", never "is production *on main*". Any commit that descends from
the bar passes — including commits on branches that were never reviewed and never
merged.

Two things make this reachable rather than theoretical:

- `fetch-depth: 0` makes `actions/checkout@v4` fetch
  `+refs/heads/*:refs/remotes/origin/*` (confirmed against the v4 source of
  `getRefSpecForAllHistory`). Every pushed branch is present locally, so the
  `git cat-file -e` gate at line 124 waves through commits from any branch. The
  message on that gate — *"production is running code that was never merged
  here"* — describes a case the gate does not actually detect.
- The new runbook (`ALERTING.md`, step 2) tells the operator to **"redeploy from
  the dashboard"**. Railway's redeploy UI lets you pick a source. The runbook and
  the guard's blind spot point at the same button.

**Failure scenario (executed, harness scenario S6).** `main` tip `B` (5 h old).
Someone pushes branch `rogue`, commit `R1` descending from `B`, and hand-deploys
it from the Railway dashboard while debugging. `R1` is never merged.

```
[ !!!! ] S6 prod on unmerged branch  (expected FAIL, got PASS, rc=0)
      | OK: production at eae6f6f, at or ahead of 7f1f658 (bar: newest commit older than '20 minutes ago').
```

Production is running unreviewed code; the guard says `OK` and the workflow goes
green. The PR body states the guard answers *"is production the thing we
merged?"* — in this case it answers "yes" and is wrong.

**Fix.** Add the reverse ancestry test before the bar comparison:

```sh
if ! git merge-base --is-ancestor "$DEPLOYED" refs/remotes/origin/main; then
  echo "::error::Production is running ${DEPLOYED:0:7}, which is NOT on main. It exists in this repo but was never merged -- someone deployed a branch. Redeploy from main."
  exit 1
fi
```

Verified: turns S6 into a correct FAIL with a message that names the actual
problem, and leaves S1/S2 unchanged.

---

### 2. HIGH — FALSE ALARM: `workflow_dispatch` from any non-`main` ref reports drift

**Where:** `.github/workflows/synthetic-smoke.yml:118` — `HEAD`

**What is wrong.** On `schedule`, `HEAD` is the default-branch tip and the logic
holds. On `workflow_dispatch`, `actions/checkout` checks out **whatever ref the
run was dispatched from**. The bar then becomes a commit on that branch — a
commit which by definition is not in production and never will be.

This is not a hypothetical use of dispatch. Line 60 of the same file advertises
it: `workflow_dispatch: {}     # manual run, e.g. right after a deploy`. "Right
after a deploy" is precisely when a developer is sitting on a branch.

**Failure scenario (executed, S5).** `main` tip `B` merged 5 h ago and is
correctly live in production. A developer on branch `mybranch` (WIP commit
`W1`, 100 min old, unpushed to main) clicks Run workflow:

```
[ !!!! ] S5 dispatch from branch  (expected PASS, got FAIL, rc=1)
      | ::error::DEPLOY DRIFT -- production is running 96dc196 but b44a704 merged over 20 minutes ago and never shipped.
      | Merged but NOT in production:
      | b44a704 unmerged wip
```

Production is perfectly current. The alert names a WIP commit and asserts it
"merged over 20 minutes ago" — a statement that is simply false; nothing merged.
The operator is sent to the Railway dashboard to fix a non-problem.

The PR body records hitting exactly this ("A scenario labelled 'in-flight
deploy' began failing after a rebase... the bar had become a commit produced by
this very branch") and concludes **the label was wrong**. The label was wrong,
but so is the guard: it resolved the symptom in the harness and left the cause
in the shipped code.

**Fix.** Never trust `HEAD`. Walk `refs/remotes/origin/main` explicitly (present
on the runner because of `fetch-depth: 0`; see finding 1). See finding 3 for the
combined one-line change.

---

### 3. HIGH — FALSE ALARM: a merge-commit PR defeats the grace window entirely

**Where:** `.github/workflows/synthetic-smoke.yml:118` — `git rev-list -1 --before="$GRACE" HEAD`

**What is wrong.** `--before` filters on **committer date** — verified by
execution, not by reading the docs:

```
a700845 A:2026-08-20 23:48:52 C:2026-08-21 04:46:52  squash-merged now, authored 5h ago
ec0b4c3 A:2026-08-20 23:48:52 C:2026-08-20 23:48:52  base
rev-list -1 --before='20 minutes ago' HEAD -> ec0b4c3   <- the base, i.e. committer date
```

For squash- and rebase-merges GitHub rewrites the committer date to merge time,
so committer date == "when it landed on main" and the reasoning is sound. **For
merge commits it is not.** A "Create a merge commit" PR splices side-branch
commits carrying their *original* committer dates into main's ancestry. Those
commits are older than the grace window the moment they land, so the bar jumps
straight past the grace window onto the side branch — which the
currently-deployed main tip does not contain.

This is live, not prospective:

- `gh api repos/leowan7/tools-hub` → `"allow_merge_commit": true`, `"allow_rebase_merge": true`.
- `git rev-list --merges --count origin/main` → **162**. Merge commits are not
  merely permitted here; they are the historical norm, including
  `Merge branch 'main' into <feature>` commits that import even older dates.

**Failure scenario (executed, S3).** Feature branch commits 2 h old, merged
via a merge commit 5 minutes ago, deploy legitimately in flight, production at
the previous main tip `B`:

```
[ !!!! ] S3 in-flight MERGE COMMIT  (expected PASS, got FAIL, rc=1)
      | ::error::DEPLOY DRIFT -- production is running c92adaf but 945fb6a merged over 20 minutes ago ...
      | Merged but NOT in production:
      | 60e0bca Merge pull request #999
      | 945fb6a feature work 2
      | 49bd124 feature work
```

The grace window — described in both the workflow comment and `ALERTING.md` as
the whole reason this does not flake — provides **zero** protection here. The bar
`945fb6a` is 2 h old the instant the merge lands. Every merge-commit PR whose
deploy overlaps a scheduled run emails the repo owner a false alarm.

**Fix (closes findings 2 and 3 together, one line).**

```sh
EXPECTED=$(git rev-list -1 --before="$GRACE" --first-parent refs/remotes/origin/main)
```

`--first-parent` restricts the walk to main's own line, which is exactly the set
of "things that landed on main", making the bar immune to imported side-branch
dates; `origin/main` removes the dispatch-ref dependency. Verified: S3 and S5
become correct PASSes, S1 stays a correct FAIL.

Apply the same `--first-parent origin/main` to the reporting line at 136, so the
"Merged but NOT in production" list shows the PRs that landed rather than every
individual commit inside them.

---

### 4. HIGH — Step ordering: the guard suppresses the workflow's primary function

**Where:** `.github/workflows/synthetic-smoke.yml:100-137`, placed as step 2 of 5

**Assessment requested explicitly; the recommendation is to change it.**

The guard `exit 1`s in four of its five paths. Because it sits before
`Set up Python`, `Guard - RK_LIVE_KEY`, and `Run Platform API smoke`, **any**
guard failure means the Platform API smoke never executes.

The most damaging case is not drift at all — it is a real outage. When `/health`
is unreachable the guard exits at line 109 (executed, S9), and the deep smoke
that is this workflow's entire reason to exist never runs. Before this PR an
outage produced the smoke's full per-step diagnosis (targets / cost-estimate /
create / replay / read-back / withdraw, with the `status=0` transport
signature that `ALERTING.md:546` teaches the operator to read). After this PR
the same outage produces one line. **A guard meant to be additive removes
diagnostic capability from the layer it sits in front of.**

That also makes the guard's own error text self-refuting:

> `If the smoke below also fails this is an outage, not deploy drift -- follow the outage runbook, not this one.`

The smoke below cannot fail. It never runs. The message instructs the reader to
consult a result its own placement guarantees will not exist.

The stale-and-also-broken case named in the brief has the same shape: you learn
production is stale and nothing about whether it also works.

**Recommendation: make it a separate job in the same workflow.**

```yaml
jobs:
  deploy-drift:
    name: Production is running current code
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - name: Guard - production is running current code
        ...
  smoke:
    name: Platform API end-to-end smoke
    # unchanged; no `needs:`, no `fetch-depth: 0`
```

Why this over the alternatives:

- **Both always run.** Drift and brokenness are independent questions and you get
  independent answers to both, which is the whole argument for defence in depth
  that `ALERTING.md:72` already makes.
- **The failure signal is better, not worse.** GitHub reports per-job status, so
  the check name itself says which question failed — strictly more information
  than one job whose failure could be either.
- **The "read drift first" intent is preserved.** Ordering the *reader's*
  attention does not require aborting the *run*. Naming the job
  "Production is running current code" puts it at the top of the checks list.
- **The smoke keeps its shallow checkout**, so `fetch-depth: 0` is paid once by
  a 5-minute job rather than imposed on the 10-minute one.
- Cost: roughly 20-30 s of extra runner time, four times a day. The existing
  `concurrency: group: synthetic-smoke` covers the whole run, so no extra
  queueing.

`continue-on-error: true` plus a trailing `if: always()` re-check also works but
is more machinery in one job than simply using two. Do not do that.

---

### 5. MEDIUM — the most likely misconfiguration gets the wrong runbook

**Where:** `.github/workflows/synthetic-smoke.yml:113-116` vs `blueprints/public.py:_build_sha`

`_build_sha()` ends with `.strip() or "unknown"`. The `build` field is
**never empty and never absent** — when neither `RAILWAY_GIT_COMMIT_SHA` nor
`BUILD_SHA` is set, it is the literal string `"unknown"`.

Consequences:

1. `jq -r '.build // empty'` never yields empty for the real app, so the
   `if [ -z "$DEPLOYED" ]` branch — the one carefully worded to name
   `RAILWAY_GIT_COMMIT_SHA` — is **dead code against production**.
2. `"unknown"` falls through to the `git cat-file -e` gate and produces the
   wrong diagnosis (executed, S4):

```
[  ok  ] S4 build=unknown  (rc=1)
      | ::error::Production reports build unknown, which is not a commit in this repository.
      |   Either the build SHA is being set from somewhere unexpected, or production is
      |   running code that was never merged here.
```

Nothing was "set from somewhere unexpected" and nothing unmerged is running. The
env var is missing. `blueprints/public.py` flags this exact case as the
anticipated one: *"UNVERIFIED until a deploy is observed: if Railway names the
variable something else, this reports 'unknown'"*. The guard routes the
anticipated failure to the wrong message.

This also falsifies the PR body's "Four failure modes answer differently" table:
mode 2 is unreachable, and mode 3 fires in its place.

**Fix.** `if [ -z "$DEPLOYED" ] || [ "$DEPLOYED" = "unknown" ]; then` on line 113.

---

### 6. MEDIUM — FALSE PASS on symbolic `build` values

**Where:** `.github/workflows/synthetic-smoke.yml:124, 129`

`$DEPLOYED` is fed straight to `git cat-file` and `git merge-base` as a
revision, with no check that it is a SHA. Git revision syntax resolves. Executed:

```
build=main      rc=0  OK: production at main, at or ahead of b8335bb ...
build=HEAD      rc=0  OK: production at HEAD, at or ahead of b8335bb ...
build=@         rc=0  OK: production at @, at or ahead of b8335bb ...
```

Three inputs that carry no information about production whatsoever, each
certified `OK`. Reachable via the `BUILD_SHA` manual override that
`_build_sha()` documents "for any other host" — `BUILD_SHA=main` in a Railway
variable is an easy thing to type. No shell injection (every use is quoted); the
problem is git-revision-syntax, not shell.

**Fix.** Before line 124:

```sh
if ! [[ "$DEPLOYED" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "::error::/health reports build '${DEPLOYED}', which is not a commit SHA. Check RAILWAY_GIT_COMMIT_SHA / BUILD_SHA on the web service."
  exit 1
fi
```

---

### 7. MEDIUM — a fifth failure mode exists and has no message at all

**Where:** `.github/workflows/synthetic-smoke.yml:112`

If `/health` answers HTTP 200 with something that is not JSON — a CDN or proxy
interstitial, a Railway edge error page, an HTML maintenance splash — `jq`
exits non-zero, `pipefail` propagates it, `set -e` kills the step. Not a false
pass, but (executed, S7):

```
[  ok  ] S7 non-JSON body  (expected FAIL, got FAIL, rc=2)
      | jq: error (at <stdin>:0): Expecting value: line 1 column 1 (char 0)
```

No `::error::` annotation, no runbook pointer, nothing telling the operator
this is not drift. The PR body claims exactly four failure modes that "answer
differently"; this is a fifth, and it answers with a bare parser error.

**Fix.**

```sh
DEPLOYED=$(printf '%s' "$BODY" | jq -r '.build // empty' 2>/dev/null) || {
  echo "::error::${HEALTH_URL} answered but the body is not JSON -- likely an edge/proxy error page rather than the app. This is an outage symptom, not deploy drift. First 200 bytes: ${BODY:0:200}"
  exit 1
}
```

---

### 8. MEDIUM — "the alert email carries the diff" is false

**Where:** PR body, "Behind" bullet; `.github/workflows/synthetic-smoke.yml:135-136`

Two independent reasons the claim does not hold:

1. The `git log --oneline` output is a plain `echo`/log line, not part of the
   `::error::` annotation. Only line 134 becomes an annotation.
2. GitHub's Actions failure email carries the workflow name, repo, branch,
   commit and a link to the run. It does not embed job logs or annotations.

So the operator gets a link, not a diff. The claim matters because it sets the
expectation that the email is self-sufficient during an incident; it is not.

**Fix.** Either drop the claim, or write the commit list into
`$GITHUB_STEP_SUMMARY` so it lands on the run summary page the email links to.
Marked read-assessed — see the "could not check" section.

---

### 9. MEDIUM — the alert understates severity by design

**Where:** `.github/workflows/synthetic-smoke.yml:134` — `${GRACE/ ago/}`

The expansion itself is correct: `GRACE='20 minutes ago'` → `20 minutes`, giving
"merged over 20 minutes ago". Confirmed by execution. It is bash-only syntax,
which is fine on `ubuntu-latest`.

The problem is that `20 minutes` is a **constant**. It is the grace window, not
the age of the gap. Executed, S1, where the missed commit landed 3 hours ago:

```
::error::DEPLOY DRIFT -- production is running 2d094a9 but 5c5e3d3 merged over 20 minutes ago and never shipped.
```

The incident that motivated this PR ran **8.6 hours**. This guard would have
reported it as "over 20 minutes". An operator triaging a 6 a.m. email cannot
tell a 21-minute blip from a day-long outage.

**Fix.** Report the real age: `$(git log -1 --format=%cr "$EXPECTED")`. Verified
in the patched harness — the 8.6 h case renders as *"landed on main 9 hours ago
and never shipped"*, which is the sentence you actually want in an alert.

---

### 10. MEDIUM — `ALERTING.md` now contradicts itself in two places

**Where:** `ALERTING.md:228-268` (new) vs `ALERTING.md:580` and `ALERTING.md:606`

The new section states: *"A merge to `main` does not reliably redeploy."* That is
the entire premise. But the document still says, untouched:

- `:580` — `2. Build failure: fix and push (Railway auto-deploys main).`
- `:606` — `- Trunk: `main`, auto-deploys to Railway on push.`

An operator following the `:580` runbook pushes a fix and stops, believing the
deploy is automatic — the exact behaviour that caused the 8.6-hour incident.
Both need a pointer to the drift section.

**Second contradiction, internal to the new section.** Step 1 of the runbook says
to confirm drift by comparing `/health` against `git rev-parse origin/main` — the
**tip**. Two paragraphs earlier the same section explains that comparing against
the tip is wrong because it fails during an in-flight deploy. A reader who
follows step 1 during a normal deploy sees a mismatch and concludes drift.

**Fix.** Step 1 should say to compare against the newest commit that landed more
than 20 minutes ago, or more practically: "a mismatch with the tip is only drift
if the missing commit landed more than ~20 minutes ago; check
`git log -1 --format=%cr <missing>`."

---

### 11. MEDIUM — the runbook is filed where an operator will not look

**Where:** `ALERTING.md:228`

Three placement problems:

1. **The runbook is in the architecture half.** `ALERTING.md:517` is
   `## Runbook: responding to an alert`, and every other alert type has an entry
   there — UptimeRobot DOWN, `/readyz` down, **Synthetic smoke FAILED** (`:546`),
   Railway deploy failure, operator alert. Deploy drift does not. The document's
   established convention is architecture up top, runbook down there; this
   section puts a numbered runbook in the architecture half and breaks it. An
   operator who jumps to "Runbook" during an incident finds nothing.
2. **`## Detection architecture (defense in depth)` (`:72`) was not updated.** It
   enumerates five layers under "Each layer catches what the layer above misses".
   The PR's central argument is that *none* of those five catches drift — and the
   new sixth layer is not added to the list.
3. **The `## TL;DR status` table (`:11`) was not updated.** A new detection
   capability absent from the TL;DR is invisible to anyone who reads only the
   TL;DR, which is what a TL;DR is for.

**Fix.** Keep the "why it exists / why it does not flake" prose under Tier 2, move
the numbered steps to a `### Deploy drift detected` entry under
`## Runbook: responding to an alert`, add a sixth bullet at `:72`, and add a row
to the TL;DR table. Then re-check that the workflow's pointer string still
resolves.

*(The pointer string itself is fine: line 134 says `see 'Deploy drift detected' in
ALERTING.md` and the heading is `### Deploy drift detected`. Exact match, verified.)*

---

### 12. LOW — unvalidated remote body echoed into the workflow-command channel

**Where:** `.github/workflows/synthetic-smoke.yml:114` — `Body: ${BODY}`

The full `/health` response body is interpolated into an `::error::` line with no
truncation and no escaping. Executed, S10, with a multi-line JSON body:

```
::error::/health answered but carried no build field ... Body: {
  "status": "ok",
  "note": "::error::injected 100% done"
}
```

Three problems, all minor but all real:

- GitHub annotations are single-line. Everything after the first newline is
  emitted as raw runner log output.
- A raw log line beginning `::` is parsed by the runner as a **workflow command**.
  `::error::`, `::add-mask::` and `::stop-commands::` all become reachable from a
  remote HTTP response. Blast radius is small here (`permissions: contents: read`,
  one secret, step exits immediately) but writing remote bytes into a control
  channel is the wrong default.
- GitHub requires `%` in an annotation to be escaped as `%25`; `100%` above is
  mangled.

**Fix.** `Body: ${BODY:0:200}` with newlines stripped:
`printf '%s' "$BODY" | tr -d '\n\r' | cut -c1-200`.

---

### 13. LOW — "did not answer" is the wrong words for an HTTP error

**Where:** `.github/workflows/synthetic-smoke.yml:108`

`curl -f` exits 22 on any 4xx/5xx. A 502 or a 503 **is** an answer, and a
diagnostically useful one. Collapsing "connection refused", "DNS failure", "TLS
error", "timeout" and "502" into "did not answer" throws away the distinction the
message then asks the operator to make. `curl -fsS -w '%{http_code}'` or dropping
`-f` and branching on the status would keep it.

---

### 14. LOW — the 20-minute grace window is asserted, never measured

**Where:** `.github/workflows/synthetic-smoke.yml:103`

Nothing in the repo records how long a Railway deploy of the `web` service takes.
Grep found build durations for Modal images only (`docs/ATOMIC-TOOLS.md`: 8-12,
10-15, 20-25 min) — a different platform and a different build. The web service
builds via Railpack/mise. If a cold build ever exceeds 20 minutes, every deploy
overlapping a scheduled run emails the owner a false alarm, and the value has no
recorded basis to argue from.

Note this compounds finding 3: with `--first-parent` fixed, the window only has
to cover build+release time. Before that fix, no window size helps a
merge-commit PR at all.

**Fix.** Read three recent deploy durations off the Railway dashboard and record
the number next to the constant, so the next person tuning it has a basis. If
p99 is under 10 min, 20 is fine; if it is 18 min, raise the window — a 60-minute
grace still catches the 8.6-hour incident comfortably inside the 6-hour cadence.

---

### 15. LOW — detection latency is ~6 h and is stated nowhere

The cron is `0 */6 * * *`. Worst case between a dropped deploy and the alert is
roughly 6 h 20 m. That is a genuine improvement on the 8.6 h incident, but a
reader of the new section sees "20-minute grace window" and can easily come away
believing detection is ~20 minutes. One sentence in `ALERTING.md` fixes it.

---

### 16. LOW — the PR's test evidence is real but does not cover this change

**Where:** PR body, "Testing"

> The four suites that read `.github/workflows` or `ALERTING.md` —
> `test_deploy_paths_exclusions.py`, `test_deploy_trigger_covers_dockerfile_copies.py`,
> `test_lab_project_confirmation.py`, `test_preflight_panel_contract.py` — pass:
> **189 passed, 4 skipped**.

Checked by grep:

- **No test in the repo reads `ALERTING.md`.** Zero. The only non-test reference
  is a comment in `shared/idempotency.py:414`.
- **No test reads `synthetic-smoke.yml`.** Both deploy tests are pinned to
  `_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-modal.yml"`.
- `test_lab_project_confirmation.py` and `test_preflight_panel_contract.py`
  mention `.github/workflows` only inside **docstrings** ("`.github/workflows`
  installs no node"). They do not open the directory.

The 189 passed are real and unrelated. Neither changed file has any committed
coverage, and the harness that found the actual bugs is explicitly not committed.
Given the repo's own tally of seventeen guards that certified false, a guard with
zero committed tests is the wrong thing to add.

**Fix.** The lazy version is one small `tests/test_deploy_drift_guard.py` that
(a) parses the YAML and asserts the pointer string in the error message matches a
heading present in `ALERTING.md` — the cheapest possible defence against the two
files drifting apart — and (b) runs the extracted step script against two fixture
repos, one drifted and one in-flight. The harness in this review is about 60
lines and already does (b); it just needs `bash` located rather than assumed.

---

## What was verified by EXECUTION vs by READING

### Executed

The guard's `run:` block was extracted from the YAML with PyYAML (not retyped),
written to `guard.sh`, and executed under Git-for-Windows bash 5.2.37 with
`curl` and `jq` stubbed and synthetic git repos with controlled commit dates.
Scratch harness lives in the session scratchpad (`harness.sh`, `h2.sh`, `h3.sh`,
`h4.sh`); nothing was written into the repo except this report.

| # | Scenario | Expected | Actual | Finding |
|---|---|---|---|---|
| S1 | Real drift, gap merged 3 h ago | FAIL | FAIL | ok (severity wording → #9) |
| S2 | In-flight squash-merge, tip 5 min old | PASS | PASS | ok |
| S3 | In-flight **merge commit** | PASS | **FAIL** | **#3** |
| S4 | `build: "unknown"` | FAIL | FAIL, **wrong message** | **#5** |
| S5 | `workflow_dispatch` from a feature branch | PASS | **FAIL** | **#2** |
| S6 | Production on an unmerged branch | FAIL | **PASS** | **#1** |
| S7 | Non-JSON body | FAIL | FAIL, **no annotation** | **#7** |
| S8 | Valid JSON, no `build` key | FAIL | FAIL, correct message | ok |
| S9 | `/health` unreachable | FAIL | FAIL, correct message | ok (→ #4) |
| S10 | Multi-line body | FAIL | FAIL, **annotation truncated** | **#12** |

Also executed:

- **`--before` uses committer date, not author date** — built a commit with an
  old author date and a new committer date; `rev-list -1 --before` skipped it.
  This is what makes the guard correct for squash-merges and incorrect for
  merge commits (#3).
- **`build` accepting git revision syntax** — `main`, `HEAD`, `@` all exit 0
  (#6). `origin/main` and `main^` correctly fail.
- **`${GRACE/ ago/}` and `${DEPLOYED:0:7}`** render correctly under bash 5.2.
- **`set -euo pipefail` interactions**: `BODY=$(curl ...) || {...}` correctly
  suppresses `set -e` and runs the block; `DEPLOYED=$(... | jq ...)` correctly
  does *not*, so a jq failure kills the step (rc=2) rather than passing.
- **The YAML parses** and the step `env:` block (`HEALTH_URL`, `GRACE`) is
  well-formed and reaches the script.
- **The proposed fix** was implemented (`guard-fixed.sh`) and run against all
  scenarios: **9/9 correct**, including S3, S5 and S6.
- **`allow_merge_commit: true`, `allow_rebase_merge: true`** via
  `gh api repos/leowan7/tools-hub`; **162 merge commits** on `origin/main`.
- **`actions/checkout@v4` `getRefSpecForAllHistory`** returns
  `+refs/heads/*:refs/remotes/origin/*` — fetched from the v4 source. This
  confirms both that `origin/main` exists on the runner (the fix works) and that
  every branch is present (finding 1 is reachable).
- **Live production**: `curl https://tools.ranomics.com/health` →
  `{"build":"9432824f...","status":"ok"}`, identical to `git rev-parse origin/main`.
  Production is current right now; the guard would pass on merge.
- **`_build_sha()`** read directly at `blueprints/public.py`; the
  `.strip() or "unknown"` tail is the basis for #5.
- **No test reads either changed file** — grep over `tests/`.
- **The ALERTING.md pointer string matches the heading** exactly.
- **Contradicting lines** `ALERTING.md:580` and `:606` located by grep.

### Assessed by reading only

- **Step ordering (#4).** Read from the step list; the consequence (later steps
  do not run after a non-zero exit) is standard Actions semantics, not executed.
- **GitHub failure-email contents (#8).** No failing run was triggered.
- **Workflow-command parsing of `::` in raw log lines (#12).** The truncation was
  executed; the runner's interpretation of it was not.
- **`concurrency: cancel-in-progress: false`.** Reviewed, no interaction with the
  guard. It does mean a manual dispatch "right after a deploy" queues behind an
  in-progress scheduled run instead of starting immediately — pre-existing, not
  introduced here.
- **`permissions: contents: read`** is sufficient for the guard. No change needed.

---

## What could NOT be checked, and why

1. **Whether the guard fires correctly on the real runner.** Everything above ran
   under Git-for-Windows bash against synthetic repos. `ubuntu-latest` has a
   different bash build, real `jq`, and real `actions/checkout` behaviour. The
   findings that depend on the runner are #1's refspec (mitigated: verified
   against the checkout v4 source) and #12's log-line parsing. The cheap
   confirmation is to merge to a scratch branch and `workflow_dispatch` it —
   which, per finding #2, will itself produce a false DEPLOY DRIFT and so doubles
   as a live reproduction.

2. **Real `jq` exit codes and error text.** `jq` is not installed locally; the
   stub parses with Python and exits 2 on malformed input. Only *non-zero* is
   load-bearing for the control flow in #7, so the conclusion holds, but the
   exact exit code in the S7 transcript is the stub's, not `jq`'s.

3. **Railway deploy duration** (#14). No dashboard access from here, and nothing
   in the repo records it. The 20-minute window could not be validated against
   anything.

4. **The PR body's seven-scenario harness.** Not committed, so its claims could
   not be reproduced or audited. Its scenario 1 ("live production, current →
   passes") is unverifiable after the fact. Notably, its scenario list contains
   no merge-commit case, no dispatch-from-branch case, and no
   deployed-from-a-branch case — the three that fail here.

5. **Whether GitHub Actions failure email actually reaches leo@** — still listed
   as the open action in `ALERTING.md:24`. If it does not, this guard has no
   delivery path at all, and every finding above is moot.

6. **The full pytest suite was not run.** The change touches no Python and no
   test reads either file (verified by grep, #16), so a full run would confirm
   only that unrelated tests still pass. The base commit's own baseline was not
   measured, so no delta could be quoted in any case.

---

## Summary of what to change before shipping

| Priority | Change | Lines |
|---|---|---|
| 1 | `--first-parent refs/remotes/origin/main` instead of `HEAD` on line 118 | 1 |
| 2 | Add the reverse `merge-base --is-ancestor "$DEPLOYED" origin/main` gate | 4 |
| 3 | Split the guard into its own job | ~8 |
| 4 | Treat `"unknown"` as "no build SHA"; add a `^[0-9a-f]{7,40}$` check | 5 |
| 5 | Give the non-JSON path an `::error::`; truncate `${BODY}` to 200 chars | 3 |
| 6 | Report the real gap age with `git log -1 --format=%cr` | 1 |
| 7 | Move the numbered runbook under `## Runbook`; fix `:580` and `:606`; add to `:72` and the TL;DR table | doc |
| 8 | One `tests/test_deploy_drift_guard.py` — at minimum, the pointer-string↔heading check | ~60 |

Items 1, 2, 4, 5 and 6 were implemented and executed together in this review and
produce 9/9 correct outcomes. The idea is sound and worth shipping; the current
implementation is not.
