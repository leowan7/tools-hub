# rfdiffusion live charging: production test, 2026-10-14

Subject: PR #427, merged as squash `5b01615f`. Two things are under test.

1. **The start gate** (new in #427): `shared/wallet_guard.py::requires_wallet`
   refuses to START rfdiffusion when `0 < balance < live_start_usd(...)`.
2. **The live stop**: the `campaigns:tick` meter debits a running job about
   every 5 min and, at $0, cancels the Modal call while keeping the designs
   already uploaded (`shared/job_recovery.py::reconstruct`).

Test account: `leowan7+t1@gmail.com`. Leo signs in and runs every prod write
himself; this session reads prod and drives the browser only.

Status of this document: **both steps run and recorded.** Step 1 passed. Step 2
ran to completion but did **not** exercise the behaviour it was designed to
test, because the run finished with money left in the wallet. Figures written
before the run are kept under headings that say so, and are not restated as
results. Nothing in this file is a prediction dressed as a result.

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

Commissioned by the test brief, which asks for exactly one paid run. **Launched
and in progress at the time these lines were written.** The balance precondition
was met first ($1.20, verified below), and Leo gave the per-job go-ahead for the
GPU spend in session, which memory `feedback-gpu-spend-needs-approval` requires
per job rather than per session.

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

### The balance precondition, blocked then cleared

Step 2 needs the wallet at $1.20. It read $1.13 on every read taken before
2026-10-15 04:06 and $1.20 after. The reads below are production page reads, not repository
facts; nothing in this repo can reproduce them.

First, at $1.13 (2026-10-15, browser session signed in as the test account):
`/account/wallet` and `/account` both read $1.13, and
`/account/wallet/transactions` read `2 entries` -- the original
`SIGNUP CREDIT +$5.00` and the step-1 `ADJUSTMENT -$3.87` to $1.13. At $1.13
the gate proven in step 1 would simply refuse again, so step 2 was void.

Then the browser pane signed itself out: `/account/wallet` began redirecting to
`/login?next=/account/wallet`, and the brief forbids this session from entering
passwords. A hub-lead relay that Leo had re-run the balance script was recorded
at the time as reported-not-verified, because a relay is not a production read.

Cleared by the time of the reads below, taken in the browser where Leo was
already signed in. The ledger stamps the clearing row `2026-10-15 04:06`; the
page prints no time zone, so that stamp is reproduced as shown rather than
converted:

| Page | Reads |
| --- | --- |
| `/account` | `EMAIL leowan7+t1@gmail.com`, `WALLET BALANCE $1.20` |
| `/account/wallet/transactions` | `CURRENT BALANCE $1.20`, `3 entries` |

The third row is `ADJUSTMENT +$0.07` to $1.20, carrying the note
`T1 rfdiffusion live-stop test 2026-10-14: set balance to $1.20 (just above
start minimum)`. That wording comes from the balance script, which lives in this
session's scratchpad outside the repository and so cannot be diffed against the
note from here. The three amounts sum to the balance
(`5.00 - 3.87 + 0.07 = 1.20`), the email is the test account and not the main
one, and there is no `HOLD`, `RUN` or `CHARGE` row, so no run has ever billed
this wallet. The relay was accurate.

**One method note, because it nearly produced a false report.** The first read
of the transactions page in Leo's browser showed $1.13 and `2 entries`, which
reads as a flat contradiction of the relay. What is observed: that tab was
already open on that URL, the first action against it was a text read rather
than a navigation, and re-navigating the same URL immediately after returned
$1.20 and `3 entries`. The likeliest cause is that the read returned the page
as Leo had last loaded it, before the write, but a cached or lagging response
fits the same observation and this test cannot tell them apart. Either way the
operational rule is the same, and it is the part worth keeping: a page read is
evidence only for the moment the page was *loaded*, which need not be the
moment it was read. Navigate first, then read.

What still gates the launch is Leo's own go-ahead for the GPU spend, given in
the test session; under the brief a hub-lead relay is not spend approval.

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
| CSV | Predicted **no** | `candidates_to_csv` takes `candidates`, `fetch_bytes` and `default_job_id` only (`shared/exports.py:704-707`) — no job and no result dict reach it, so it has no field that could carry a stop reason. Its `fieldnames` are `leading + metrics + sequence_columns(rows)` (`:729-731`), every one of them per-candidate |

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
hits folded onto one line, and a short annotation added to each:

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

### The launch, as it happened

Leo gave the per-job go-ahead in the test session on 2026-10-15. The run was
submitted from the browser already signed in as `leowan7+t1@gmail.com`.

**Job `cbaa3b67-8663-4b6d-8772-34cc5ef576b3`**, submitted `15 Oct 2026, 04:33
UTC` as printed by the job page.

Form as submitted, read back off the page before the click:

| Field | Value |
| --- | --- |
| Target PDB | `4ZQK.pdb`, uploaded whole |
| Target chain | `A` |
| Hotspot residues | `54,56,115`, typed |
| Binder length | 55 to 65 |
| Number of designs | 4 |
| Preset, as the job page labels it | `trial run` |

The free `Check my settings` pass ran first and returned `Settings look good.
Nothing was charged and no run was started.` Its readout agreed with the target
notes recorded above launch: `chain A -- 115 residues`, `Hotspots 54, 56, 115
-- all preserved`, `Size envelope: 115 aa target (cap 500 aa on A100-40GB)`,
and cleanup dropping 10 residues (waters / HETATM / incomplete backbone) plus
chain B as the non-target chain. The page offered to substitute the AlphaFold
model `AF-Q9NZQ7`; that was declined, because the brief fixes the crystal
structure and swapping it would change the target mid-test.

**The hotspot field had to be typed twice.** The first attempt used a
reference-targeted triple-click followed by a type, inside a batch issued right
after the upload. Every action reported success, yet the field stayed empty:
the summary chip still read `4ZQK.pdb, chain A, 0 hotspots` and the form showed
the blocking message `This tool needs at least one hotspot residue.` A
coordinate click on the field followed by the same typing took, and the chip
moved to `3 hotspots`. The earlier read had also shown
`textbox "54,56,115" ... placeholder="54,56,115"`, where the accessible name is
the placeholder and not a value -- the field reads as populated when it is not.
Anyone scripting this form should assert on the summary chip's hotspot count,
not on the field.

**Preset naming, resolved.** The job page prints `Preset trial run` while the
cost derivation in this document was computed for the slug `pilot`. These are
the same preset, and the chain is short enough to check: `job_detail.html:122`
renders `Preset <strong>{{ job.preset | preset_label }}</strong>`, and
`preset_label` (`shared/tool_meta.py:50`) looks the stored slug up in
`_PRESET_LABELS`, whose first entry is `"pilot": "trial run"`
(`shared/tool_meta.py:30`). The comment above that table says why the display
name differs at all -- the `Preset.label` strings are form sentences, too long
for a badge -- and `tools/rfdiffusion/meta.py:174` carries the long form,
`Trial run: 8 binders`. So the page shows a label, the row stores `pilot`, and
the figures below rest on the slug the costing used.

**First ledger row, at submission.** The transactions page gained one row
within seconds of the submit:

| When | Kind | Detail | Amount | Balance after |
| --- | --- | --- | --- | --- |
| `2026-10-15 04:33` | `RUN` | `rfdiffusion` / `charged as it runs; $0.00 so far` | `$0.00` | `$1.20` |

That is the lineage anchor predicted from `0045_live_run_debits.sql`: an opening
row of amount zero, not a reservation. The balance is untouched at submission,
which is what "queue and boot are not billed" looks like from the ledger side.
**The progress line, read twice.** The status went `PENDING` -> `RUNNING`
within about a minute of the click. Two reads of that page, verbatim:

| Read at | Status | Progress line |
| --- | --- | --- |
| ~`04:34` | `RUNNING` | `Running RFdiffusion 0 of 4 designs complete` |
| `04:36` | `RUNNING` | `Waiting for pipeline to start...` |

The second read is the earlier-sounding message, so the line moved backwards.
This document does not claim to know why; a cold GPU container, a progress
field that is written optimistically at dispatch and then re-derived from the
job row, and a cached first response all fit what was seen. What matters for
the test is that `Waiting for pipeline to start...` is the unmetered state:
the first heartbeat is what stamps `started_at` and begins billing, so a run
can sit in `RUNNING` without owing anything. The ledger agreed -- still one
`$0.00` row at that point.

### Observed, as it ran

Both pages were re-navigated before each read, never read off an already-open
DOM -- an earlier read in this test returned a pre-write balance because the
first action on a standing tab was a read rather than a navigation.

| Clock (UTC) | Job page status / progress | Balance | Ledger entries | RUN row detail |
| --- | --- | --- | --- | --- |
| `04:33` | submitted | `$1.20` | 4 | `charged as it runs; $0.00 so far` |
| ~`04:34` | `RUNNING` / `Running RFdiffusion 0 of 4 designs complete` | `$1.20` | 4 | `charged as it runs; $0.00 so far` |
| `04:36` | `RUNNING` / `Waiting for pipeline to start...` | `$1.20` | 4 | (not re-read) |
| `04:39` | `RUNNING` / `Waiting for pipeline to start...` | `$1.07` | 4 | `charged as it runs; $0.13 so far` / `1 charge while it ran` |
| `04:40` | `RUNNING` / `Waiting for pipeline to start...` | `$1.07` | 4 | `charged as it runs; $0.13 so far` |
| `04:42` | `RUNNING` / `Waiting for pipeline to start...` | `$0.73` | 4 | `charged as it runs; $0.46 so far` |
| `04:45` | `RUNNING` / `Waiting for pipeline to start...` | `$0.73` | 4 | `charged as it runs; $0.46 so far` |
| `04:47` | `SUCCEEDED` / `Completed in 816 GPU-seconds.` | `$0.20` | 5 | `charged as it ran; it closed at $0.99` / `3 charges while it ran` |

**Finding 1 (as written in flight, and WRONG -- the correction follows):
the per-tick debits are not ledger rows on this page.** What was observed is
that after the first metered charge the entry count was still `4`, and the
charge appeared as prose inside the opening row's detail cell (`$0.13 so far`,
`1 charge while it ran`). From that I concluded the individual `run_debit` rows
were not rendered at all and could only come from the read-only query.

**That conclusion was false. The rows are rendered.** `1 charge while it ran`
is the `<summary>` of a `<details class="wallet-tx-debits">`
(`templates/wallet/transactions.html:159-169`), whose list is built from the
anchor's `run_debit` children at `blueprints/wallet.py:143-147` and passed to
the template at `:200`. It is a disclosure control, collapsed by default, and
expanding it yields exactly what the brief asked for:

| Tick (UTC) | Amount |
| --- | --- |
| `2026-10-15 04:35` | `-$0.13` |
| `2026-10-15 04:40` | `-$0.34` |
| `2026-10-15 04:45` | `-$0.34` |

Three ticks, `$0.81` in total, landing on the `*/5` cron boundaries -- the
first direct confirmation in this test that the cadence is five minutes rather
than the `~60-90s` of the stale comments catalogued earlier in this document.

How the error happened is worth recording, because the mechanism is a repeat.
`find` resolved the summary text to one node with role `generic`, and I read
that role label as a statement about the DOM -- "static text, nothing to
expand" -- without ever trying to expand it. That is asserting a structural
property from evidence narrower than the claim, the same mechanism as
`qc-count-from-a-filtered-grep`. The data I called unobtainable took one
`details[open]` toggle to read.

What survives is a real but much smaller defect: the disclosure is **collapsed
by default**, so what a customer sees first for a running job is the prose
roll-up -- and the roll-up is the figure that is a cent off (Finding 4).

**Finding 2: the opening row's own AMOUNT and BALANCE AFTER columns go stale.**
At `04:39` that row still read `$0.00` in AMOUNT and `$1.20` in BALANCE AFTER
while the live balance was `$1.07`. BALANCE AFTER is literally defensible --
the page's own preamble defines it as "what you had right after each entry
cleared", and $1.20 was correct right after the anchor cleared -- but the row
is the only place this job appears, so a reader scanning the AMOUNT column sees
`$0.00` for a job that has by then taken 13 cents. The true figure is carried
in free text one cell to the left.

**Finding 4: the roll-up figure is a cent below the rows it summarizes.** At
`04:42` the page showed `$0.46 so far` against a CURRENT BALANCE of `$0.73`.
Those cannot both be exact: $1.20 - $0.46 = $0.74.

The debit rows recovered afterwards localize the cent, which the in-flight
reading could not. By `04:42` two ticks had fired, `$0.13` and `$0.34`, summing
to `$0.47` -- and $1.20 - $0.47 = $0.73, exactly the balance shown. So the
balance and the individual rows agree to the cent; it is the `so far` roll-up
that reads low. The same discrepancy recurs at settlement, in the same
direction, which is what makes this a finding rather than a one-off rounding
wobble -- see the sharpened version below.

**Rate check, which does hold.** Cumulative went `$0.13` -> `$0.46` between
about `04:36` and `04:42`, so roughly `$0.33` across one tick interval. The
derived rate is `$0.0728` a minute, which over a 5-minute tick predicts
`$0.364`. Close enough to confirm the meter is billing A100-40GB time at about
the expected rate rather than drifting.

**Finding 3 (as written in flight, and largely a measurement artifact -- the
correction follows): the progress line reverted to "not started" after showing
real progress, and stayed there while the wallet billed.** The readings
themselves, from the table above: at ~`04:34` the page read `Running
RFdiffusion 0 of 4 designs complete`; from `04:36` through `04:45` it read
`Waiting for pipeline to start...`; and across that span the wallet took
`$0.13` and then `$0.34`.

I recorded two readings I could not then distinguish -- a benign render that
stops resolving `_progress`, or a container that crashed and restarted while
heartbeats kept the meter running -- and wrote down the discriminator: a stuck
container yields nothing, while a working run that was merely invisible still
produces designs for the GPU time paid for. The 4-of-4 result excluded the
crash reading.

**The render reading was wrong too, and the template says why.** `Waiting for
pipeline to start...` is not a state the server computed. It is the hard-coded
initial content of the span, emitted for any job in `('pending', 'running')` at
`templates/job_detail.html:188`. The only code that ever overwrites it is
`renderProgress`, from the poll response's `progress.stage` (`:756`), and the
poll is kicked off by `setTimeout(poll, pollIntervalMs)` at `:803` with
`pollIntervalMs = 5000` (`:579`). So the **first** fetch lands five seconds
after load, and until it does the placeholder stands unmodified.

Every later reading followed the navigate-then-read rule from
`reference-preopened-tab-reads-stale`, which means every one of them sampled
the page within a second or two of load -- inside that five-second window. The
confirming detail is the design count: `0 of 4 designs complete` comes from a
*separate* span (`#progress-text`, `:189`) which the server renders **empty**,
and in all the later readings it was absent rather than reading zero. Both
spans were simply in their pre-poll state. The `04:34` reading, which showed
both, is the one where a poll had already landed.

So there was no eleven-minute stall and no regression. A smaller real defect is
left: nothing server-renders current progress, so **every** load of a running
job shows `Waiting for pipeline to start...` for five seconds, however far
along it is. A customer who reloads a job ten minutes in, while their balance
is visibly falling, is told it has not started. `inputs._progress` already
holds what the span needs. Severity is five seconds per load, not eleven
minutes; the in-flight framing overstated it by two orders of magnitude.

**Rate check, which does hold.** Cumulative went `$0.13` -> `$0.46` between
about `04:36` and `04:42`, so roughly `$0.33` across one tick interval. The
derived rate is `$0.0728` a minute, which over a 5-minute tick predicts
`$0.364`. Close enough to confirm the meter is billing A100-40GB time at about
the expected rate rather than drifting. The first tick is consistent too:
`$0.13` is about 107 seconds at that rate, and the anchor row carries a
minute-resolution timestamp of `04:33`, so the window to the `04:35` tick is
between 60 and 120 seconds. 107 falls inside it. No finer claim is possible
from the UI, which shows no figure to better than a cent or a minute.

### The result: the run finished before the money did

**The balance stop was never exercised.** This is the central outcome and it is
a negative result, not a pass. The run completed normally with money left in
the wallet, so no part of the stop-at-zero path -- the cancellation, the partial
delivery, the `stopped_for_balance` predicate, the page and email wording, the
`completed_no_yield` classification -- was reached by this test.

Final state, read off both pages at `04:47`:

| Thing the brief asks for | Observed |
| --- | --- |
| Job id | `cbaa3b67-8663-4b6d-8772-34cc5ef576b3` |
| Status | `SUCCEEDED` |
| GPU time | `Completed in 816 GPU-seconds.` |
| Designs kept | `CANDIDATES (4)` / `Returned 4 / 4` -- all four, not a partial |
| Downloadable | `Download CSV`, `FASTA`, `Structures (ZIP)` offered, plus a per-design `.pdb` on every row |
| Balance after | `$0.20`, from `$1.20` |
| Cost as the page states it | `net for this job $0.99` |
| Page says stopped by balance? | No -- correctly, it was not |
| Email says stopped by balance? | Not applicable, and not readable from inside the app |
| CSV says stopped by balance? | Not applicable; the CSV structurally carries no stop reason |

The ledger closed with two rows for this job, one of which is new:

| When | Kind | Detail | Amount | Balance after |
| --- | --- | --- | --- | --- |
| `2026-10-15 04:47` | `CHARGE` | `rfdiffusion` / `net for this job $0.99` | `-$0.19` | `$0.20` |
| `2026-10-15 04:33` | `RUN` | `rfdiffusion` / `charged as it ran; it closed at $0.99` / `3 charges while it ran` | `$0.00` | `$1.20` |

Scores, for the record, since a 4-of-4 return on a 115-residue target is a
better result than this test expected: ipTM `0.860 / 0.790 / 0.740 / 0.730`,
pLDDT `95.3 / 89.7 / 51.5 / 89.2`, i_pAE `3.73 / 5.25 / 5.96 / 6.59` A. Three of
the four meet the page's own quality bar; design 3 fails it on pLDDT `51.5`.

### Why it could not have stopped, which is the finding worth keeping

The brief predicted "0 or 1 design" on the reasoning that "$1.20 sits just
above the start minimum". That reasoning does not hold, and the arithmetic says
why. All three figures use the rate derived earlier in this document,
`$0.0728` a minute on A100-40GB:

| Quantity | GPU-seconds | Dollars |
| --- | --- | --- |
| Start minimum, `live_start_usd` | 937 | `$1.1371` |
| What the test wallet could buy | 989 | `$1.20` |
| What the run actually needed | **816** | **`$0.99`** |

The start minimum is not the price of *starting*. `live_start_usd` computes it
as `runtime_estimate_min(TOOL_RULES[tool_slug], 120, 1) * 60`, which is the
estimated cost of a whole small run -- and for this target that estimate (937 s)
came in **above** what a real 4-design run consumed (816 s). So a wallet holding
exactly the start minimum can already afford the entire job. The gate and the
stop are calibrated such that clearing the first makes the second unreachable,
for this tool at this size.

That is a structural property, not an accident of this run:

- To reach `$0` mid-run, actual GPU time must exceed the estimate the gate
  charged admission for. Here the estimate was conservative by about 15%.
- `num_designs=4`, which the brief fixed, is half of what this preset is named
  for (`tools/rfdiffusion/meta.py:174`, `Trial run: 8 binders`). Eight designs
  on the same target would plausibly need roughly twice 816 s, about `$1.98`,
  which `$1.20` cannot cover -- and `live_start_usd` would still have admitted
  it, because it prices 1 design at 120 residues regardless of the 8 requested.

So the way to exercise the stop is a run whose true cost exceeds the gate's
one-design estimate: more designs, a larger target, or a tool whose estimate is
known to under-quote. `bindcraft-runtime-anchor` records bindcraft under-quoting
by 4.6x on a second run, which is exactly the shape that would trigger it.
Nothing about that needs this test re-run at a lower balance; a lower balance
simply fails the gate, which step 1 already proved.

**The rate derivation is confirmed to the cent.** 816 GPU-seconds at
`$0.0728`/minute is `$0.9901`, and the page charged `$0.99`. The pricing model
used throughout this document is correct for A100-40GB rfdiffusion.

### Finding 3 resolved: the pipeline was fine, and so was the page

Two readings were open while the run was in flight. The 4-of-4 result closed
the first: a crashed-and-restarted container cannot produce four scored designs
in 816 GPU-seconds, so the work proceeded normally throughout.

Reading the template afterwards closed the second, and against my own
in-flight conclusion. `Waiting for pipeline to start...` is the span's
hard-coded server-rendered default (`templates/job_detail.html:188`), the only
writer is the poll callback (`:756`), and the first poll fires five seconds
after load (`:803`, interval `:579`). My polling protocol -- navigate, then
read immediately -- sampled inside that window every time, and the empty
`#progress-text` span (`:189`) in each of those readings confirms it: both
spans were pre-poll, not regressed. There was no stall.

The residual defect is narrow and still worth a ticket: a running job's page
asserts "not started" for the first five seconds of every load, no matter how
far along the run is, because current progress is never server-rendered even
though `inputs._progress` holds it. Five seconds, not eleven minutes.

The more useful lesson is about the measurement. `reference-preopened-tab-reads-stale`
says a pre-opened tab reads stale, so navigate first. Following that rule
mechanically introduced the opposite error -- reading *before* the page had
caught up -- and the artifact was indistinguishable from a server-side defect
until I read the template. A page that hydrates after load needs both: navigate
first, **then** wait past its own poll interval before quoting it.

### Finding 4 sharpened: the roll-up is a cent under, twice, in the same direction

The recovered debit rows turn this from an unexplained gap into a localized
one. Every money figure the page showed now reconciles except the roll-ups:

| Figure | Source | Value |
| --- | --- | --- |
| Three run debits | expanded disclosure | `$0.13` + `$0.34` + `$0.34` = **`$0.81`** |
| Settlement | `CHARGE` row AMOUNT | **`-$0.19`** |
| Sum of rows that moved money | | **`$1.00`** |
| Balance movement | `$1.20` -> `$0.20` | **`$1.00`** |
| `so far` roll-up at `04:42` | vs `$0.47` of rows by then | **`$0.46`** |
| Closing roll-up | `charged as it ran; it closed at` / `net for this job` | **`$0.99`** |

The rows and the balance agree exactly, at both readings. The two roll-up
figures are each one cent below the rows they summarize. One cent, twice, in
the same direction, computed by the same code path.

The rate check says which figure is the accurate one: 816 GPU-seconds at the
derived `$0.0728`/minute is `$0.9901`, so the roll-up's `$0.99` is right and
the per-row displays are each rounded up a fraction of a cent, summing to a
cent of overstatement across four rows.

That leaves one question the UI cannot answer, and it is the only part of this
finding that could matter: whether the **stored** balance lost that cent or
merely displays truncated. If `balance_usd` is decremented by rounded per-tick
amounts, a customer really pays a fraction of a cent extra per tick, and a long
run has many ticks -- a 10-hour run is 120 ticks. If the balance is stored
unrounded and only displayed to two places, nothing is lost and this is
cosmetic. Both fit every figure above, because nothing on screen resolves finer
than a cent. The read-only query returns unrounded `amount_usd` and
`balance_after_usd` per row and settles it in one look.

## Verdict

Step 1: **PASS.** The start gate refuses rfdiffusion at $1.13 in both layers,
creates no job and no ledger row, and leaves the balance untouched.

Step 2: **ran clean, but did not test the stop.** Job `cbaa3b67` went out at
`04:33` UTC against the fixed target with the wallet at $1.20, and succeeded at
`04:47` in 816 GPU-seconds, returning 4 of 4 designs for `$0.99` and leaving
`$0.20`. The balance never reached zero, so the stop-at-zero path was never
entered. **The brief's question is therefore still open**, and the reason is
structural rather than a mistake in the run: `live_start_usd` admits a wallet
only if it already holds the estimated cost of a whole small run (937
GPU-seconds here), while the real run needed 816, so anything that clears the
gate can also finish. Exercising the stop needs a job whose actual cost
overruns that estimate -- more designs than the gate prices, a larger target,
or a tool that under-quotes.

Three defects survive scrutiny, none of which moves money in the wrong
direction. Two more were written down in flight and then disproved by reading
the code; both corrections are in the sections above, and the commit message of
`4a96a2fa` still carries the false version of the first:

1. **The roll-up figures are a cent below the rows they summarize** -- twice,
   in the same direction, from the same code path. The rows and the balance
   agree exactly; `$0.46 so far` and the closing `$0.99` are each a cent under.
   The rate check says `$0.99` is the accurate cost, so the row displays are
   the ones rounding up. Whether the **stored** balance also loses that cent
   per tick, or only its display truncates, is the one open question here, and
   it decides whether this is cosmetic or compounds over a long run's ticks.
2. **The anchor row's AMOUNT and BALANCE AFTER columns go stale**, reading
   `$0.00` and `$1.20` for a job that had taken 13 cents by then.
3. **A running job's page asserts "not started" for five seconds after every
   load**, because current progress is never server-rendered even though
   `inputs._progress` holds it. A customer reloading a job that is ten minutes
   in and visibly being charged is told it has not begun.

**Withdrawn on review, recorded because the errors are instructive:**

- *"The per-tick run debits are never rendered."* False. They are rendered,
  with time and amount, in a `<details>` that is collapsed by default
  (`templates/wallet/transactions.html:159-169`). I inferred a DOM property
  from one node's role label without trying to expand it. What is left is that
  the disclosure is collapsed, so the roll-up -- the figure that is wrong -- is
  what a customer reads first.
- *"The progress line showed `Waiting for pipeline to start...` for eleven
  minutes."* A measurement artifact. That string is the server-rendered
  placeholder and the first poll lands five seconds after load; my
  navigate-then-read-immediately protocol sampled inside that window every
  time. Overstated by two orders of magnitude.

What the rate model got right: the derivation used throughout this document
predicted `$0.9901` for 816 GPU-seconds, and the charge was `$0.99`.
