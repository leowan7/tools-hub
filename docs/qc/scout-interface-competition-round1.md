# QC round 1 — Scout `interface_competition` was a constant 1.0

**Verdict: PASS WITH CORRECTIONS.**

The fix is correct, the bug is real, and every headline number the builder quoted
reproduces on my own machine — including the decisive one, that the new test file
goes red against the pre-fix code. Nothing here should block the fix landing.

Three things need correcting first, none of them in the fix itself: one demonstrated
new failure path the builder said did not exist (D1), one dangling documentation
citation that would ship broken (D2), and two false statements in the new doc's
disposition section (D3).

## What I actually reviewed

* **Under review:** the uncommitted working tree in
  `C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\jovial-hermann-c54d81`,
  on top of `48b4b71`. `git status` at the start and end of this review was identical:
  ` M scout/feasibility.py`, ` M scout/pipeline.py`, `?? docs/SCOUT-2026-08-18-interface-competition-impact.md`,
  `?? tests/test_scout_interface_competition.py`. I mutated nothing there except this report.
* **Pre-fix comparison:** a fresh detached worktree at `48b4b71` under my scratch dir,
  with only the new test file copied in. Removed afterwards.
* **Interpreter:** `C:\Users\lab\Documents\Claude_projects\tools-hub\venv\Scripts\python.exe`
  throughout. scipy 1.18.0, numpy 2.4.4, BioPython present; `freesasa` confirmed absent
  (`ModuleNotFoundError`).

## Claim-by-claim

| # | Claim | Result | Evidence I gathered |
|---|-------|--------|---------------------|
| 1 | `detect_ppi_interfaces` never existed in history | **Verified** | `git log --all -S"def detect_ppi_interfaces"` → empty. `git log --all -S"detect_ppi_interfaces"` → exactly two commits: `3ba4c5d` (2026-04-23, *added* the call; `3ba4c5d^` has zero occurrences) and `cb224d1` (a QC doc on an unmerged branch). Direct probe at `48b4b71`: `ImportError: cannot import name 'detect_ppi_interfaces' from 'scout.interfaces'`. The module's only public callable is `detect_interfaces`. Bug was live ~4 months. |
| 2 | `detect_interfaces` is a correct substitute; same shape; `routes.py:607` already calls it the same way | **Partially correct** | Signature match verified: `scout/routes.py:607` and `scout/pipeline.py:700` are byte-identical calls `detect_interfaces(pdb_path, chain_id)`, both passing a `Path`. The consumed key `contact_residues` is present and is `list[int]` of target-chain residue numbers, matching what the old code read, and both sides key on `residue.id[1]` with the same HETATM filter — the intersection is apples-to-apples. But "same return shape" is vacuous: the phantom function has no ground truth to compare against. Also unstated: the *argument* changed from a parsed `model` to a path, so the fix re-parses the structure from disk. |
| 3 | Removing the try/except surfaces failures without a new crash path for ordinary users | **Partially correct; two premises refuted** | See D1 and the "Judging claim 3" section below. Failures do surface (verified end-to-end). But (a) these routes are **not** anonymous — `scout/routes.py:953/961/1057` are all `@login_required`; (b) a new failure path **is** introduced and I reproduced it. The user-facing outcome is benign (an inline error banner, not a 500), so this is a correction, not a blocker. |
| 4 | Tie-break changed from first-with-overlap to max-overlap | **Verified, and correct** | Old code `break`-ed on the first interface with any overlap, iterating a list sorted by *total* contact count. New code takes the max. Strictly more conservative and better justified. It changed nothing observable, because the old loop never executed. Does it matter? Yes for multi-partner co-crystals — but see D4: `max` is still not the right aggregation. |
| 5 | No other scored dimension has the import-in-try → neutral-constant shape | **Verified, with additions** | My own sweep of `scout/`. `scout/accessibility.py` and `scout/glycan.py` import numpy/scipy at module level with no `try`; their `return 1.0` / `return 0.5` sites are domain early-outs (no CB coords, no sequons, nearest sequon beyond `max_dist`), not exception fallbacks. `scout/sasa.py::compute_rsa` imports freesasa lazily and *raises* rather than degrading. The two sub-claims hold: `scout/interfaces.py:148` (`except ImportError: return []`) is unreachable from this path because `scout/pipeline.py:27-28` imports `numpy` and `Bio.PDB` at module level, so a missing dep kills the import first; and the scipy fallback (`try:` at line 218, `except ImportError:` at 240) is a real brute-force computation, not a constant. Line numbers were cited as 145/218; actual 148/218. **Additions the builder missed** — see D-notes below `scout/interfaces.py:162`. |
| 6 | Single-chain never affected; max inflation 0.09; at most one tier boundary | **Verified, measured myself** | Single-chain synthetic, same residue numbers: `competition=1.0`, `composite=0.982` — identical to the buggy constant, because the partner loop has zero iterations. Two-chain, epitope fully buried: `competition=0.1`, `composite=0.892`, PPI risk factor present. Delta exactly **0.090** = `0.10 x 0.9`. Tier gaps are 0.20 (0.70→0.50) and 0.15 (0.50→0.35), both > 0.09, so at most one boundary can be crossed. All three sub-claims correct. |
| 7 | Nothing persists; no backfill possible | **Partially correct — conclusion holds, evidence is wrong in two places** | `cleanup_old_jobs(base_dir=Path("tmp"), max_age_seconds=3600)` at `scout/jobs.py:163` — 1 hour confirmed. Migration `0007_scout_handoffs.sql` stores no score — confirmed. **But** the doc missed `supabase/migrations/0002_scout_runs.sql`, and two of its supporting statements are false. See D3. The *conclusion* (no stored score → nothing to backfill; feasibility runs specifically are unlogged) survives. |
| 8 | New test goes red pre-fix, 4 of 7 | **Verified — my own run** | Fresh detached worktree at `48b4b71`, only the new test file copied in, repo venv: **4 failed, 3 passed in 4.17s**. Reds: `test_buried_epitope_scores_below_the_neutral_default` (`assert 1.0 < 1.0`), `test_competition_discriminates_between_the_two_epitopes` (`assert 1.0 < 1.0`), `test_ppi_risk_factor_is_reachable` (`'natural protein-protein interface' in 'None identified'`), `test_pipeline_does_not_swallow_a_broken_detector` (`DID NOT RAISE`). Every one carried the captured log line `WARNING scout.pipeline:pipeline.py:704 PPI interface detection failed; using default score` — direct proof the swallow fired on every call, not an inference. Same file in the builder's worktree: **7 passed**. |
| 9 | 5269 passed / 20 skipped / 0 failed; collection 5289 vs 5282 | **Verified — my own run** | `-m pytest -q` from the builder worktree root, no path argument, output redirected to a file (not piped): **`5269 passed, 20 skipped, 854 warnings in 557.79s`**, exit 0. Zero failures, so no node flake to re-run. Collection: **5289** in the builder worktree vs **5282** in the clean `48b4b71` worktree (measured after removing my copy of the new test file). Delta exactly 7. |

## Judging claim 3 — the highest-risk part

I traced this end to end and then exercised it through a Flask test client rather than
reasoning about it.

**The routes are not anonymous.** `/scout/feasibility` (`routes.py:953`),
`/scout/feasibility/analyze` (`961`) and `/scout/feasibility/progress` (`1057`) all carry
`@login_required`, which redirects to `/login`. Anonymous visitors reach `/scout/analyze`
(`@anon_rate_limit` + `@requires_scout_quota`), which is a different code path. The task
brief's framing of this as a free-tier anonymous surface is wrong for the changed code.

**The UI path degrades, it does not 500.** `templates/scout/feasibility.html:404` opens
the SSE stream first and only calls `fetch('/scout/feasibility/analyze')` after the stream
reports `done`. The SSE worker (`routes.py:1135`) catches bare `Exception` and yields a
`stage: error` event. Measured with the test client, patching `detect_interfaces` to raise:

```
[healthy]         SSE 200  data: {"stage":"done","pct":100,...}
[healthy]         JSON 200 {"composite_feasibility":0.892,...}
[detector raises] SSE 200  data: {"stage":"error","msg":"detector exploded: C:/secret/path/input.pdb"}
```

So the user sees the error banner, not a crash. The JSON route *does* 500 on a raise
(`routes.py:1010` catches only `(ValueError, FileNotFoundError)`; I have the traceback),
but the UI never reaches it in the failure case.

**The "analyze would have failed first" argument does not hold.**
`detect_interfaces` is already called unconditionally in `analyze()` at `routes.py:607`,
so it is tempting to conclude any raising input already fails upstream. It does not:
`templates/scout/feasibility.html:286/305` gives the feasibility page **its own** upload
and fetch-PDB, and it never calls `/scout/analyze`. Upload → pick chain → type residues →
run is a complete flow that reaches the changed code without passing through `analyze()`.

**Verdict on claim 3:** the trade is right — a visible error beats a silently wrong score
on a dimension carrying 10% of the composite — but the claim "does not introduce a new
crash path" is refuted, and D1 is the concrete instance.

## The new test file, on its own merits

It is a real guard, not a certifier.

* It drives the real `run_feasibility_pipeline` and the real `detect_interfaces` over a
  real BioPython parse of a real file. Only `compute_rsa` is stubbed.
* **The `compute_rsa` stub is legitimate.** I grepped every `rsa_map` use: inside
  `run_feasibility_pipeline` it is read at exactly one site, `scout/pipeline.py:664`, the
  `max_burial` normalisation for `surface_topology`. The other uses (lines 332/418/455)
  belong to the *other* pipeline, `run_pipeline`. Nothing on the interface-competition
  path touches it. Stubbing it pins `surface_topology` at 1.0, which none of the
  assertions depend on. `freesasa` is genuinely absent from the venv and is imported
  lazily inside `scout/sasa.py::compute_rsa`, so the module imports fine and only the call
  would fail. The stub hides nothing.
* **It cannot pass while the dimension is constant.** `test_buried...` requires `< 1.0`,
  `test_clear...` requires `== 1.0`, and `test_competition_discriminates` requires
  `buried < clear`. Any constant of any value fails at least one. That is the correct shape.
* `test_pipeline_does_not_swallow_a_broken_detector` works because the pipeline does
  `from scout.interfaces import detect_interfaces` inside the function body at call time,
  so the module-attribute patch takes effect on each invocation. It is the guard against
  the try/except returning.
* Fixture assertions are appropriately loose (`_BURIED ⊆ contacts`,
  `_CLEAR ∩ contacts == ∅`) rather than pinning the exact 9-22 contact range, so a
  geometry tweak will not produce a false red.
* **Gap:** nothing pins the aggregation across *multiple* overlapping partners — which is
  the one behaviour the fix actually changed (first → max). A third chain would cover it,
  and would also pin D4 either way.
* **Gap:** no test asserts the single-chain case scores 1.0, which is the "not affected"
  half of the blast-radius claim in the doc.

Worth recording: no pre-existing test referenced `interface_competition` at all. Nothing
was certifying the constant — but this dimension shipped for four months with zero coverage.

## Defects

### D1 — `_extract_chain_names` can raise on user-supplied PDB text, and that now fails the run (medium-low)

`scout/interfaces.py`, DBREF fallback branch:

```python
for line in text.splitlines():
    if line.startswith("DBREF "):
        chain_id = line[12].strip()
```

`startswith("DBREF ")` only guarantees 6 characters; `line[12]` needs 13. The branch runs
only when COMPND yielded no names, which is normal for hand-edited, stripped, or
tool-generated files. Everything else in `detect_interfaces` is guarded — imports
(`:148`), parsing (`:162`) — so this slice is the hole.

Reproduced: prepend the single line `DBREF  1ABC` to an otherwise valid two-chain
structure.

```
detect_interfaces        -> RAISED: IndexError string index out of range
run_feasibility_pipeline -> PIPELINE RAISED: IndexError string index out of range
```

**Failure scenario.** A signed-in user uploads such a file on `/scout/feasibility` (which
has its own upload and never calls `/scout/analyze`), selects a chain, types residue
numbers, and runs. Pre-fix: the ImportError was swallowed before this code was ever
reached, so they got a score. Post-fix: the SSE stream emits
`{"stage":"error","msg":"string index out of range"}` and the banner shows exactly that —
no indication of what is wrong or what to fix. If anything later makes
`/feasibility/analyze` the primary path, the same input is a 500.

**Fix.** One line in `scout/interfaces.py`: skip DBREF lines shorter than a full record,
or guard the slice. Partner chain names are cosmetic — a display label — and must never
be able to fail interface detection.

### D2 — the new doc's primary provenance citation is not in this tree (low)

`docs/SCOUT-2026-08-18-interface-competition-impact.md:3-4` cites
`docs/qc/anon-ratelimit-phase-1-round2.md §§337, 342, 801`. I pulled the blob and the
three line references are accurate. But that file exists only in commit `cb224d1`, on the
unmerged branch `docs/anon-ratelimit-evidence`:

```
git merge-base --is-ancestor cb224d1 HEAD  -> NO
git branch -a --contains cb224d1           -> docs/anon-ratelimit-evidence
git ls-tree -r HEAD --name-only | grep anon-ratelimit  -> (nothing)
```

Merging this change without that branch ships a dangling citation as the sole provenance
for "Found:". Land `docs/anon-ratelimit-evidence` first, or cite it as
`cb224d1:docs/qc/anon-ratelimit-phase-1-round2.md`.

### D3 — two false statements in the doc's disposition section (low)

**(a) "Scout keeps no record of who ran what" — false.**
`supabase/migrations/0002_scout_runs.sql` defines `public.scout_runs`, and
`scout/routes.py:776` → `scout/quota.py:237` inserts one row per completed analyse for
signed-in users with `metadata = {job_id, chain, uniprot_id}` plus `user_id` and
`created_at`, exposed through a rolling-30-day view. The doc's *conclusion* still holds —
no score is stored anywhere, and the feasibility routes carry neither
`@requires_scout_quota` nor a `record_scout_run` call, so feasibility runs specifically
are unlogged — but the sentence as written is wrong, and the evidence list should name
`0002` alongside `0007`.

**(b) "`cleanup_old_jobs` ... is called on every upload and analyse request" — false.**
It is called at `scout/routes.py:380`, `476`, `518` — `upload()`, `fetch_pdb()`,
`example()`. Not from `analyze()`, and not from any feasibility route.

### D4 — `max` over partners under-counts multi-partner occlusion (low, methodology)

`scout/pipeline.py:702-707` takes the largest single-partner overlap. If partner B buries
epitope residues 12-14 and partner C buries 15-17, the epitope is fully occluded but the
score is `1 - 3/6 = 0.5` rather than `0.1`. The union is the same one-line change and is
the more defensible aggregation. This is realistic on homodimers and multi-subunit
co-crystals, and it errs *optimistic* — the same direction as the bug being fixed.

### D5 — the overlap denominator is the requested residue count, not the resolved one (low, pre-existing, now live)

`competition_score = max(0.1, 1.0 - max_overlap / len(epitope_set))` divides by every
number the user typed, including numbers with no standard-AA residue in the chain.
`templates/scout/feasibility.html:376-388` is a free-text box, and
`run_feasibility_pipeline` raises only if *no* residue matches — a partial mismatch
passes silently. Measured: the buried 6-residue epitope scores `0.1`; adding a
nonexistent `9999` to the same list scores `0.143`. A selenomethionine does the same —
`MSE` is not in `scout/sasa.py::STANDARD_AA`, so it is dropped from `patch_residues` but
stays in `epitope_set`. `len(patch_residues)` is the correct denominator. The pre-fix code
had the same shape, so this is not a regression, but it only became observable with this fix.

### D6 — the SSE error event forwards `str(exc)` to the browser (informational)

`scout/routes.py:1135-1137`. My probe received
`data: {"stage":"error","msg":"detector exploded: C:/secret/path/input.pdb"}` in the
response body. Pre-existing — `run_feasibility_pipeline`'s own `FileNotFoundError` already
carried a path — but removing the try/except widens the set of exception types, and
therefore messages, that can reach a client.

### D7 — `/feasibility/analyze` catches only `(ValueError, FileNotFoundError)` (informational)

`scout/routes.py:1010-1012`. Anything else is an unhandled 500; confirmed by traceback.
Latent rather than live, because the UI calls this route only after the SSE reports
`done`. It becomes the live path the moment the SSE step is removed or bypassed.

### Residual instances of the same shape the builder did not list

`scout/interfaces.py:162` (`except Exception: ... return []` on parse failure) and the
`if target_chain_id not in chain_map: return []` immediately after are still silent routes
to `competition_score == 1.0`. Both are narrow — pipeline Step 1 already parsed the same
file, and with a *stricter* parser than `detect_interfaces` uses — but the new code
comment's claim that `detect_interfaces` returns `[]` only "for the legitimate ... cases"
is not quite true. Separately, `scout/scoring.py:416-428` degrades DSSP → phi/psi → `{}`
("everything is a loop"); not a feasibility dimension and the fallback is disclosed, but
it is the same family and worth a line in whatever register tracks these.

## Positives worth recording

* The `scout/feasibility.py` edit is docstring-only and *corrects* a stale docstring: it
  claimed six dimensions including `prior_precedent` at 0.15 with 0.20/0.20/0.20, whereas
  `DIMENSION_WEIGHTS` has had five at 0.25/0.25/0.25/0.15/0.10 for some time. Sum verified
  at 1.00. Drive-by, but correct and in scope.
* No `ZeroDivisionError` risk in the new arithmetic: an empty epitope raises `ValueError`
  earlier in the pipeline.
* The doc's measured numbers reproduce exactly — I got composite `0.982` → `0.892` on my
  own synthetic two-chain case without having seen theirs.

## Could not verify

* **CI behaviour on Linux with real `freesasa`.** Everything I ran was Windows with
  `freesasa` absent, either stubbed or not on the path. The interface-competition path does
  not touch freesasa, so the risk is low, but the green 5269 is a Windows result.
* **Whether any real user hit an inflated tier in production.** No telemetry stores the
  score — which is the finding itself, not a gap I can close.
* **Cost on large multi-chain structures.** `detect_interfaces` re-parses the file and
  builds two KDTrees per partner chain, and a feasibility view runs the whole pipeline
  *twice* (SSE worker, then the JSON route). `analyze()` already pays the same cost once
  per run, and the `max_burial` loop in the pipeline is heavier, so I expect this to be
  noise — but I did not measure it on a real structure with many chains.
