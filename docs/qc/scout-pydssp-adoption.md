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
electrostatic H-bond map and reads helices and bridge ladders off it — DSSP's
own algorithm without the binary, simplified as upstream documents (no
beta-bulge, approximate amide H, 3-state output), which is what the measured
97.9% rather than 100% agreement reflects.

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
added a banner to it and shifted every line below it by 17.)

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
fixed sequence offsets. And `ss_method` still reads `"pydssp"` throughout: the
run carries no signal that its labels were degraded.

> **CLOSED by a later change — MSE and SEC now enter the pydssp backbone.**
> See `_MODIFIED_AA` and `_is_polymer_residue` in
> `scout/scoring.py`. The blocker recorded here — "residue selection is shared
> with `compute_rsa` and patch construction, so changing it late risks
> misaligning keys across modules" — was avoided rather than accepted: the new
> selector is LOCAL to the pydssp branch, and `scout/sasa.py::STANDARD_AA` is
> left untouched, so `compute_rsa` and patch construction see exactly what they
> saw before. The extra `('H_MSE', n, ' ')` keys the map gains are never read,
> because both consumers (`_majority_ss`, `_continuous_ss_score`) do
> `ss_map.get(key, "loop")` over patch residues and nothing iterates the map.
>
> Independently measured on ten SeMet structures (nine measurable): 25 MSE,
> 1222 residues shared with a MET-ized junction-free control, 31 differing
> → 97.46%, worst case `13CT` 89.66%. After the change: 0 differing, 1.0000,
> shared count up by exactly 25. Nine no-MSE controls score exactly 1.0000
> both before and after, which is what makes the attribution causal. Those
> figures corroborate the `3fhk:A` and `2i39:A` numbers above on a third
> corpus.
>
> **Read those two numbers with their caveats — an independent re-derivation
> reproduced all of them exactly, and then found both framings flattering in
> opposite directions.**
>
> The post-change **1.0000 is true by construction and is not evidence of DSSP
> accuracy.** MET-izing rewrites only the record name and the resname;
> coordinates and residue order are untouched. Once the selector accepts
> `MSE`, both arms hand pydssp a byte-identical coordinate array, so the fix
> arm *must* return 1.0000. What it demonstrates is that the selector is wired
> in and nothing reorders — the published 97.9% mkdssp headline was NOT
> re-measured, and cannot be here (no `mkdssp` in this environment).
>
> The pre-change **97.46% flatters the OLD behaviour, so the defect is worse
> than that figure suggests.** "Shared" excludes the 25 MSE the old selector
> never labelled at all. In production those do not vanish; they fall to the
> loop floor via `ss_map.get(key, "loop")`, and 13 of the 25 are non-loop in
> the control. Scored the way `run_pipeline` actually consumes the map — the
> control's 1247 residues as denominator, missing keys read as `"loop"` — the
> old behaviour is **44 wrong / 1247 = 96.47%**, not 97.46%. The bias runs
> against the fix's own case, which is why the smaller figure was the one
> originally quoted.
>
> **The change is NOT monotone**, despite a 178-chain sweep recording zero
> deleted keys. `_PYDSSP_MAX_RESIDUES` is checked against `len(standard)`,
> which MSE/SEC now inflate, so a chain whose canonical count falls in
> `(2000 - n_MSE, 2000]` crosses the cap and loses its entire map — on the
> SCOPED path production uses. Measured on a SeMet-ized `1HEW` with the cap
> pinned at the old count: 127 labels → 0. No chain in the sweep came near
> 2000, so "monotone" was a sweep observation stated as a universal. The cap
> is deliberately left alone — it bounds the allocation pydssp actually makes,
> and `len(standard)` is now the honest size where before it under-counted;
> the affected chain trades a junction-corrupted map for phi/psi, with
> `ss_method` still truthful. Pinned by
> `test_modified_residues_count_toward_the_max_residue_cap`.

**One claim in this section was wrong and is retracted.** It said the phi/psi
branch "computes each dihedral locally and is barely affected by a junction".
Measured directly against the same MET-ized control, phi/psi loses 2 labels on
`13CT` (3 MSE), 6 on `1BKB` (4), 5 on `1AT0` (3) and 7 on `1ASW` (4) — roughly
1.5 residues per junction, comparable to pydssp's 1.24, and it labels **none**
of the MSE residues. PPBuilder splits the polypeptide at each MSE, so the
residues at every new terminus lose a dihedral and fall to "loop".

> **CLOSED by a later change — modified residues no longer split the peptide.**
> See `_ScoutPPBuilder` in `scout/scoring.py`. `build_peptides(aa_only=0)` was
> rejected as predicted, and now with a measurement behind it: it accepts all
> **1032** entries of Biopython's `protein_letters_3to1_extended` and then any
> residue at all carrying an atom named `CA`. `SEP` and `FME` are taken with
> **no warning**; `SEC` is not in the extended table at all.
>
> Instead the builder overrides `PPBuilder._accept` — the one question
> `build_peptides` asks about residue identity — leaving `_is_connected`, the
> real C→N peptide-bond test, untouched. It **ORs onto** the stock rule
> (`is_aa(residue, standard=True)`) rather than replacing it, so it is a
> strict superset and the peptide can only grow.
>
> **That superset property is the whole correctness argument, and the first
> version of this change did not have it.** It reused
> `_is_pydssp_polymer_residue`, which additionally demands `hetflag == " "`
> for the canonical 20 — something stock `_accept` never checks, since
> `is_aa` reads the resname only. The two accept-sets were therefore
> **incomparable, not nested**, and the builder cut the peptide at any
> in-polymer canonical residue recorded as `HETATM`: on `1HEW` chain A with
> residues 60–62 re-spelled, 129 labels → 126 with residues 59 and 63 falling
> to `"loop"`. That is the very defect being fixed, reintroduced under a
> different trigger, equally silent. **Two independent reviews found it; the
> author's own 69-structure corpus could not**, because in-polymer
> HETATM-spelled canonical residues are rare (a reviewer found 2 in 88
> structures, both free ligands `_is_connected` already excluded). Pinned by
> `test_phi_psi_accept_is_a_strict_superset_of_biopythons` and
> `test_phi_psi_does_not_cut_at_hetatm_spelled_canonical_residues`.
>
> The hetflag gate is right for pydssp and wrong here: pydssp has no
> connectivity test, so only the hetflag keeps a free solvent amino acid out
> of its coordinate array, whereas here `_is_connected` excludes it on
> geometry regardless of spelling.
>
> `_accept` is **private** Biopython API and `requirements.txt` pins a range
> (`>=1.81,<2.0`). It fails safe but **silently**, so
> `test_phi_psi_keeps_modified_residues_in_the_peptide` pins it.
>
> **FOUR loss modes, not three.** The MSE itself; psi of the residue before;
> phi of the residue after; and — the one the first write-up missed — in an
> `MSE-X-MSE` motif, `X` is isolated between two rejected residues, forms no
> connected pair, and gets **no key at all**. Found by review, which measured
> 39 such keys over its own 70-structure corpus.
>
> **Measured, 69 structures / 30650 control labels.** Read the split, not the
> total: of 744 missing keys, **721 are the MSE residues themselves** — which
> `pipeline.py:356` filters out of patches under *either* spelling, so
> production never reads them — and only **23 are canonical residues
> stranded** by the motif above (14 non-loop). Adding those to the 1164
> mislabelled canonical residues gives **1178 wrong AND consumed = 3.84%** of
> the labels. An earlier draft quoted 1787 by counting MSE keys production
> discards; that overstated it by ~50%. After the change: 0.
>
> A 169-chain sweep adds **744 keys and deletes 0**, 0 exceptions, 0 chains
> newly empty, 36 byte-identical.

**Do not over-read those numbers.** Four caveats, three raised by review:

1. **The post-fix `1.0000` is true by construction and carries no information
   about label accuracy.** MET-izing changes only the record name and resname;
   once the selector accepts `MSE`, both arms hand identical coordinates to
   identical peptide decomposition, so the fixed arm *cannot* return anything
   else. Critically, this control is **blind to any error symmetric under
   MSE→MET** — which is exactly why it never saw the HETATM regression above.
   A perfect score on a metric that cannot fail is not evidence of
   correctness. Never quote it beside the 97.9%/70.2% mkdssp headlines.
2. **`94.17%` is an agreement rate; the error rate is 5.83%** (3.84% after
   restricting to consumed labels). Writing "1787 wrong = 94.17%" invites
   reading it as "94% wrong".
3. **phi/psi is the third fallback and rarely runs at all.** It won 3 of 169
   chains here (1.8%); an independent reviewer's 178-chain corpus gave 1
   (0.6%). It is corpus-specific — do not quote either as a property of the
   code. And a chain in a corpus is not a production run: `assign_dssp` is
   called once per run on the user's chosen chain.
4. **The corpus is MSE-selected, i.e. the maximum-impact population.** RCSB
   holds ~10278 MSE entries against ~253370 protein entries, a **4.06%** base
   rate. Every figure above is conditional on the structure containing MSE.

Where phi/psi *did* win, the error was severe: `1AQC:B` 15/122 (12.3% of the
chain), `1B89:A` 21/321 (6.5%), `1B24:A` 9/173 (5.2%) — all certified
`ss_method="phi_psi"`. `1B24:A` is instructive: the pydssp measurement had to
exclude it because residue 179 is missing backbone atoms, and that is exactly
why phi/psi wins there. **The chains where pydssp bails are this branch's
population, so the two defects were never redundant.**

The rename is narrower than first written: `_PYDSSP_MODIFIED_AA` →
`_MODIFIED_AA`, because that set genuinely is shared. The *predicate* keeps its
`_is_pydssp_polymer_residue` name — the two branches deliberately do **not**
share it, which was the whole bug. `_assign_ss_by_pydssp` output is
byte-identical across 169 chains either side of the rename.

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
(in `scout/pydssp_numpy.py`). Behaviour-identical, but it *is* a second
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
pydssp          ->  "pydssp"     <- the normal path once this lands
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

The mkdssp branch has the same hole — QC drove a stub `DSSP()` exposing a
single `property_key` over a 130-residue two-chain model and got
`ss_method="dssp"` at 1/130 coverage with no fall-through — and the rule is
deliberately **not** extended to it either. `dssp_map` keys use the DSSP
file's `res_id`, which misses HETATM-flagged standard residues, so a strict
`len(map) == len(standard)` test would fall through on any structure
containing one `MSE`. Since SeMet phasing is routine, that would quietly
disable the most accurate branch for a whole class of real inputs in order to
close a hole in a branch that, on current evidence, never runs in production.
Enforcing completeness where it costs nothing (pydssp) and declining where it
would cost the branch itself is the deliberate asymmetry.

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
unless it labels every standard residue *in scope* — the scored chain on the
production path, every chain when none is named — so a partial result can
never be stamped `"pydssp"`. Guarded by
`test_pydssp_refuses_partial_assignment_so_ss_method_cannot_lie`.

The cost of that strictness *was* that one unusable chain sent the whole
model to phi/psi, including chains pydssp could have read — a truthful column
bought with a marginally worse label. **That trade is now retired**, because
it was avoidable rather than fundamental: assignment is scoped to the scored
chain, so an unreadable neighbour is never looked at and can neither sink the
map nor cost an allocation (section 8). The all-or-nothing rule still holds,
now over the chain actually in scope.

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

Mean of 5 calls after a warm-up, on the vendored numpy backend and the repo
venv. (Re-measured in QC round 9: the original row values — 3.3 / 93.7 /
160.4 ms — were 1.7-2.7x high and did not reproduce, and the `6m0j` entry was
bit-for-bit the measured `1igy` value, which is what a column shifted by one
row looks like. Residue counts were correct throughout.)

These three do not need the PDB files to check. Anchoring on an independent
in-repo measurement — L = 1000 at 0.249 s, with L = 2000 and 3000 at 1.022 s
and 2.288 s confirming the exponent (ratio 2.24 against a predicted 2.25) —
O(L^2) predicts 1.4 / 46.9 / 88.7 ms for 76 / 434 / 597 residues. The values
above sit at **1.02x** that curve for the two larger chains (the 76-residue
one runs under it, where fixed overhead dominates and a pure quadratic
under-predicts). The replaced values sat at 1.8-2.3x it.

| chain | residues | per call |
|---|---|---|
| `1ubq:A` | 76 | 1.1 ms |
| `1igy:B` | 434 | 48.0 ms |
| `6m0j:A` | 597 | 90.5 ms |

The H-bond map is O(L^2) in **time and memory**, and memory is the binding
constraint — the earlier draft of this section quoted no memory figure at all,
which is how the ceiling below went unnoticed:

| residues in one chain | time | peak |
|---|---|---|
| 600 | 0.09 s | 0.05 GB |
| 1000 | 0.26 s | 0.13 GB |
| **2000 (the cap)** | **1.02 s** | **0.51 GB** |
| 6000 | ~9 s | ~4.3 GB |
| ~25,900 (the 8 MB upload cap) | — | **~85 GB** |

The branch this replaced was O(L), so nothing upstream ever needed a residue
bound. `ANON_MAX_UPLOAD_BYTES = 8 MB` (`scout/routes.py`) admits roughly
25,900 backbone-only residues in a single chain on an **unauthenticated**
route. Under Linux's default memory overcommit — the production platform, and
not a numpy property — an allocation that large *succeeds*, so there
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
13x the cap's per-chain time — at 1.02 s per 2,000-residue chain, roughly
**13 CPU-seconds on one anonymous request**, against a branch
that was O(L) and effectively free. Bounded in practice only by the route's
concurrency slot and the anon rate limiter, both of which now carry more cost
per admitted request than when they were sized.

**This was fixed in the follow-up rather than papered over with a second
constant.**
`assign_dssp` has exactly one caller (`run_pipeline`), and that caller scores
**one** chain: `surface_residues` is built from `model[chain_id]`, patches
come from those residues, and `_majority_ss` / `_continuous_ss_score` look up
only `(chain_id, ...)`. Every other chain in the model is labelled and thrown
away. Scoping the assignment to the scored chain would bound the model-level
cost at one chain with no new constant, and would also retire the
all-or-nothing trade-off in section 4 — an unreadable neighbour chain could no
longer drag the scored chain down to phi/psi.

Done, in the commit carrying this paragraph (**not yet deployed**).
`assign_dssp` takes a `chain_id`; every branch is restricted to it, including
the mkdssp branch's output — which closed a latent hole of its own, since a map
covering only OTHER chains used to satisfy the "did this branch produce
labels?" test and stamp `ss_method="dssp"` over a scored chain with none.

Measured, one 65-residue chain replicated N times, chain A scored:

| chains | whole-model | scoped | saving | ceiling |
|---|---|---|---|---|
| 2 | 2.21 ms | 1.10 ms | 2.01x | 2x |
| 4 | 4.43 ms | 1.10 ms | 4.03x | 4x |
| 8 | 9.23 ms | 1.09 ms | 8.47x | 8x |
| 13 | 14.81 ms | 1.09 ms | **13.64x** | 13x |

(Medians of 15 timed calls after a warm-up. A first single-shot pass gave
2.6x and 4.7x at N=2 and N=4 — *above* the chain-count ceiling, which the
scoped path cannot beat by more than the work it skips. Those were noise;
these are medians, and they sit just under or at the ceiling as they must.)

The scoped column is **flat in chain count** — that is the property that
matters, not the ratio. The per-chain cap is now the per-request cap, so the
~13 CPU-s SS figure is no longer reachable. (It does not put a whole request
back under the ~9 CPU-s the anon limiter was sized on: that budget covers
`run_pipeline` entire and was measured when SS was free. See
`docs/qc/anon-ratelimit-phase-0.md`.)

On a model whose chain B is CA-only, chain A now returns `ss_method="pydssp"`
where it previously returned `"phi_psi"` — it moves from the branch that
averages ~70% agreement to the one that averages 97.9%. Those are corpus
means over 30 chains, not this structure's rates; what is proven for a given
input is the branch it lands on.

The mkdssp branch is filtered but not made cheaper — the binary still reads
the whole file, and a map that misses the scored chain now falls through and
runs pydssp as well, which is strictly more work. That is a correctness fix,
not a saving, and mkdssp is believed absent in production anyway.

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
- **The 97.9% was measured before the all-or-nothing rule existed.** Section 1
  says both arms were driven through the real shipped functions, and they
  were — but the shipped function has since changed: it now returns `{}` for
  everything in scope if a chain in scope has a standard residue missing a
  backbone atom, and the corpus measurement drives the whole-model path
  (section 4). Under today's code, a corpus structure with one such residue
  would contribute no labels at all rather than a near-complete map, so the
  headline could not be reproduced from it unchanged. QC probed 10 of the ~29
  corpus structures — 1A6M, 1BJ1, 1EMA, 1IGY, 1TUP, 1UBQ, 3AVE, 3HFM, 4HHB,
  6M0J, chosen to cover every multi-chain, antibody and disordered candidate —
  and **all have zero standard residues missing a backbone atom**, so all
  still return full maps. The figure is therefore very likely intact, but it
  was not re-run end to end after the rule landed.
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
  — `1ema:A` and `1igy:B` — so the headline figure
  contains the cost but samples it thinly. **Correction:** `1ema:A` was
  recorded here as having 5 gaps. Four of those five were the excluded MSE at
  78, 88, 153 and 218, which the `_MODIFIED_AA` change removes; only
  the break between 64 and 68 is genuine unresolved density. `1igy:B`'s 26 are
  all real (that chain contains no MSE, checked). So the gap sample is
  **1 real gap plus 26**, thinner than stated, and it rests almost entirely on
  one chain. The 97.9% headline was therefore measured WITH the MSE junctions
  present, so its direction of travel is upward — but **the magnitude is
  unknown and may be small**, and re-measuring needs mkdssp, which is not
  installed in this environment. Note an unresolved discrepancy before anyone
  quotes a revised figure: `scout-dssp-fallback-measurement.md` reports "1
  residue in 4487" HETATM-flagged standard residues over these same 30 chains,
  which cannot be reconciled with `1ema:A` alone holding 4 MSE. That count was
  most likely taken against a residue set that excludes MSE, in which case it
  does not measure MSE at all and the corpus MSE content is still unknown. (The 31-chain / 4552-residue figure quoted in section 3 is
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

The in-repo regression anchors are the nine new tests in
`tests/test_scout_ss_assignment.py`, which need no binary and no corpus.
