# Epitope Scout: is the DSSP fallback a calibration problem?

**Verdict: yes, fix it — but on the normal cycle, not as an incident. One part
is urgent-adjacent for a different reason.**

Epitope Scout has never once run DSSP in production. Every run since the
feature shipped has scored secondary structure with the phi/psi Ramachandran
fallback, which agrees with real DSSP on **70.2%** of residues, calls **66% of
true loops** as helix or strand, biases `ss_score` **+0.23 high**, and produces
a **different top-ranked epitope on 37%** of the structures measured. The
`secondary_structure` label shown in the UI is right about **half** the time.
That is a real quality defect in the product's headline output, and it has been
live since launch.

The urgent part is not the fallback. It is that **`assign_dssp` reads the wrong
column of the Biopython DSSP tuple**, so the one obvious remediation — install
`mkdssp` — would have *halved* accuracy rather than fixed it (70.2% → 37.6%).
That landmine sits directly on the fix path and is a one-character change; it is
fixed in this branch.

---

## 1. Oracle and structure set

**Oracle: real `mkdssp` 4.2.2. Not a proxy, not a pure-Python reimplementation.**

`mkdssp` is not installed on this Windows machine and `sudo` in WSL needs a
password. Instead the Ubuntu noble `dssp` 4.2.2-2 package (plus `libcifpp5`,
`libcifpp-data`) was downloaded with `apt-get download` and unpacked with
`dpkg -x` into a user directory inside WSL Ubuntu-24.04 — no root, nothing
installed system-wide. A Windows `.bat` shim forwards `mkdssp` invocations into
WSL and translates the path with `wslpath`.

The comparison then runs through `Bio.PDB.DSSP.DSSP` — the exact class
`scout/scoring.py::assign_dssp` calls — so the oracle is what production *would*
produce, not an approximation of it. Biopython 1.87 detects the 4.x version and
passes `--output-format=dssp`, so the binary being absent is the only thing
standing between Scout and working DSSP.

**Structure set: 30 chains across 29 structures, 4487 residues**, chosen to span
CATH classes and the input types Scout actually receives.

| Class | Chains | Residues | Examples |
|---|---|---|---|
| all-alpha | 6 | 1195 | 1A6M, 4HHB, 1MBA, 256B, 1ENH, 6M0J:A (ACE2) |
| mostly-alpha | 1 | 164 | 2LZM |
| all-beta | 8 | 1421 | 1EMA, 1TEN, 1TIT, 1SHG, 1BFG, 1TUP, 3AVE, 1IGY |
| alpha/beta | 6 | 673 | 1UBQ, 1PGB, 3CHY, 2CI2, 1RA9, AF-P01116 |
| alpha+beta | 7 | 930 | 1HEW, 2VB1, 3HFM, 1BJ1, 1BRS, 6M0J:E (RBD), AF-P00698 |
| irregular | 2 | 104 | 5PTI, 1CRN |

Includes chain breaks and glycans (6M0J, 1TUP, 1IGY, 3AVE), two AlphaFold models
with pLDDT in the B-factor column, and two of the three PDB files Scout ships as
built-in examples.

**Caveats.**
- `freesasa` has no Windows wheel, so RSA came from Biopython's `ShrakeRupley`
  normalised by Tien et al. (2013) max-ASA rather than freesasa. It is applied
  **identically to every arm**, so it cannot bias the DSSP-vs-fallback delta,
  but absolute patch composition may differ slightly from production.
- `static/example/3s7g_fc_ab.pdb` is excluded: `mkdssp` 4.2.2 refuses it (see
  §6).
- One oracle version. DSSP 4 differs from DSSP 2/3 on some codes (notably the
  PPII `P` code, which maps to loop under Scout's code sets — correctly, since
  `DSSP_HELIX_CODES`/`DSSP_STRAND_CODES` do not contain `P`).

Reproduction scripts are in the session scratchpad, not committed: `measure.py`
(oracle + pipeline arms), `proof.py` (index bug, NaN, `{}` reachability),
`report.py` / `addendum.py` (aggregation).

---

## 2. Q1 — does the DSSP path ever execute in production?

**No. Not once, ever.** Confirmed independently of the brief:

- `nixpacks.toml` sets `nixPkgs = ["gcc"]` — the entire native dependency set.
- No `Dockerfile` for the web tier (the eight `Dockerfile.modal` files are GPU
  tool images, unrelated). No `Aptfile`, no `railway.json`, no `.nix` file, no
  apt or nix layer anywhere in the deploy path. `Procfile` and
  `nixpacks.toml [start]` only launch gunicorn.
- Neither `requirements.txt` nor `requirements-dev.txt` contains any DSSP
  package.
- `git log -S 'dssp' -- nixpacks.toml requirements.txt Procfile` returns
  **nothing**: the binary has never been in the deploy at any commit.
- No `NIXPACKS_*` / `APT_PKGS` override is declared anywhere in the repo, and
  `nixpacks.toml` is the only native-dependency source (`README.md:154`).

One thing this cannot rule out from the repo alone: a package added by hand in
the Railway dashboard's own build settings. Confirming that needs someone with
dashboard access, or a Scout run whose logs can be read — neither of which was
in scope here. Everything the repository controls says the binary is absent.

**Since when:** `assign_dssp` and its `residue_data[1]` read both arrived in
`3ba4c5d` (2026-04-23), the commit that shipped the Scout blueprint. The
function has therefore taken the fallback branch on 100% of runs for its entire
life.

**Was there a signal?** One, and it was loud enough:

```
logger.warning("DSSP binary unavailable (%s); falling back to phi/psi classification", exc)
```

fires at WARNING level on every single run. Nobody read it. There is no metric:
`shared/metrics.py` defines `SCOUT_RUNS` but nothing records SS provenance, and
neither `results.csv` nor the UI says which method produced the label — so the
only way to know was to read Railway logs. A `provenance` column in
`results.csv`, or a label on `SCOUT_RUNS`, would have made this visible from the
dashboard.

---

## 3. Q2 — how closely does the fallback agree with real DSSP?

**Pooled per-residue agreement: 70.18%** (4487 residues, 30 chains).
Per-chain range 0.551 (1TIT) to 0.848 (1A6M).

### Confusion matrix (rows = real DSSP, columns = phi/psi fallback)

| truth \ predicted | helix | strand | loop | total | **recall** |
|---|---|---|---|---|---|
| **helix** | 1546 | 3 | 38 | 1587 | **0.974** |
| **strand** | 9 | 971 | 53 | 1033 | **0.940** |
| **loop** | 471 | 764 | 632 | 1867 | **0.339** |
| total | 2026 | 1738 | 723 | 4487 | |
| **precision** | 0.763 | 0.559 | 0.874 | | |

The shape of the error is the whole story. The fallback almost never *misses* a
real helix or strand — it is the false-positive direction that collapses. **Two
thirds of true loops are promoted to regular secondary structure**: 471 to helix
(10.5% of all residues) and 764 to strand (17.0%). Helix↔strand confusion is
negligible (12 residues total, 0.27%).

That is expected from the method: the fallback has no hydrogen-bond information,
and its strand window
(`-180 < phi < -60 and (100 < psi < 180 or -180 < psi < -120)`) covers the entire
extended / polyproline-II basin, where most coil actually sits. The docstring
calls the boundaries "deliberately generous to avoid under-assigning regular
secondary structure" — they are, and this is the cost.

Because `_SS_SCORES` is `{strand: 1.00, helix: 0.80, loop: 0.20}`, every one of
those 1235 promoted loops moves that residue's contribution by **+0.60 or
+0.80**, always upward.

### By structural class

| Class | Agreement |
|---|---|
| mostly-alpha | 0.823 |
| all-alpha | 0.790 |
| alpha/beta | 0.712 |
| alpha+beta | 0.683 |
| irregular | 0.644 |
| **all-beta** | **0.626** |

Worst on all-beta — which is where Scout's own domain lives (Fc, Ig folds,
antibody targets; 3AVE 0.682, 1IGY 0.597, 6M0J:E 0.598).

### Effect on `ss_score` itself

`composite_score` differs between arms *only* in the `0.20 * ss_score` term, so
per-patch `ss_score` can be recovered exactly from the arm composites. Over
**281 patches**:

| | real DSSP | phi/psi fallback |
|---|---|---|
| mean `ss_score` | 0.483 | **0.714** |
| sd | 0.203 | 0.141 |

- bias **+0.231**, mean absolute error **0.243** on a [0.2, 1.0] scale — about
  **30% of the term's full range**
- fallback scores **higher** than truth on **224/281 patches (80%)**
- **Pearson r = 0.373, Spearman rho = 0.343**

An r of 0.37 is the finding. The term is not slightly miscalibrated; it is
mostly noise with a large positive offset.

---

## 4. Q3 — does the epitope RANK ORDER change? (the one that matters)

Yes. Measured by running the **real `run_pipeline`** three times per chain with
`assign_dssp` forced to (a) the true DSSP map, (b) the phi/psi map, (c) `{}`,
everything else held identical, and diffing the ranked `results.csv`.

| Metric (phi/psi vs real DSSP) | Result |
|---|---|
| **Top-1 epitope differs** | **11 / 30 chains (37%)** |
| Top-3 set not identical | 16 / 30 chains |
| Patches that change rank | 191 / 281 (68%) |
| Mean \|rank shift\| per patch | 1.08 places |
| **Largest single-patch shift** | **12 places** (1IGY:B, 35 patches) |
| Mean Spearman rho | 0.787 |
| Mean \|Δ composite\| | 0.046 (max 0.154) |
| **Displayed `secondary_structure` label agrees** | **50.4% of patches** |

Scout reports the top 3 epitopes above threshold in the UI. Changing the #1
epitope on more than a third of structures, and disagreeing on the displayed SS
label half the time, is a product-level defect, not a rounding difference.

### The control that decides the recommendation

Compare the fallback against simply *dropping* the SS term (the `{}` map, which
gives every patch a constant 0.2), both scored on how well they recover the
real-DSSP ranking:

| | mean rho | median rho | top-1 differs | mean \|shift\| |
|---|---|---|---|---|
| phi/psi fallback | 0.7872 | 0.865 | 11/30 | 1.08 |
| no SS term at all | 0.7874 | 0.833 | 8/30 | 1.16 |

Paired over 29 chains with a defined rho: phi/psi closer on 10, no-SS closer on
13, 7 ties. **Wilcoxon signed-rank p = 0.73.**

**The 20% secondary-structure weight currently buys no measurable rank-order
fidelity.** The fallback is not merely imprecise — at patch level it is
statistically indistinguishable from having no secondary-structure signal at
all. A fifth of the composite is being spent on a term that, as computed today,
carries no information about the answer it is supposed to improve.

---

## 5. Q4 — is the `{}` outcome reachable, and can a NaN reach a label?

**`{}` via the documented route is effectively unreachable.** `assign_dssp` only
logs "Phi/psi fallback also failed" and returns `{}` when
`_assign_ss_by_phi_psi` *raises*. It does not raise on any degenerate input
tested — a model with no chains, a chain with no residues, a water-only chain,
or exactly collinear backbone atoms. It returns `{}` (or all-loop) **through the
normal return path**, so the second warning never fires and an all-loop score
carries no log signal at all. The reachable route to `{}` is any chain where
`PPBuilder` yields no peptides — e.g. a chain with no backbone atoms.

**Rank consequence of `{}`: none.** Every patch gets `ss_score == 0.2`, so the
term becomes a constant offset and cancels out of the sort entirely. It deflates
every `composite_score` by the same amount and mislabels the displayed SS column
as "loop", but it cannot reorder the list. Asserted in
`test_empty_ss_map_scores_every_patch_identically`.

**The NaN: reproduced, and it cannot produce a wrong label.** The
`RuntimeWarning: invalid value encountered in scalar divide` comes from
`Bio/PDB/vectors.py:359`:

```python
c = (self * other) / (n1 * n2)
```

which is 0/0 when a cross product has zero length — i.e. collinear or duplicated
backbone atoms. But the NaN is laundered two lines later:

```python
c = min(c, 1)    # min(nan, 1) keeps nan
c = max(-1, c)   # max(-1, nan) returns -1
```

so `arccos(-1) = π`. And ±180° falls outside every Ramachandran window in
`_assign_ss_by_phi_psi`, giving **"loop"**. Independently, if a NaN ever did
reach the comparisons, `np.degrees(nan)` compares `False` against every bound
and also gives **"loop"**.

So a degenerate angle always silently becomes `loop`, never a spurious helix or
strand. The failure direction is conservative. It is now asserted by
`test_phi_psi_fallback_degenerate_geometry_yields_loop_not_a_crash` so a future
refactor cannot turn it into a confident wrong label.

---

## 6. The defect nobody was looking for: `assign_dssp` reads the wrong column

Biopython's DSSP tuple is
`(dssp_index, amino_acid, secondary_structure, rel_asa, phi, psi, ...)`.

`scout/scoring.py` read **index 1** — the one-letter **amino acid** — and matched
it against `DSSP_HELIX_CODES = {"H","G","I"}` and
`DSSP_STRAND_CODES = {"E","B"}`.

`H`, `G`, `I` and `E` are legal letters in *both* alphabets. So with `mkdssp`
installed, the DSSP branch would classify every **His, Gly and Ile as "helix"**
and every **Glu as "strand"**, by amino-acid identity, regardless of geometry.
Verified directly against the oracle on 1UBQ:

| resseq | residue | index 1 (read) | index 2 (real SS) | label produced | correct label |
|---|---|---|---|---|---|
| 3 | ILE | `I` | `E` | helix | strand |
| 4 | PHE | `F` | `E` | loop | strand |
| 10 | GLY | `G` | `S` | helix | loop |
| 13 | ILE | `I` | `E` | helix | strand |

**Measured pooled agreement of that branch with real DSSP: 37.6%**
(per-chain 0.234–0.539), against **70.2%** for the phi/psi fallback it was
supposed to improve on.

This is why the ordering is load-bearing: **installing `mkdssp` without this fix
would have made Scout's secondary-structure scoring roughly twice as wrong.**
It survived because it is dead code — the binary has never been present — and
because `assign_dssp` and `_assign_ss_by_phi_psi` had **zero test coverage**
before this branch.

### `mkdssp` will not cover every input

`static/example/3s7g_fc_ab.pdb` — one of Scout's three shipped examples — starts
with `ATOM`, not `HEADER`, and `mkdssp` 4.2.2 rejects it outright
("did not start with a valid PDB HEADER line"). Uploads are written to disk
verbatim (`scout/routes.py:388`, `:483`), and headerless PDBs are routine output
from design pipelines (RFdiffusion, BindCraft, MPNN). Those inputs will keep
taking the fallback after `mkdssp` is installed. That is an argument for keeping
the fallback and documenting it honestly, not for skipping the install.

---

## 7. Q5 — recommendation

### Do (implemented in this branch, uncommitted)

1. **`residue_data[1]` → `residue_data[2]`** in
   `scout/scoring.py::assign_dssp`. Required regardless of every other
   decision. Evidence: §6. No behaviour change today (the branch is dead), but
   it removes a trap from the fix path.
2. **`tests/test_scout_ss_assignment.py`** — 7 tests, the coverage that was
   missing. Fakes the DSSP object so the branch runs without the binary.
   Verified to fail (2 tests) against the pre-fix code and pass after.
3. **Correct the code comments that claim DSSP runs** — `scout/pipeline.py`
   module docstring, `run_pipeline` step 9, the step-9 inline comment, and the
   `run_feasibility_pipeline` docstring (which claimed to reuse DSSP; it never
   calls `assign_dssp` at all — `run_pipeline` is the only caller); plus a
   deployment note on `assign_dssp`. The user-facing copy in
   `templates/scout/index.html` says "secondary structure score" without naming
   DSSP, so it is not false and was left alone.
4. **`nixpacks.toml`: `nixPkgs = ["gcc", "dssp"]`** — the actual remediation.
   **DEFERRED — not in this branch.** Split out 2026-08-19: it activates a
   code path that has never once executed in production, and no Railway build
   was run to verify it. It also has to clear its own value bar first — the
   §4 control says dropping the SS term entirely is statistically
   indistinguishable from real DSSP on rank fidelity (Wilcoxon p = 0.73), and
   `3s7g_fc_ab.pdb`, a shipped Scout example, has no PDB HEADER, which
   mkdssp 4.x rejects outright. Tracked as separate, build-verified work.
   Justified by r=0.37, +0.23 bias, 37% top-1 change, 50% label agreement.
   `pkgs/by-name/ds/dssp` was confirmed to exist in nixpkgs and provides
   `bin/mkdssp`.
   **Unverified: no Railway build was run.** Before merge, confirm the build log
   shows `dssp` installing and that a Scout run stops logging "DSSP binary
   unavailable". Must ship with (1) or not at all.

### Also worth doing (not implemented — out of scope for this branch)

Record SS provenance — a `provenance` column in `results.csv` or a label on the
`SCOUT_RUNS` metric. The whole reason this ran unnoticed for four months is that
"DSSP was used" and "the fallback was used" look identical in every artefact the
product emits.

### Do not

- **Do not recalibrate the phi/psi thresholds.** The dominant error is
  loop→strand (17% of all residues), so tightening the strand window would
  reduce the +0.23 bias. But the term's real problem is r = 0.37 without
  hydrogen-bond information, and no threshold set fixes that. Fitting new
  windows to 30 structures would be overfitting in place of installing the
  correct tool.
- **Do not remove or reweight the SS term.** §4 shows it contributes nothing to
  rank fidelity *as currently computed* — that is an argument for making it
  real, not for deleting a literature-backed component that works when DSSP is
  present.
- **Do not touch the DSSP key format.** `dssp_map` keys use the DSSP file's
  `res_id` `(' ', resseq, icode)` while `_continuous_ss_score` looks up
  `(chain, residue.get_id())`, so HETATM-flagged standard residues (MSE) would
  miss. Measured impact: **1 residue in 4487**, and `run_pipeline` step 4 already
  skips HETATM residues before patch formation so they never reach the lookup.
  Moot.

---

## 8. Test suite

Both runs are `python -m pytest -q` from the repo root, no path argument, using
the repo venv interpreter. Baseline measured on `7fd180d` before any edit.

| | Result |
|---|---|
| Baseline (`7fd180d`, unmodified) | **5273 passed, 20 skipped** (956s) |
| After these changes | **5280 passed, 20 skipped** (720s) |

5280 − 5273 = 7, exactly the new tests in `tests/test_scout_ss_assignment.py`.
No existing test changed state. Both runs exited 0.

---

## 9. Files changed in this branch

| File | Change |
|---|---|
| `scout/scoring.py` | `residue_data[1]` → `[2]`; deployment note + why-comment |
| `scout/pipeline.py` | Four docstring/comment corrections: SS comes from the fallback, not DSSP; feasibility pipeline never used DSSP at all |
| `nixpacks.toml` | **Unchanged here** — the `dssp` install is deferred to a separate, build-verified PR (§7 item 4) |
| `tests/test_scout_ss_assignment.py` | New — 7 tests, first coverage of either SS function |
| `docs/qc/scout-dssp-fallback-measurement.md` | This document |
