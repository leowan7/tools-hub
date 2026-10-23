# QC findings — multi-chain finish, 2026-08-07

Three independent QC agents, none of which wrote the code, each in its own
isolated worktree. Full reports (transcripts) were written to the session
scratchpad; this file is the durable register. **Nothing here is fixed unless
it says FIXED.**

Reviewed: tools-hub `47092e5` (the stale-base snapshot; the rebased branch
`feat/multichain-ui-finish-v2` carries the same UI change) and
llm-proteinDesigner `46527d5`.

---

## A. Live on trunk, NOT caused by this work — money paths

These are the most valuable things QC found. All confirmed by execution.

- **A1 — BLOCKER. A bare hotspot on a target whose chains have different
  numbering burns a GPU.** `_check_hotspots` range-checks a bare int against
  the UNION of named chains; `tools/base.py:108` attributes it to the FIRST
  chain and rewrites it as `"A520"`. On a Fab (H/L) or any heterocomplex they
  disagree. Executed: A=1..40, B=500..539, target `"A,B"`, typed `520` →
  panel READY → adapter emits `["A520"]` → boltzgen submit gate returns
  `ready, ok=True` **with `dropped:['A520']`** (`hotspots_required=False`, so a
  drop does not block) → payload ships the token → container raises at
  `docker/boltzgen/run_pipeline.py:1083`. **A100 starts, wallet hold is taken,
  job dies.**
- **A2 — MAJOR. The preflight panel and the submit gate check different
  values.** Panel at `blueprints/tools.py:810` runs pre-validate, submit at
  `:1272` post-validate. Same input: panel says READY, submit refuses with
  "backbone is incomplete" — false; the residue exists and is complete.
- **A3 — MAJOR. Every multi-chain job's Boltz-2 refold 500s.**
  `blueprints/jobs.py:405/437` does `[int(x) for x in raw_hotspots]` and a
  multi-chain job carries `["A296","B264"]`. Uncaught `ValueError` out of the
  route; single-chain control passes. `tools/base.py:132` normalises **even
  bare-int input** to prefixed strings whenever >1 chain is named, so this is
  every multi-chain job, not an edge case. Its comment "SOURCE_TOOLS all
  persist hotspot_residues as list[int]" is now false.
- **A4 — MAJOR. `hotspot_picker.js:80` regex `/^([A-Za-z])?(-?\d+)$/` allows
  ONE chain character**, while all three Python parsers resolve chain ids
  longest-first for multi-character mmCIF ids. Executed: target `"A,AB"`,
  field `"AB12,A5"`, one click → `"A5,A7"`; `AB12` silently deleted.

## B. Introduced by this work — ALL CLOSED

**Status as of 2026-08-08: every B item is fixed on
`feat/multichain-ui-finish-v2` (head `0c722d4`, 23 commits off `origin/main`,
local and unpushed). Suite 3706 passed / 19 skipped / 0 failed.**

Five QC rounds plus a bounded verification pass, each by an agent that did not
write the code. The severity curve went blocker → major → guard strength →
one unasserted constant. Round 5 judged the work converged on substance —
"ships no false statement to any user", checked across seven rendered surfaces
and two MIME parts on four mail paths — and explicitly advised AGAINST further
rounds on two of its own findings, calling them "an arms race against English".
That advice was taken; the limitation is written down where the guard lives.

Three findings were REFUTED by execution rather than implemented, and all three
refutations held up under independent re-checking: bindcraft's refusal copy was
true on both submit paths (a second gate at `blueprints/tools.py:1606` that the
reviewer's trace missed); memoising the page sweep would have made a
side-effect test pass while checking nothing; and two suggested lock-out tests
would each have pinned a false claim.

The recurring defect, worth carrying forward: **a statement true of the code and
false of what the user sees**, and its cousin, **a justification that reads
correctly and was never executed**. The last item closed on this branch was the
second kind — a constant documented as a bound that bounded nothing.

### The original findings, for the record

- **B1 — BLOCKER — FIXED.** The reworded boltzgen ipTM legend asserted the
  number **is** `design_iptm`, "the binder-to-target interface". That is only
  true once llm-proteinDesigner#18 is merged AND DEPLOYED; until then the
  container emits the complex-wide value — which is the whole reason boltzgen
  is in `MULTICHAIN_IPTM_UNRELIABLE_TOOLS`. `candidate_table.html:433` stacks
  that legend into the `?` tooltip on the very column the banner disclaims, so
  a user saw both statements on one screen. A test **pinned the false
  version**. Legend reworded to be true today; the test now asserts the
  legend does NOT claim the unshipped fix, and ties itself to the frozenset so
  the two move together on deploy.
- **B2 — MAJOR — FIXED.** Two citations pointed at `docs/MULTI-CHAIN-TARGETS.md`,
  which does not exist in this repo (it is in the sibling). Both now qualified.
- **B3 — MAJOR — FIXED (`be49132`). Two of six banner call sites had no
  behavioural coverage.** `tests/test_multichain_iptm_notice.py:188-196` covers
  `runs/detail.html` and `targets/detail.html` by GREPPING the template source
  for `"multichain_iptm_notice("`. Breaking the argument while leaving the call
  text intact (`.get('target_chain')`→`.get('target_chains')`;
  `target.target_chain`→`target.chain`) leaves the whole 3632-test suite
  byte-identical. The banner would be permanently dead on the campaign and
  pooled-target pages with nothing to notice. **The code is correct** — QC
  drove all six through real Flask routes — but the tests cannot fail.
- **B4 — MAJOR — OPEN. bindcraft's new copy promises what bindcraft cannot
  do.** Its form now advertises `A296,B264` while `chainPrefixed` is
  deliberately held off, so (executed through this commit's own harness) typing
  `A296` then clicking once turns the field into `54` — the hotspot is silently
  destroyed. Its new target-chain help promises "several chains… every named
  chain reaches the model" while its gate returns
  `needs_fix, "Bindcraft can't run a multi-chain target yet"`. Fix: revert
  bindcraft's copy to the single-chain wording, and move it out of
  `MULTI_CHAIN_FORMS` in `tests/test_multichain_form_affordances.py` — that
  grouping is what let this pass.
- **B5 — MAJOR — OPEN.** `templates/targets/new.html`'s new copy tells users to
  type `A296, B264` while `blueprints/targets.py:_parse_residue_list` answers
  `"'A296' is not a residue number."`
- **B6 — MAJOR — FIXED (`be49132`). The node harness had two fidelity holes**,
  each proven
  by a surviving mutation. Deleting the entire NGL click chain-gate from
  `hotspot_picker.js:240-243` leaves 65 tests green, because
  `hotspot_picker_harness.cjs:151-155` **reimplements** the gate instead of
  registering the real handler. And pointing a form's `viewerId` at a
  nonexistent element also stays green, because the stub `getElementById`
  auto-creates any id, so a structurally dead picker looks healthy.
- **B7 — MAJOR — OPEN. A false justification.** The macro comment claims
  re-ordering "would change nothing about which designs the user can
  download". False on the campaign and target pages:
  `aggregate_campaign_candidates(..., limit=300)` sorts by ipTM then truncates,
  and both ZIP export limits are 300 — so ipTM order does decide what lands in
  the download. The decision not to re-sort may still stand; the stated reason
  does not.
- **B8 — MAJOR — OPEN. The mechanism sentence may be over-specific.** The
  banner says ipTM is "a maximum over residues". Seven pipeline files in this
  repo (`tools/af2/run_pipeline.py:202`, colabfold, esmfold, boltz2, mpnn,
  iggm, proteina) instead say it is "averaged over EVERY chain pair". Under a
  mean the conclusion still holds but the "~0.9" figure does not. Recommend
  rewording to something true under either construction.
- **B9 — MINOR — OPEN.** The banner says "re-fold the top candidates with
  Boltz-2 below" on `targets/detail.html`, which has no refold control.
- **B10 — MINOR — OPEN.** Two redundant tests to drop: trunk's
  `test_preflight_keeps_the_hotspots_validate_emits`
  (`tests/test_multichain_targets.py:388`) is strictly stronger than the edit
  this work made at `:293`, and most of `test_hotspot_chain_prefix_gates.py`
  maps onto existing trunk coverage. Only the asymmetric fixture and its
  guard-the-guard are unique and worth keeping.

- **B11 — MAJOR — OPEN, and NEW as of the #18 merge.** llm-proteinDesigner#18
  is now merged AND deployed: `origin/master` is `311c29f`, it carries
  `design_to_target_iptm` first in `IPTM_KEYS` (squash-merged, so both commits'
  content landed), and the Modal deploy for that SHA succeeded. The deployed
  container therefore now reports a binder-to-target value, which makes B1's
  legend wording and boltzgen's membership of
  `MULTICHAIN_IPTM_UNRELIABLE_TOOLS` stale — **for new runs only**. Designs
  that ran BEFORE the deploy still carry the old complex-wide number in the
  database, and a results page renders whatever the job stored, so a blanket
  removal silently drops a caveat those old runs still need. At least one
  multi-chain boltzgen run predates the deploy (the 4ZQK GPU verification).

- **A5 — MAJOR, live on trunk, money path. Found by QC round 3, confirmed by
  execution.** `/targets/<id>/launch` funds and drives a multi-chain run for
  ALL FOUR gated tools — bindcraft, rfdiffusion, pxdesign **and rfantibody** —
  with `target_chain="A,B"`. All four reach create → fund → drive. rfantibody
  is `multi_chain_supported=False`: the MODEL cannot do it, and it launches
  anyway. That route runs size checks only (`shared/targets.py::size_error`)
  and never calls `preflight_for_tool`, so the multi-chain container gate is
  bypassed entirely on the path users are steered toward for repeat runs. This
  is the Round-5 concern from PR #120 that was left unreproduced — it is now
  reproduced. Deliberately NOT fixed as part of the multi-chain UI change: it
  is a pre-existing hole on a paid path and deserves its own branch.
  **Sharpened by a second reviewer:** `blueprints/targets.py` contains ZERO
  references to `preflight_for_tool` — the only callers anywhere are
  `blueprints/tools.py:893`, `:1277` and `shared/pdb_intake.py:511`. And
  `_collect_launch_specs`'s own docstring (`blueprints/targets.py:205-209`)
  states the invariant it breaks: *"This, not the form UI, is the guard
  against paying for a mis-configured GPU run, so every check that the
  single-tool create route performs has to happen here too."* The function
  documents the rule and then omits the container gate.

## C. Scope decisions for Leo

- **C1.** Nine ipTM surfaces remain uncaveated, three of which leave the app:
  the completion **email** (`templates/email/job_complete.html:37`), the
  **public share card** (`blueprints/jobs.py:639-642`, "Top score ipTM 0.812"
  in an OG title), and every **CSV/FASTA/ZIP export**. Also `jobs_compare.html`
  (whose whole purpose is cross-run ipTM comparison), the live running-job
  table, `boltz2_results.html`, and `shared/metric_glossary.py:22-29` (which
  says "binder–target interface **specifically**"). Caveating these is real
  scope, not a tidy-up.
- **C2.** Should `blueprints/jobs.py` (A3) be fixed here? The banner
  recommends the Boltz-2 refold button, and that button 500s on exactly the
  jobs the banner appears on. Either fix the parse (small, uses trunk's own
  `split_hotspot`) or stop recommending it.

## D. Unresolved, worth one zero-GPU check

- **D1.** `tools/pxdesign/__init__.py:5` says its score comes from the **AF2
  monomer** model in initial-guess mode. AF2-monomer has no native `asym_id`,
  and AF2-IG harnesses commonly binarise it to binder=0/target=1 — which would
  mask out the target's own A↔B pairs and make the number binder-vs-target
  only. If so the banner is FALSE for pxdesign. Not resolvable from this repo;
  none of the four GPU containers is vendored here.
- **D2.** No real `aggregate_metrics_analyze.csv` exists in either repo's
  history (681 commits searched), so the BoltzGen column list is unbacked.
  Committing one real 3-row fixture would settle several questions at once.
- **D3.** proteina's `af2_iptm` column may read high on a multi-chain target;
  whether its `i_pae` (which IS its ranking key) uses the right chain pair is
  untraced. Reasoning recorded in `shared/score_legends.py`.

---

## What QC verified as correct

- All six banner call sites render correctly through real Flask routes, both
  directions; `agg.tools` really is a list of slug strings and
  `campaign.params['target_chain']` really survives sanitization on both
  creation paths.
- Input handling: 39 probed cases, no finding. `"AB"` → False is correct.
- **The Fc launch doc: 15 of 15 claims re-derived and correct** — 211/208
  residues, 234–444 / 237–444, glycans in C/D only, 419 total, P01857, zero
  gaps, A296=TYR and B264=VAL both with complete backbones, all four caps, and
  both runtime estimates reproduced exactly.
- Trunk's hotspot parser is a **strict superset** of the rewrite that was
  dropped: 170-cell battery, 160 identical, 10 differ, trunk wins all ten.
  Notably the dropped version never taught `_check_internal_gaps` the prefixed
  form, so with `chainPrefixed` on it would have silently disabled the
  gap-near-hotspot hard fail for pxdesign and boltzgen.
- The deleted macro is genuinely dead. The rfantibody carve-out is correct on
  both halves. `maxlength` 32 is safe everywhere (per-token cap 4, no
  whole-string cap, `target_chain text` in the DB).
- llm-proteinDesigner: `design_iptm` really is the binder→target interface,
  verified against upstream `compute_ptms`. Every README claim, the doc test
  roll-call, and the deploy mechanics all check out.
