# Handoff: Phase 2 QC rounds 3 to 10

Written 2026-07-29, updated 2026-07-30 after round 10. Supersedes earlier
versions of this file.

## State in one line

Rounds 3 to 5 are committed as `216a2b5`. **Rounds 6 to 10 plus A51 are fixed
and mutation-verified in the working tree but NOT committed** -- the standing
rule is that a non-author QC reads the diff clean before a commit, and the
round-10 remediation has not had that yet. Push and PR remain a separate ask.

**A46 RESOLVED** (checked in the Railway dashboard: the cron is real, runs
`*/5 * * * *`, and every run succeeds). **A51 authorised by Leo and fixed.**
**A52, A54, A56 filed. A53 and A55 both need Leo** and must be done together:
they are the same defect (wallet display rounding) across THREE classes of
figure -- costs, balances, and the required top-up -- found by three separate
rounds. Leo's stated sequence is to commit this diff first and do those on
their own.

**Do not execute A53 + A55 as a find-and-replace.** `tx.amount_usd` (a cost,
rounds UP) and `tx.balance_after_usd` (a balance, rounds DOWN) sit in the same
row of the transactions table; applying both rules mechanically makes
consecutive rows fail to reconcile, on the one page whose purpose is that the
reader can check the arithmetic. That needs a decision, not a sweep.

**Every round from 6 to 11 found a real defect in the previous round's fixes,
and every one was on a money display path.** If you are picking this up, that is
the base rate to plan around, not an anomaly to explain away.

`tests/money_display_guard.py` is a NEW file and is `git add`ed but uncommitted.
Two test modules import it at module scope, so committing without it is a
collection error across ~120 tests, not a single failure.

```
HEAD      216a2b5 fix(targets): QC remediation for Phase 2 multi-tool launch
          9e4f92d feat(targets): launch N tools against one target in one gated action (Phase 2)
          485cfb6 docs: Phase 2 handoff, git-verified state and open decisions
          fb1cee0 feat(targets): composite preauth and concurrency division for multi-tool launch
branch    feat/phase2-multi-tool-launch   (4 commits ahead of origin/main, NOT pushed)
dirty     blueprints/campaigns.py  blueprints/targets.py  shared/compute_campaigns.py
          shared/idempotency.py  shared/target_launch.py  templates/runs/new.html
          templates/targets/detail.html  templates/targets/launch.html
          docs/audit-...-open-items.md  tests/test_compute_campaigns.py
          tests/test_compute_campaign_routes.py  tests/test_idempotency.py
          tests/test_target_multi_launch_routes.py
new       tests/money_display_guard.py
```

## Rounds 3 to 5 (committed)

Seven agents, three rounds. Every round found defects in the previous round's
fixes, twice on a money path. Full write-ups: audit register addenda
2026-07-29d, e and f. Headlines: `preauth_message` named a required top-up BELOW
the gate that had just refused the user; the launch route reported funded,
billing campaigns as "not started and nothing was charged" in two separate
cases, each inviting a relaunch that would fund a second full set;
`_store_response` was unscoped, so a losing sibling's 400 overwrote the winner's
cached 302.

## Round 6 (addendum 2026-07-29g)

Two agents, verdicts FIX FIRST and SUITE UNTRUSTWORTHY. The stalled disclosure
was nested inside the started one and went dark under the same fault that
strands a run; the launch template asserted "nothing was charged" under every
error; `money()` rounded the consent figure DOWN; `from_row` sat outside four
callers' `try`; three tests could not fail.

## Round 7 (addendum 2026-07-29h) -- and A48

One agent over the round-6 diff. Verdict **FIX FIRST**. It restored the tree
byte-identically and said so, which is what makes a mutation report readable.

1. **The round-6 money fix introduced a money regression.** Ceiling each row and
   ceiling the exact total are different numbers, so the itemised column stopped
   adding up to its own headline: rows of $2.02 + $5.03 under a printed Total of
   $7.04, under a checkbox reading "the amount above will be held". 50 of 52
   cohorts affected, against 18 of 52 and unbiased before the change. Fixed with
   `display_total_usd`, which sums the DISPLAYED rows. Re-measured: 224 cohorts,
   0 mismatches, 0 totals below exact.
2. **Three safety mechanisms could each be reverted with the suite green**: the
   `funded_any` commit-point flag (now derived from `started` instead), three of
   the four `_campaign_or_none` conversions, and the JS drift guard (bypassable
   by `'$' + d.balance_usd`).
3. **A test that could not fail in its own direction**: the balance test used
   $573.6736, where floor and nearest agree.
4. **The fake disabled the code the new test exercised**: `_Query` had no `lt()`
   or `is_()`, and both `sweep_paused_campaigns` queries swallow an
   AttributeError into an empty list. Its `neq` also kept NULL rows where
   PostgREST drops them.
5. Five false or stale comment claims, including the `is_` null-spelling count
   wrong for the third round running (13, then 14, actually **15**).

**A48 is fixed** (Leo authorised it explicitly). `api_runs_estimate` now ships
`*_display` strings and `templates/runs/new.html` does no arithmetic. Checked
rather than assumed: that panel is NOT the H1 shape, because `BUDGET_BUFFER` is
1.15 so the budget is not per-chunk times sub-jobs, and nothing on it is
presented as the total of anything else on it. Its failed-estimate handler now
also clears state and disarms the button, which is what makes the raising
display helpers fail closed there.

## Round 8 (addendum 2026-07-29i)

One agent over the whole uncommitted stack. Verdict **FIX FIRST**, two HIGH.

1. **Round 7's money fix moved the same defect one screen over.** Making the
   panel total its rows fixed the column, but `preauth_message` still rounded
   the exact total up, so the refusal sentence said "$9.18 to start" while the
   panel on the same 400 said $9.19 above "the amount above will be held". 128
   of 240 refused cohorts printed two different holds. There is now ONE
   displayed hold per screen: `preauth_message` takes `required_display` and
   renders it verbatim, produced by `first_wave_display_at_pace()`. The same
   defect on the "Starting narrow would need $X" line is fixed with it.
   Re-measured over 254 cohorts, everything agrees.
2. **Round 7's fix for "an unpinnable safety mechanism" pinned nothing.**
   `nothing_charged=not started` is a constant at every reachable `_err`, and
   mutating it to `True` passed all 277 tests. The register's claim that
   "existing tests do pin" it was false and has been struck. Now guarded at the
   source by an AST test that forbids a literal.
3. **The drift guard had three ways past it**, all demonstrated green:
   arithmetic on a display string, a rounding call it did not know
   (`toPrecision`), and a computed key (`d['balance' + '_usd']`). The first two
   are closed; the third is not closable by pattern matching and the guard now
   says so rather than overstating its reach.
4. `tests/money_display_guard.py` was untracked. Plus six comment or fixture
   corrections, including two docstrings 39 lines apart contradicting each other
   because round 7 fixed one and left the other.

**A51 filed, not fixed:** consent on `/campaigns/new` is not invalidated when
the inputs change, so ticking at 24 designs, typing 5000 and submitting inside
the 250 ms debounce prices 5000 against consent for 24. The multi-tool page was
hardened against this; the deployed single-tool page was not. Same shape as A48:
needs Leo's word because it changes deployed behaviour.

## Verification state

- **Full suite after round 13: 1841 passed, 6 skipped, 0 failed** (527 s), run
  on a frozen tree. Round 13 is 2 net new (the slot-crossing guard on both
  consent pages) and no regression.
- **Round 13 returned the first "shipping code is sound" verdict since round 5:
  18 of 18 mutations against the diff's behaviour were killed.** Its two
  shipping findings (A58, A59) are both OUTSIDE the diff, on the deployed
  single-tool route. The commit gate is met.
- Previous: **round 12: 1839 passed, 6 skipped, 0 failed** (286 s), run
  on a frozen tree with no edit after the run started. Baseline before any of
  this work was 1704 / 6; rounds 3-5 took it to 1794 / 6; round 6 to 1815 / 6;
  round 7 to 1824 / 6; round 8 to 1828 / 6; round 9 plus A51 to 1833 / 6;
  round 10 to 1835 / 6; round 11 to 1838 / 6. Round 12 is 1 net new (the
  refusal test parametrized over both paces) and no regression. There is no
  pytest config, so always confirm the collected count is in the 1800s, not 10.
- **Round 12 remediation: 5 of 6 mutations killed.** The survivor is
  DELIBERATE and documented: `money(d.fw_display)` with the member assigned
  above still passes the display guard. A pattern matcher cannot establish a
  value's provenance, four rounds of trying produced four false docstrings, and
  the guard is now documented as a lint for the accidental mistake rather than
  a boundary. Do not reopen that arms race without reading
  `tests/money_display_guard.py`'s docstring first.
  - Two earlier round-8 runs are **not** the record: each had a production file
    edited after it started, so neither describes any tree that existed. Freeze
    first, then run. A suite figure is only about the tree it actually ran on.
    A third run was killed mid-flight for the same reason rather than left to
    finish and be quoted.
- **Round 9 remediation: 13 mutations, 13 killed.** **Round 10 remediation: 6
  mutations, 6 killed.** Harnesses at `scratchpad/mutate_r10.py`,
  `mutate_r11.py`, `mutate_round10_fixes.py`.
- **Line endings are PER FILE, not per repo.** `templates/runs/new.html` is
  **CRLF**; `templates/targets/launch.html` is **LF**. A harness that assumes
  one silently no-ops on the other and reports SURVIVED, which reads as a
  missing test and sends you hunting a hole that is not there. Two of round
  10's six mutations "survived" for that reason (plus one wrong test-file
  argument); all six died once the harness detected the ending per file.
  Always assert the file actually changed before drawing any conclusion.
- **Check WHICH test file guards the thing you mutated.** `runs/new.html`'s
  money guard lives in `tests/test_compute_campaign_routes.py`;
  `targets/launch.html`'s lives in `tests/test_target_multi_launch_routes.py`.
  Running the wrong one produces a false SURVIVED.
- **Round 7 is mutation-verified: 12 mutations, 11 killed. Round 8: 7 of 7.**
  Harnesses at `scratchpad/mutate_r8.py`, `mutate_r8b.py` and `mutate_r9.py`;
  they back files up by COPY, never `git checkout`, because the tree carries
  uncommitted work. The one survivor is understood and recorded below.
  - Reusing that harness: these files are **CRLF**. An anchor written with `\n`
    matches zero times and the case reports SKIP while looking like it ran.
- Both templates parse as Jinja and both pass
  `tests/money_display_guard.py`.

### Known limits, stated rather than left implied

- Blanking the body of the fake's `is_()` leaves the sweep test green. Only the
  method's ABSENCE is pinned, which is the failure mode the comment warns about;
  the filter's fidelity is not observable through that fixture.
- Neither page's `<script>` is executed by any test (**A47**). Every money guard
  is static: they prove the KNOWN routes to a wrong figure are closed, never
  that the right figure is printed.
  - Round 9 walked through the guard three ways (arithmetic on a display
    string); round 10 walked through the FIX three more (`money(x)` with `x`
    pre-computed, since the grammar allowed a bare `x` to accommodate
    `function money(x)`). Both sets are now closed: calls are matched with a
    lookbehind excluding the definition, and the argument must be a dotted or
    indexed path ending in `_display` with an optional string fallback.
  - **The count of open routes in that docstring has been wrong twice.** Treat
    it as a claim to re-test. One route is genuinely open: an expression that
    never names a 4dp field, never names a coercion, and never passes through
    `money()`.
- One displayed figure neither round could break: the refusal sentence, the
  panel total and the row sum agree across 254 cohorts, re-measured twice
  independently.

## Next, in order

1. **A non-author QC has not read the round-9 diff.** That is the standing gate
   before a commit. Eight rounds running, each review has found a real defect in
   the previous round's fixes: round 7 found a money regression in round 6's
   money fix, round 8 found the same defect again inside round 7's fix for it on
   the same screen, and round 9 found round 8's own carry-forward rule broken by
   round 8's own diff, in two files it never opened.
2. Push and PR are a **separate explicit ask** from Leo. Do not push. When you
   do commit, `tests/money_display_guard.py` must be in the commit. The branch
   is 1 behind `origin/main`, but only by PR #99's merge commit; the branch is
   built on top of that PR's feature commit, so there is no content divergence
   and the merge is trivial.
3. **A53 needs Leo's word**, same shape as A48 and A51 but on 13 deployed tool
   forms rather than 1. Now measured **here, not taken from the review**: 6 of
   13 print a cost BELOW the real estimate (af2, esmfold, esmfold2-design, iggm,
   mpnn, opendde). Round 9 reported 7 of 14; it counted the partial itself as a
   form and included proteina, which has no form template. This handoff
   previously said "either measure it or stop calling it a third instance". It
   is measured, and it is one.
4. No live production walk -- Leo chose "No live walk yet". Wallet is $24.50.
   Cheapest full-path walk: rfdiffusion + pxdesign at 1 design each holds $9.18.
   Report figures, do not spend.
5. **A54** (the narrow alternative gated on the exact figure, quoted as the
   ceiling) and **A52** (the estimate never sends `preset`) are both filed with
   measurements and deliberately not fixed. A52 is provably a no-op today:
   pricing is preset-invariant for both tools whose preset a user can pick.

## Still open, filed not fixed

**A41-A45** (idempotency TOCTOU, `blueprints/tools.py` 200-on-error,
`_store_response`'s own failure path, the single-tool route discarding
`fund_campaign`'s bool, a deliberate re-launch inside 60 s reading as a no-op).

**A46** campaign tick has no in-repo schedule. **A47** launch JS has no
behavioural coverage. **A49** both fakes assume PostgREST returns the updated
representation from `.update()`, which underpins every CAS in
`compute_campaigns` and is unverified against the live backend. **A50** the
confirming read narrows the fund ambiguity but cannot close it.

**A48 is RESOLVED** in this working tree (round 7). **A51** (stale consent on
the deployed `/campaigns/new`) is filed and needs Leo's word.

**A40, remeasured: 26 of the 32 test files that reference `create_app` have no
`isolate_supabase`**, so they read production. Not fixed because the fixture
blanks env for a whole module and adding it to 26 files changes the environment
of several hundred unrelated tests.

## Rules still in force

- **Repo-root `.env` holds real production Supabase service-role credentials**
  and `app.py` calls `load_dotenv()` at import. Any test importing `app` and
  exercising a route reads and writes **production** unless it opts into
  `tests/conftest.py::isolate_supabase`. **Grep any test file for
  `isolate_supabase` before running it.** Never write an ad-hoc script that
  calls `create_app()`.
- Pure planner functions (`divide_concurrency`, `plan_multi_launch`) and the
  display helpers can be probed safely by blanking the four `SUPABASE_*` vars
  inline: a blank URL or key makes `shared/supabase_client.py` return `None`.
- **Commit != push.**
- **Comments are claims.** Six rounds running, the top defect class. A number in
  a comment is a claim too: the `is_` null-spelling count has now been wrong
  three times (13, 14, 15).
- **A rounding rule is not local, and its blast radius is every surface that
  prints the figure.** Round 6 rounded one number and broke the column beside
  it. Round 7 fixed the column and broke the refusal sentence on the same
  screen. Enumerate every place the number appears before changing how it is
  displayed.
- **"A test pins this" is itself a claim, and it needs a mutation, not a
  reading.** Round 7 wrote "which existing tests do pin" into the register
  without running the thirty-second mutation that disproved it.
- **Prefer a source guard to a comment when a property is unreachable at
  runtime.** If no behavioural test can distinguish the right expression from a
  literal, say so and guard the shape, rather than swapping one unpinnable
  expression for another and calling it fixed.
- **A guard is a claim about which exceptions can reach it.** Before catching,
  enumerate what actually throws.
- **A fake that omits a method fails silently in the safe-looking direction.**
  This bit again in round 7, on the very test written to close round 6's gap.
  A fake's filter semantics decide what the code under test can even be handed:
  PostgREST `neq` DROPS a NULL row, Python `!=` keeps it.
- **A boolean from a function that swallows exceptions is three-valued.** Any
  user-facing claim built on one -- above all "you were not charged" -- must
  confirm independently or not make the claim.
- **A bare `except` at the call site erases a guard's signature.** Assert the
  log.
- **Patching out the function that computes the number under test makes the
  number a fixture.**
- **Prefer a derivation the tests already pin to a flag they cannot reach.**
  `funded_any` was correct and unpinnable; `started` is the same answer and is
  covered.
- **Mutate your own fix, not just the code it touched.** Two of round 7's new
  tests could not fail when first written, including one written specifically to
  close a "this test cannot fail" finding.
- No emojis. No em dashes, en dashes, or connector hyphens in prose.
