# Scout `interface_competition` scored a constant 1.0 — impact and disposition

**Found:** 2026-08-18, incidentally, during anonymous rate-limiting QC.
That report (`docs/qc/anon-ratelimit-phase-1-round2.md`, lines 337, 342, 801) is
**not on `main`** — at the time of writing it exists only on the unmerged branch
`docs/anon-ratelimit-evidence` (`cb224d1`), so the path dangles at HEAD.
**Introduced:** `3ba4c5d` ("feat(phase-3): sortable candidate table, export routes,
campaigns flow, admin, Scout Blueprint").
**Fixed:** this commit.

## What was wrong

`scout/pipeline.py` "Step 8: Interface competition" imported
`scout.interfaces.detect_ppi_interfaces` inside a bare `try`. That name has never
existed anywhere in the repo's history — `git log --all -S"def detect_ppi_interfaces"`
returns nothing. The real function is `scout.interfaces.detect_interfaces`, which
`scout/routes.py:607` already called correctly with the same `(pdb_path, chain_id)`
signature.

The `except Exception` swallowed the `ImportError` on every call, so
`competition_score` kept its neutral default of `1.0` for every Scout feasibility
run between `3ba4c5d` and this fix.

Two downstream consequences:

* `interface_competition` carries 0.10 of the composite
  (`scout/feasibility.py:40`), so a tenth of every feasibility score was a free
  full-marks pass.
* `scout/feasibility.py:168` gates the "Epitope overlaps a natural
  protein-protein interface" risk factor on `< 0.50`, so that warning was
  unreachable and never shown to any user.

## Blast radius (measured, not estimated)

**Single-chain uploads were not affected.** With one chain there is no partner to
compete with, `detect_interfaces` correctly returns `[]`, and the score is `1.0` —
identical to the buggy constant. Verified directly.

**Multi-chain uploads were affected only where the selected epitope overlaps a
partner-chain interface.** Worst case (epitope entirely buried in an interface) the
score floors at 0.1, so the composite was inflated by at most
`0.10 x (1.0 - 0.1) = 0.09`. Measured on a synthetic two-chain case: composite
0.982 -> 0.892, and the risk factor went from absent to present.

Tier thresholds sit at 0.70 / 0.50 / 0.35, so a 0.09 inflation can cross **one**
tier boundary — a result reported as "Straightforward" could correctly be
"Moderate". It cannot skip two tiers.

The error was one-directional: affected results were always reported as *easier*
than they are, never harder.

## Disposition: no backfill is possible, and none is needed

Nothing to correct server-side:

* `feasibility_results.csv` lives in the job directory under `tmp/`, which
  `cleanup_old_jobs(max_age_seconds=3600)` reaps after **1 hour**. It is called
  from `upload()`, `fetch_pdb()` and `example()` — *not* from `analyze()` or any
  feasibility route — so a job directory is reaped by the next visitor's upload
  rather than by its own run.
* Feasibility scores, dimensions, and tiers are **never written to Supabase**.
  `scout_handoffs` (migration `0007`) stores only the staged PDB path, target
  chain, hotspot residues, and job/epitope ids — no score — and expires in 2 hours.
* `scout_runs` (migration `0002`, written by `scout/quota.py::record_scout_run`)
  **does** record one row per completed run: `user_id`, `created_at`,
  `result_hash`, and a free-form `metadata` blob. So a list of users who ran Scout
  in a window *is* derivable.

What is **not** recoverable is which of those runs were affected. `scout_runs`
stores no dimension scores, no tier, and no chain count, so there is no way to
tell a multi-chain run from a single-chain one (the latter was never wrong), and
no way to recompute what a given user was shown. A notification would therefore
have to go to everyone who ran Scout, telling most of them about an error that
did not affect them.

Given that, and the small one-tier ceiling on the error, the proportionate
response is this note rather than an outbound message. The only surviving
artefacts are CSVs users downloaded themselves, or screenshots.
If a user asks why a re-run of the same multi-chain target now scores lower than a
download from before 2026-08-18: the older number was inflated, the new one is
correct, and a drop of up to 0.09 with a newly-appearing PPI risk factor is the
expected shape of the correction.

## Guard

`tests/test_scout_interface_competition.py` (11 tests) pins both ends of the
dimension — a buried epitope must score below 1.0 and a clear one must score
exactly 1.0 — so a constant of *any* value fails. It also asserts that a detector
which raises fails the run rather than falling back to a neutral score, that
occlusion by several partners is unioned rather than maxed, that residue numbers
absent from the structure do not dilute the score, that a malformed `DBREF`
record does not break detection, and that `DIMENSION_WEIGHTS` still sums to 1.0.

Every regression test in the file was confirmed red against the code it guards
before the fix was trusted: 4 of the original 7 against pre-fix `pipeline.py`,
and each of the three defect tests below against its own unfixed state.

## Independent QC

`docs/qc/scout-interface-competition-round1.md` — verdict PASS WITH CORRECTIONS.
The reviewer reproduced every headline number independently. Three defects it
found are fixed here:

* **Removing the `try` did introduce one new failure path.**
  `scout/interfaces.py::_extract_chain_names` indexes `line[12]` after testing
  only the `DBREF ` prefix, so a truncated record raised `IndexError` straight
  through `run_feasibility_pipeline`. Chain names are cosmetic and must never
  fail detection; the line is now length-guarded.
* **Occlusion by several partners was maxed, not unioned.** Two partners each
  burying half an epitope occlude all of it, but the largest single partner
  scored it half-free — optimistic in the same direction as the original bug.
* **The buried fraction was scored over the requested residues, not the resolved
  ones.** A residue number absent from the structure diluted the denominator and
  inflated the score (measured 0.143 where 0.1 is correct). It is now scored over
  the same residue set as the other four dimensions.

Two lower-severity findings are recorded in the QC report and left open as
pre-existing: the SSE progress stream forwards raw `str(exc)` (which can include
filesystem paths), and `/feasibility/analyze` catches only `ValueError` and
`FileNotFoundError` so anything else becomes a 500.

**Correction to the premise this work started from:** the feasibility routes are
`@login_required` (`feasibility_page`, `feasibility_analyze`,
`feasibility_progress`). Anonymous visitors reach `/scout/analyze`, a different
path. This bug was never on an anonymous surface.
