# Scout: replacing the phi/psi SS fallback with in-process DSSP

**Date:** 2026-08-21
**Change:** `scout/scoring.py::assign_dssp` gains a middle branch, `pydssp`,
between the mkdssp binary and the phi/psi Ramachandran fallback.
**Status:** coded, NOT shipped.

---

## 0. Why this exists

`docs/qc/scout-dssp-fallback-measurement.md` established that the phi/psi
fallback — the branch that actually runs in production — agrees with real
DSSP on about 70% of residues, and that the user-visible consequence is the
`secondary_structure` column being wrong roughly half the time. It recovers
only **33.9%** of true loops, calling the other two thirds helix or strand.

That doc also argued *against* fixing this by recalibrating the Ramachandran
thresholds, on the grounds that phi/psi alone carries r=0.37 against DSSP
because it has no hydrogen-bond information. **That argument is correct and
is not challenged here.** It is precisely the reason this change does not
touch the thresholds: rather than tune a signal that lacks the necessary
information, it supplies the missing information. `pydssp` computes the
electrostatic H-bond map and reads helices and bridge ladders off it — the
real DSSP algorithm, just without the binary.

The parallel option, installing `mkdssp`, was examined in
`docs/qc/scout-dssp-install-decision.md` and blocked: Railway builds this
service with **Railpack via mise**, which does not read `nixpacks.toml`, so
the obvious fix is a silent no-op. Section 6 shows the binary would not have covered every input either:
mkdssp 4.2.2 refuses headerless files that pydssp reads. That is a *coverage*
argument only -- mkdssp is the oracle here, so on accuracy the binary would
still have won by the 2.1 points pydssp gives up.

---

## 1. What was measured

Oracle: **mkdssp 4.2.2**, the same binary and the same 30-chain set used for
the original fallback measurement, invoked through the same WSL shim. DSSP
codes folded H/G/I to helix, E/B to strand, everything else to loop.

**phi/psi is the control.** If the harness does not reproduce the published
0.702 for the existing branch, it is not comparable to the earlier doc and
the pydssp figure means nothing. It reproduces it to four decimals.

Both arms were driven through the **real shipped functions**
(`scout.scoring._assign_ss_by_phi_psi` and
`scout.scoring._assign_ss_by_pydssp`), not a harness reimplementation.

### Per-residue, n = 4487 residues over 30 chains

| | phi/psi (production today) | pydssp (this change) |
|---|---|---|
| **agreement with mkdssp** | 0.7018 *(doc says 0.702)* | **0.9786** |
| helix recall / precision | 0.974 / 0.763 | 0.981 / 0.985 |
| strand recall / precision | 0.940 / 0.559 | 0.971 / 0.984 |
| **loop recall / precision** | **0.339** / 0.874 | **0.981** / 0.970 |

The loop row is the defect. The generous Ramachandran windows swallow two
thirds of all true loops into helix or strand, which is what drags strand
precision to 0.559 and makes the displayed label unreliable.

### Per-patch — what the user actually sees

Driven through `run_pipeline` with each SS arm injected, 281 patches over the
same 30 chains (29 structures; 6M0J supplies two):

| | phi/psi | pydssp |
|---|---|---|
| displayed `secondary_structure` correct | 49.8% (140/281) | **97.2% (273/281)** |
| top-1 epitope matches truth | 19/30 | **27/30** |
| top-3 set matches truth | 14/30 | **25/30** |

Cross-check against the earlier doc: top-1 differing on 11/30 matches its
"**11 / 30 chains (37%)**" exactly, and the per-residue 0.7018 matches its
70.2% exactly. The label figure lands at 49.8% against its stated 50.4% — a
0.6-point gap that is **not explained**. It is *not* patch-boundary jitter:
both runs report the same 281 patches and clustering runs before SS is
assigned, so the boundaries provably did not move; and 50.4% x 281 = 141.6 is
not an integer, so the earlier figure is not a count over 281 and the two
denominate different things. Left unresolved. The comparison rests on the two
control figures above, which do reproduce exactly.

(Line numbers are deliberately not cited into that document: this very commit
added a banner to it and shifted every line by 14.)

---

## 2. Ranking churn — the product consequence

Measured directly, `phi_psi` output against `pydssp` output on the same 30
chains — not inferred from the two truth comparisons:

| | unchanged | **changes** |
|---|---|---|
| top-1 epitope | 18/30 | **12/30 (40%)** |
| top-3 set | 12/30 | **18/30 (60%)** |

So **60% of analyses would return a different top-3 set** than they do today.
That is a correction, not a regression — today's ranking matches mkdssp truth
on 14/30 structures, pydssp on 25/30 — but it is user-visible, and anyone
comparing against a saved result will see different ranks.

For scale, this is the same order as the churn the earlier doc attributed to
the fallback itself: it recorded the phi/psi top-3 set differing from DSSP
truth on 16/30 (53%). The defect was always this large; adopting pydssp makes
the change visible rather than introducing it.

There is no version of fixing this defect that does not move rankings. The
alternative considered — relabelling without touching the ranking — would
have left the ranking wrong on purpose while making the label claim it was
right. That option was dropped for this reason.

### The shift rank churn does not capture

Ranking is relative; two user-visible things are absolute, and a uniform
downward shift moves both while leaving ranks untouched.

`_continuous_ss_score` spans 0.20 (loop) to 1.00 (strand) and carries
`_COMPOSITE_SS_WEIGHT = 0.20`, so the SS term alone can move a composite by
0.16. Correcting a systematic over-call of helix/strand therefore pushes
composites **down**, and `scout/routes.py` filters displayed epitopes at an
absolute `_MIN_COMPOSITE = 0.40`.

Measured over the three distinct bundled structures (1HEW, 3ave, 3s7g;
57 patches), driving this repo's own `cluster_surface_residues` with a
CA-density surface proxy because `freesasa` is absent from the dev venv:

| Δ composite from the SS term | |
|---|---|
| mean | **−0.053** |
| median | −0.048 |
| patches that lose score | 47/57 (82.5%) |
| lose more than 0.05 | 27/57 (47.4%) |
| lose more than 0.10 | 9/57 (15.8%) |
| largest single drop | **−0.160** |
| largest single gain | +0.008 |

The shift is one-directional: essentially every patch loses, none gains
materially. An epitope sitting just above the floor — within the shift, so
roughly [0.40, 0.45) at the mean, though the per-patch loss varies — can
therefore drop out of the displayed set. `top3` is drawn only from candidates
clearing the floor, so a structure whose patches all sit near it can return
fewer epitopes, or none.

Two things bound how much this matters. The empty case is handled rather than
left stale: `epitopes_annotated.csv` is written when `top3` is non-empty and
`_unlink_quietly`'d when it is not. And the floor gates only the *displayed*
set — `results_annotated.csv` is written from `all_epitopes` unfiltered, so a
sub-floor epitope is still in the download. What changes is what Scout puts in
front of the user.

The displayed label and its quality flag move with it. `compute_quality_flags`
raises **"loop-only anchor"** on `secondary_structure == "loop"`:

| over the same 57 patches | before | after |
|---|---|---|
| displayed `secondary_structure` unchanged | — | 28/57 (49.1%) |
| patches flagged "loop-only anchor" | 11/57 (19%) | **40/57 (70%)** |

Surfaces are loop-rich, so 70% is the honest number and 19% was the artefact
— but a user comparing against a saved report sees a warning appear on half
their epitopes.

Both figures corroborate the earlier 30-chain measurement independently: it
recorded mean |Δ composite| 0.046 (max 0.154) and displayed-label agreement
50.4%, against 0.053 / 0.160 and 49.1% here on a different corpus and code
path.

**`_MIN_COMPOSITE` is left at 0.40 by this change.** Moving a user-visible
display threshold is a product decision, and the fallback-measurement doc's
argument against recalibration (r = 0.37 without hydrogen-bond information)
is specifically about the *phi/psi angle windows*, not about this floor — it
does not settle the question either way. What is no longer true is the
premise the floor inherited: it was chosen while every SS score carried a
+0.23 bias.

### Gap damage scales with junction COUNT, not residues lost

A residue the file never resolved, or one excluded as non-standard, never
enters the standard-residue count, so the completeness guard sees no mismatch
and the coordinate array closes over it. DSSP finds turns and bridges at fixed
array offsets (i to i+3/4/5), so those offsets now run on renumbered indices —
one junction perturbs every offset that spans it, whether the junction swallowed
one residue or fifty.

Measured on `1HEW:A` against its vendored mkdssp oracle (intact agreement
0.969), removing the same residues two ways:

| residues removed | junctions | agreement | delta |
|---|---|---|---|
| one 12-residue block | 1 | 0.957 | −0.012 |
| 12 scattered singles | 12 | 0.897 | **−0.072** |
| one 24-residue block | 1 | 0.943 | −0.026 |
| 24 scattered singles | 24 | 0.857 | **−0.112** |

Identical residue loss, **six times the damage** when it arrives as separate
junctions — roughly 0.5–0.6 points per junction, near-linear. An independent
QC pass reached the same law on a different corpus (−0.5 to −0.8 pp per
junction; a 26-junction chain losing 19.4 points).

**The common trigger is selenomethionine.** `STANDARD_AA` has 20 entries and
`MSE` is not among them, so every SeMet residue is a junction — and SeMet
phasing is routine in the PDB. The same QC pass measured `3fhk:A` falling
99.3% → 89.9% with its 9 MSE residues, and `2i39:A` 100% → 90.9% with 7.

Two things follow. This is a real cost of adopting an algorithm that reads
fixed sequence offsets, where the phi/psi branch it replaces computes each
dihedral locally and is barely affected by a junction. And `ss_method` still
reads `"pydssp"` throughout: the run carries no signal that its labels were
degraded. **Not fixed here** — mapping `MSE` to `MET` would close most of it,
but residue selection is shared with `compute_rsa` and patch construction, so
changing it late in this branch risks misaligning keys across modules. It is
the strongest follow-up candidate.

---

## 3. Vendoring, and why not `pip install pydssp`

Upstream is **PyDSSP 0.9.1**, MIT licensed, (c) 2022 Shintaro Minami,
<https://github.com/ShintaroMinami/PyDSSP>.

`pip install pydssp` is **not** viable: the package declares `torch` as a
hard install dependency for its backend dispatcher. That is roughly 800 MB
pulled onto the web dyno to run a 113-line numpy routine that never touches
it. Only `pydssp/pydssp_numpy.py` is needed, so that file is vendored to
`scout/pydssp_numpy.py` with attribution.

**No new dependency is added.** `numpy` is already in `requirements.txt`.

### The edits, and their proof

Upstream imports `einops` for **eleven** calls -- 8 `repeat` (upstream lines
21, 50-53, 67, 71-72) and 3 `rearrange` (83, 100, 112). Adding a second
dependency for eleven broadcast operations is not worth it, so they were
rewritten as numpy broadcasting and `swapaxes`. Every `repeat` fed an
elementwise operation that broadcasts identically, so this is a no-op —
**verified, not assumed**:

```
chains compared : 31
residues        : 4552
mismatches      : 0
IDENTICAL -- einops removal is a no-op
```

Compared with **exact array equality** (not tolerance), on three surfaces:
the final labels; the float H-bond map underneath, where drift would appear
first; and the batched `[batch, L, atom, xyz]` path that Scout never uses but
whose shape branches still have to be right.

`test_vendored_pydssp_needs_no_einops` guards this by importing the module
with `einops` poisoned — testing the property rather than grepping for the
word, which would false-positive on the comments documenting the change.

One further, non-einops edit: upstream's
`np.clip(cutoff - margin - e, a_min=-margin, a_max=margin)` is written
positionally as `np.clip(cutoff - margin - e, -margin, margin)`
(`scout/pydssp_numpy.py:107`). Behaviour-identical, but it *is* a second
difference from upstream, and this file's whole justification is that it stays
diffable -- so it is recorded rather than described as "the only edit".

**The vendored file is kept a faithful copy.** It is deliberately not
refactored to taste (the unused batch dimension and the unused `donor_mask`
proline hook both stay) so it remains diffable against upstream.

---

## 4. Branch order, and why phi/psi survives

```
mkdssp binary   ->  "dssp"
      | unavailable / raises / empty result
pydssp          ->  "pydssp"     <- the normal path in production
      | empty result
phi/psi         ->  "phi_psi"
      | empty result
{}              ->  "none"
```

A branch is skipped when it raises **or** returns no labels, so an unreadable
structure falls through instead of pinning `ss_method` to a branch that
measured nothing.

phi/psi is **not deleted**, because DSSP needs the carbonyl oxygen and
dihedrals do not. An **O-stripped** model yields nothing from pydssp and still
assigns under phi/psi (measured on 1HEW: pydssp 0, phi/psi 129). Covered by
`test_phi_psi_still_covers_backbones_pydssp_cannot_read`.

A **CA-only** model is *not* covered by either branch — PPBuilder needs C and
N, so phi/psi returns `{}` as well and `ss_method` is `"none"` (measured).
Earlier drafts of this document and of the `scoring.py` comment claimed
CA-heavy models were a reason to keep phi/psi. They are not; the O-stripped
case is the whole justification.

`ss_method` stays honest: it gains `"pydssp"` as a fourth value, recorded per
run by `scout/pipeline.py` exactly as before.

The all-or-nothing rule below is **this branch's rule, not a property of the
column**. phi/psi still returns whatever it can and stamps `"phi_psi"` over
it: on `3s7g` with chain B reduced to CA-only, `assign_dssp` returns 65 labels
of 130 under `ss_method="phi_psi"`, and the other 65 residues score on the
`"loop"` floor. That behaviour predates this branch and is unchanged by it,
but it means the column guarantees *which branch ran*, not *that the branch
covered everything*. Only the pydssp branch guarantees both.

An earlier draft of this section asserted that assignment "is whole-model
all-or-nothing, so that single per-run value remains truthful". **It was not,
and QC caught it.** `_assign_ss_by_pydssp` skipped any chain missing a
backbone atom and returned what it had; `assign_dssp` falls through only when
the map is *entirely* empty. So an O-stripped chain A alongside an intact
chain B produced `ss_method="pydssp"` with zero labels for A — and since
`run_pipeline` scores one selected chain, selecting A meant every patch landed
on the `"loop"` floor while the provenance column claimed a measurement. The
same held per residue.

The property is now **enforced rather than assumed**: pydssp returns `{}`
unless it labels every standard residue of every chain, so a partial result
can never be stamped `"pydssp"`. Guarded by
`test_pydssp_refuses_partial_assignment_so_ss_method_cannot_lie`.

The cost of that strictness: one unusable chain sends the whole model to
phi/psi, including chains pydssp could have read. That is the deliberate
trade — a truthful column over a marginally better label. It is also
avoidable rather than fundamental: `run_pipeline` consumes labels for the
scored chain only, so scoping the assignment to that chain would retire the
trade-off entirely (section 8).

---

## 5. Per-chain feeding (a deliberate, measured compromise)

`_assign_ss_by_pydssp` runs **one chain at a time**, so inter-chain beta
pairing is invisible to the bridge search while the mkdssp oracle sees the
whole file. Measured cost: single-chain-only structures score 0.982 versus
0.979 across all chains — about **0.3 points**.

Concatenating all chains into one array instead would invent a peptide bond
at every chain junction, which is both worse and unmeasured. Per-chain is a
deliberate choice, not an oversight.

---

## 6. Coverage: pydssp accepts inputs mkdssp refuses

| input | mkdssp 4.2.2 | pydssp |
|---|---|---|
| `static/example/3s7g_fc_ab.pdb` | **refuses** | 130 residues assigned (chains A+B, 65 each) |
| headerless PDB (ATOM/TER/END only, 970 to 604 lines) | **refuses** | 76 residues assigned |
| `static/example/1HEW.pdb` | accepts | accepts |

The headerless row is the one that matters for users: it is what RFdiffusion
and BindCraft emit, and Scout accepts uploads. (Not MPNN — this repo's adapter
reads a PDB and writes a FASTA, `tools/mpnn/run_pipeline.py:688`.)

On "refuses": Biopython surfaces this as *DSSP failed to produce an output*
(`Bio/PDB/DSSP.py:201`). mkdssp 4.2.2 itself says the file did not start with a
valid PDB HEADER line so mmCIF was assumed, and that failed too.

**Correction to an earlier claim in this workstream:**
`static/example/3s7g_fc_ab.pdb` is **not** a Scout example — it is a
**Proteina Fc campaign target** (`docs/HANDOFF-2026-08-07-fc-run-ready.md`),
unreachable from Scout's UI and COPYed into no image.
`docs/qc/scout-dssp-fallback-measurement.md` already corrected this on
2026-08-19, and the claim was repeated anyway during this work before being
caught. An earlier draft of *this paragraph* then called it an "iggm fixture",
which is also wrong — iggm and 3s7g co-occur only in a hypothetical, reverted
QC experiment in `docs/qc/deploy-trigger-static-example-round1.md`. Three
wrong answers about one file, the last two inside corrections of the first.
Scout's actual bundled example is
`static/example/1HEW.pdb` (`scout/routes.py:861`), which mkdssp handles
fine. The coverage argument rests on the headerless row, not on that file.

---

## 7. Real-structure regression anchor

`static/example/1HEW.pdb` is Scout's own bundled example. mkdssp 4.2.2 truth
for chain A (129 residues, resseq 1-129 contiguous, full backbone throughout)
is embedded in the test file, and the vendored implementation reproduces
**125/129 = 0.969** of it. The test floor is 0.90 — comfortably under the
measured value, far above anything phi/psi (~0.70 here) could reach — plus an
assertion that the output is not a degenerate single-label answer that could
pass a loose ratio.

`test_pydssp_label_columns_are_not_transposed` separately pins the one-hot
column order `(loop, helix, strand)`. Transposing helix and strand would keep
every label a legal value and every downstream call working while making the
answers wrong — exactly the failure mode #161 shipped.

---

## 8. Runtime

Mean of 5 calls after a warm-up, on the vendored numpy backend:

| chain | residues | per call |
|---|---|---|
| `1ubq:A` | 76 | 3.3 ms |
| `1igy:B` | 434 | 93.7 ms |
| `6m0j:A` | 597 | 160.4 ms |

The H-bond map is O(L^2) in **time and memory**, and memory is the binding
constraint — the earlier draft of this section quoted no memory figure at all,
which is how the ceiling below went unnoticed:

| residues in one chain | time | peak |
|---|---|---|
| 600 | 0.09 s | 0.05 GB |
| 1000 | 0.26 s | 0.13 GB |
| **2000 (the cap)** | **1.59 s** | **0.51 GB** |
| 6000 | ~9 s | ~4.3 GB |
| ~25,900 (the 8 MB upload cap) | — | **~85 GB** |

The branch this replaced was O(L), so nothing upstream ever needed a residue
bound. `ANON_MAX_UPLOAD_BYTES = 8 MB` (`scout/routes.py`) admits roughly
25,900 backbone-only residues in a single chain on an **unauthenticated**
route. numpy's lazy commit means an allocation that large *succeeds*, so there
is no `MemoryError` to catch — the worker dies touching the pages. A
try/except cannot defend this; the size must be checked first.

`_PYDSSP_MAX_RESIDUES = 2000` therefore bounds it: above the cap a chain falls
through to phi/psi, which is O(L) and gives worse labels but cannot exhaust
the box, and `ss_method` honestly reports `"phi_psi"`. Guarded by
`test_pydssp_falls_through_above_the_residue_cap`.

**The cap is per chain, and the model-level cost is not bounded.** Memory is
genuinely bounded — the arrays are freed between chains, so peak stays at one
chain's 0.51 GB, and the OOM vector is closed. Time is not: the same ~25,900
residues split as 13 chains of 1,999 clears every per-chain check and costs
13x the cap's per-chain time — at the 1.59 s per 2,000-residue chain in the
table above, roughly **21 CPU-seconds on one anonymous request**, against a branch
that was O(L) and effectively free. Bounded in practice only by the route's
concurrency slot and the anon rate limiter, both of which now carry more cost
per admitted request than when they were sized.

The waste is avoidable, which is why no second constant was added here.
`assign_dssp` has exactly one caller (`run_pipeline`), and that caller scores
**one** chain: `surface_residues` is built from `model[chain_id]`, patches
come from those residues, and `_majority_ss` / `_continuous_ss_score` look up
only `(chain_id, ...)`. Every other chain in the model is labelled and thrown
away. Scoping the assignment to the scored chain would bound the model-level
cost at one chain with no new constant, and would also retire the
all-or-nothing trade-off in section 4 — an unreadable neighbour chain could no
longer drag the scored chain down to phi/psi. Deliberately **not** done in this
branch: it changes `assign_dssp`'s contract, and this branch is already large.

**Knock-on:** `docs/qc/anon-ratelimit-phase-0.md` sized the anonymous rate
limiter by measuring the *phi/psi* branch and recording that as production's
real path. That is no longer true, and the `~9 CPU-s` adversarial `/progress`
figure at `scout/routes.py:145` has **not** been re-measured against pydssp. A
note to that effect has been added to that document.

---

## 9. What was NOT verified

Stated as plainly as the rest:

- **Not exercised through a live HTTP request.** `run_pipeline` *was* run
  end to end with `assign_dssp` completely unpatched, on
  `static/example/1HEW.pdb`: mkdssp was reported unavailable, the fallback
  fired, and `results.csv` came out with `ss_method="pydssp"` across all 6
  patches and a non-degenerate helix/loop label spread. Only `compute_rsa`
  was substituted, because `freesasa` is not installed in the dev venv and
  the SASA source cannot affect branch selection. What remains unexercised
  is the Flask `/analyze` route wrapping that call.
- **Not deployed.** No production run has emitted `ss_method="pydssp"`.
  Confirm after deploy by reading the `ss_method` column of a real
  `results.csv`, per the existing rule of reading that column rather than
  inferring which branch ran.
- **mkdssp's absence in production remains unconfirmed at runtime.** This
  change does not establish it; the earlier hedge is preserved verbatim in
  the `assign_dssp` docstring.
- **Oracle coverage.** mkdssp refused `3s7g`, so it contributes to the
  coverage table in section 6 but not to the 30-chain accuracy figures.
- **How many epitopes actually cross the `_MIN_COMPOSITE = 0.40` floor.**
  The *shift* is now measured (section 2: mean −0.053, max −0.160,
  one-directional) but the shift alone does not say how many epitopes cross
  the floor, because that needs the absolute composite and `freesasa` is
  absent from the dev venv, so `run_pipeline` could not be driven over the
  corpus. Still the cheapest thing to check after deploy: count rows in a
  real `results.csv` at `composite_score` in [0.40, 0.45).
- **No proline correction.** Upstream's `donor_mask` hook (proline has no
  amide H) is left unused, exactly as during measurement. The 97.9% figure
  is *with* that approximation, so closing it could only help. Independently
  measured on upstream's own TS50 corpus: enabling it moves mean per-chain
  accuracy 0.97268 → 0.97542, i.e. **+0.27 points**, entirely in loop recall.
  Small, real, and not taken here.
- **Non-standard residues.** Residues outside `STANDARD_AA` are skipped and
  read as "loop" downstream, matching prior behaviour for missing keys, but
  this was not separately measured. (Residues missing a *backbone* atom are a
  different case and no longer skipped — one of them now aborts the whole
  assignment, per the all-or-nothing rule in section 4.)
- **Chain gaps: the per-residue cost is now measured (section 2), the
  patch-level cost is not.** Only **2 of the 30** accuracy chains are gapped
  — `1ema:A` (5 gaps) and `1igy:B` (26 gaps) — so the headline figure
  contains the cost but samples it thinly. (The 31-chain / 4552-residue figure quoted in section 3 is
  the *vendoring-equality* corpus, which adds a 3s7g chain that mkdssp
  refuses and so cannot contribute to accuracy.) `1igy:B`, the worst case
  available, still labelled 34 of its 35 patches (97.1%) correctly. A structure with many short gaps could do worse
  than the headline suggests.

---

## 10. Reproducing

The measurement scripts live in the session scratchpad rather than the repo,
since they depend on a WSL-hosted mkdssp shim and a 30-structure corpus that
are not checked in:

- `final_measure.py` — per-residue, both arms, through the real functions
- `verify_vendor.py` — exact-equality proof of the einops removal
- `measure_patch.py` — per-patch label and ranking effect
- `coverage_test.py` — inputs mkdssp refuses, plus timing
- `gen_truth.py` — regenerates the embedded 1HEW truth string

The in-repo regression anchors are the six new tests in
`tests/test_scout_ss_assignment.py`, which need no binary and no corpus.
