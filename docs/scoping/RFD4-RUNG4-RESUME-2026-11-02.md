# RFD4-Proteina rung 4 — the decision record, 2026-11-02

> **SUPERSEDED IN PART, 2026-11-03.** This file was written while rung 4 was
> parked, and its present tense describes that state. **The paid run has since
> run**: 217.4 s on one A100-80GB, returncode 0, 8/8 designs, result artefact
> `rung4-probe/gpu_result_20261103T114437.json`, written up in §5.8 of
> `RFD4-PROTEINA-SCOPE-2026-10-30.md`, which is the authority for the result.
> This file is kept for the decision record — why the spec variants were chosen,
> what the free pre-flight proved, and the two decisions Leo made on 2026-11-03.
> Read every "has not run", "still needs" and "$0.00 so far" below as true of
> 2026-11-02, not of now.

**As of 2026-11-02, GPU spend on rung 4 was $0.00 of the ~$1.85 Leo approved,
and no GPU function had been invoked.** (Superseded: one paid GPU run has since
executed — see the banner above.) At the time of writing, — `gpu_run` in `docs/scoping/rung4-probe/rung4_app.py`
was only ever *created* by the Modal client, never called; every probe ran through
`cpu_probe` or `variant_probe` (`cpu=4`, no `gpu=` and no weights volume mounted).
The evidence is that the committed console logs show only `Created function
gpu_run` with no invocation of it, and that `rung4-probe/` holds no gpu-mode
result file at all (`ls` of the directory at that time: only `cpu_result.json`
and `variants_result_v5v7.json`; it now also holds the paid run's
`gpu_result_20261103T114437.json` and two timestamped CPU results). The CPU probe containers are themselves not free — just
small, and not against this budget. The $0.00 is specifically the approved GPU
budget.

The CPU side is now measured rather than left open. `modal billing report --start
2026-10-28 --json`, filtered to the four rung-4 probe app ids, totals
**$0.04012393** (the four per-app amounts below do sum to it; **the CLI output
itself is not committed, so these are readings, not artefacts**), every row dated
2026-11-02 (ap-2uZ3k5ao3MUNNBtbPZ441m
$0.01981090, ap-bHdGlb3K7IHQjH2QioJedb $0.01108102, ap-ZbtJPgdCrt2GPLeyOz5zhi
$0.00613846, ap-YNFF2ZVyOBhwGBbG9dWHKY $0.00309355). `modal billing report --for
today` shows `0E-8` — $0.00000000 — for 2026-11-03. An earlier version of this
paragraph said no billing report had been pulled; that was true when written and
is not now.

**A crash-loop on 2026-11-03 cost $0.00 and is fixed.** The review fix that
replaced the hardcoded `_REPO_RFD4` path with `_HERE.parents[2]` crash-looped
every container (`IndexError: 2`, quoted in full in
`rung4-probe/crashloop_20261103.log`), because Modal mounts the script at
`/root/rung4_app.py` and **re-executes its module-level code inside the
container**, where `_HERE` is `/root` and has one parent. The hardcoded path had
never been dereferenced remotely. **The mechanism is an inference, not a
measurement:** that is consistent with `Image.from_dockerfile` reading its
argument only at local declare time, but nothing here measures the laziness. `_REPO_RFD4` now searches upward for a real
`tools/rfd4/Dockerfile.modal` and falls back, so the import cannot raise in
either place. The local-side check that passed before the push covered only the
local side; the container path was never exercised by it.
Parked on the hub lead's instruction because the laptop is going to sleep and the
approval was for a run Leo can watch.

This note exists so the paid run can be fired in one step when he is back.

## Why rung 4 has not run yet

The rung-4 input, built to match the recorded `proteina` comparator run (3AVE
fucosylated IgG1 Fc, **both** protomers, chains A and B, the same eight
EU-numbered hotspots per protomer), **fails upstream's own pre-run validation**
before any GPU is allocated. The check is
`check_index_group_molecule_connectivity_violations`
(`src/rfproteina/transforms/invariants.py:823-884` at the §5.3 pinned SHA
`aaccdd2ad0720c180468df5f9cf037a713d403e5`):

```
ValueError: fc_binder: condition_spec validation failed:
[index_group_molecule_must_be_connected] 23088 token pair(s) of one index group
have no path between them in the bond graph though they are one molecule:
C/1-B/1, C/1-B/2, C/1-B/3, ...
```

`C` is the generated binder chain, `B` the second Fc protomer. **This failure cost
$0** — it is raised by `scripts/write_condition_spec_cifs.py` on CPU.

## The six spec variants tested, all on free CPU

Specs are in `docs/scoping/rung4-probe/`. Each row is the exit code of
`write_condition_spec_cifs.py <spec> --out-dir ... --verbose` run inside the §8
step-1 image (`tools/rfd4/Dockerfile.modal`, `RFD4_ACCELERATOR=cu13`, unmodified).

| | Spec | Target chains | Target indexed? | `rc` | Built a CIF? |
|---|---|---|---|---|---|
| V1 | `condition_spec_fc.yaml` | A + B, one file | yes | **1** | no |
| V2 | `spec_v2_twofiles.yaml` | A + B, two independent input files | yes | **1** | no |
| V3 | `spec_v3_unindexed.yaml` | A + B, `C_IDX: false` on both, one shared group | no | **0** | yes |
| V4 | `spec_v4_chainA.yaml` | A only | yes | **0** | yes |
| V5 | `spec_v5_nocrdgrp.yaml` | A + B, absolute `C_CRD` instead of `C_CRDGRP` | yes | **1** | no |
| V7 | `spec_v7_unindex_b.yaml` | A + B, `C_IDX: false` on B only | A yes, B no | **0** | yes |

Modal apps: V1 `ap-bHdGlb3K7IHQjH2QioJedb` and `ap-YNFF2ZVyOBhwGBbG9dWHKY`;
V2/V3/V4 `ap-2uZ3k5ao3MUNNBtbPZ441m`; V5/V7 `ap-ZbtJPgdCrt2GPLeyOz5zhi`. Raw
result JSON for V5/V7 is `rung4-probe/variants_result_v5v7.json`; **the V2/V3/V4
result JSON was overwritten by the V5/V7 sweep**, so those three rows were read
from it in-session and are **UNVERIFIED from committed evidence**. Re-deriving
them now works as written -- `--mode variants` takes `--spec-name` as a
comma-separated list and writes a timestamped result file -- but in the commit
first pushed it did not: `main()` hard-coded the V5/V7 pair and ignored
`--spec-name`. The V1 rows are evidenced by `rung4-probe/cpu_probe2_console.log`
and by `rung4-probe/cpu_result.json` (`write_cifs_rc: 1`, `built_cifs: []`).

**What the sweep measured, stated narrowly.** V5 fails with the byte-identical
error, so `C_CRDGRP` is **not** the trigger; so does V2, so splitting the dimer
across two input files is not a workaround. V7 (chain B unindexed) and V3 (both
chains unindexed) both pass, so the measured rule is **at most one** indexed
target chain -- not "exactly one", which V3
contradicts. Per-chain `molecule_iid` was *not* captured — the
diagnostic failed on an import (`add_id_and_entity_annotations` is not in
`atomworks.io.utils.selection` in this image; see `moliid_rc: 1` in
`variants_result_v5v7.json`). **So the mechanism by which the two protomers are
judged one molecule is UNVERIFIED.** What is verified is the rule above and the
pairs the error names.

**A release-1 finding either way.** Upstream's own guidance says to use one
positive coordinate-group ID for target atoms "including across chains of a rigid
target complex" (`docs/how-to/input_prep/yaml-json.md:217-230`), and its only
worked PPI example has a single-chain target
(`docs/how-to/input_prep/examples/condition_spec_ppi.yaml`, PD-L1 chain A).
`C_IDXGRP` is not settable from the YAML spec — it is absent from the condition
table at `docs/reference/yaml-json.md:130-143` and listed as Python-API only at
`docs/how-to/input_prep/python-api.md:34`. **So through the spec format, a
multi-chain rigid target is not expressible.** This is material to release 1
because our recorded `proteina` comparator ran with `target_chain "A,B"`.

## Which variant to run, and why not the other two

**Run V4 — `spec_v4_chainA.yaml`.** It is structurally identical to upstream's
exercised PD-L1 PPI example (one `C_CRDGRP` rigid backbone group, `C_SEQ` over the
whole target chain, atom-level `C_HOT`), with our target substituted. It is the
only passing variant in which the target is a fixed, fully indexed body.

**Do not run V3 or V7 for this rung.** `C_IDX: false` does not mean "ignore the
indices of a fixed target" — upstream documents it as making an *unindexed motif*
that "the model places freely **along the new chain**"
(`docs/how-to/input_prep/index.md:113`), which is motif-scaffolding semantics: the
unindexed residues get built into the designed chain. The companion metrics
corroborate the reading (`inline_metrics.design_unindexed_motif_allatom_rmsd`,
`inline_metrics.unindexed_insertion_mapping`). Running V3 or V7 would risk folding
a target protomer into the binder, which would contaminate the one question rung 4
exists to answer. **Not tested on GPU — this is a documentation read, not a
measurement.**

**This no longer costs anything, because question 2 is withdrawn** (Leo,
2026-11-03, see "Question 2 is withdrawn" below). For the record of why it would
have: V4 designs against one protomer, while the recorded `proteina` comparator
designed against the A+B dimer, so the head-to-head would not have been
like-for-like on target composition.

## Production configuration, already verified on CPU

From the resolved Hydra dump (`rung4-probe/cpu_result.json`, `cfg_rc: 0`) and the
tag-selection check in the same file (`tagcheck_rc: 0`):

- `inference_experiment=inference_on_user_inputs` keeps the 15-bucket
  `compile_lengths` (128 through 1024), where rung 3's `quickstart` had it off.
  Whether the `torch.compile` warm-up is therefore actually paid is **INFERRED,
  not measured**: the same dump also shows `nn.torch_compile_mode: null`, and
  nothing on CPU exercised compilation. Question 3 is what settles it.
- `metrics_tags=[design,interchain_interactions,hotspot_conditioning]` selects
  **11** metrics and skips **42**. The **selected** 11 are 9 `inline_metrics.*`
  plus 2 `pre_oracle_filters.*`, and they include `amino_acid_composition`
  (question 1) and `hotspot_obedience`. The **skipped** 42 are 15
  `inline_metrics.*`, 11 `pass_criteria.*`, 4 `pre_oracle_filters.*`, 8
  `refolding_metrics.*` and 4 `refolding_oracles.*` -- every oracle and every
  pass criterion, so **no AF3 oracle and no PyRosetta container is needed**.
  (Both lists counted from the `tagcheck` block of `rung4-probe/cpu_result.json`;
  15+11+4+8+4 = 42. An earlier draft of this bullet attached the 9+2 to the
  skipped set instead of the selected one.)
- `delete_failing_pre_oracle=False` is required: tag `design` arms
  `pre_oracle_filters.sequence_adherence_filter`, which would otherwise delete
  outputs we are paying to look at.
- `num_replicates=4`, not 8: the V4 target chain is 211 residues against the 115
  of rung 3's PD-L1 example, `batch_size` is 1 in this config, and the spend
  ceiling is fixed.

## The exact fire sequence when Leo is watching

Scratchpad copies may have been cleaned up; the authoritative copies are in
`docs/scoping/rung4-probe/`. **The three input CIFs are now committed there**
(`3ave_fc_AB.cif`, `3ave_fc_A.cif`, `3ave_fc_B.cif`, ~830 KB in total), so there
is no rebuild step and the sequence starts at the pre-flight.

On regenerating them — for the record, not for the fire sequence: `prep_input.py`
takes two positional arguments and writes **one** file
(`prep_input.py <source> <out.cif>`), and it always keeps chains A and B, so it
reproduces `3ave_fc_AB.cif` only. The two single-chain files were split by an
inline `gemmi` command that was **not** committed, so `prep_input.py` alone does
not reproduce them. That is why the CIFs are committed rather than regenerated:
an earlier draft of this note claimed a bare `prep_input.py` rebuilt all three,
which would instead have died on `sys.argv` at the first step of the paid run.

## The free pre-flight RAN and PASSED on 2026-11-03 (commit 422787d2)

`--mode cpu --spec-name spec_v4_chainA.yaml`, app `ap-URDXzh8fvVRksAPNWLjbcC`,
exit 0, app stopped itself; result committed as
`rung4-probe/cpu_result_20261103T103209.json`. Cost **$0.00466825**
(`modal billing report --for today`). No GPU, no weights volume: `cpu_probe` is
declared `cpu=4, memory=16384` with no `gpu=` and no `volumes=`
(`rung4-probe/rung4_app.py:68`).

What it checked, and what each check settles:

1. **The approved spec builds its conditioning CIF.** `write_cifs_rc: 0`,
   `write_condition_spec_cifs.py` exit 0 in 27.2 s, wrote one file,
   `/tmp/cifs/fc_binder.cif`.
2. **`C_HOT` lands on the eight intended residues — this closes the hotspot
   mechanism question.** The readback column
   `_atom_site.condition_hotspot_1_atom` has exactly 8 residues `True` and 204
   `False`. The eight are PHE/PHE/PRO/LYS/THR/VAL/VAL/ARG at output residues
   8, 10, 11, 13, 27, 29, 31, 68. **Compare by identity, not by number:** the
   output CIF renumbers the target chain 1-based from the input's first present
   residue (input `label_seq_id` 10-220, so output = input − 9), and after that
   shift the flagged set is identical to the intended set in both number and
   amino acid — input labels 17/19/20/22/36/38/40/77, which are author/EU
   241/243/244/246/260/262/264/301. Asserted, not eyeballed, and now reproducible: run
   `rung4-probe/check_hotspot_readback.py`, which re-derives the remap from the
   committed `cpu_result_20261103T103209.json` and asserts the flagged set matches
   by number **and** by residue identity (PHE PHE PRO LYS THR VAL VAL ARG).
   Read naively off the numbers alone this looks like an off-by-9 wrong-epitope
   bug, which is the §10 item-5 failure class; it is not one here.
3. **`C_CRDGRP` covers the whole target chain.** CoordinateGroup `1` holds 211
   residues, exactly the chain-A residue count of the input (211, counted from
   `3ave_fc_A.cif`). The `-1` bucket reports 212; that one-residue excess is
   **UNVERIFIED** — the probe truncates long lists to a placeholder, so the
   bucket cannot be enumerated from the committed result. The likely cause is
   the per-atom split (the spec selects only `[N,CA,C,O]`, so sidechain atoms of
   the same residues fall to `-1`), but that is inference.
4. **`C_SEQ` reveals the target and leaves the binder masked.** The per-residue
   sequence column carries chain A by residue type, and the binder appears once
   as `B/1/1/60-120` under the mask key `<M>`, so the 60-120 length range
   survives into the conditioning CIF.
5. **The production configuration composes.** `cfg_rc: 0` for
   `inference_experiment=inference_on_user_inputs` with
   `metrics_tags=[design,interchain_interactions,hotspot_conditioning]`,
   `num_replicates=4` — not `quickstart`.
6. **The tag gate needs no oracle.** `tagcheck_rc: 0`; 11 metrics selected
   including `inline_metrics.amino_acid_composition` (question 1) and
   `inline_metrics.hotspot_obedience`, 42 skipped including every
   `refolding_oracles.*`, so no AF3/OpenFold3 and no PyRosetta container is
   needed for the paid run.

**Still UNKNOWN after the pre-flight:** §10 item 4, the real
`per_example_metrics.csv` header with `metrics_tags` set. The tag gate shows
which metrics are *selected*; only a run that writes the CSV shows the header.
Questions 1-4 all still need the paid run. (Superseded: the paid run answered
1, 3 and 4; question 2 was withdrawn. §5.8 carries the answers.)

Step 1, free CPU pre-flight — **this has not been run for V4 and must be, to
confirm the hotspot flags land on the eight intended residues** (author/EU
241, 243, 244, 246, 260, 262, 264, 301 maps to `label_seq_id` 17, 19, 20, 22, 36,
38, 40, 77; upstream resolves spec res_ids against `label_seq_id`, per
`docs/reference/yaml-json.md`):

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m modal run docs/scoping/rung4-probe/rung4_app.py --mode cpu --spec-name spec_v4_chainA.yaml
```

Read `condition_columns` in the written `cpu_result_<timestamp>.json`. It is keyed
by output CIF file name, one entry per CIF the writer built; in every entry the
`C_HOT` column must mark exactly those eight residues of chain A and nothing else.
**If it does not, stop — do not spend.**

Before either step the entrypoint checks locally that the chosen spec and every
CIF it names are present next to the script, and exits without calling Modal if
one is missing, so a stale glob cannot allocate an A100 and bill for an input that
was never staged (`rung4_app.py`, the loop over `specs` in `main()`).

Step 2, the one approved paid run (A100-80GB, `timeout=1020` s). **The two cost
bases must not be mixed:** §6.1 says its ladder figures, the ~$1.85 for this rung
included, are our **raw** A100-80GB cost. At the §6 raw rate of
$0.001028/GPU-second the 1020 s timeout bounds this run at **$1.05 raw**, inside
the approval; the same ceiling is **$1.78** at the customer rate of
$0.0017476/customer-GPU-second (raw x 1.70). The subprocess self-kills at 870 s so
a Modal timeout cannot swallow the outputs:

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m modal run docs/scoping/rung4-probe/rung4_app.py --mode gpu --num-replicates 4 --spec-name spec_v4_chainA.yaml
```

It writes `gpu_result_<timestamp>.json` next to the script — timestamped, so a
second invocation cannot overwrite the paid run's only artifact — with `wall_s`,
`rc`, `timed_out`, the per-file first-sighting times (for the first-design wall-clock and the
`"Compiling model for padded length N tokens"` log line), the stdout/stderr,
and the output files inline — rung 3 lost the metrics CSV to a dead container and
needed a second paid run to get it back. The capture is narrower than
"everything": it takes files whose suffix is `.cif`, `.csv`, `.json`, `.yaml` or
`.txt`, plus `*.cif.zst`, and it truncates stdout and stderr to the last 200 000
characters each. `tree` lists every path in the output directory, so anything not
captured is at least named.

**One run only.** If it fails, stop and report the cost and the proposed change;
do not retry.

## Already prepared, no further work needed

- The comparator side is built with **no GPU spend**: `rung4-probe/comparator_seqs.py`
  and `comparator_seqs.json`, read from the recorded `proteina_direct_out/`
  coordinates (binder is chain C in all eight designs; lengths 113, 98, 74, 107,
  74, 113, 107, 98).
- **Superseded: question 2 is withdrawn, so §5.8 carries none of this bullet or
  the next.** Kept as the record of why the comparison would have been weak.
  Pairing each binder length with its
  longest exact shared block against target chain A: 113/51, 98/98, 74/4, 107/54,
  74/74, 113/51, 107/80, 98/98. **Three of the eight comparator binders are
  verbatim substrings of the target.** Of the other five the copied fractions are
  45%, 5%, 50%, 45% and 75%, so "largely copied" fits one of the five, not most —
  an earlier draft of this line overstated it. The recorded AF2 scores on that run
  are also junk (af2_iptm 0.087–0.100, binder_scrmsd 34–41 A). A favourable
  head-to-head against this baseline would mean very little — which is part of why
  dropping it costs us nothing.
- **Evidence status for the bullet above:** the eight binder lengths are verified
  from `rung4-probe/comparator_seqs.json`. The shared-block lengths and the AF2
  scores were computed in-session from `proteina_direct_out/` and
  `.proteina_direct_call_state.json`, **neither committed here**, so those two
  figures are UNVERIFIED from this commit alone.

## Still open for §5.8

## Question 2 is withdrawn (Leo, 2026-11-03)

Leo withdrew question 2 — the `proteina` head-to-head — before the paid run, in
his own words: *"our users choose the tools, not us, we don't have to tell them
whats better or not, that will change depending on target."* It is a product
decision, not a measurement problem, and it stands.

Two consequences:

1. **The Fc target is no longer required, only incidental.** The target was never
   recorded as a decision anywhere in the scope doc; what forced it was question
   2's need to match the one recorded `proteina` comparator run. With question 2
   gone, nothing forced this target, and **Leo then dropped it** — see the next
   section. §5.8 must not imply the Fc target was chosen on its merits.
2. **The comparator analysis is history, not a deliverable.** The binder-length
   and shared-block figures below, and the junk AF2 scores, were gathered to let
   §5.8 qualify a head-to-head. There is no head-to-head now, so §5.8 carries
   none of it. The bullets stay here as the record of why that comparison would
   have been weak, and the chain-A-vs-dimer mismatch above stops being a cost at
   all.

The paid run answers questions 1, 3 and 4. Its target and design count changed
with the switch recorded in the next section.

- Questions 1-4 are the four the rung-4 brief set, not a numbering from the scope
  doc: (1) sequence credibility under a production configuration, against rung 3's
  ~70% glutamate; (2) head-to-head against the recorded `proteina` run —
  **WITHDRAWN by Leo 2026-11-03, not answered and not to be answered**; (3)
  first-design wall-clock where the compile warm-up is actually paid; (4) §10 item
  4, the real `per_example_metrics.csv` header with `metrics_tags` set. All four
  need the paid run; none can be answered from CPU.
- §10 item 4, the real `per_example_metrics.csv` header **with** `metrics_tags`
  set, is reachable oracle-free (the tag check above proves the selection, not the
  header) — it closes only if step 2 runs.
- Per-chain `molecule_iid` derivation: UNVERIFIED, see above. Not a blocker.

## Target switched to rung 3's input (Leo, 2026-11-03), pre-flight PASSED

Leo's instruction: *"switch to the same as rung 3, so we see if the settings give
us a protein without poly Glu."*

**Why this is the stronger experiment.** Rung 3's 70%-glutamate binder
(scope doc §5.7) was produced from
`/opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml` under
`inference_experiment=quickstart`. Running rung 4 on the Fc spec would have
changed the target *and* the configuration at once, so a credible result could
not have been attributed to either. Reusing rung 3's exact input leaves the
configuration as the only variable, against a recorded baseline.

**What the switch costs.** The target is 115 aa (scope doc §5.7) against the Fc
chain A's 211, so the question-3 wall-clock is again measured on a
smaller-than-production target — the same limitation §5.7 carries. Accepted
deliberately: question 3 only informs the $1.00 start-minimum figure, which
§7.1 already records as a Leo decision with roughly 8x headroom, whereas
question 1 decides whether RFD4 ships at all.

**What the switch does not cost.** The Fc work was not wasted and is not
re-bought: the 2026-11-03 pre-flight above already closed the hotspot-numbering
mechanism (§10 item 5's failure class) on our own PDB-to-CIF converted file, on
free CPU. No GPU spend was ever needed for it.

**Pre-flight on the new input, app `ap-nBkWyfPz7zJbLE0xAmIUsN`, exit 0,
self-stopped.** Result committed as
`docs/scoping/rung4-probe/cpu_result_20261103T112936.json`; every figure below is
a key in that file. Command:

```
python -X utf8 -m modal run docs/scoping/rung4-probe/rung4_app.py --mode cpu   --spec-name //opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml
```

The leading `//` is a Git-Bash path-conversion escape, not part of the path: a
single leading `/` was rewritten to `C:/Program Files/Git/opt/...` and the run
exited before any container started.

1. **The spec builds its conditioning CIF.** `write_cifs_rc: 0`, `built_cifs:
   ["/tmp/cifs/pdl1_binder.cif"]`.
2. **It sets hotspots, and they are a three-residue set** — `TYR40`, `MET99`,
   `TYR107` carry `_atom_site.condition_hotspot_1_atom: True`; 116 residues carry
   `False`. So hotspot conditioning is exercised on GPU after all, which the Fc
   spec was partly there to do.
3. **`C_SEQ` reveals the target and masks the binder** at `B/1/1/50-120` — the
   50-120 binder-length draw, matching §5.7's contig `50-120, /0, A17-131`.
   Target chain A is revealed residue-by-residue, 115 residues.
4. **`C_CRDGRP` group 1 covers 115 residues**, equal to the revealed target
   count. The `-1` bucket's 116 is **UNVERIFIED** for the same reason as the Fc
   readback: the probe truncates lists over 40 entries, so the one-residue excess
   cannot be identified from the committed file.
5. **The production configuration composes against this spec** — `cfg_rc: 0` for
   `inference_experiment=inference_on_user_inputs` with `metrics_tags` set,
   `delete_failing_pre_oracle=False` and `ckpt_name=rfd4_proteina_flow-ema.ckpt`.
6. **The tag gate selects an amino-acid-composition metric and no oracle.**
   `tagcheck_rc: 0`; selected includes `inline_metrics.amino_acid_composition`
   and `inline_metrics.hotspot_obedience`; every `refolding_oracles.*` is
   skipped. **Question 1 therefore gets a measured column from the model itself,
   not only a composition count of my own** — and the run needs no AF3,
   OpenFold3 or PyRosetta.
7. **`pre_oracle_filters.sequence_adherence_filter` is armed** in the selected
   set, which is why `delete_failing_pre_oracle=False` is required: without it a
   design we paid for could be deleted before we read it.

**Spend.** The pre-flight is CPU-only — `cpu_probe` declares `cpu=4`,
`memory=16384`, no `gpu=` and no `volumes=` (`rung4_app.py:71`). `modal billing
report --for today` does not yet itemise `ap-nBkWyfPz7zJbLE0xAmIUsN`; the
identical earlier probe `ap-URDXzh8fvVRksAPNWLjbcC` cost **$0.00466825**, so this
one is expected to be the same order. That figure is **unverified for this app**
until billing catches up. GPU spend as at 2026-11-02 was **$0.00** of the approved ~$1.85. (Superseded by
the paid run; §5.8's spend table is the authority, and at the time of writing it
the GPU app was still not itemised by `modal billing report`.)

**The paid command this changes to**, 8 replicates to match §5.7 design-for-design:

```
python -X utf8 -m modal run docs/scoping/rung4-probe/rung4_app.py --mode gpu   --num-replicates 8   --spec-name //opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml
```

A100-80GB, `timeout=1020` on the Modal function with an inner subprocess
self-kill at 870 s (`rung4_app.py:293`; the `timeout=1020` is at `rung4_app.py:252`). Projecting §5.7's measured
`65 + 12.3 x N` to N=8 gives 163 s eager; the production preset adds a
`torch.compile` warm-up upstream describes as costing minutes, which is the thing
question 3 exists to measure. The 1020 s ceiling bounds raw spend near **$1.05**,
inside the approved ~$1.85 — and **below** what the 211 aa Fc target would have
cost. No new spend approval is needed for a cheaper run; the change of shape is
reported to the lead regardless.
