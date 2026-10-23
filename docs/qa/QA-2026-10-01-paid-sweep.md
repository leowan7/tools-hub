# QA 2026-10-01 — paid sweep of the eight tools the 30 Sep sweep never ran

Report only. No code changed, no branch, no PR.

> **Location note.** The brief asked for this file in the main checkout
> (`tools-hub/docs/qa/`). A session hook blocks writes from this worktree into the
> base checkout, so it was written to the worktree's `docs/qa/` instead. Untracked
> and uncommitted either way. Copy it across with:
> `cp .claude/worktrees/vigorous-antonelli-bab2f7/docs/qa/QA-2026-10-01-paid-sweep.md docs/qa/`

Follow-up to `QA-2026-09-30-functional.md`, which made fresh paid runs on six tools
(mpnn, colabfold, af2, rfantibody, esmfold2-design, proteina) and judged the other
eight from old finished jobs. This sweep puts **one fresh paid run on each of those
eight**: esmfold, boltz2, iggm, rfdiffusion, boltzgen, bindcraft, pxdesign, opendde.

| | |
|---|---|
| Build at start | `b2f1398976cdaabffe52dd0bd4e4354c7f05598d` (`/health`, 20:10 UTC) |
| Build at end | `cc85754dccc9f756337e84c8ba331086fed7ebb7` (`/health`, 22:14 UTC) |
| Prod redeployed mid-sweep | yes — one commit, `#404` (auto-reload guards) |
| Wallet at start | $994.42 |
| Approved by Leo in this session | quote total $37.18, holds $47.09 |
| Peak open holds (mine) | $46.54 at 20:19 UTC — ceiling was $75 |
| Runs | 8 submitted, 0 retried |

**Prod changed under the sweep.** `/health` returned a different build at the end than at
the start. The single commit between them is `cc85754d fix(wallet): both auto-reload
safety guards fail CLOSED on an unreadable ledger (#404)`, authored 21:15 UTC, so the
deploy landed while BoltzGen and BindCraft were running and after the other six had
settled. Checked for interference with this report's citations: every hunk of that commit
in `shared/wallet.py` is at line 930 or later (`git show cc85754d -- shared/wallet.py`),
i.e. after `settle` and far after the rate table, so the line numbers cited below still
point at the symbols named. Nothing in it touches settlement, exports or any results page.
Findings observed before ~21:30 were observed on `b2f1398`; the two long runs' settlement
and everything after it on `cc85754d`.

A second worker was running `esmfold2-design` on the same account throughout
(hold at 17:02, released 20:13). Its ledger rows are identified by tool name and
were read but not touched.

---

## Spend

Quote = the price the form showed. Hold = what the site reserved. Settled = what was
actually charged. Ratio = quote ÷ settled.

| Tool | Job | Quote | Hold | Settled | Quote ÷ settled |
|---|---|---|---|---|---|
| opendde | `666f51fd` | $14.79 | $15.00 | $0.71 | **20.8x** |
| pxdesign | `211eca08` | $6.30 | $9.44 | $0.57 | **11.1x** |
| boltz2 | `6ebcda6c` | $0.22 | $0.33 | $0.08 | 2.8x |
| iggm | `587565e7` | $0.08 | $0.11 | $0.07 | 1.1x |
| esmfold | `08df0826` | $0.0728 | $0.11 | $0.05 | 1.5x |
| rfdiffusion | `dc43476a` | $3.37 | $5.00 | $2.50 | 1.35x |
| boltzgen | `7dd2b723` | $6.07 | $9.10 | $7.72 | **0.79x** (under-quoted) |
| bindcraft | `489a17e0` | $6.29 | $8.00 | $8.00 | **0.79x** (under-quoted, capped) |
| **Total** | | **$37.19** | **$47.09** | **$19.70** | |

Settled total is **$19.70 against the ~$44 Leo approved**, and 53% of the quoted total.
Six of eight runs came in far under; the two long ones came in over.

The two long runs invert F-1: they are the only runs in the sweep that cost *more* than
quoted. BindCraft's $8.00 is its hard cap, not its metered cost — see F-17 and F-18.

*One cent of bookkeeping:* the quote column sums to $37.19, while the approval I asked
Leo for in this session said $37.18. The per-run figures above are the ones the forms
showed at submit; which figure in the approval list was a cent different I did not
record, so the difference is unexplained rather than reconciled. It is below every
threshold in the brief and no run exceeded its listed quote at submit.

Hold arithmetic is correct in every settled case — e.g. pxdesign released $8.87 against
a $0.57 charge on a $9.44 hold. The problem is the size of the quote, not the accounting.

---

## Per-tool results

Five checks per the 30 Sep format: 1 run completes · 2 results make sense ·
3 request honoured · 4 downloads · 5 viewer and money.

### ESMFold — job `08df0826` — PASS with two defects

Input: `>1HEW_A lysozyme` + the 129 aa 1HEW chain A sequence, standalone preset, 1 design.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 22 GPU-seconds |
| 2 | Results sane | mean pLDDT 92.00, pTM 0.907, length 129, model `facebook/esmfold_v1` — all in range |
| 3 | Request honoured | 1 sequence in, 1 structure out, 129 aa = input length |
| 4 | Downloads | PDB 81,042 B parses to 1 chain A, 129 residues, 1000 atoms, 0 HETATM, LYS1→LEU129, B-factors 45–98 mean 91.34 (consistent with the reported 92.00). PAE `.npz` 25,872 B contains `pae.npy`, float16, shape `(129, 129)`, 847 distinct values, diagonal 0.25, properly asymmetric — a genuine PAE. Both are `data:` URIs embedded in the page, not server endpoints. No CSV/FASTA/ZIP link is offered. |
| 5 | Viewer and money | **No 3D viewer exists** — the string `molstar` appears nowhere in the page's 216,565 bytes and there is no `<canvas>`. Quote $0.0728 / hold $0.11 / settled $0.05. No over-estimate notice, correctly. Ledger: `esmfold net for this job $0.05 true-up surplus released`. |

Defects: F-3 (tool page says there is no PAE), F-6 (no viewer), F-7 (404 explainer link),
F-8 (cost panel self-contradiction), F-9 (`<title>` separator).

### Boltz-2 — job `6ebcda6c` — PASS with two defects

Input: standalone preset, 1hew.pdb chain A, hotspots 62,63,101, 1MEL VHH (132 aa) pasted as the binder.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 69 GPU-seconds |
| 2 | Results sane | scores populated and varied; "Hotspot hits 0 of 3" is **correct** — see Withdrawn W-1 |
| 3 | Request honoured | 1 design requested, 1 design returned |
| 4 | Downloads | structure carries the full 129-residue target chain and 0 HETATM. **FASTA has 2 records for 1 design** — the second is my own 129 aa target chain echoed back — and both headers carry the verdict string `[does not meet bar: Hotspot hits 0, below 4]`. |
| 5 | Viewer and money | viewer renders. Quote $0.22 / hold $0.33 / settled $0.08. |

Defects: F-2 (FASTA echo + verdict in headers, carried forward from 30 Sep P1-8),
F-4 (preflight panel under-reports), F-1 (2.8x over-quote).

### PXDesign — job `211eca08` — FAIL on downloads

Input: 1hew.pdb chain A, hotspots 62,63,101, binder_length 60, num_designs 2.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 324 GPU-seconds |
| 2 | Results sane | CSV columns `rank,pdb_key,source_rank,pAE,ipTM,pLDDT`, 2 rows, values populated |
| 3 | Request honoured | 2 designs requested, 2 returned; binder chain B0 is exactly 60 aa in both — the requested length |
| 4 | Downloads | **`export.fasta` is 43 bytes reading `# No sequences found in this job's output.`** while the job's two `.cif` files each contain a distinct 60 aa designed binder. The entire product of the run is missing from the sequence export. |
| 5 | Viewer and money | viewer renders. Quote $6.30 / hold $9.44 / settled $0.57 — **11.1x** |

The two designed binders, recovered from the structures:
`SEVDAKKEAIVKEAFKTYGAEADAILNDPSLTEAEKAAAIAAAAAKVQAELNAKLAEVEA` (design_001)
`MTPEEQAWKAEALAAVSKAKSDQEKRDAIGAAMTAYRQAHPDTPLERVLALEAEVLKAID` (design_002)

Defects: **F-5 (P0)**, F-1 (11.1x over-quote), F-7 (404 explainer link).

### OpenDDE — job `666f51fd` — PASS with one defect

Input: ABAG preset, guided; 1VFB `>H` 116 aa + `>L` 107 aa + `>G` 129 aa; sample 2, step 200, cycle 10, seed 1, n_seeds 1.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 172 GPU-seconds (~3 min) |
| 2 | Results sane | per-sample scores genuinely differ (pTM 0.9278 vs 0.9274, ipTM 0.9377 vs 0.9373) — the #245 fix holds |
| 3 | Request honoured | 2 samples requested, 2 returned |
| 4 | Downloads | CSV and structures parse; ZIP opened and listed |
| 5 | Viewer and money | viewer renders. Quote $14.79 / hold $15.00 / settled $0.71 — **20.8x, the worst in the sweep** |

Defects: F-1 (20.8x over-quote), F-10 (static runtime copy says 15–60 min; the run took 3).
Observation: CSV `runtime_seconds` reads 1–2 s per design against 172 s of job GPU time — the two numbers are not reconcilable, cause not established.

### IgGM — job `587565e7` — PASS

Input: complex_prediction preset, D1.3 H+L FASTA, 1hew.pdb antigen chain A, epitope `18,19,22,23,24,25,26,27,102,103,116,117,118,119,120,121,122,124,125,126,129`, num_samples 1.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 56 GPU-seconds |
| 2 | Results sane | the per-residue epitope grid is **correct** — see Withdrawn W-2 |
| 3 | Request honoured | 1 sample requested, 1 returned |
| 4 | Downloads | structure parses. The designed antibody chains come back **backbone-only** (atom types exactly C, CA, N — 348 atoms for 116 residues) while the antigen keeps full side chains (1000 atoms for 129 residues). Not flagged anywhere in the UI. |
| 5 | Viewer and money | viewer renders. Quote $0.08 / hold $0.11 / settled $0.07 — **accurate** |

Defects: F-4 (preflight "Nothing to clean").

### RFdiffusion — job `dc43476a` — PASS, the cleanest run in the sweep

Input: 1hew.pdb chain A, hotspots 62,63,101, binder 55–65, num_designs 10.

| # | Check | Result |
|---|---|---|
| 1 | Completes | succeeded, 2058 GPU-seconds (34.3 min) against the preflight's "~49.7 min for 10 designs" |
| 2 | Results sane | 10 rows, ipTM strictly descending 0.740 → 0.130, pLDDT 92.8 → 75.9, i_pAE 6.60 → 21.45 Å. No blank or constant column. "Top" badge sits on row 1. Each verdict string is arithmetically consistent with its own row — rows 1 and 2 read "Meets ipTM 0.65, pLDDT 80 and i_pAE 10 Å", rows 3–10 name the exact failing thresholds. 2 of 10 clear the bar. |
| 3 | Request honoured | 10 designs requested, **10 returned** — no multiplication (contrast rfantibody's 5x on 30 Sep). Binder lengths 57, 61, 61, 62, 65, 63, 63, 55, 60, 55 — **every one inside the requested 55–65** |
| 4 | Downloads | CSV 561 B, 10 rows in page order, values identical to the page (`1,designs/design_001.pdb,1,0.74,6.6,92.82,95.83`). FASTA 1,577 B, **10 real records, no target chain echoed** — sequence lengths match the structures exactly. PDB 116,154 B parses to chain A 129 residues (target) + chain B 57 residues (binder, = FASTA record 1), 1,429 atoms, 0 HETATM. ZIP 244,064 B contains all 10 `designs/design_NNN.pdb`, sizes 106–119 kB. |
| 5 | Viewer and money | Viewer renders — Mol* initialises, canvas 994×418, `design_001.pdb` fetched (116,454 B), and the screenshot shows the green target with the orange designed helix docked against it. Quote $3.37 / hold $5.00 / settled $2.50 — **1.35x, accurate**. Ledger: `rfdiffusion net for this job $2.50 true-up surplus released`. |

Only defect: F-2's header half (verdict text in FASTA headers).

Note on the viewer: my first click landed while the tab was backgrounded and the panel sat
at "Loading 3D viewer..." indefinitely. Fronting the tab let it finish. This is the harness
artefact the 30 Sep sweep mistook for a product defect (its withdrawn W-1), reproduced
deliberately here to confirm the diagnosis. **Not a finding.**

### BoltzGen — job `7dd2b723` — PASS, and the only tool that returned a design meeting its own bar

Input: 1hew.pdb chain A, hotspots 62,63,101, binder 50–70, protocol protein-anything,
budget 50. Submitted 20:16 UTC.

1. **Completes.** SUCCEEDED, 6360 GPU-s, 106.0 min wall. Status read from
   `/jobs/7dd2b723.../status.json`; page reads `Completed in 6360 GPU-seconds.`
2. **Results make sense.** 50 rows. ipTM 0.527 → 0.142 strictly descending; pLDDT 43.0–82.9;
   refolding RMSD 0.35–10.81 Å. No column blank, none constant. `TOP` badge on rank 1 only.
   Spot-checked the per-row verdict text against its own numbers: rank 22 (pLDDT 80.4,
   RMSD 0.55) reads "Meets pLDDT 80 and Refolding RMSD 1.5 Å"; rank 11 (79.9) reads
   "below 80" — correct at the displayed precision. 3 of 50 meet the bar (ranks 22, 41, 42).
   *Note, not a defect:* the table sorts by ipTM, but the pass bar is pLDDT + RMSD only, so
   the three passing designs sit at ranks 22/41/42 beneath twenty-one failing ones.
3. **Request honoured.** Budget 50 → 50 candidates. Binder lengths 51–69 aa, every one
   inside the requested 50–70 window (measured from the FASTA, per record, not per line).
4. **Downloads.** CSV 2,618 B, 50 data rows + header, and it matches the page row for row
   in page order — rank 1 `0.52744 / 76.06 / 2.18492` against the page's `0.527 / 76.1 /
   2.18`, rank 50 `0.14212 / 66.57 / 0.61618` against `0.142 / 66.6 / 0.62`. FASTA
   17,469 B, 100 records (2 chains × 50 designs). ZIP 2,056,727 B, 50 local headers,
   `designs/design_001.cif` … `design_050.cif`, matching the table exactly. Opened
   `design_001.cif`: 131,393 B, 19 `_atom_site.` columns, 1,404 atoms, chain A 129
   residues (the target) and chain B 57 residues (the binder) — and chain B's length
   agrees with the FASTA's `_chainB` record for the same design.
5. **Viewer and money.** Viewer renders: clicking "View 3D" on rank 1 opens a 994×418
   Mol* canvas with a drawn cartoon and the button toggles to "Hide 3D". Quote $6.07,
   hold $9.10, settled **$7.72**. The over-estimate notice shows, and is accurate:
   "This run used more GPU time than estimated: estimated $6.07, charged $7.72." Ledger
   row names the tool: `hold release · boltzgen · net for this job $7.72 · true-up surplus
   released · +$1.38`, which is exactly $9.10 − $7.72.

Defects: F-2 (verdict prose in FASTA headers).

### BindCraft — job `489a17e0` — ZERO CANDIDATES, charged its full cap

Input: 1hew.pdb chain A, hotspots 62,63,101, binder 50–100, num_designs 2.
Submitted 20:16 UTC.

1. **Completes.** SUCCEEDED, 6186 GPU-s, 103.1 min wall.
2. **Results make sense.** There are none. `CANDIDATES (0)`, with the banner "BindCraft's
   filters rejected all designs in this run. This is a known outcome for difficult targets
   — try expanding the binder_length range or adjusting hotspots." That copy is honest and
   actionable, and it is the right failure message for this outcome.
3. **Request honoured.** 2 designs asked for, 0 returned — within the tool's documented
   behaviour, not a silent loss.
4. **Downloads.** The page offers none. The three export endpoints nevertheless answer 200
   with nothing: CSV 26 B (header only), FASTA the 43 B stub, ZIP 22 B (empty-archive
   EOCD). Identical to ESMFold — see F-14, which this generalises.
5. **Viewer and money.** No viewer, no rows to view. Quote $6.29, hold $8.00, settled
   **$8.00** — the entire hold, which is also the tool's hard cap. The job page reports it
   correctly ("estimated $6.29, charged $8.00"); the ledger does not — see F-17.

Defects: F-14 (generalised), F-17, F-18.

---

## Findings

### P0

**F-5 — PXDesign throws away every sequence it designed.**
`https://tools.ranomics.com/jobs/211eca08-2525-4a63-b90a-41b9689914e2/export.fasta`
returns 43 bytes: `# No sequences found in this job's output.` The same job's two
structure files each contain a distinct 60 aa designed binder (quoted above). FASTA is
the format a protein engineer orders synthesis from; a binder-design run whose sequence
export is empty has delivered nothing usable without hand-parsing mmCIF.
Re-verified independently at 21:06 UTC by parsing the `_atom_site` loop of both CIFs
(20 columns, keyed on `auth_asym_id` / `label_comp_id` / `label_atom_id`):

| file | chain A0 | chain B0 |
|---|---|---|
| design_001.cif (139,088 B) | 129 aa, `KVFGRCELAAAMKRHGLDNYRG…WIRGCRL` — the 1HEW target | **60 aa**, `SEVDAKKEAIVKEAFKTYGAEADAILNDPSLTEAEKAAAIAAAAAKVQAELNAKLAEVEA` |
| design_002.cif (137,846 B) | same 129 aa target | **60 aa**, `MTPEEQAWKAEALAAVSKAKSDQEKRDAIGAAMTAYRQAHPDTPLERVLALEAEVLKAID` |

Both binders are exactly the requested `binder_length` 60, and they are different from each
other — so the run did its job. The same job's `export.csv` (129 B) correctly lists both
designs. **Only the FASTA is empty**, which rules out "the job produced nothing".

Repro: run pxdesign with any target, open the job page, fetch `export.fasta` (43 B, empty),
then follow the page's own `.cif` link for design_001 and read the chain B0 CA residues.
Note the `.cif` link must be taken from the page — a hand-built `/api/jobs/<id>/cif/...`
path returns `404`, `text/html`, 20,781 bytes, which parses to zero chains rather than
raising. I hit this trap myself mid-sweep; see the observation on wrong download paths.

This upgrades 30 Sep's P1-3, which recorded the stub but did not establish that real
sequences existed behind it.

### P1

**F-1 — The quote is wrong in both directions, by up to 21x one way and 1.3x the other.**
Measured this sweep: opendde $14.79 quoted / $0.71 charged (20.8x over); pxdesign $6.30 /
$0.57 (11.1x over); boltz2 $0.22 / $0.08 (2.8x over). Close on iggm (1.1x), rfdiffusion
(1.35x) and esmfold (1.5x).
It is **not** a flat multiplier applied everywhere: rfdiffusion, the longest run among
the first six, quoted within 35% of its real cost.
**And two runs went the other way.** BoltzGen quoted $6.07 and charged $7.72; BindCraft
quoted $6.29 and charged $8.00, which was its cap, against a metered $10.81 — 1.7x over
quote before the clamp. Both were the long runs, so the pattern across all eight is that
the estimator is calibrated for a run that behaves, over-quotes the short ones by an
order of magnitude, and under-quotes the ones that overrun. Over-quoting blocks users who
have the money; under-quoting bills them for more than they agreed to. pxdesign and
opendde are the two to look at first for the over-quote; F-18 covers the under-quote.
The hold is worse than the quote — opendde reserved $15.00 to spend 71 cents. A user
with $10 in the wallet cannot start a run that would cost them 71 cents. This is the
same class as 30 Sep's P1-5 (rfantibody, 7x) but three times the magnitude and on two
further tools, so it is systemic rather than one tool's constant. Note the hold is not
always generous either: where 1.5x the quote exceeds the tool's hard cap the hold *is*
the cap, which is how BindCraft's $6.29 quote became an $8.00 hold that was then spent in
full (F-17).
Repro: `/account/wallet/transactions`, compare each `reserved` row to its `hold release`.

**F-2 — FASTA headers carry verdict prose.**
Confirmed on three tools now — boltz2, rfdiffusion and boltzgen — so it is not one tool's
quirk. RFdiffusion's reads
`>rank3_design_003.pdb [does not meet bar: ipTM 0.260, below 0.65; pLDDT 67.9, below 80; i_pAE 16.23 Å, above 10 Å]`.
BoltzGen's reads
`>rank1_design_001.cif_chainA [does not meet bar: pLDDT 76.1, below 80; Refolding RMSD 2.18 Å, above 1.5 Å]`.
A FASTA header is an identifier: spaces, semicolons, commas and a non-ASCII `Å` inside it
break the record id for most downstream parsers, which truncate at the first space and
silently keep `>rank3_design_003.pdb`. Worse, the text is inconsistent *within one file* —
passing records carry no bracket at all (rfdiffusion ranks 1–2, boltzgen ranks 22/41/42),
so header shape varies row to row in the same download.

I previously filed a second half to this finding — "boltz2 echoes the user's own target
sequence" — and have **withdrawn it** on wider evidence. See W-7.

**F-17 — when the cap clamp bites, the ledger tells the customer "estimate matched actual".**
BindCraft's settlement row at `/account/wallet/transactions` reads:
`charge · bindcraft · net for this job $8.00 · estimate matched actual · $0.00`.
The estimate did not match the actual. The quote was $6.29, the metered cost was $10.81
(6186 GPU-s × $0.0017476), and the customer was charged $8.00 because the cap clamped it.
The same job's own page says so plainly: *"This run used more GPU time than estimated:
estimated $6.29, charged $8.00."* Two surfaces, one job, flatly contradicting.

This is not a coincidence — **the clamp manufactures the condition that selects that
string**, so it will fire on every clamped run:

* the hold row stores the *hold* amount in `estimated_cost_usd`
  (`0035_phase2_remove_daily_cap.sql`, `try_hold_for_job` inserts `p_amount_usd` into that
  column), and here the hold was $8.00 — the ledger's own `reserved` row confirms
  `−$8.00`, because 1.5 × $6.29 = $9.44 exceeds the $8.00 cap so the hold *is* the cap;
* `settle_hold` reads that column into `v_estimate`
  (`0020_wallet_corrections.sql`, head of the function);
* `v_capped_actual := LEAST(p_actual_usd, p_hard_cap_usd)` (`0020:188`) pulls $10.81 down
  to $8.00;
* `v_diff := v_estimate − v_capped_actual` (`0020:191`) is therefore $8.00 − $8.00 = 0;
* `v_diff = 0` takes the `ELSE` branch, whose note is the literal
  `'estimate matched actual'` (`0020:263`).

Whenever the hold equals the cap and the metered actual reaches it, `v_diff` is
*necessarily* zero. The branch was written for the case where the estimate was simply
right; the clamp routes the worst overruns into it.

**Second false line on the same job.** That job's `reserved` row reads "reserved for this
job; the unused part was returned when it settled". Nothing was returned — the whole
$8.00 was consumed. The template picks that wording on `ann.settled` alone
(`templates/wallet/transactions.html:137-142`) without checking whether any amount came
back.

The SQL carries the same claim as a comment (`0020:255`, "v_diff = 0: estimate matched
actual"), so the code says it too.

Scope: the net figure, the balance and the arithmetic are all correct. What is wrong is
the explanation attached to them, on precisely the runs where a customer most needs to
understand what happened. Filed P1 rather than P2 because it is money-facing prose that is
false, not merely unclear.

Repro: run any tool whose 1.5× hold would exceed its hard cap until the metered cost
reaches the cap; read the settlement row.

Re-checked once, live, at 22:2x UTC — reading the rendered table in the tab, not a
`fetch()` (W-6). All four rows verbatim:

```
2026-10-01 22:02 | HOLD RELEASE | boltzgen net for this job $7.72 true-up surplus released | +$1.38 | $970.31
2026-10-01 21:59 | CHARGE       | bindcraft net for this job $8.00 estimate matched actual | $0.00 | $968.93
2026-10-01 20:16 | RESERVED     | bindcraft reserved for this job; the unused part was returned when it settled | -$8.00 | $972.90
2026-10-01 20:16 | RESERVED     | boltzgen reserved for this job; the unused part was returned when it settled | -$9.10 | $980.90
```

The `-$8.00` reserved row is the independent confirmation that bindcraft's hold was its
cap and not 1.5× its quote. BoltzGen, which settled cleanly, shows the contrast: both
halves of its release row are true.

**F-18 — BindCraft's runtime estimate was 4.6x low, and it is low in the direction that costs the user money.**
Its preflight panel promised *"Estimated runtime: ≈22.3 min for 2 designs"*. The run took
6186 GPU-s — **103.1 min**.

The estimate reproduces exactly from the repo, so there is no doubt what produced it:
`shared/pdb_preflight_rules.py` gives bindcraft `runtime_base_min=40.0`,
`runtime_alpha=1.5`, `runtime_baseline_designs=4` at a 120 aa anchor, and
40 × (129/120)^1.5 × (2/4) = 22.3 min.

That curve rests on a single measurement, and the source comment says so in terms
(`shared/pdb_preflight_rules.py:617-648`): job `1c4d5803`, 1170 GPU-s for 2 designs
against a ~115 aa target — *"It is ONE POINT AND NOT A FIT, and it is the only point there
is."*

**This run is the second point, and it is 5.3x off the first.** 6186 GPU-s for 2 designs
against a 129 aa target is 3093 GPU-s/design against the anchor's 585. The target is 12%
larger; at the shipped `alpha=1.5` that accounts for a factor of 1.19, nowhere near 5.3.

*Measured* is the pair of points above. *Inferred*, and marked as such: the difference
between the two runs is that the anchor run produced designs and this one produced none,
and BindCraft samples trajectories until enough pass or the container times out — so a
target where nothing passes runs to the ceiling. If that is right, the estimate is
implicitly conditioned on success, and the quote derived from it under-quotes exactly the
case where the user receives nothing. I did not verify the sampling loop; it lives in
llm-proteinDesigner, not this repo.

The money followed: $6.29 quoted, $8.00 charged (the cap), $10.81 metered, so Ranomics
absorbed $2.81 and the customer paid $8.00 for zero designs. The caps worked as designed
(`shared/wallet_estimates.py:443-455` already says caps are not a ceiling on real spend) —
this is about the number shown to the user before they commit.

Repro: `/tools/bindcraft`, 1hew.pdb chain A, hotspots 62,63,101, binder 50–100,
num_designs 2; read the preflight runtime line, then the finished job's GPU-seconds.

### P2

**F-3 — ESMFold ships a PAE file its own tool page says does not exist.**
`/tools/esmfold` states: "Predicted PDB with per-residue pLDDT. No PAE (single-sequence
prediction has no inter-domain signal)." The results page offers "Download PAE (npz)",
and the file is a valid `(129, 129)` float16 PAE with 847 distinct values. The copy is
wrong and it undersells the product — users are told not to expect PAE, so they will not
look for it.

**F-4 — The preflight panel under-reports on boltz2 and iggm.**
Same `1hew.pdb` to all tools. boltzgen, bindcraft, pxdesign and rfdiffusion report
"129 residues" and "103 residues dropped … Other chain(s) dropped: B". boltz2 reports
"chain A — 0 residues" and both boltz2 and iggm report "Nothing to clean" — yet boltz2
still certifies "Hotspots 62, 63, 101 — all preserved" and asserts position == residue
number "On this file". The returned structure proves cleaning did happen (129-residue
chain, 0 HETATM), so this is a panel defect, not a run defect — but it certifies a file
it reports as empty.

**F-6 — ESMFold has no 3D viewer.**
Every other tool in the sweep renders its structure in-page. ESMFold's job page contains
no `molstar` reference and no `<canvas>` in 216,565 bytes of HTML. The only way to see
a structure you paid for is to download the PDB and open it elsewhere.

**F-7 — Two results pages link paying users to a 404.**
PXDesign's results page links "De novo protein design failure modes" and "Read explainer"
to `https://www.ranomics.com/resource-hub/de-novo-protein-design-failure-modes` → **404**.
ESMFold's links "ColabFold vs AlphaFold, when to use which" and "Read explainer" to
`https://www.ranomics.com/resource-hub/colabfold-vs-alphafold-when-to-use-which` → **404**.
Verified with `curl -L -o /dev/null -w "%{http_code}"`; the other eight outbound marketing
links from these pages all return 200. This is the reverse-funnel path, so it fails at the
exact moment a user has just finished a successful paid run.

**F-8 — ESMFold's cost panel contradicts itself.**
The panel displays "$0.08" while "BALANCE AFTER THIS JOB" subtracts $0.07 from $966.23 to
reach $966.16. The API value is $0.0728 — one line rounds up, the other down.

**F-9 — Page `<title>` separator is inconsistent.**
`RFdiffusion -- Ranomics Tools` (two hyphens), `ESMFold - Ranomics Tools` (one hyphen),
and an em dash elsewhere. 30 Sep filed this as P2-7 against proteina alone; it is broader.

**F-10 — Static runtime copy contradicts the per-run preflight on the same page.**
Every trial page says "This trial run takes 15-60 min on a real GPU" while the preflight
on the same page estimates boltzgen ~92.5 min, bindcraft ~22.3 min, pxdesign ~5 min,
rfdiffusion ~49.7 min. OpenDDE actually finished in 3 minutes.

**F-11 — The "Load these settings →" trial link never yields a runnable example.**
boltz2 flips a preset radio only; iggm and pxdesign change nothing; bindcraft drops
num_designs 4→2 and the quote $12.59→$6.30. On all of them the required target and
hotspot fields stay empty, so the advertised one-click trial cannot be submitted.

**F-12 — A failed run's ledger line loses its tool name.**
Successful settles read `esmfold net for this job $0.05 true-up surplus released`.
Full-refund rows read `net for this job $0.00 pipeline` — no tool name, and the sentence
stops mid-clause. Three instances in the ledger (2026-10-01 01:12, 2026-09-30 21:52,
2026-09-30 14:53), each +$15.00. These rows belong to other sessions' runs, not mine.
Carried forward from 30 Sep P2-8, still live.

### F-16 (P2) — the progress line is rendered twice from two different sources, and they can disagree

Observed in the live DOM at 21:16 UTC, both jobs still running:

- BindCraft: `Running BindCraft - 0/2 designs (0/2)`
- BoltzGen: `Running BoltzGen (0/50)`

`status.json` shows why. It returns `progress.stage`, a string the tool supplies, and
separately `progress.designs_completed` / `progress.designs_total`. The template prints
the stage string, then appends `(completed/total)` of its own. BindCraft's stage string
already ends in `0/2 designs`, so the count renders twice in one line. BoltzGen's stage
string carries no count, so it renders once.

Right now the two agree, because nothing has completed and both are 0. They come from
different sources, so they are free to disagree — and that is what was seen on
RFdiffusion job `dc43476a` mid-run: `Running AF2 validation - 9/10 designs (8/10)`
alongside `8 of 10 designs complete`. I did not see that myself and the job has since
finished, so **the disagreeing values stay unverified**. The duplicated rendering that
makes a disagreement possible is verified, live, on BindCraft.

Repro: `GET /jobs/<running bindcraft job>/status.json`, compare `progress.stage` against
`progress.designs_completed`/`designs_total`, then read the rendered line in the live DOM.

### F-14 (P2) — on the two tools with no candidates table, all three export endpoints return 200 and nothing

`/jobs/08df0826.../export.csv` is 26 bytes, the header row `rank,pdb_key,source_rank`
and no data. `export.fasta` is the 43-byte stub `# No sequences found in this job's
output.` — for a job whose *input* was a sequence. `export.zip` is 22 bytes, signature
`50 4b 05 06`, an empty-archive end-of-central-directory record and nothing else. All
three answer 200.

Scope limiter, and the reason this is P2 and not higher: **the ESMFold results page
links to none of them.** Its only downloads are the two `data:` URIs, "Download PDB" and
"Download PAE (npz)", and those carry the real output. A user clicking around the page
cannot reach the empty files. They are reachable by typing the URL, and by anything that
builds export URLs from a job id the way the other seven tools' pages do.

**BindCraft generalises it from one tool to two.** Its run succeeded and produced zero
candidates, and its three exports are the same three empty bodies at the same three
status codes: `export.csv` 26 B, `export.fasta` 43 B, `export.zip` 22 B. Its page also
links to none of them — unlike every tool that produced candidates, it renders no
download controls at all.

So the behaviour is not an ESMFold quirk. It is what the export routes do whenever the
candidates set is empty, whether because the tool never fills one (esmfold) or because
the run filtered everything out (bindcraft). An empty ZIP that answers 200 is
indistinguishable, to a script, from a ZIP that downloaded fine.

Repro: `GET /jobs/<id>/export.{csv,fasta,zip}` signed in on job `08df0826` (esmfold) or
`489a17e0` (bindcraft); compare with the same three paths on any tool that has a
candidates table.

### F-15 (P2) — the "designs returned" count has four different labels, and is missing on the two tools where it matters

My first read of this was "RFdiffusion is missing a line that esmfold2-design has". That
was too narrow — checking all seven pages, the line exists on four of them, under four
different labels:

| tool | label | value |
|---|---|---|
| esmfold2-design ‡ | "Designs returned" | 3 / 3 |
| opendde | "Predictions returned" | 2 / 2 |
| boltz2 | "Designs folded" | 1 / 1 |
| iggm | "Designs" | 1 / 1 |
| **rfdiffusion** | **absent** | — |
| **pxdesign** | **absent** | — |
| **boltzgen** | **absent** (prints `CANDIDATES (50)` only) | — |
| esmfold | absent (no candidates table; single structure) | n/a |
| bindcraft | absent (zero candidates) | n/a |

So it is not one tool missing a line. Four tools each invented their own wording for the
same number, and the three that print nothing are **rfdiffusion, the only run in the
sweep that asked for ten designs; pxdesign, the tool that silently dropped every sequence
it designed (F-5); and boltzgen, which returned fifty candidates and shows only the
header `CANDIDATES (50)` — a count of what came back, with nothing to compare it to**. On the four tools that do show it the count is trivially 1/1, 2/2 or
3/3 and nobody needed it. On the three that don't, it is the number a user would actually
want — and on pxdesign it is the number that would have exposed the P0 from the page
alone, without opening a single file.

Repro: open each finished job page and search the rendered text for `N / M`.

Related: 30 Sep's P1-4 (rfantibody returned 5x the designs requested) is exactly the
class of defect this line would surface. It is absent on the tool most able to show it.

---

## Structure access, per tool (asked for separately, read-only)

Measured on each finished page by reading the DOM, not by clicking: count of rows in the
candidates table, count of "View 3D" controls, count of rows carrying a `.pdb`/`.cif`
link, count of cells rendering `—`, and the byte length of each of the three exports.

| tool | rows | "View 3D" | rows w/ structure link | `—` cells | CSV | FASTA | ZIP |
|---|---|---|---|---|---|---|---|
| rfdiffusion | 10 | 10 | 10 | 0 | 561 B | 1,593 B | 244,064 B |
| opendde | 2 | 2 | 2 | 0 | 251 B | 1,136 B | 96,088 B |
| pxdesign | 2 | 2 | 2 | 0 | 129 B | **43 B** | 76,795 B |
| boltz2 | 1 | 1 | 1 | 0 | 188 B | 433 B | 36,165 B |
| iggm | 1 | 1 | 1 | 0 | 98 B | 451 B | 29,620 B |
| boltzgen | 50 | 50 | 50 | 0 | 2,618 B | 17,469 B | 2,056,727 B |
| esmfold | **0** | **0** | **0** | 0 | **26 B** | **43 B** | **22 B** |
| bindcraft | **0** | **0** | **0** | 0 | **26 B** | **43 B** | **22 B** |
| *esmfold2-design* ‡ | 3 | 3 | 3 | 0 | 357 B | 671 B | 142,437 B |

‡ not my run — job `26c1f866`, another worker's. Page read only, nothing submitted,
clicked or settled; included because it was the requested comparison and it is the only
succeeded esmfold2-design job on the account. Its three failed siblings have no results.

**On every finished page that has a candidates table, every row has both a working
"View 3D" and a per-design structure download, and no row renders `—`.** Verified open
and parsed on rfdiffusion (PDB 116,154 B → chain A 129 aa target + chain B 57 aa binder),
pxdesign (CIF 139,088 B and 137,846 B, 20-column `_atom_site` loop, two real 60 aa
binders), boltzgen (`design_001.cif` 131,393 B, 19-column loop, 1,404 atoms, chain A 129
aa target + chain B 57 aa binder; ZIP 2,056,727 B holding 50 entries) and boltz2.

Three carve-outs, all already filed above:

- **ESMFold has no table at all** (F-6 for the missing viewer, F-14 for the empty
  exports). It is a single-structure folding tool, so there is nothing to put rows of —
  but the consequence is no 3D view on the page and no working export path.
- **BindCraft returned zero candidates**, so its page has no table, no viewer and no
  download controls, and its three export endpoints are the same empty bodies (F-14).
  Nothing here is wrong given an empty result set; it is listed so the matrix is complete.
- **PXDesign's FASTA is the 43-byte stub** while its CIFs hold two real designed
  sequences. That is the P0, F-5. Its ZIP and per-design links are fine.

The `—` in the 3D column that Leo saw is confined to the **running** page. Both long
jobs have since finished, and neither gave me a chance to re-observe it: BoltzGen showed
no candidates table at all until it completed, then showed fifty complete rows at once
(50 "View 3D", 50 structure links, zero `—`), and BindCraft never produced a table.
So a partial row mid-stream remains unreproduced by me, and **every finished page in the
sweep is clean**. I am not re-running a finished job to chase it.

Likewise the two disagreeing counters seen mid-run ("Running AF2 validation — 9/10
designs (8/10)" against "8 of 10 designs complete"): **the finished RFdiffusion page has
nothing comparable, because it prints no design count at all** — see F-15. Nothing can
disagree there. The same is true of the finished BoltzGen page, which prints only the
header `CANDIDATES (50)`. The mid-run disagreement is unverified by me and stays that
way: the job has finished and I am not re-running it. What I did verify is the live-DOM
half of the same mechanism, filed as F-16 — two progress strings rendered from two
sources, which is the likeliest explanation of what Leo saw.

### Observations (not filed as defects)

- **IgGM returns designed antibody chains backbone-only.** Atom types exactly C, CA, N;
  348 atoms for 116 residues, against 1000 atoms for the 129-residue antigen. This may be
  intended for a backbone-design model, but nothing in the UI says the side chains are absent.
- **OpenDDE CSV `runtime_seconds` is 1–2 s per design** against 172 s of job GPU time. The
  two numbers cannot both describe the same work; cause not established.
- **A wrong download path returns 20,749 bytes of HTML with status 404**, not an error a
  parser would notice. Cost me one silently-empty parse before I spotted it.
- **A running job page shows no cost at all.** Scanned the live DOM of both long-running
  jobs for a currency figure: none. A user watching a job that has been going for over an
  hour can see the quote they were given at submit and the hold in their wallet, but not
  what the run has accrued. Not filed as a defect — the hold is visible at
  `/account/wallet/transactions` and bounds the exposure — but it is why every in-flight
  cost figure in this report is extrapolated rather than read.
- **Both long jobs exceeded the runtime their own page advertises.** The status block says
  "This trial run takes 15-60 min on a real GPU"; BoltzGen finished at 106.0 min and
  BindCraft at 103.1 min, both having sat at 0 designs well past the 60-minute figure.
  BindCraft's preflight promise is now filed as F-18, since its number is derived in this
  repo and was 4.6x low. The "15-60 min" copy is a shared status-block string, not a
  per-tool estimate, so it is left as an observation. Whether an enforced ceiling exists
  behind either is not established — out of scope for a report-only pass.
- **ESMFold's downloads are `data:` URIs**, roughly 143 KB of base64 inlined into the job
  page, where every other tool serves structures from `/api/jobs/<id>/...`.

---

## The two long runs: accrual against quote, hold and cap

Both BoltzGen and BindCraft overran every runtime figure the product shows. Tracking them
raised a money question worth recording with its sources, because the answer is
reassuring and was not obvious from the UI.

**Rates, verified in this repo** (not taken on trust): `WALLET_MARKUP = 1.70` at
`shared/wallet.py:74`; `A100-40GB 0.000714`, `A100-80GB 0.001028` at
`shared/wallet.py:183-184`. Per-tool class and cap at `shared/wallet_estimates.py`:
bindcraft `gpu_class="A100-80GB"`, `base_hard_cap_usd=8.00` (:360-362); boltzgen
`gpu_class="A100-40GB"` (:431-433). So the customer rate is $0.0017476/GPU-s for
bindcraft and $0.0012138/GPU-s for boltzgen.

**The charge is clamped at the per-tool hard cap, and the clamp is real.**
`shared/wallet.py:46` asserts it; enforcement is in SQL, not Python — `settle` passes
`p_hard_cap_usd` through at `shared/wallet.py:875-884` and
`supabase/migrations/0020_wallet_corrections.sql:188` does
`v_capped_actual := LEAST(p_actual_usd, p_hard_cap_usd)`. Checked for a later
redefinition: `0035` replaces only `try_hold_for_job`, and `0036`–`0044` do not touch
settle.

**Confirmed against the ledger, which is what this section was waiting for.** BindCraft
billed 6186 GPU-s, which metered at $0.0017476/GPU-s is **$10.81**. The ledger row at
`/account/wallet/transactions` reads `charge · bindcraft · net for this job $8.00`. The
shipped database does clamp, exactly as the migration says. Ranomics absorbed $2.81 on
this one job. The clamp is no longer an inference.

**Decision on the overrun.** Both jobs were left to run to their own ceilings rather than
cancelled. Reported to me at 21:40 UTC by the hub-lead session as Leo's decision — I did
not receive it from Leo directly, so it is recorded here as second-hand. It changed
nothing I did: cancelling would have been the action, letting a running job finish is the
default, and the sweep's brief forbids retries either way. The clamp finding below was
independently reproduced by that session (`settle_hold` is defined only in `0017` and
`0020`).

**Accrual observed** (`/jobs/<id>/status.json`, the endpoint the page itself polls; the
`/api/jobs/<id>/status` path is one of the 404-HTML traps):

| UTC | BoltzGen GPU-s | BindCraft GPU-s |
|---|---|---|
| 21:16:34 | 3329 | 3565 |
| 21:22:48 | 3932 | 3933 |
| 21:27:08 | 4233 | 4177 |
| 21:34:00 | 4534 | 4604 |
| final | **6360** (106.0 min) | **6186** (103.1 min) |

Both sat at 0 designs complete and 0 partial candidates throughout the sampled window.
BoltzGen's fifty candidates all materialised at the end; BindCraft's never did.

**One self-correction.** A single interval read 1.61 GPU-s per wall second on BoltzGen
and I briefly inferred a second device. The next interval read 1.16 and the cumulative
ratio never left ~1.0 (4534 GPU-s against 4540 s of wall clock at 21:34). The counter
updates in lumps; any one short interval over- or under-reads it. Only the cumulative
ratio is meaningful, and it says one GPU at essentially 100% duty on each job.

**Outcome, both terminal.** Both predictions held.

| | BoltzGen `7dd2b723` | BindCraft `489a17e0` |
|---|---|---|
| state | SUCCEEDED, 50 candidates | SUCCEEDED, **0 candidates** |
| GPU-s / wall | 6360 / 106.0 min | 6186 / 103.1 min |
| metered at tool rate | $7.72 | $10.81 |
| quote → hold → charged | $6.07 → $9.10 → **$7.72** | $6.29 → $8.00 → **$8.00** |
| cap | $10.00, not reached | $8.00, **clamped** |
| ledger row | `hold release` · `true-up surplus released` · **+$1.38** | `charge` · `estimate matched actual` · $0.00 |

BindCraft crossed its $6.29 quote at ~3600 GPU-s and its $8.00 cap at ~4578 GPU-s, around
21:34, then ran a further 1600 GPU-s the customer did not pay for. BoltzGen finished
under both its hold and its cap, so it posted a straightforward surplus release and no
true-up. **Neither job exceeded the money held at submit**, which was the question.

These are also the only two runs in the sweep that cost *more* than quoted, which is why
they are the only two where the over-estimate notice could appear — and it did, on both
job pages, with correct figures. That retires one of the sweep's Unverified items.

The timeout values (bindcraft 14400 s, boltzgen 6600 s) come from llm-proteinDesigner,
which is not this repo — **unverified here**; note BoltzGen's 6360 GPU-s lands just under
the 6600 s figure, consistent with but not proof of a ceiling.

Two findings came out of these two runs and are filed above: **F-17**, the clamped
settlement telling the customer "estimate matched actual", and **F-18**, BindCraft's
4.6x-low runtime estimate.

The wider point is not the wallet. The same source comment notes boltzgen's container can
run to 82800 s, which at the 40GB rate would bill $100.50, ten times `base_hard_cap`
(`shared/wallet_estimates.py:443-455`). The caps bound what the *customer* pays; Ranomics
absorbs the remainder. A tool that burns an hour of A100 to produce zero designs is a
cost-of-goods problem even when the invoice looks fine.

---

## Withdrawn

Flagged during the sweep, then disproved before reporting.

**W-7 — "Boltz-2's FASTA echoes the user's own target sequence back as a design." (was half of F-2)**
Filed as the second half of F-2, then disproved by checking the other tools instead of
one. The claim was that boltz2 returning two records — a 129 aa chain A identical to the
1HEW input and a 20 aa chain B — padded a one-design result into something that looks
like two designs. Checking every FASTA in the sweep kills it:

| tool | records | shape |
|---|---|---|
| boltz2 | 2 | `_chainA` (129 aa target) + `_chainB` (20 aa binder) |
| boltzgen | 100 | 50 x `_chainA` (129 aa) + 50 x `_chainB` (51–69 aa) |
| opendde | 4 | `_chainA` + `_chainB` per design |
| iggm | 3 | `_chainH`, `_chainL`, `_chainG` |
| rfdiffusion | 10 | no chain suffix — binder-only output |

Four tools export **every chain of the complex, each one chain-labelled in its header**,
and the one tool whose output is binder-only exports exactly ten unlabelled records for
ten designs. That is uniform, self-describing behaviour: the FASTA mirrors the structure
file. A parser reading `>rank1_design_001.cif_chainA` has been told which chain it holds.
Calling it an "echo" was a boltz2-shaped reading of a four-tool convention.

It does narrow 30 Sep's P1-8 rather than confirming it: the record count is not a design
count on any tool that returns complexes, and nothing in the UI says so — but that is a
labelling gap at most, not the duplication I described. The surviving half of F-2, the
verdict prose inside the header, is filed above and is confirmed on three tools.

**W-6 — "A running job's only progress line says the pipeline has not started, for the
whole run." (was F-13)**
Filed, then disproved by the same mistake that produced W-1 on 30 Sep, in a new disguise.
I read the job pages with `fetch()` + `DOMParser` instead of from the live tab. That
returns the server-rendered HTML **before the page's JavaScript hydrates it**, and in
that HTML the progress line is the placeholder `Waiting for pipeline to start...`. I read
it that way three times — RFdiffusion at 20:48, then BoltzGen and BindCraft at 21:12 —
and each time concluded the line was frozen. It never was. Read from the live DOM at
21:16, with both jobs still running, the same two pages show `Running BoltzGen (0/50)`
and `Running BindCraft - 0/2 designs (0/2)`. The page updates, and `status.json` was
serving a correct `stage` the whole time. The 20:48 RFdiffusion reading cannot be
re-observed and I will not re-run a finished job, so I cannot prove that one was the same
artefact — but it was taken the same way, so I am withdrawing it rather than reporting
it. Lesson, identical to W-1: a `fetch()` of a page is not the page. Any claim about
rendered state has to come from the live DOM.

**W-1 — "Boltz-2 reports Hotspot hits 0 of 3 for a hotspot-steered run."**
Not a defect; the count is right. The predicted pose sits 11.91 Å (62), 11.77 Å (63) and
18.71 Å (101) from the requested hotspots and contacts residues
[33, 34, 35, 36, 37, 44, 46, 52, 57, 110, 114] instead. Boltz-2 is a co-folding predictor,
not a steered designer — it is reporting honestly that the pose it predicted ignores the
hotspots. Measured from the returned structure.

**W-2 — "IgGM's per-residue epitope grid renders blank cells."**
Not a defect. `textContent` is empty by design; the cells differ in markup
(innerHTML length 120 vs 150). The marked cells are at indices 2, 3, 11, 12, 16 → residues
{19, 22, 116, 117, 121}, which is exactly the contact set I computed independently at a 5 Å
cutoff from the returned structure. The displayed "5 / 21" is correct, and the hub's contact
computation is independently validated.

**W-3 — "BoltzGen's flat pricing across budget values is a pricing bug."**
Not a defect. One shard is one container; the band is intentional
(`tools/boltzgen/meta.py`, `shared/compute_campaigns.py`).

**W-4 — "The job page leaks raw CSS into its visible text."**
Not a defect. `.preflight-panel { … }` does appear in the page's `textContent`, but it sits
in a proper `<style>` element nested inside `<main>` — `textContent` always includes style
rules, and no text-bearing leaf element renders it. Checked directly: no element other than
`<style>` contains the rule.

**W-5 — "OpenDDE shows sample-1 scores for every sample."**
Not a defect; the #245 fix holds. Per-sample values genuinely differ
(pTM 0.9278 vs 0.9274, ipTM 0.9377 vs 0.9373).

Seven withdrawn against seventeen filed. 30 Sep withdrew three. The recurring trap is the
same one that produced its withdrawn W-1 — reading an instrument's silence as the
product's failure. W-1 (a score that looked wrong until the geometry was measured),
W-2 (cells that looked blank to `textContent`) and W-4 (CSS that looked leaked to
`textContent`) are all that mistake. The RFdiffusion viewer would have been a fourth:
it sat at "Loading 3D viewer..." indefinitely until the tab was brought to the front.

**W-6 is the sharpest instance and the one to learn from**, because it survived three
separate readings on three different jobs before I checked it properly. Every one of
those readings used `fetch()` instead of the live tab, so every one returned the same
pre-hydration placeholder, and the agreement between them read as corroboration. Three
consistent measurements from one broken instrument are one measurement. The fix is cheap
and should be the default: for any claim about what a page *shows*, read
`document` in the tab, not a response body.

---

## Unverified

- **Email notices.** No mailbox access from this session. Whether completion or
  over-estimate emails were sent, and what they contain, is untested. Same gap as 30 Sep U-4.
- ~~**The over-estimate notice.**~~ **Now verified, positive case included.** The first six
  runs all came in under quote and correctly showed nothing. The two long runs both came
  in over, and both job pages showed the notice with correct figures — boltzgen
  "estimated $6.07, charged $7.72", bindcraft "estimated $6.29, charged $8.00". What is
  *not* verified is the email version of the same notice (see the first bullet).
- **Failed-run page advice.** All eight runs reached SUCCEEDED, so the failed-run page was
  never reached — including BindCraft, which succeeded with zero candidates and so renders
  the normal results page, not the failure page. F-12's evidence comes from other sessions'
  ledger rows, not from a failure I saw.
- **OpenDDE `runtime_seconds`.** Discrepancy recorded, cause not investigated.
- **Why BindCraft burned 103 minutes for nothing.** The two-point comparison in F-18 is
  measured; the explanation — that the sampler runs to its ceiling when no design passes
  — is reasoned from the outcome, not read. The sampling loop is in llm-proteinDesigner,
  not this repo.
- **Any ceiling behind the "15-60 min" copy.** Both long runs passed it without being
  stopped; whether an enforced timeout exists and at what value is not established here
  (the 14400 s / 6600 s figures come from another repo).
- **Whether F-1's over-quoting is deliberate headroom.** Measured from the ledger; the
  intent behind the estimator was not read.

---

## For Leo

Eight tools, eight paid runs, all finished. Seven returned usable results; one returned
nothing and still charged the full amount.

**Where we are.** The sweep is done and cost about twenty dollars of the forty-four you
approved. One serious problem: a design tool throws away the sequences it just made, so
the customer gets structures but nothing they can order. Two money problems: the price
shown before you run is often ten to twenty times too high, which blocks people who can
afford the job, and on the two long runs it was too low. And when a run hits its price
ceiling, the receipt tells the customer the estimate was correct when it plainly was not.

**Blockers.** None. Nothing was changed, nothing is waiting on you.

**Next steps.** Fix the missing sequence file first, then the receipt wording, then the
pricing estimates.
