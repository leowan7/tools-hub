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

115 residues is the closest available match to the 120-residue figure
hard-coded in `live_start_usd` (`shared/wallet_guard.py:83`), so the start
minimum derived in this document is the one that should apply at submit.
Chain B (PD-1, 106 CA) is not used.

The three hotspots came from the rfdiffusion form's own worked example; they
are not an independent epitope choice by this test.

### Blocked

Leo reported the balance set to $1.20. Three reads on 2026-10-15 disagree:

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

## Verdict

Step 1: **PASS.** The start gate refuses rfdiffusion at $1.13 in both layers,
creates no job and no ledger row, and leaves the balance untouched.

Step 2: not yet run. Blocked on the wallet balance, not on any defect.
