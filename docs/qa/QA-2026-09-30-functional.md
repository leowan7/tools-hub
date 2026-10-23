# Functional QA — tools.ranomics.com — 2026-09-30

Scope: functional output QA (does each tool produce the right results, tables, files and charges),
distinct from the button-level sweeps in `docs/qa/QA-2026-09-29-logged-in.md` / `-logged-out.md`,
which are not repeated here.

Method: Leo's signed-in session driven through Claude in Chrome; every page, table, CSV, FASTA and
download fetched same-origin from the live page. Repo read for expected behaviour
(adapter result code, Jinja result templates, `shared/score_legends.py`, `shared/exports.py`).

**Prod build at start:** `54638c415d3a4c3e37aeccf5cea4fed815b3454c` (`/health`)
**Prod build at end:** `2dffb144796e085e69a6b7a01643e43286a383f2` (`/health`)
The build changed mid-sweep. Findings below were observed under one build or the other; no finding
was re-tested across both unless stated.

**Phase 2 (paid runs): NOT STARTED.** No paid form was submitted. Proposal at the end, awaiting approval.

---

## 1. Per-tool audit (Phase 1 — existing succeeded jobs + free runs)

Checks: (1) run completed · (2) results table sane · (3) request honoured · (4) downloads honest ·
(5) viewer / scale-up / email / charge.

Job ids below were re-anchored by paging `/jobs?page=N`; an earlier id I had been carrying
(`d1cd23a8-…`) was wrong and everything anchored to it was discarded and re-done.

### Developability Scout (free, run live 2026-09-30)

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Submitted live on `/tools/developability`; results rendered. |
| 2 table | PASS | Composite score recomputed by hand from the displayed component scores and weights; matches. |
| 3 request | PASS | Input sequence length echoed in the result header. |
| 4 downloads | not exercised | No download offered on this tool's result page. |
| 5 charge | PASS | Free; wallet unchanged. |

### Library Planner (free, three runs live 2026-09-30)

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Three submissions, all rendered. |
| 2 table | **FAIL** | NGS budget is `3.00e+08` for requested coverage 50 %, 90 % and 99 % alike. See P1-1. |
| 3 request | PARTIAL | Combinatorics (library size, functional fraction, sortable ceiling) track the inputs correctly; coverage does not. |
| 4 downloads | n/a | None offered. |
| 5 charge | PASS | Free. |

### Epitope Scout — analysis (free, run live 2026-09-30)

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Job `abbf3add-8805-42ff-ad7c-ce2cdff659c3`, chain A, 5 epitopes. |
| 2 table | PASS | Feasibility 0.86 reproduced by hand from the five dimension scores and `scout/feasibility.py:34-40` weights. |
| 3 request | PASS | Requested chain A honoured; hotspots listed per epitope. |
| 4 downloads | PASS | Both CSVs parse; row counts match the on-page epitope and residue lists. |
| 5 handoff | **FAIL** | "Design binders" hands off to a design tool and errors out every time. See P0-1. |

### Epitope Scout → design-tool handoff

| Check | Result | Evidence |
|---|---|---|
| handoff | **FAIL** | POST `/scout/handoff/tool` from job `abbf3add-…` (epitope 3, chain A, hotspots 33,34,37,114,122) redirects to `…handoff_error=stage_failed` for **rfdiffusion, bindcraft and rfantibody**. Input PDB is present: `GET /scout/pdb/abbf3add-…` returns 200, 130,653 bytes. |

### iggm — job `0f6be32f-e971-41f7-b7ec-33fbf795addd`

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Succeeded, results table rendered. |
| 2 table | PASS | Columns populated, no constant column. |
| 3 request | PASS | Design count on the page matches the submitted `num_samples`. |
| 4 downloads | **FAIL** ×2 | CSV row 1 is `000_design_0.pdb` while page row 1 is `009_design_17.pdb` — CSV `rank` is the stored-list index, not the page's order (`shared/exports.py`). FASTA is the "no sequences" stub. See P1-2, P1-3. |
| 5 scale-up | PASS | Page offers "$7.29" for the scaled run; `/api/wallet/estimate?tool=iggm&num_samples=…` returns `$7.2828`. An earlier claimed mismatch here was my own error (wrong count key) and is withdrawn. |

### rfantibody — job `ef3f5bd3-e8eb-4ad8-a6c0-e1340a3602f2`

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Succeeded. |
| 2 table | PASS | 20 rows, values in range. |
| 3 request | **FAIL** | Preflight snapshot on the same page reads "Estimated runtime: ≈13.9 min for 4 designs" while the table and CSV both hold 20 designs. See P1-4. |
| 4 downloads | PARTIAL | CSV matches the page row-for-row. FASTA is the stub. |
| 5 viewer/price | PARTIAL | Viewer renders (see Withdrawn W-1). Scale-up offers "$281.41" for 100 designs (API agrees); API quotes `$80.40` for 20 designs, but this same 20-design run charged `$1.01`. See P1-5. |

### proteina — job `6b0c9b44-3d24-4361-9b82-4f4fd2c78d6f` (18 Jul 2026)

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Succeeded. |
| 2 table | **FAIL** ×2 | `af2_plddt` values 11.07–21.93 — this job predates `1a43f1f7` (2026-08-09), which fixed pLDDT being read from the loss column, so these are inverted. No caveat is shown; the only era notice that exists is rfdiffusion's. Separately `rf3_score` is blank in every row. See P0-2, P2-1. |
| 3 request | PASS | Design count matches. |
| 4 downloads | PARTIAL | CSV matches page. FASTA is the stub. |
| 5 | not re-measured | Job is 2.5 months old; no estimate/charge comparison attempted. |

### boltz2 — job `dc88afb7-7b2f-49fd-bf33-3a9602b71968` (23 Sep 2026)

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Succeeded — the only job in scope submitted on or after 2026-09-20. |
| 2 table | PASS | Scores populated and in range. |
| 3 request | PASS | Design count matches `n_designs_total`. |
| 4 downloads | PARTIAL | CSV matches page. FASTA is the stub. |
| 5 charge | **FAIL (small)** | Quoted `$0.22`, charged `$0.32`. See P1-6. |

### mpnn — most recent succeeded job

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | Page shows 2 designs with Score, Recovery and Sequence. |
| 2 table | PASS | Values present. |
| 4 downloads | **FAIL** | CSV body is empty: header `rank,pdb_key,source_rank`, zero rows, and none of the three displayed columns. FASTA is correct here (2 records). See P0-3. |

### af2 / colabfold — most recent succeeded jobs

| Check | Result | Evidence |
|---|---|---|
| 2 table | **FAIL** | `ipTM` column is `—` in every row on both tools. Counts render as `35.00` / `1.00` (a count shown to 2 dp). See P1-7, P2-2. |
| 4 downloads | PARTIAL | FASTA is the stub. |

### esmfold / esmfold2-design — most recent succeeded jobs

| Check | Result | Evidence |
|---|---|---|
| 2 table | **FAIL** | esmfold2 `iPTM_proxy` is `—` in every row. See P1-7. |
| 4 downloads | PARTIAL | FASTA is the stub. |

### bindcraft — most recent succeeded job

| Check | Result | Evidence |
|---|---|---|
| 2 table | PASS | Populated. |
| 4 downloads | PARTIAL | CSV matches the page row-for-row. FASTA is the stub. |

### boltzgen — most recent succeeded job

| Check | Result | Evidence |
|---|---|---|
| 2 table | PASS | "Not measured" verdicts render correctly rather than as a fake pass. |
| 4 downloads | PARTIAL | FASTA is the stub. |

### pxdesign `a3429923-4f76-4509-ae12-5ccd6cfa81a1`, opendde `4a3e3203-391e-49ee-a60f-9cfd471226f7`, rfdiffusion `01c3b3a6-605a-4ce4-86c0-8631e481a489`

| Check | Result | Evidence |
|---|---|---|
| 1 run | PASS | All three succeeded and render. |
| 2 table | PASS | No blank or constant column found. |
| 4 downloads | PARTIAL | FASTA is the stub on all three. |
| 5 | not measured | Estimate-vs-charge not compared; these jobs predate the current pricing surface. |

### proteina "validate" (free path)

| Check | Result | Evidence |
|---|---|---|
| empty state | PASS | Free validate shows its own empty state, not the paid zero-candidate copy. |

---

## 2. Findings

### P0 — customers see wrong results, money wrong, or the run breaks

**P0-1 — Scout "design binders" handoff is dead in production.**
Repro: run any Epitope Scout analysis → results → "Design binders" → pick rfdiffusion, bindcraft or
rfantibody → submit. Every one redirects to `/scout/...&handoff_error=stage_failed`.
Observed on job `abbf3add-8805-42ff-ad7c-ce2cdff659c3` (epitope 3, chain A, hotspots 33,34,37,114,122)
for all three tools. The staged input is not the problem: `GET /scout/pdb/abbf3add-…` returns 200 with
130,653 bytes. `scout/handoff.py:167-227` returns `None` — and therefore `stage_failed` at
`scout/routes.py:2122-2205` — on any of: unresolved user id, `stage_pdb` failure, missing service
client, or an empty insert into `scout_handoffs`. Which one fires in prod is **not** established from
the outside; it needs a server log. This is the advertised path from the free top-of-funnel tool into
every paid tool, and it is 0 % working.

**P0-2 — a proteina job shows inverted pLDDT with no warning.**
Job `6b0c9b44-3d24-4361-9b82-4f4fd2c78d6f` (18 Jul 2026) displays `af2_plddt` 11.07–21.93. Commit
`1a43f1f7` (2026-08-09) fixed `af2_plddt` reading the loss column, i.e. every pre-fix value is the
inverted quantity. A customer reading that page today sees good designs scored as garbage. The
score-era machinery exists (`templates/components/score_era_notice.html`) but is wired only to
rfdiffusion (`templates/runs/detail.html:179-183`, `templates/tools/rfdiffusion_results.html:4,16`)
and `RFDIFFUSION_SCORE_ERA_BOUNDARY` is the only boundary constant in `shared/score_legends.py`.
There is no proteina boundary at all.

**P0-3 — mpnn CSV export is empty and missing its columns.**
Repro: open the most recent succeeded mpnn job → Download CSV. The file is
`rank,pdb_key,source_rank` with zero data rows, while the page shows 2 designs with Score, Recovery
and Sequence. The export carries neither the rows nor the three columns the customer is looking at.
(The mpnn FASTA is fine — 2 records.)

### P1

**P1-1 — Library Planner ignores the requested NGS coverage.**
Repro: submit the planner three times with target coverage 50 %, 90 %, 99 %, everything else equal.
The "NGS budget" is `3.00e+08` all three times. Correct Poisson values for that library are
6.93e7 / 2.30e8 / 4.61e8. Cause: `tools/library_planner/ngs_depth.py:42-75` — `coverage_profile()`
computes `recommended = reads_for_coverage(library_size, 0.95)`, a hard-coded 95 %, while
`tools/library_planner/planner.py:125-137` writes the label from the *request*
(`int(inputs['target_coverage'] * 100)`). Label from the user, number from a constant.
`templates/library_planner_results.html:120-128` then prints the constant under prose promising
"the requested coverage".

**P1-2 — CSV row order does not match the page.**
Repro: iggm job `0f6be32f-e971-41f7-b7ec-33fbf795addd`. Page rank 1 is `009_design_17.pdb`; CSV rank 1
is `000_design_0.pdb`. The CSV `rank` column is the 1-based index of the stored list
(`shared/exports.py`), not the displayed sort. The "Download CSV" link carries no `?sort`, and adding
`?sort=score` does not reorder the file. Anyone who ranks candidates in the CSV picks a different
top design from the one the site showed them.

**P1-3 — the FASTA button is dead on every structure-only tool.**
Repro: any succeeded job on af2, colabfold, esmfold, boltz2, boltzgen, bindcraft, pxdesign, opendde,
iggm, rfantibody or proteina → FASTA → the file is
`# No sequences found in this job's output.` The link is rendered unconditionally at
`templates/components/candidate_table.html:435-438`; the stub body is
`blueprints/jobs.py:1499-1526`. A download button that always returns a comment should not be shown.

**P1-4 — rfantibody result page contradicts itself on design count.**
Job `ef3f5bd3-…`: the preflight snapshot says "Estimated runtime: ≈13.9 min for 4 designs"; the table
and the CSV both hold 20 designs.

**P1-5 — rfantibody estimate is ~80× the charge for the same run.**
`/api/wallet/estimate?tool=rfantibody&num_designs=20` returns `$80.40`. The same 20-design run on the
same target (`ef3f5bd3-…`) charged `$1.01`. The 100-design scale-up offer on the page ("$281.41")
agrees with the API, so page and API are consistent with each other and both are far from what the
work actually cost. Caveat: I called the estimate endpoint **without a target size**, so part of this
gap may be a default target rather than a pricing bug — see Unverified U-2.

**P1-6 — boltz2 charged more than it quoted.** `dc88afb7-…`: quoted `$0.22`, charged `$0.32` (+45 %).

**P1-7 — confidence columns are empty on three tools.** `ipTM` is `—` in every row on af2 and
colabfold; `iPTM_proxy` is `—` in every row on esmfold2-design. Either the value is not being
parsed or the column should not be offered.

### P2

**P2-1 — proteina `rf3_score` column is blank in every row** of job `6b0c9b44-…`.
**P2-2 — design counts render with two decimals** (`35.00`, `1.00`) on af2 / colabfold result pages.
**P2-3 — `/scout/handoff/tool` accepts a POST with no CSRF token,** and the exemption comment at
`app.py:289-320` ("Only these two blueprints self-manage CSRF posture") is false for scout:
`grep -rn "csrf" scout/*.py` returns nothing. Low severity while the route is broken anyway (P0-1),
but it is a state-changing POST on a signed-in session.

---

## 3. Withdrawn

**W-1 — "the 3D viewer never renders" is WRONG; withdrawn.** I had observed the viewer row staying
hidden with no canvas and Mol* console errors. The cause was my own harness: the automated tab is
backgrounded (`document.visibilityState === "hidden"`, verified live), the toggle defers viewer init
to `requestAnimationFrame` (`static/js/candidate_table.js:484-501`), and rAF never fires in a hidden
tab — so `initMolViewerFromUrl` was never called (`data-initialized` unset, no Mol* script tag
injected). Calling the init function directly on the same job and the same PDB URL renders a 996×420
canvas with no errors. **The viewer works in production.**

**W-2 — "iggm scale-up over-prices by $2.78" is WRONG; withdrawn.** I passed `num_designs`; iggm's
count key is `num_samples`, so the endpoint priced 1 unit. Page and API agree at ~$7.28.

## 4. Unverified

- **U-1** — root cause of `stage_failed` (P0-1). Four candidate causes in `scout/handoff.py:167-227`;
  distinguishing them needs a server log, which I have no access to.
- **U-2** — estimate-vs-actual ratios (P1-5, P1-6) were computed with estimate calls that omitted
  target size. The boltz2 gap was measured against a real job's own quote shown on its page, so it
  stands; the rfantibody ratio may shrink once a real target is priced.
- **U-3** — structure downloads (ZIP) were not opened and parsed for chain content on any tool. Check
  4 is therefore only half-covered: CSV and FASTA were checked everywhere, structures nowhere.
- **U-4** — "email says ready" was not checked on any tool; no run completed during the sweep.
- **U-5** — jobs older than 2026-09-20 (all tools except boltz2) may not reflect current code. Their
  table and export defects were observed on today's rendering of those jobs, so rendering/export
  findings hold; anything about the run itself may be stale.
- **U-6** — the build changed mid-sweep (`54638c41` → `2dffb144`). No finding was re-confirmed on
  both builds.

---

## 5. Phase 2 proposal — NOT RUN, awaiting approval

Only one tool (boltz2, `dc88afb7-…`, 23 Sep 2026) has a succeeded job on or after 2026-09-20. Every
other tool's most recent success predates the window, so strictly all thirteen qualify for a fresh
run. Under the **$25 hard cap** that is not affordable, so this list is prioritised by what Phase 1
actually flagged, and each price must be re-read from the site's own estimate at submit time before
anything is launched.

Candidates, in priority order:

1. **mpnn** — empty CSV (P0-3): confirm the export defect on a fresh job.
2. **af2** — empty `ipTM` (P1-7).
3. **colabfold** — empty `ipTM` (P1-7).
4. **esmfold2-design** — empty `iPTM_proxy` (P1-7).
5. **proteina** — confirm `rf3_score` and current-era pLDDT on a post-fix run (P0-2, P2-1).
6. **rfantibody** — estimate-vs-charge (P1-5), design-count contradiction (P1-4).

**I have not fetched the live estimate for these six yet**, so I am not putting numbers on them that
I cannot cite. On request I will pull each one's own on-page estimate for the smallest run on a
built-in example target, present the list with real prices and a total against the $25 cap, and wait
again before submitting anything.

---

## 6. Re-test after #388 / #389 (same day, prod build `53f7a60ffce680852fd81d95108a26a504373dd4`)

### P0-1 Scout handoff — **FIXED, verified live**

The original job `abbf3add-…` is gone ("Job not found or expired"; `/scout/feasibility?job_id=abbf3add-…`
refuses to auto-load), so this used one fresh free analysis: **Scout job `5ef02cb1-f495-41df-9521-ed2c0039a5bf`**,
built-in example 1HEW, chain A, quota 3 runs remaining before the run.
Epitope rank 1 = `epitope_id=3`, hotspots `33,34,37,114,122` — the same patch as the original test.

Submitting the real handoff form on `/scout/feasibility?job_id=5ef02cb1-…&epitope_id=3&chain=A`
(form's own `_csrf` token, one submit per tool) now lands on the prefilled tool form for all three:

| Tool | Result | Landed on |
|---|---|---|
| rfdiffusion | PASS | `/tools/rfdiffusion?handoff=b123442d-090c-4688-98a9-5abd4b60b952`, hotspots prefilled |
| bindcraft | PASS | `/tools/bindcraft?handoff=739479c4-ceac-4761-8290-7131dd2afc56`, hotspots prefilled |
| rfantibody | PASS | `/tools/rfantibody?handoff=821e4a14-a480-4117-b68d-094e95a9adbd`, hotspots prefilled |

No `handoff_error` on any of the three. **Stopped at the prefilled form; nothing submitted.**

### P2-3 CSRF on `/scout/handoff/tool` — **FIXED, verified live**

The tokenless POST that succeeded this morning now returns `403 Forbidden — "CSRF token missing or
invalid."` Only the real form's `_csrf` gets through. (This also means synthetic POSTs can no longer
stand in for a user click on scout routes.)

### Exports re-check (P0-3, P1-2, P1-3) — **DONE, see section 8**

At the time this section was written `/health` still reported `53f7a60f…`. `0687acb9` (#389) has
since deployed and all three were re-run against it: **P0-3 closed**, **P1-2 closed**,
**P1-3 narrowed to pxdesign alone**. Evidence is in section 8.

---

## 7. Phase 2 prices — live, smallest run, NOT SUBMITTED

Pulled from `/api/wallet/estimate` (the same endpoint each tool form calls) at each tool's form
**minimum** count, confirmed against the form fields' own `min` attributes on `/tools/<slug>`.

| # | Tool | Smallest run | Estimate | Hard cap | Buys us |
|---|---|---|---|---|---|
| 1 | mpnn | `num_seq_per_target=1` | **$0.0212** | $0.15 | P0-3 empty CSV |
| 2 | colabfold | `n_designs_total=1` | **$0.1457** | $0.50 | P1-7 empty ipTM |
| 3 | af2 | `n_designs_total=1` | **$0.5243** | $1.50 | P1-7 empty ipTM |
| 4 | rfantibody | `num_designs=1` | **$4.3697** | $13.00 | P1-4 count contradiction, P1-5 estimate-vs-charge |
| 5 | esmfold2-design | `n_seeds=1` | **$9.8614** | $15.00 | P1-7 empty iPTM_proxy |
| 6 | proteina | `num_designs=1` | **$12.5827** | $15.00 | P0-2 era, P2-1 blank rf3_score |

All six: estimate **$27.51** — over the $25 cap before a single run starts, and the worst case
(everything bills at its hard cap) is **$45.15**.

**Recommended set = rows 1–4: $5.06 estimated, $15.15 if every one bills at its hard cap.**
That fits the cap with room even in the worst case and covers the P0 export defect plus three of the
four flagged tools. Adding either row 5 or row 6 keeps the *estimate* under $25 but pushes the
worst case to $30.15 / $30.15, i.e. over the cap if the caps bind.

**Nothing has been submitted. Awaiting Leo's explicit approval.**

---

## 8. Phase 2 execution log (Leo approved all six, $27.50 cap, 2026-09-30)

Build at launch and throughout: `/health` → `0687acb981fca17aaca666506ff40ca48b1ccd9e`.

**Correction to section 5.** Section 5 said there is no ledger. There is one: `/account/wallet`
shows the last ten movements and `/account/wallet/transactions` the full history, with a
`reserved` row at submit and a `hold release` row at settlement. The four API paths tried in
section 5 do 404, but the UI ledger exists and is the authority for the figures below. Holds
were also cross-checked as 4-dp balance deltas from `GET /api/wallet/estimate` before and
after each submit; the two agree.

mpnn's settled cost is in that ledger: `reserved -$0.03` at 14:40, `hold release +$0.03` at
14:41, detail `mpnn net for this job <$0.01 true-up surplus released`. So the mpnn run cost
under a cent against a $0.03 quote.

| # | Tool | Job id | Quote | Hold | Hold rule | Status |
|---|------|--------|-------|------|-----------|--------|
| 1 | mpnn | `eb50bac8-8631-4d09-a3be-cc4f4973f44d` | $0.0300 | — | — | succeeded |
| 2 | colabfold | `8188ca02-f4b4-4b28-8856-1d54d13aacc2` | $0.1457 | $0.2186 | 1.5x estimate | see §9 |
| 3 | af2 | `7fd8e410-3cbf-4994-8adc-d69fee73ec8b` | $0.5243 | $1.5000 | at hard cap | see §9 |
| 4 | rfantibody | `84953585-0b39-4d5d-8ef5-469d085f7fa4` | $4.3697 | $6.5546 | 1.5x estimate | see §9 |
| 5 | esmfold2-design | `a327d5fe-987a-45f0-9e5b-821cd8c79298` | $9.8614 | $15.0000 | at hard cap | **failed, refunded in full** |
| 6 | proteina | `d0f161c7-0975-4159-81bf-c26aca02d988` | $12.5827 | $15.0000 | at hard cap | see §9 |

Committed worst case at the last launch: **$23.30 of the $27.50 cap** (esmfold2-design's
$15.00 released before proteina was launched, so the two never overlapped). Balance track:
996.2938 → 996.0752 → 994.5752 → 988.0206 → 973.0206 → 988.0206 (esmfold2 refund) → 973.0206.

Two hold rules are live side by side: `colabfold` and `rfantibody` hold 1.5x the quote,
`af2`, `esmfold2-design` and `proteina` hold at the tool's `hard_cap_usd`. Both are
deliberate (`shared/wallet_estimates.py:1052-1054` names af2 as a cap-holder), but the
quote the user reads on the form is not the amount that leaves their balance, and nothing
on the form says so. See P2-6.

### P0-4 (new) — ESMFold2 design fails on its own default settings

`/tools/esmfold2-design` served with preset `minibinder`, framework
`trastuzumab_framework_vhvl`, target `cd45`, seed `0`, `n_seeds` 1, `batch_size` 3. **Nothing
was changed.** Submitted as served → job `a327d5fe-987a-45f0-9e5b-821cd8c79298`, status
`failed`:

> Error: Category: pipeline — `design() raised: linalg.svd: (Batch element 0): The algorithm
> failed to converge because the input matrix is ill-conditioned or has too many repeated
> singular values (error code: 3).`

The money path is correct: "The $15.00 hold was returned to your wallet in full. You were not
charged for this run", and the balance confirms it (988.0206 → 973.0206 → 988.0206).

Two problems beyond the crash itself:

1. The advice on the failure page is **"Try again with the same settings."** The run is
   seed-pinned (`seed=0`) and the failure is a deterministic numerical one, so the same
   settings are the one thing that cannot help. The cause/fix rules from #371 fire, but this
   rule is wrong for this cause.
2. A raw `torch.linalg.svd` message is shown to a bench biologist as the explanation.

n=1. A second run would confirm determinism, but Leo's standing rule for this sweep is no
retry after a failure without asking, so it was not retried. **Unverified: reproducibility.**

### P2-4 (new) — ColabFold refuses the `:` multimer separator it documents

The ColabFold page says pTM and ipTM are reported "on a multimer". Pasting a multimer the way
ColabFold itself writes one — `CHAIN_A:CHAIN_B` in a single record — is refused:

```
POST /tools/colabfold/validate
{"error":"record 'query' contains non-canonical residues: [':']","ok":false}
```

`tools/colabfold/__init__.py:187` rejects the colon as a residue; the supported form is two
`>` records, which `tools/colabfold/__init__.py:221` then joins with `:` itself. The refusal
message never mentions that. The error banner **does** render (a red panel reading "Check your
inputs: …"), so this is a wording problem, not a silent failure — an earlier suspicion of a
silent no-op submit is **withdrawn**; the server answered correctly every time.

### P1-2 — CLOSED, verified fixed

iggm job `0f6be32f-e971-41f7-b7ec-33fbf795addd` under build `0687acb9`:
CSV header `rank,pdb_key,source_rank,n_epitope,antigen_chain,n_epitope_contacts`; rows 1-8
carry contacts `4,2,2,2,2,2,1,1`, identical to the page's first eight rows in page order, and
`source_rank` preserves the pre-sort index (9,0,15,16,32,35,2,6). Row counts match: 40 CSV
data rows against 40 candidates (the table's 80 `tbody` rows are 40 candidates + 40 collapsed
viewer rows).

### P1-3 — NARROWED from "11 tools" to one

FASTA export sampled on the most recent succeeded job per tool, build `0687acb9`:

| Tool | Job | FASTA | Records |
|------|-----|-------|---------|
| iggm | `0f6be32f` | 18,143 B | 120 |
| rfdiffusion | `01c3b3a6` | 4,000 B | 16 |
| proteina | `6b0c9b44` | 2,366 B | 16 |
| boltz2 | `dc88afb7` | 1,383 B | 6 |
| opendde | `4a3e3203` | 1,184 B | 4 |
| **pxdesign** | `a3429923` | **43 B** | **0** |

Only pxdesign returns a stub — `# No sequences found in this job's output.` — with HTTP 200.
That is truthful for pxdesign (its candidates are `.cif` structures; its CSV is
`rank,pdb_key,source_rank,pAE,ipTM,pLDDT`, no sequence column), but the page still offers a
FASTA button that can only ever deliver a comment line. P1-3 is therefore **a P2 on pxdesign
alone**, not a P1 across 11 tools. The original count is withdrawn.

### P2-5 (new) — proteina prices 1 design and 8 designs identically

`/api/wallet/estimate?tool=proteina&num_designs=N`:

| N | estimate | required (hold) |
|---|----------|-----------------|
| 1 | $12.5827 | $15.00 |
| 2 | $12.5827 | $15.00 |
| 8 | $12.5827 | $15.00 |
| 50 | $101.2907 | $101.2907 |
| 200 | $361.7526 | $361.7526 |
| 500 | $911.6166 | $911.6166 |

Correct behaviour (one shard = one container), but a user who reduces the count from 8 to 1 to
save money saves nothing and gets an eighth of the output. Nothing on the form says the first
container is already paid for. The Phase 2 proteina run was moved back to 8 designs for this
reason — same price, eight times the evidence.

### P2-6 (new) — rfantibody hotspots must be bare integers, and the message does not say which

`A62,A63,A101` (chain-qualified, the form's own `target_chain` is `A`) is refused with
"Hotspot residues must be comma-separated integers (e.g. 54,56,115)." The example makes the
format clear, but the field sits directly under `target_chain`, and chain-qualified hotspots
are exactly what Scout's handoff and the proteina docs use elsewhere on the site.

### P2-7 (new) — proteina page title uses `--` where every other tool uses an em dash

`<title>` on `/tools/proteina` is `Proteina-Complexa -- Ranomics Tools`. Every other tool page
sampled renders `Tool name — Ranomics Tools`.

### Scout observation (new) — a fresh job's structure disappears within minutes

Scout job `5ef02cb1-…` analysed successfully; minutes later `/scout/pdb/5ef02cb1-…` returned
66 bytes, `{"error":"Structure file not found. Please re-upload your file."}`. The earlier job
`abbf3add` had already expired with "Job not found or expired". Not chased further this
sweep. **Unverified: the retention window.**

---

## 9. Phase 2 per-job audits

### Run 1 — mpnn `eb50bac8-8631-4d09-a3be-cc4f4973f44d` — PASS

| Check | Result | Evidence |
|-------|--------|----------|
| 1 runs | pass | status succeeded, 12 GPU-seconds |
| 2 results | pass | 1 row, Score 0.758, Recovery 0.53 — both in range |
| 3 request honoured | pass | 1 sequence requested, 1 returned |
| 4 downloads | pass | CSV header `rank,score,recovery,chain,sample,sequence`, 1 row matching the page; FASTA 1 record. **This closes P0-3** (empty mpnn CSV) under build `0687acb9` |
| 5 money | pass | quote $0.03, reserved $0.03, settled "<$0.01", surplus released |

### Run 4 — rfantibody `84953585-0b39-4d5d-8ef5-469d085f7fa4` — FAILS checks 3 and 4

Input: 1HEW chain A uploaded as the target, hotspots `62,63,101`, CDR lengths left at the
served default `H1:8,H2:7,H3:10-16` (heavy chain only, i.e. a VHH run),
**`num_designs` = 2**.

| Check | Result | Evidence |
|-------|--------|----------|
| 1 runs | pass | succeeded in ~9 min |
| 2 results | pass | 10 rows, columns `# ★ ipAE(Å) pLDDT pAE(Å) vs. quality bar 3D Structure`; sorted ascending by ipAE (lower is better — correct); badges agree, row 1 "Meets pLDDT 80, ipAE 10 Å and pAE 5 Å" at 5.02/90.0/3.48, every other row names the exact number that missed |
| 3 request honoured | **FAIL** | 2 designs requested, **10 candidates returned** — see P1-4 below |
| 4 downloads | **partial** | CSV: 10 rows, header `rank,pdb_key,source_rank,pAE,ipAE,pLDDT`, row 1 `1,designs/design_001.pdb,1,3.48,5.02,90.0` matches the page exactly ✓. Structure: `/api/jobs/.../pdb/designs/design_001.pdb` → 248,752 B, 3,701 ATOM records, chains H and T, parses ✓. FASTA: **20 records for 10 designs** — see P1-8 below |
| 5 money | see P1-5 | quote $4.3697, reserved $6.5546, **settled $0.63** |

#### P1-4 — CONFIRMED and reproduced: "number of designs" is not the number of results

Requested 2, got 10 — a 5x multiplier, the same ratio as the 4-to-20 case already in section 3.
The rfantibody pipeline runs a sequence-design step per backbone, so "designs" on the form
means backbones and the results table counts sequences. Nothing on the form or the results
page says so, and the wallet quote is computed from the backbone count. Repro: submit
`/tools/rfantibody` with any PDB, `num_designs` = N, and count the rows.

#### P1-5 — CONFIRMED, re-measured, still a large over-quote

Ledger row: `rfantibody net for this job $0.63 true-up surplus released, +$5.92`. Against a
$4.3697 quote that is a **7x over-estimate**; the earlier case in section 3 was $80.40 quoted
against $1.01 charged (80x). The estimate has improved by an order of magnitude but still
reserves seven times what the run costs, which is what decides whether a user with a small
balance can start the run at all.

#### P1-8 (new) — the rfantibody FASTA ships ten copies of the user's own target

20 records for 10 designs, because every design contributes `..._chainH` (the design, 121-124
aa) **and** `..._chainT` — 129 aa, byte-identical to the 1HEW chain A that was uploaded as the
target. A user who counts records, or feeds the file to a downstream sequence tool, gets ten
copies of their own input mixed in with the designs.

Second problem in the same file: the quality verdict is written **into the FASTA header**:

```
>rank2_design_002.pdb_chainH [does not meet bar: ipAE 12.05 Å, above 10 Å; pAE 6.83 Å, above 5 Å]
```

The id up to the first space is still clean, so most parsers survive, but the header carries
non-ASCII `Å` and a `;`, and the whole verdict is duplicated onto the target record too.

---

## 9. Phase 2 audits (continued)

### 9.1 proteina — job `d0f161c7-0975-4159-81bf-c26aca02d988` — PASS with two findings

Submitted 2026-09-30 14:57 UTC. Request: 8 designs, target 1HEW chain A (uploaded PDB),
hotspots `62,63,101`, binder length 60-80. Quote $12.5827, hold placed at `hard_cap_usd`
$15.00. Build `0687acb981fca17aaca666506ff40ca48b1ccd9e` (`/health`).

| # | Check | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | Run completes | PASS | `/jobs/d0f161c7…` shows `Status succeeded` |
| 2 | Results table | PARTIAL | 8 rows, columns `# ★ Reward af2_iptm af2_plddt rf3_score binder_scrmsd 3D Structure`; `rf3_score` blank on all 8 rows — see P2-1 below |
| 3 | Request honoured | PASS | 8 designs asked, 8 rows and 8 CSV rows; downloaded `design_001.pdb` carries chains A+B, 1500 atoms, binder chain B is 67 aa (inside the requested 60-80) |
| 4 | Downloads | PASS with a caveat | structure fetch HTTP 200, 121,827 bytes, parses (1500 `ATOM` records, chains A/B); CSV header `rank,pdb_key,source_rank,af2_iptm,af2_plddt,rf3_score,total_reward,binder_scrmsd,target_numbering` with 8 rows whose values match the page to the displayed precision; `export.fasta` holds **16** records, not 8 — see P1-8 note below |
| 5 | Viewer / charge | PASS | `View 3D` present on every row; ledger `/account/wallet/transactions` shows `14:57 reserved −$15.00` then `15:05 hold release +$14.20 "proteina net for this job $0.80 true-up surplus released"` |

Page table, as rendered (ranking is by descending Reward, monotone and sane):

```
1 Top  -0.74  0.12  72.61  —   7.45
2      -0.75  0.12  51.97  —  10.32
3      -0.81  0.08  70.52  —  25.36
4      -0.83  0.08  69.71  —   9.72
5      -0.84  0.07  72.76  —  16.38
6      -0.85  0.08  60.98  —  16.87
7      -0.86  0.06  73.46  —  13.80
8      -0.86  0.07  73.73  —  12.77
```

CSV first three rows (`/jobs/d0f161c7…/export.csv`), matching the page:

```
1,designs/design_001.pdb,1,0.117,72.61,,-0.7379,7.4528,input
2,designs/design_002.pdb,2,0.1243,51.97,,-0.7504,10.3204,input
3,designs/design_003.pdb,3,0.0824,70.52,,-0.807,25.3647,input
```

**P0-2 re-test — does NOT reproduce on a current run.** P0-2 was raised on the archived
job `6b0c9b44`, whose `af2_plddt` column read 11.07-21.93 — the signature of a pLDDT
reported on the wrong polarity/scale. On this fresh run under build `0687acb9` the same
column reads **51.97-73.73**, an ordinary pLDDT range for a de novo binder complex, and
it correlates the right way with the other columns (the worst row, 51.97, is rank 2 by
reward but has no unusual `binder_scrmsd`). *What this shows:* current proteina runs
report `af2_plddt` on the correct scale. *What it does not show:* that the archived job's
page has been corrected, or which commit changed it — I did not bisect. The archived job
still renders its original 11.07-21.93 numbers with no era caveat on the page, so a user
opening an old proteina job is still shown a wrong-scale column. That residual is the
part of P0-2 that stands; the live-run part is closed.

**P2-1 reproduced.** `rf3_score` is present as a column, blank (`—`) in all 8 page rows
and empty in all 8 CSV rows. An all-blank column fails check 2 as written in the brief.
This is a fresh run on the current build, so P2-1 is not historical.

**P1-8 (FASTA count) also applies to proteina, benignly.** `export.fasta` holds 16
records for 8 designs: each design emits `rankN_design_00N.pdb_chainA` (129 aa) and
`_chainB` (67 aa). Chain A at 129 aa is the uploaded 1HEW target, so half the FASTA file
is the user's own input echoed back. Unlike the rfantibody case this is at least labelled
by chain, but the count still does not match the design count a user would expect.

**Over-quote, materially worse than P1-5.** Quote $12.5827; settled $0.80. That is a
**15.7x** over-quote, against the 7x measured on rfantibody. The hold was taken at the
hard cap ($15.00), so a user with $14 of balance could not have launched a job that
actually cost eighty cents. Recorded as P1-5 evidence, not a new finding.

### 9.2 colabfold — job `8188ca02-f4b4-4b28-8856-1d54d13aacc2` — PASS

Submitted 14:48 UTC. Request: a two-record FASTA (two `>` records, 144 aa total) so the
run would exercise the multimer path. Quote / hold $0.22 (1.5x rule). Settled **$0.13**.

| # | Check | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | Run completes | PASS | `status.json` → `succeeded`; page reads `Completed in 104 GPU-seconds` |
| 2 | Results | PASS | this is the standalone-fold surface, not a candidate table: `mean pLDDT 33.22 · pTM 0.190 · ipTM 0.080 · length 144 aa (2 chains)`, plus `Recycles: 1 · Templates: off · MSA: single_sequence` |
| 3 | Request honoured | PASS | 144 aa across 2 chains is exactly the submitted pair; the downloaded model has chains A and B and 144 residues |
| 4 | Downloads | PASS | `Download PDB` and `Download PAE (npz)` both resolve; PDB decodes to 90,963 bytes / 1118 `ATOM` records / chains A+B / 144 residues, B-factor column 18.83-52.16, consistent with the stated mean pLDDT 33.22 |
| 5 | Viewer / charge | PASS | Mol* viewer block present; ledger `15:10 hold release colabfold net for this job $0.13` |

**ipTM populates on a multimer — P1-7 does not reproduce here.** P1-7 was raised on a
blank ipTM. This run reports `ipTM 0.080`: a real number, low but populated, on a genuine
two-chain input. Combined with af2 below (ipTM 0.380 on the same 144 aa pair), the blank
ipTM is **not** a property of the multimer path on the current build. I did not establish
what the original P1-7 job differed in, so P1-7 is narrowed, not closed.

### 9.3 af2 — job `7fd8e410-3cbf-4994-8adc-d69fee73ec8b` — PASS, cleanest run of the six

Submitted 14:50 UTC, same 144 aa two-chain input as colabfold. Hold $1.50 (taken at
`hard_cap_usd`). Settled **$0.31**.

| # | Check | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | Run completes | PASS | `succeeded`, `Completed in 180 GPU-seconds` |
| 2 | Results | PASS | a real metric table with a `Meaning` column: `pTM 0.870`, `ipTM 0.380` ("> 0.6 is a common confidence threshold"), `pLDDT (mean) 89.4`, `pLDDT range 26.5 - 98.9`, `Chains / AA 2 / 144` ("Model preset: multimer") |
| 3 | Request honoured | PASS | preset reported as `multimer`, 2 chains / 144 aa as submitted |
| 4 | Downloads | PASS, internally consistent | `/jobs/…/af2.pdb` → HTTP 200, 90,963 bytes, 1118 atoms, chains A+B; its B-factor column runs **26.52-98.88**, which matches the page's stated `pLDDT range 26.5 - 98.9`. `/jobs/…/af2_pae.npz` → HTTP 200, 27,202 bytes |
| 5 | Viewer / charge | PASS | NGL viewer on `#af2-ngl-viewer` from an inline `data-pdb-b64`; ledger `15:10 hold release af2 net for this job $0.31` |

Two cross-tool inconsistencies worth filing as **P2-7**, neither affecting correctness:

1. **The two fold tools present their scores differently.** af2 renders a three-column
   table with thresholds spelled out; colabfold renders one bare sentence with the same
   three numbers and no interpretation. A user comparing them has to know what 0.080
   means on one page and is told on the other.
2. **They deliver structures by different mechanisms and use different viewers.** af2
   serves `/jobs/<id>/af2.pdb` and renders with NGL; colabfold embeds the whole structure
   and the whole PAE array in the page as `data:` URIs (the job page is 220,063 bytes, of
   which 164,152 are those two hrefs) and renders with Mol*. Everything works, but the
   colabfold page carries its payload twice and grows with structure size.

Also worth noting for expectation-setting rather than as a defect: the **same input**
scored `pLDDT 89.4 / ipTM 0.380` on af2 and `33.22 / 0.080` on colabfold. The colabfold
page does disclose why (`MSA: single_sequence`, `Recycles: 1`, `Templates: off`), so this
is a defaults choice, not a wrong number — but a user running "the same fold" on both
tools gets a confident model from one and a poor one from the other.

### 9.4 Phase 2 spend, against the $27.50 cap Leo approved

All six approved runs launched. Settled charges, every figure read from
`/account/wallet/transactions`:

| Tool | Job | Hold | Settled |
|------|-----|------|---------|
| mpnn | (14:40) | $0.03 | <$0.01 |
| colabfold | `8188ca02` | $0.22 | $0.13 |
| af2 | `7fd8e410` | $1.50 | $0.31 |
| rfantibody | (14:52) | $6.55 | $0.63 |
| esmfold2-design | (14:53) | $15.00 | $0.00 (failed, fully released) |
| proteina | `d0f161c7` | $15.00 | $0.80 |
| **Total** | | | **$1.87** |

**$1.87 spent of the $27.50 approved.** Holds peaked at $23.30 outstanding; the
esmfold2-design hold released at 14:53 before proteina's was taken at 14:57, so the two
$15.00 holds never overlapped. No run was retried.

Two ledger defects, both legibility not money:

- **P2-8 — the failed run's ledger line loses its tool name and its sentence.** The
  esmfold2-design release reads `net for this job $0.00 pipeline`, where every successful
  release reads e.g. `af2 net for this job $0.31 true-up surplus released`. The tool name
  is missing and the trailing word is `pipeline` rather than a phrase. The refund itself
  is correct: `+$15.00`, balance restored to $988.02.
- **P2-9 — auto-released holds appear as reservations that are never returned.** Three
  `colabfold reserved … −$0.22` rows appear (14:44, 14:48, 14:48) against a single
  colabfold `hold release`. Two of those were submits the validator refused, whose holds
  auto-release without a matching ledger row. All three rows show the *same* balance
  after, $996.07, so no money moved — but a user reading the ledger sees $0.66 reserved
  and $0.09 returned. I did not read the release path, so the mechanism is inferred from
  the rows and the unchanged balance; the duplicated 14:48 pair in particular is
  unexplained.

---

## 10. Close-out

**Prod build at end of Phase 2:** `/health` → `0687acb981fca17aaca666506ff40ca48b1ccd9e`,
unchanged across every Phase 2 launch and audit (sections 7-9).

**Withdrawn during the sweep** (recorded so they are not re-reported):

- The "colabfold silently no-ops on submit" finding. The server answered every time; my
  error detector missed the banner wording `Check your inputs: …`. The real refusals were
  a `:` chain separator (P2-4) and hotspot format (P2-6).
- The "`5 to 1 min` garbled runtime band" on the fold forms. The page reads
  `0.5 to 1 min (some presets vary)`; my regex clipped the leading `0.`.
- "Both folds sat queued far past their advertised band." The
  `Waiting for pipeline to start...` string is the **server-rendered default** in
  `templates/job_detail.html:187`; the live stage text is written client-side from
  `/jobs/<id>/status.json`. A server-side `fetch` of the page always shows the default,
  so it is not evidence of queue time. Both jobs had in fact already succeeded.

**Unverified — carried forward:**

- **U-7** — the esmfold2-design failure (`linalg.svd` non-convergence) was not retried,
  per the standing no-retry rule. Whether it reproduces is unknown, so P0-4 is a
  single-observation finding.
- **U-8** — "email says ready" (check 5) could not be tested from here for any of the six
  runs; no mailbox access.
- **U-9** — P0-2: current proteina runs report `af2_plddt` on the correct scale, but I
  did not bisect when that changed, and the archived job `6b0c9b44` still renders its
  original 11.07-21.93 figures with no era caveat.
- **U-10** — P2-9's mechanism (auto-released holds not writing a ledger row) is inferred
  from ledger rows and an unchanged balance, not from reading the release code path.
- **U-11** — the Scout structure-retention window was not measured.

---

## 11. Exports re-confirmation, requested by hub-lead after #389 deployed

Hub-lead asked for P0-3, P1-2 and P1-3 to be re-tested against prod once #389 was live at
build `0687acb9`. They had already been re-run against that build (sections 8 and 9); this
section is a second, independent pass run after the request, against the build prod is
serving **now**.

**Correction to section 10.** Section 10 records the build at the end of Phase 2 as
`0687acb981fca17aaca666506ff40ca48b1ccd9e`, which was correct for every Phase 2 launch and
audit. Prod has since redeployed: `/health` now reports
`582e0ebc575200741fa5d2c8a7fc79dd580d55b3`. The three checks below ran under `582e0ebc`, so
they are a confirmation on a *later* build than the one hub-lead named, not on the same one.

| Finding | Verdict | Evidence, read live under `582e0ebc` |
|---|---|---|
| **P0-3** mpnn CSV empty and missing its columns | **FIXED** | `/jobs/eb50bac8…/export.csv` header is `rank,score,recovery,chain,sample,sequence` with **1 data row**, against a page showing 1 row. The old header was `rank,pdb_key,source_rank` with zero rows. FASTA is real too: 1 record, `>mpnn_rank1 score=0.7585 recovery=0.5271` |
| **P1-2** CSV row order does not match the page | **FIXED** | `/jobs/84953585…` (rfantibody): page shows 10 candidate rows, CSV has 10 data rows, and the `pdb_key` column runs `designs/design_001.pdb, design_002, design_003, design_004…` in the same order as the page's own structure links (`design_001.pdb, design_002.pdb, design_003.pdb, design_004.pdb`). The defect was CSV row 1 = `000_design_0.pdb` against page row 1 = `009_design_17.pdb` |
| **P1-3** FASTA button is dead on structure-only tools | **STILL BROKEN on pxdesign; fixed elsewhere** | rfantibody `84953585` returns **20 real records** (`>rank1_design_001.pdb_chainH` …). pxdesign job `a3429923-4f76-4509-ae12-5ccd6cfa81a1` still returns the 43-byte stub `# No sequences found in this job's output.` |

**Why pxdesign is still stubbed, from the code rather than inference.**
`shared/exports.py:545-547` builds a FASTA record only from a candidate's own
`sequence` / `binder_sequence` field and `continue`s past any candidate that has neither —
it does not read the structure file. pxdesign's candidates carry neither: its CSV header,
live, is `rank,pdb_key,source_rank,pAE,ipTM,pLDDT`, with no sequence column, and its one
candidate's `pdb_key` is `designs/design_001.cif`. So the FASTA button on pxdesign offers a
download that can only ever be a comment line, and the fix has to be in what the pxdesign
adapter persists per candidate, not in the export.

This matches the narrowing in section 8: P1-3 is **not** an 11-tool defect, it is pxdesign
alone, and at P2 severity rather than P1 — no data is wrong, a button is inert.

*(Correcting my own earlier guess: I first supposed #389 had taught the exporter to read
sequences out of the structure files, and that pxdesign's `.cif` was refused by that reader.
The code above shows there is no such reader — the exporter only ever reads a candidate
field. The `.cif` extension is incidental.)*
