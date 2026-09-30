# Runtime reconciliation: what we start vs what the container is stopped at

2026-09-30. Written for the paid runs that failed in September. The brief calls
it six runs and names five by job id; of those five, **three** died on the
pipeline's own wall-clock (`c43329f3`, `dd7eaf99`, `b5707a1d`) and were refunded
in full, which is the class this document and the gate are about. The other two
are different mechanisms, listed in section 1 and not addressed here: a
no-progress timeout (refunded) and a safety kill that absorbed $3.63. Every
number below names the file, job row or
document it came from. Nothing here was measured on a GPU in this session — no
GPU job was launched.

## 1. Source rows

The failures, as reported in the hub-lead QA brief for this task
(`FAILED-RUNS-2026-09-30`, an out-of-repo report). Reproduced here because the
code comments added by this change cite them and needed a resolvable source.
None of these five rows can be re-derived from anything in this repository --
treat every figure in this table as the brief's, not as verified here.

| job | tool | outcome | wall | billed |
|---|---|---|---|---|
| `cd7150c1` | bindcraft pilot | `no_progress_timeout` | 5015 GPU-s | refunded |
| `763247f5` | bindcraft pilot | `safety_kill` | 5012 s | $3.63 absorbed |
| `c43329f3` | bindcraft pilot, 3 designs | timed out | 14400 s | refunded |
| `dd7eaf99` | boltzgen pilot | timed out | 6600 s | refunded |
| `b5707a1d` | boltzgen pilot | timed out | 6600 s | refunded |
| — | (bindcraft total $45.11, boltzgen total $31.46 across the set) | | | |

Runtime anchors used throughout, both from `docs/VALIDATION-LOG.md`:

* bindcraft job `1c4d5803`: 1170 GPU-s = 19.5 min, 2 trajectories, 4ZQK chain A
  (115 aa).
* boltzgen job `758c45e5` (2026-05-28): 4944 GPU-s = 82.4 min, 4ZQK chain A
  (115 aa), with the wrapper's fixed 200-design pool.

One measured run per tool, at one target size each. That is the whole evidence
base and it is why section 8 exists.

## 2. The reconciliation

Five numbers were supposed to agree per (tool, preset). They did not, and three
of the five turn out not to bound anything.

| | bindcraft pilot | bindcraft full | boltzgen pilot | boltzgen full |
|---|---|---|---|---|
| `gpu/modal_client.py` PRESET_CAPS | 7200 | 14400 | 3600 | 7200 |
| Modal `@app.function` timeout | 82800 | 82800 | 82800 | 82800 |
| **pipeline subprocess timeout** | **14400** | **14400** | **6600** | **6600** |
| wallet point estimate (spec) | 3600 GPU-s | 3600 | 5000 | 5000 |
| wallet hard cap (base / absolute) | $8 / $500 | | $10 / $300 | |
| envelope `runtime_ceiling_s` (new) | 14400 | 14400 | 6600 | 6600 |

* **PRESET_CAPS binds nothing.** It is never placed in the Modal payload. Its
  only value-carrying reader on the request path is
  `shared/compute_campaigns.py::_campaign_container_seconds`; offline,
  `scripts/calibration/poll_results.py` reads it for a SLOW_SUCCESS threshold.
  `gpu/modal_client.py` tests it against zero and echoes it in `SubmitResult`
  as `gpu_seconds_cap`, which no non-test consumer reads. boltzgen's 3600
  sits *below* the 4944 s run that succeeded, so if it had bound anything that
  run would have died.
* **The Modal function timeout binds nothing either** at 23 h
  (`infrastructure/modal/{bindcraft,boltzgen}_app.py::_MAX_SESSION_S = 82800`).
* **What actually stops a run** is a hardcoded `subprocess` timeout in
  llm-proteinDesigner (`origin/master` = `8632f32`; cited by symbol because the
  line number differs between `master` and `origin/master`):
  * `docker/bindcraft/run_pipeline.py`, the
    `run_command(cmd, timeout=14400, cwd=BINDCRAFT_DIR)` call inside the
    BindCraft heartbeat block.
  * `docker/boltzgen/run_pipeline.py`, `boltzgen_timeout = max(6600, 7200 -
    1800)` = 6600, passed to `run_command` for the `boltzgen` CLI.
* **`TOTAL_BUDGET_HOURS` is advisory.** Both Modal apps set it into the
  container environment (`bindcraft_app.py:94`, `boltzgen_app.py:64`) and
  neither `run_pipeline.py` reads it — `git grep TOTAL_BUDGET_HOURS` over those
  two files returns nothing. (Case-insensitive `budget` does hit real code in the
  boltzgen one; that is its `--budget` design count, not the session budget.) So the campaign layer's per-chunk budget does not change the
  timeout the chunk is killed at.

### What a timeout costs us

Neither of those two call sites catches `subprocess.TimeoutExpired`
(bindcraft's has no handler; boltzgen's has `except RuntimeError`, which does
not match). So the exception reaches the wrapper's catch-all, which posts a
FAILED webhook with no error bucket;
`shared/jobs.py::classify_terminal_state` buckets a bucket-less failure as
`unclassified`; `unclassified` refunds the hold in full. The customer pays
nothing, every Accepted design written so far is discarded, and Ranomics
absorbs the entire GPU bill for the run.

The preview tiers do salvage partial output, because their calls *are* wrapped:
bindcraft smoke 2400 s / mini_pilot 6000 s (`SMOKE_SUBPROCESS_TIMEOUT_S`,
`MINI_PILOT_SUBPROCESS_TIMEOUT_S`), boltzgen smoke 1200 s / mini_pilot 1800 s.
Neither tier is user-facing.

## 3. Why `c43329f3` ran to 14400 s on a preset capped at 7200

Because the 7200 never left the hub. PRESET_CAPS is read by the campaign
chunker and by a non-zero check in `submit`; it is absent from the Modal
payload. The pilot path in the container runs the same
`run_command(..., timeout=14400)` as `full`, so pilot and full are the same
wall-clock bound: 4 hours. `c43329f3` was estimated at 74.5 min and was killed
at 14400 s — **at least 3.2x over its estimate**. It was killed, not
finished, so 240 / 74.5 is a LOWER BOUND on the miss; the true runtime is
unknown and unknowable from this row. That ratio is the single most important
number in this report, and section 8 is about it.

`cd7150c1` and `763247f5` stopped earlier, at ~5012–5015 s, and for two
different reasons: the brief buckets `763247f5` as `safety_kill`, the mid-run
2.0x cost kill — which was **removed** in `3818b4a4` (2026-07-07, #62), leaving
only a 1.5x warning email — and `cd7150c1` as `no_progress_timeout`, a
liveness check, not a cost one. Run today, `763247f5` would go to 14400 s and be
refunded like `c43329f3`; `cd7150c1` would still stop when it stalls. The September figures therefore *understate* the
loss per incident.

## 4. What this change does

One gate, at the one function both the tool form and both campaign money routes
reach: `shared/pdb_preflight.py::_check_size_envelope`.

* `SizeEnvelope` gained `runtime_ceiling_s` (the pipeline timeout) and
  `runtime_fixed_designs` (a pinned pool, for tools that ignore the form's
  count).
* boltzgen's runtime curve was re-anchored on job `758c45e5`: base 86.0 min at
  the 120 aa anchor, baseline 200 designs, alpha 1.0. The 600.0 it replaces was
  "~5–10 min/design x 100" from nowhere and predicted 1150 min for that same
  82.4 min run — 14x over.
* A submission whose estimate exceeds the tool's `runtime_ceiling_s` is
  **refused**, with copy that names what would fit: a smaller target
  (`largest_target_aa_within_ceiling`) for a pinned-pool tool, a smaller count
  (`max_designs_within_ceiling`) otherwise.
* `_BINDCRAFT_CAMPAIGN_CONTAINER_S` 36000 → 14400, so a chunk is planned
  against the timeout that kills it. bindcraft chunks drop from 16 designs to 6.
* The pinned-pool override lives inside `runtime_estimate_min`, not in each
  caller, because the form panel and the result page reached it by different
  routes and printed 5 min and 82 min for the same boltzgen run.

Four defects in the first cut of the gate itself, fixed before this shipped.
(They were raised by the `review-code` pass on the delta -- provenance, stated
for the record and not verifiable from the tree.) Each has a test that fails if
it comes back:

* `POST /targets/<id>/launch` — the other campaign money route — called
  `size_error` without a design count, so the ceiling never fired there for a
  per-design tool. A 400 aa bindcraft launch was refused by `POST /campaigns`
  and funded by this one.
* Both campaign gates judged `plan.chunk_size`, a per-tool constant
  `plan_chunks` never clamps to the request. A 1-design bindcraft campaign
  (chunk 6) was refused although its only child runs one design, and the
  refusal named a smaller count that changes nothing — no input cleared it.
  The gate now reads `designs_for_chunk(0)`, the largest count a child runs.
* A target over the residue cap AND over the ceiling got the cap message with
  its "narrow to at most N residues" sentence stripped, because the early
  return fired on the ceiling flag while the message came from the cap branch.
  For a pinned-pool tool that needed no caller to pass a count, so every
  boltzgen size refusal on both campaign routes was advice-free.
* The full preflight's fix line quoted the residue cap, which is not the
  binding limit above the ceiling: a 500 aa boltzgen upload read "Targets up to
  about 153 residues fit" and "keep it at or under 600 residues" in the same
  panel. It now takes whichever limit is smaller.

The server-rendered preflight panel also skipped its size-envelope line for a
ceiling refusal (the condition listed the cap flags only), so the one refusal
that is about runtime was the only one printing no runtime; and that line's
"advisory only, long runs are supported" now says the opposite when the run is
being refused for it.

`tests/test_runtime_ceiling.py` fails if the *refusal figures* regress: every
residue count a refusal quotes must itself be admitted, the panel must post the
design count, and a revived runtime figure must carry its own basis. Its
docstring states what it does **not** claim: that a run which passes the gate
finishes. Two things in this section are **not** covered by a test: the panel
template's `over_runtime_ceiling` branch is only checked for the flag name
appearing in the template, not by rendering it, and the "past the limit one GPU
run is stopped at" versus "advisory only" wording has no test at all.

Consequence, accepted deliberately: **boltzgen is now small-target-only**,
~153 aa at alpha 1.0. Its 360 aa soft warn is unreachable. Both September
boltzgen losses (421 aa) would now be refused at the form. Any alpha at or
above ~0.20 still refuses 421 aa (solved numerically against the 6600 s
ceiling at the pinned 200-design pool), so this conclusion does not depend on the
unmeasured exponent.

## 5. Leo's question: a 100-design campaign

### bindcraft, 100 designs

From `plan_chunks("bindcraft", 100, "pilot")`:

* **17 chunks of 6 designs**, one A100-80GB container each, 4 in flight
  (`launch_concurrency_for`).
* Per chunk: estimate $18.87, hold $24.00, hard cap $24.00 (the $8 base scaled
  by 6/2). Campaign budget $368.99; first-wave gate $96.00.
* Each chunk's container is killed at 14400 s. Expected runtime for a 6-design
  chunk, from the curve: **56 min at 115 aa**, 84 min at 150 aa, **237 min at
  300 aa** (99% of the ceiling), 394 min at 421 aa (refused by the new gate;
  the gate refuses bindcraft above **302 aa at this 6-design chunk size**; the
  396 aa that `largest_target_aa_within_ceiling` reports is the boundary at the
  envelope's 4-design baseline, not at a chunk).
* Raw GPU cost of one chunk that runs the full 14400 s: 14400 x $0.001028 =
  **$14.80**. At the 1.70 markup that would bill $25.17, above the $24 cap, so
  even a *successful* wall-clock chunk absorbs ~$1.17.
* **When a chunk times out it is refunded in full and bills nothing** (section
  2). Chunks are separate jobs with separate holds, so **chunks that finished
  do still bill** — a campaign can end up half paid for.

**Worst case: 17 chunks x $14.80 = $251.65 of GPU absorbed, customer charged
$0.** That is not hypothetical arithmetic on an impossible input: a ~250 aa
target estimates 180 min for a 6-design chunk (inside the 240-minute ceiling,
so it passes the gate) and, at the 3.2x lower-bound miss `c43329f3`
establishes, would take at least ~9.6 h and be killed at 4 h. Every
chunk of that campaign fails the same way, because they all share the target.

So the honest answer to "will it time out at 100 designs": **not because of the
design count** — the count is chunked into 6s that fit — **but yes, all 17
chunks together, if the target is large enough that the estimate is optimistic
by the factor we have already observed once.** $251.65 is the exposure, per
campaign, and the new gate does not remove it. What removes it is section 8's
first item: catching the timeout in the pipeline and returning what the run
already produced, so a kill bills for real designs instead of refunding to zero.

### boltzgen, 100 designs

* **2 chunks of 50** (`BOLTZGEN_DESIGNS_PER_JOB`), one A100-40GB each.
* Per chunk: estimate $6.07, hold $9.10, cap $10.00. Campaign budget $13.96.
* Each container is killed at 6600 s = 110 min. Expected runtime is **82.4 min
  at 115 aa** (75% of the ceiling) and 107.5 min at 150 aa — it does not fit
  above ~153 aa, and above that the new gate refuses the launch.
* Raw GPU cost of a chunk that runs to the wall: 6600 x $0.000714 = **$4.71**.
* **Worst case: 2 x $4.71 = $9.42 absorbed.**

boltzgen's exposure is small, and for a structural reason worth stating: one
container per chunk, a low rate card, and a 110 min wall. Its danger was never
the money per run, it was that 75% of the ceiling is spent on the *measured*
case, leaving no headroom at all for a target only slightly larger.

### The `_DESIGN_PARAM_KEY` = `"budget"` question

Real, and worth fixing, but not for loss. `_dispatch_chunk` sends boltzgen's
count under `budget`, while `wallet_estimates.TOOL_SPECS["boltzgen"]` has
`scaling_param="num_designs"`. So `_effective_scaling_value` finds nothing,
falls back to the baseline of 2, and the quote does not scale:
`estimated_cost_for_tool` returns **$6.0690 for `budget=50` and $6.0690 for
`budget=100`**, with the hard cap stuck at the $10 base.

**At 100 designs it does not matter, and the flat figure is the right one.**
boltzgen is a fixed-container tool: one chunk is one container that folds a
pinned 200-design pool (`build_payload`), bounded at 6600 s = $4.71 raw = $8.01
marked up, under the $10 cap whatever the count says. The count does not move
the cost, so a quote that does not move with the count is correct.

What is actually wrong is the other half of the mismatch:
`scaling_param="num_designs"` on a tool that does not scale per design. Feed it
the count it asks for and the quote is absurd — measured with the repo venv:

| params | `estimated_cost_for_tool` | `compute_hard_cap` |
|---|---|---|
| `{"budget": 50}` (what ships) | $6.0690 | $10.00 |
| `{"budget": 100}` | $6.0690 | $10.00 |
| `{"num_designs": 50}` | **$151.7250** | $250.00 |
| `{"num_designs": 100}` | **$300.0000** (the absolute cap) | $300.00 |

So "aligning the keys" is the wrong repair: it would quote $151.72 for a chunk
that costs $4.71 of GPU. The right repair is `scaling_param=None`, as `opendde`
already has, after which either key gives the flat figure and the disagreement
stops being load-bearing. Not done here — out of this change's scope and it
touches every boltzgen quote. It would become a real loss risk only if boltzgen
ever fanned out one container per design.

## 6. The other tools

Only bindcraft and boltzgen declare a `runtime_ceiling_s`, because they are the
only two whose pipeline timeout I could pin to a constant. The rest are
unreconciled and the gate is inert for them — stated plainly rather than
implied by absence:

* **rfantibody** still plans chunks against `_RFANTIBODY_CAMPAIGN_CONTAINER_S =
  36000` (10 h), the same 23 h-Modal reasoning that was wrong for bindcraft.
  Left alone deliberately: out of this change's scope, and unlike bindcraft it
  has no failed-run row behind it. It is the next tool to check.
* **pxdesign** derives its subprocess timeout from a passed-in `timeout_s`
  rather than a constant, so there is no single number to reconcile against
  without tracing the caller.
* rfdiffusion, proteina, iggm: no ceiling declared, not examined here.

## 7. Follow-ups in llm-proteinDesigner (not this repo)

1. **Catch `TimeoutExpired` at both pilot call sites and return the partial
   result.** This is the fix with the money behind it: it converts a $251.65
   total loss into a billed run with fewer designs. The preview tiers already
   do exactly this, so the pattern exists in both files.
2. **Read `TOTAL_BUDGET_HOURS`** and use it as the subprocess timeout, so the
   campaign layer's per-chunk budget is real rather than advisory.
3. Consider raising boltzgen's 6600 s. It leaves 25% headroom over the only run
   we have measured, which is what makes the tool small-target-only.

## 8. What is still unmeasured, and what it would cost to measure

Both alphas rest on one target size each, and they are now load-bearing — the
gate refuses on them.

* **bindcraft alpha (1.5).** The >=3.2x miss on `c43329f3` says the curve is
  optimistic somewhere above 115 aa, and a single second size would say where.
  One 2-trajectory bindcraft run at ~300 aa: ~4600 GPU-s on A100-80GB at
  $0.001028/s = **~$4.73 of GPU**. This is the highest-value measurement in the
  report: it is what decides whether the 17-chunk exposure above is real at
  250 aa or only at 400.
* **boltzgen alpha (1.0).** One pilot at ~300 aa would either confirm that
  boltzgen is small-target-only or reopen 155–600 aa. Bounded by its own
  timeout at 6600 s x $0.000714 = **~$4.71 of GPU**, and it may well hit that
  wall, which is itself the answer.

Neither was run. Both need Leo's per-job approval.

## 9. Also found, not fixed

`shared/tool_chooser.py` has no target-size dimension at all — it recommends by
task. `recommend(have, shape, chemistry)` (`shared/tool_chooser.py:512`) takes no
size argument, and no `.size.` field of `TOOL_RULES` is read anywhere in the
file, so the chooser cannot see either cap. So it can recommend boltzgen for a 400 aa target that boltzgen's own
preflight now refuses on runtime. The user gets a recommendation and then a
refusal. Worth its own change; it needs a decision about whether the chooser
should read `TOOL_RULES` size limits, which is more than a one-line fix.
