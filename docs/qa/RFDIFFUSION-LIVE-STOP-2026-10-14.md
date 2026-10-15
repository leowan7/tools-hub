# rfdiffusion live charging: production test, 2026-10-14

Subject: PR #427, merged as squash `5b01615f`. Two things are under test.

1. **The start gate** (new in #427): `shared/wallet_guard.py::requires_wallet`
   refuses to START rfdiffusion when `0 < balance < live_start_usd(...)`.
2. **The live stop**: the `campaigns:tick` meter debits a running job about
   every 5 min and, at $0, cancels the Modal call while keeping the designs
   already uploaded (`shared/job_recovery.py::reconstruct`).

Test account: `leowan7+t1@gmail.com`. Leo signs in and runs every prod write
himself; this session reads prod and drives the browser only.

Status of this document: **step 1 done and passing, step 2 not yet run.**
Sections marked "not yet run" contain no observations. Nothing in this file
is a prediction dressed as a result.

## Build under test

| Fact | How it was checked |
|---|---|
| Deployed build `6a47aeb97da88dfb2e0cd3ddd37262142a2628b4` | read `https://tools.ranomics.com/health` in this session, 2026-10-14 |
| `origin/main` tip is `6a47aeb9` ("fix(auth): say which password rule Supabase refused (#428)") | `git log origin/main --oneline -3` |
| #428 touched only `shared/auth.py`, `templates/login.html`, `tests/test_weak_password_message.py` | `git show --stat 6a47aeb9` |
| The money paths are byte-identical between `5b01615f` and `6a47aeb9` | `git diff --stat 5b01615f 6a47aeb9 -- shared/wallet_guard.py shared/jobs.py shared/job_recovery.py shared/wallet.py blueprints/wallet.py` returned empty |

So the deployed build carries #427's charging code unchanged.

## The start minimum, re-derived here

Ran against the worktree at `5b01615f` with the repo venv:

```
runtime_estimate_min(TOOL_RULES["rfdiffusion"], 120, 1) = 936.78 s
live_start_usd("rfdiffusion", {...}) = $1.1371   # num_designs 1, 4 and 8 alike
live_start_usd("boltz2", ...)         = $0
live_start_usd("esmfold2_design", ...) = $0
```

Three things this shows, each worth keeping:

- **$1.1371 is reproducible**, not just a figure from the PR body.
- **The minimum ignores `num_designs`**, because `shared/wallet_guard.py:83`
  hard-codes the runtime it prices as
  `runtime_estimate_min(TOOL_RULES[tool_slug], 120, 1)` — 120 aa, one design.
  The submitted design count never reaches the calculation. A 1000-design
  request faces the same $1.1371 bar as a 1-design one.
  It is *not* the hard cap doing this: `compute_hard_cap("rfdiffusion", ...)`
  measures $5.00 at 1 and 4 designs and $500.00 at 1000, all far above
  $1.1371, so the clamp inside `live_due_usd` never binds at the floor.
- **The gate is rfdiffusion-only.** `boltz2` is live-charged but returns $0,
  so it has no start floor. That asymmetry is by construction in
  `live_start_usd`, which early-returns 0 for every slug but `rfdiffusion`.

The $1.1371 bar is still a *derived* figure: it prices a 120-residue,
1-design runtime model that no measured rfdiffusion run has confirmed.
Target size is invisible to the wallet layer, so a larger target's true
stretch is unpriced by the gate.

## Step 1 — free gate check at $1.13

**PASS.** All four expectations met. No GPU, no spend.

### The account, read first

| Read | Value | Page |
|---|---|---|
| Signed-in email | `leowan7+t1@gmail.com` | `/account` |
| Balance | $1.13 | `/account`, `/account/wallet`, `/jobs` |
| Auto reload | **off** | `/account/wallet` |
| Ledger before the test | **2 entries**: signup credit +$5.00, adjustment -$3.87 | `/account/wallet/transactions` |

Auto reload being off matters for step 2: `_post_settle_hooks` calls
`auto_reload_if_needed`, so a run settling near $0 on a wallet with auto
reload on and a saved card would fire a real off-session Stripe charge. This
wallet cannot do that.

The adjustment row's note, as the ledger renders it: "rfdiffusion live stop
test 2026-10-14: set balance from $5.0000 to $1.13".

### A. The browser never offers the submit

On `GET /tools/rfdiffusion` the gate renders without any interaction:
"Top up to run this job / Your balance does not cover this run."

Read out of the DOM on that page:

- the form's single submit button, text "Submit run", carries the `hidden`
  attribute, computes to `display: none`, and has a null `offsetParent`;
- both "Top up" controls are `<a href="/account/wallet/topup">`, not submits.

So at $1.13 a user cannot POST this form from the page at all. That is the
client half of the gate, and it is the half a real user meets first.

### B. The server refuses the POST on its own

Because the button is hidden, the POST was forced from the page console
(`form.submit()`) at **2026-10-15T03:32:03Z**, carrying the form's own
defaults: `preset=pilot`, `num_designs=4`, `binder_length_min/max=55/65`,
`target_chain=A`, and **no file** in `target_pdb`.

The response at `/tools/rfdiffusion/submit` is the top-up gate page:
"Top up to run your job. Your balance does not cover this run. Top up, then
go back to the rfdiffusion form and run the job once your balance updates.
Nothing is submitted for you."

**Why that response is the wallet gate and not the input validator.** No PDB
was attached, and `tool_submit` refuses a missing PDB with its own message,
re-rendering the form with `error="Upload a target PDB file."`
(`blueprints/tools.py:2008`). The decorator stack is `@login_required`,
`@idempotent()`, `@requires_wallet` (`blueprints/tools.py:1885-1888`), all of
which run before the view body, where `_run_validate` is called at line 1926.
The wallet refusal therefore preempted validation. Had the gate let the
request through, the response would have named the missing file instead. This
is the one observation that distinguishes the new gate from the pre-existing
form validation, and it is why the probe was run with no file attached.

That the no-file path really does reach the view when nothing stops it first
is not a deduction: the comment above `blueprints/tools.py:2008` records a
production incident on 2026-04-30 in which a pxdesign submit with no file
attached created an orphan job that sat PENDING for 2.5 hours. The guard at
2008 exists because of it.

(The same sentence, "Upload a target PDB file.", also appears at
`blueprints/tools.py:1881`, but that is a dict-returning preflight helper
serving the separate `/tools/<tool>/validate` endpoint, which carries neither
`@idempotent` nor `@requires_wallet`. It is not what answered this POST.)

### C. Nothing was created

| Expectation | Observed | Page |
|---|---|---|
| No job row | first-run empty state, "Pick a tool to run your first design"; no rfdiffusion row | `/jobs` |
| No hold anchor | still **2 entries**, the same two rows as before the POST | `/account/wallet/transactions` |
| Balance unchanged | $1.13 | all three pages |

The absence of a hold row is the interesting one: it places the refusal
*before* `open_live_run` in the live branch of `requires_wallet`, so a
refused start leaves no lineage anchor behind to be stranded.

### What step 1 does not prove

- Nothing about the meter, the stop at $0, or whether `reconstruct` keeps
  already-uploaded designs. Step 1 never started a run.
- Nothing about whether $1.1371 is the *right* floor. It proves the floor is
  enforced, not that 936.78 s is what a 120-residue rfdiffusion run costs.
  No measured run has tested that.
- Nothing about targets larger than 120 aa, whose true stretch the gate does
  not price at all.
- It does not exercise the `?pilot=1` prefill path or a staged target;
  `/targets` is empty on this account.

### One incidental observation, for follow-up

The submit form posts `preset` as a field, value `pilot`. The memory note
`estimate-route-preset-default` records that the form sends no preset and that
the server-side `"pilot"` default is therefore load-bearing. On this build's
rfdiffusion form the POST body does include `preset=pilot`. That note may be
scoped to the estimate XHR rather than the submit POST; it is flagged here
rather than corrected, because this test read the submit form only.

## Step 2 — paid run at $1.20

Approved by Leo in session on 2026-10-14. Not yet launched: the wallet still
reads $1.13, so the balance precondition is unmet (see "Blocked" below).

### The target, fixed before launch

**PDB 4ZQK, chain A** — PD-L1 from the PD-1/PD-L1 complex. Hotspots **54, 56,
115**. Binder length 55-65, preset `pilot`, `num_designs` 4.

Chain A was read straight from the downloaded coordinate file
(`files.rcsb.org/download/4ZQK.pdb`, 309,744 bytes), counting CA atoms in ATOM
records:

| Property | Value | How read |
| --- | --- | --- |
| Molecule | Programmed cell death 1 ligand 1 (PD-L1) | `COMPND` MOL_ID 1, `CHAIN: A` |
| CA count, chain A | 115 | `awk` over ATOM/` CA `/col 22 == `A` |
| Residue numbering | 18 to 132 | first and last CA in chain A |
| Gaps | none | 132 - 18 + 1 = 115, equal to the CA count |
| Hotspot 54 | ILE, CA present | same `awk`, filtered to 54/56/115 |
| Hotspot 56 | TYR, CA present | same |
| Hotspot 115 | MET, CA present | same |
| Heteroatoms in chain A | 1 NA, 9 HOH | `HETATM` with col 22 == `A` |

Chain A's 115 residues sit just under the 120-residue figure hard-coded in
`live_start_usd` (`shared/wallet_guard.py:83`), so the start minimum derived in
this document should be close to the one that applies at submit. No other PDB
entry was compared against that figure; chain B (PD-1, 106 CA) is the only
other chain in this file and is not used.

The three hotspots came from the rfdiffusion form itself, as the hotspot
field's `placeholder="54,56,115"`
(`templates/tools/rfdiffusion_form.html:202`). They are not an independent
epitope choice by this test. A placeholder is grey hint text, not a submitted
value, so the hotspots must be typed in; `binder_length_min` 55,
`binder_length_max` 65 (lines 243, 246) and `num_designs` 4 (line 264) are
real defaults, carried by `pre_value`, and the test's chosen parameters match
them.

### Blocked

Leo reported the balance set to $1.20. Three production page reads disagree,
all taken on 2026-10-15 (UTC) through the browser session signed in as
`leowan7+t1@gmail.com`. They are page reads, not repository facts, and nothing
in this repo can reproduce them:

| Page | Reads |
| --- | --- |
| `/account/wallet` | `CURRENT BALANCE $1.13`; signup credit $1.13 of $5.00 unspent |
| `/account` | `WALLET BALANCE $1.13` |
| `/account/wallet/transactions` | `2 entries` |

The two entries are the original `SIGNUP CREDIT +$5.00` and the step-1
`ADJUSTMENT -$3.87` to $1.13. No third row exists, so the $1.20 adjustment was
never committed against this wallet. Step 2 cannot run until the wallet reads
$1.20: at $1.13 the gate proven in step 1 would simply refuse again, and the
step-2 observations would be void.

The test does not write to production, so the balance change is Leo's to make.

**Second blocker, found later on 2026-10-15: the browser session expired.**
`https://tools.ranomics.com/account/wallet` now redirects to
`/login?next=/account/wallet`, and the page offers "Continue with Google" or an
email/password form. The test brief forbids this session from entering
passwords, so the wallet cannot be re-read until Leo signs in again. The three
reads in the table above were taken before this, while the session was live.

**A second run of the balance script was reported, and is not verified here.**
The hub lead relayed on 2026-10-15 that Leo re-ran `t1-set-balance-1.20.sql`
and that both columns of its final `SELECT` read 1.20. That is recorded as
reported, not confirmed: the relay is not a production read, the hub lead
states it did not read production either, and the sign-out above prevents this
session from checking. Under the brief's GPU rule a hub-lead relay is also not
spend approval, which must come from Leo in the test session directly. So two
things gate the launch — a signed-in page read showing $1.20, and Leo's own
go-ahead.

### Predicted, from the code, before the run

Written before launch so these are predictions, not readings. Each cites the
deployed build; the run either confirms or falsifies them.

The three surfaces the brief asks about are not three independent decisions.
The page and the email both read one predicate, `stopped_for_balance`
(`shared/run_notices.py:28-38`), which is true exactly when the job status is
`succeeded` and `result["stop_reason"]` equals `WALLET_STOP_REASON`
(`"wallet_empty"`, `shared/jobs.py:1836`).

| Surface | Will it say "stopped by balance"? | Why |
| --- | --- | --- |
| Run page | Predicted yes | `templates/job_detail.html:250-251` renders "Stopped: your wallet balance reached $0" plus an "Add funds" link under `stopped_for_balance`, passed in at `blueprints/jobs.py:790` |
| Email | Predicted yes | subject `shared/email.py:73`, headline `:214`, top-up link `:229`, all three gated on the same predicate |
| CSV | Predicted **no** | `candidates_to_csv` takes `candidates`, `fetch_bytes` and `default_job_id` only (`shared/exports.py:704-707`) — no job and no result dict reach it, so it has no field that could carry a stop reason |

The CSV prediction is structural rather than an omission: the export is built
per candidate row, so there is nowhere for a run-level notice to go.

### A correction to the brief's expected outcome

The brief expects the run to end "`succeeded` + partial, or `completed_no_yield`".
On this build those are not two values of one field:

- The balance stop hard-codes `terminal_status="succeeded"` and writes
  `partial: True` with `stop_reason` (`shared/jobs.py:1951-1965`). Status is
  `succeeded` whether or not any design survived.
- `completed_no_yield` is a **`failure_class`**, a separate column written by
  `classify_terminal_state` (called at `shared/jobs.py:1689`, `:1717`,
  `:1740`, `:1997`). It is returned for a `succeeded` run whose candidate
  array is present and empty (`:865`).

So a zero-design balance stop is predicted to be `status=succeeded` **and**
`failure_class=completed_no_yield` at the same time, and still to read as
stopped-for-balance on the page and in the email, because `stopped_for_balance`
reads `status`, not `failure_class`. `completed_no_yield` is in
`_BILLED_FAILURE_CLASSES` (`shared/jobs.py:789-795`), so a zero-design run is
still billed for the GPU it consumed.

If instead the page or email goes quiet on a zero-design run, that falsifies
this reading and is worth a defect.

### The email cannot be read from inside the app

No view of sent mail was found in the app. Every `email_log` in the tree is a
pytest fixture -- three of them, at `tests/test_stripe_webhooks.py:127`,
`tests/test_wallet.py:447` and `tests/test_wallet_funnel.py:129` -- and the
name does not occur outside `tests/` at all.

A `git grep` for `sent_email`, `email_sent`, `outbox`, `mail_log`,
`emails_sent` and `email_events`, with no file filter, outside `tests/` and
outside this document, returns hits in exactly two places. Neither is a mail
log:

- `reengagement_email_sent_at` (`blueprints/admin.py:1199-1200`, `:1284`;
  `cron/reengagement.py:11`, `:217`, `:277`, `:301`), a per-user timestamp in
  `auth.users.user_metadata` recording when a re-engagement mail last went out.
  It stores no message body.
- `email_sent = 2` (`supabase/config.toml:189`), the hourly email count under
  `[auth.rate_limit]`. A rate-limit setting, unrelated to any log.

An earlier draft of this section said the search returned only the first of
those. That was written from a grep filtered to `*.py`, `*.html` and `*.sql`,
which could not have seen a `.toml` file: the claim was broader than the
search behind it. The admin area holds five templates
(`templates/admin/`: `campaign_detail`, `campaigns_list`, `signups_rejected`,
`user_detail`, `users_list`) and none of them is a mail log.

That is a bounded negative -- those six names and those five templates -- not a
survey of every route. The completion mail goes to `leowan7+t1@gmail.com`, so
answering the brief's email question needs Leo to open or forward that message.
Everything else here is readable without his inbox.

### Precondition: the live meter depends on a Railway cron

`meter_live_runs` is what debits a live run and stops it at $0. It has exactly
one caller outside `tests/`: `cron/tick_campaigns.py:68`. That module runs only
through the Flask CLI command `campaigns:tick` (`app.py:1114-1126`). The
`Procfile` declares only `release` and `web` — no worker, no scheduler — so the
schedule lives outside this repository.

**That cron is scheduled, and was confirmed by direct observation.**
`docs/audit-2026-07-22-campaign-rework-open-items.md:807` opens A46 ("The
campaign tick has no schedule in this repo"); the resolution is the bullet at
lines 820-827, "**RESOLVED 2026-07-30 by direct observation**": Railway service
`tools-hub-campaigns-tick`, start command `flask --app app campaigns:tick`,
schedule `*/5 * * * *`, auto-deploying from `leowan7/tools-hub` branch `main`.
Executions ran unbroken every 5 minutes across the observed window and all
succeeded; one fired during the check and took 8 s. `docs/HANDOFF-2026-07-29-qc-round3.md:13`
records the same resolution.

The residual risk is staleness, not absence: that observation is from
2026-07-30, about two and a half months before this test. A glance at the
Railway dashboard before launch is worth it as a sanity check. It is not a
reason to expect the meter to be dead.

Two searches over the tree, both excluding this report:
`git grep -n '60-90'` (15 hits) and `git grep -n 'A46'` (8 hits). The block
lists every hit, with long paths shortened, the three `test_proteina_smoke.py`
hits folded onto one line, and a one-word annotation added to each:

```
$ git grep -n '60-90' -- . ':!docs/qa/RFDIFFUSION-LIVE-STOP-2026-10-14.md'
app.py:1121:        Scheduled via Railway cron (~60-90s).                      STALE
blueprints/campaigns.py:99:# tick (~60-90s) reconciles every campaign ...      STALE
blueprints/campaigns.py:866:    # cron reconcile (~60-90s), or, absent ...     STALE + hedges existence
blueprints/targets.py:1463:  # ... puts a tick at ~60-90 s, but the SCHEDULE   STALE (inside the A46 block)
cron/tick_campaigns.py:15:Scheduled separately via Railway cron (~60-90s)      STALE
docs/COMPUTE-CAMPAIGNS-PLAN.md:66:- (c) ... planned at ~60-90s, but            CORRECT (corrects itself at 67-68)
docs/audit-2026-07-22-...-open-items.md:812:  ~60-90 s, but the schedule ...   HISTORICAL (A46's own problem statement)
docs/audit-2026-07-22-...-open-items.md:829:  the tick runs at ~60-90 s. ...   CORRECT (the correction bullet)
docs/design/LIVE-CHARGING-2026-10-01.md:156:- **Tick interval: UNVERIFIED.**   STALE
docs/design/LIVE-CHARGING-2026-10-01.md:400:- ... The tick is 300 s, not ...   CORRECT
gpu/modal_client.py:221:    # once warm (~60-90 s cold including model load)   OFF-SUBJECT (Modal cold start)
tests/test_proteina_smoke.py:401,407,421  ``A1-50,B1-50,A60-90``              OFF-SUBJECT (residue ranges)
tools/proteina/run_pipeline.py:2028  "A1-50,A60-90" typed on purpose          OFF-SUBJECT (residue ranges)

$ git grep -n 'A46' -- . ':!docs/qa/RFDIFFUSION-LIVE-STOP-2026-10-14.md'
blueprints/targets.py:1468:        # Filed as A46.                             STALE (asserts A46 open)
docs/COMPUTE-CAMPAIGNS-PLAN.md:68:  2026-07-30, see A46 -- so a stranded ...  CORRECT
docs/HANDOFF-2026-07-29-qc-round3.md:13:**A46 RESOLVED** (checked in the ...   CORRECT
docs/HANDOFF-2026-07-29-qc-round3.md:244:**A46** campaign tick has no ...      STALE (asserts A46 open)
docs/audit-2026-07-22-...-open-items.md:807:### A46. The campaign tick has ... HISTORICAL (entry heading; resolved below)
docs/design/LIVE-CHARGING-2026-10-01.md:156:  (same line as above)             STALE
docs/design/LIVE-CHARGING-2026-10-01.md:333:10. **Confirm** that the ...       STALE (open action, already done)
tests/test_multichain_targets.py:1108:    union offers "A46".."A55"           OFF-SUBJECT (chain labels)
```

- **The "~60-90s" cadence is wrong in five places**, all in code.
  `app.py:1121`, `blueprints/campaigns.py:99` and `cron/tick_campaigns.py:15`
  state the cron *is* scheduled, so only the parenthesised figure is wrong in
  those three. `blueprints/campaigns.py:866` hedges on existence ("absent the
  cron"). `blueprints/targets.py:1463` says the schedule "lives outside this
  repo" and sits inside the A46 comment block.
- **A46 is asserted open in two places**: `blueprints/targets.py:1468` (in the
  1460-1468 block) and `docs/HANDOFF-2026-07-29-qc-round3.md:244`.
- **Two files contradict themselves.** `HANDOFF-2026-07-29-qc-round3.md` says
  "**A46 RESOLVED**" at line 13 and lists A46 as open at line 244.
  `LIVE-CHARGING-2026-10-01.md` says "**Tick interval: UNVERIFIED**" at 156 and
  lists confirming the cron as an open action at 333, while line 400 already
  has it right. Line 156's cite of `blueprints/targets.py:1457-1461` is also
  off; that block opens at 1460.
- **HISTORICAL** marks the A46 entry's own heading and problem statement
  (`audit:807`, `:812`). Neither is stale: the same entry records the
  resolution at lines 820-827 — which is not itself a sweep hit — and the
  correction at line 829, which is.

**On line 400 and the cadence used below.** `LIVE-CHARGING-2026-10-01.md:400`
states "The tick is 300 s", the same 5 minutes the dashboard observation
recorded. It is **not** independent corroboration: `git log -S` places it in
b89e7531 on 2026-10-07, after the 2026-07-30 observation and into a tree where
the audit document already said 5 minutes, and it cites no source. Treat it as
agreement, not a second measurement. The cadence below rests on the dashboard
observation alone.

### The expected stop, computed before the run

The tick cadence is **5 minutes**, from the `*/5 * * * *` schedule observed in
the dashboard. The two docstrings that say "~60-90s" are stale: they record
what was *planned*. `docs/COMPUTE-CAMPAIGNS-PLAN.md:66-68` states the
discrepancy outright — "planned at ~60-90s, but the Railway cron actually runs
`*/5 * * * *` -- verified in the dashboard 2026-07-30, see A46 -- so a stranded
campaign waits up to 5 min".

An earlier draft of this section read those two docstrings' "~60-90s" as
authoritative and on that basis revised the cadence from 5 minutes down to
60-90 s. That was backwards: it replaced the right figure
with a stale one and did not consult the source that resolves the conflict.
The figures below are recomputed at 5 minutes.

Measured against the deployed pricing code (`live_due_usd`, `live_start_usd`,
`gpu_class_for_job`) for `rfdiffusion` at `preset=pilot`, `num_designs=4`.
These do not depend on cadence:

| Quantity | Value |
| --- | --- |
| GPU class | A100-40GB |
| Start minimum | $1.1371 |
| Cost of 60 s metered | $0.0728 |
| Cost of 936 s metered | $1.1361 |
| Balance $1.20 exhausted at | **989 s = 16.5 min** of metered GPU time |

With $1.20 the run should get about **16 and a half minutes** of metered time
after its first heartbeat, then stop. At one debit every 5 minutes that is
**about 3 to 4 debits** before the stop (989 / 300 is about 3.3), not the
11-16 an earlier draft predicted. Queue and boot are not metered, so the window
starts at the first heartbeat, not at submit.

Escalation threshold, made concrete: two ticks past $0 is about **10 minutes**.
A run still going more than ~10 minutes after the balance reads $0 is the
billing defect the brief asks to be reported immediately. A single tick's
overshoot is expected and is not a defect: the stop can only ever fire on a
tick, so the run can run up to 5 minutes past the moment it owes more than the
balance covers.

## Verdict

Step 1: **PASS.** The start gate refuses rfdiffusion at $1.13 in both layers,
creates no job and no ledger row, and leaves the balance untouched.

Step 2: not yet run. Blocked on the wallet balance, not on any defect.
