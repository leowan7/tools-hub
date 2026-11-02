# RFD4-Proteina rung 4 — PARKED before any GPU spend, 2026-11-02

**Spend so far on rung 4: $0.00 of the ~$1.85 Leo approved. No GPU function has
ever been invoked** — `gpu_run` in `docs/scoping/rung4-probe/rung4_app.py` was
only ever *created* by the Modal client, never called; every probe ran through
`cpu_probe` or `variant_probe` (`cpu=4`, no `gpu=` and no weights volume mounted).
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

## The five spec variants tested, all on free CPU

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
from it in-session and are re-derivable by re-running `--mode variants` with
those spec names. The V1 rows are evidenced by `rung4-probe/cpu_probe2_console.log`
and by `rung4-probe/cpu_result.json` (`write_cifs_rc: 1`, `built_cifs: []`).

**What the sweep measured, stated narrowly.** V5 fails with the byte-identical
error, so `C_CRDGRP` is **not** the trigger; so does V2, so splitting the dimer
across two input files is not a workaround. V7 passes, so **exactly one target
chain may stay indexed**. Per-chain `molecule_iid` was *not* captured — the
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

**The cost to question 2, stated plainly:** V4 designs against one protomer, while
the recorded `proteina` comparator designed against the A+B dimer. The head-to-head
is therefore **not** like-for-like on target composition, and §5.8 must say so.

## Production configuration, already verified on CPU

From the resolved Hydra dump (`rung4-probe/cpu_result.json`, `cfg_rc: 0`) and the
tag-selection check in the same file (`tagcheck_rc: 0`):

- `inference_experiment=inference_on_user_inputs` keeps the 15-bucket
  `compile_lengths` (128 through 1024), so the `torch.compile` warm-up **is** paid — which is what
  question 3 needs re-measured, since rung 3's `quickstart` had it off.
- `metrics_tags=[design,interchain_interactions,hotspot_conditioning]` selects
  **11** metrics and skips **42**: 9 inline metrics plus 2 pre-oracle filters,
  including `amino_acid_composition` (question 1) and `hotspot_obedience`. Every
  `refolding_oracles.*`, every `refolding_metrics.*` and every `pass_criteria.*`
  is skipped, so **no AF3 oracle and no PyRosetta container is needed**.
- `delete_failing_pre_oracle=False` is required: tag `design` arms
  `pre_oracle_filters.sequence_adherence_filter`, which would otherwise delete
  outputs we are paying to look at.
- `num_replicates=4`, not 8: the V4 target chain is 211 residues against the 115
  of rung 3's PD-L1 example, `batch_size` is 1 in this config, and the spend
  ceiling is fixed.

## The exact fire sequence when Leo is watching

Scratchpad copies may have been cleaned up; the authoritative copies are in
`docs/scoping/rung4-probe/`. Step 0 rebuilds the three input CIFs from the PDB
entry, since they are not committed (about 1 MB, regenerable):

```bash
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe docs/scoping/rung4-probe/prep_input.py
```

Step 1, free CPU pre-flight — **this has not been run for V4 and must be, to
confirm the hotspot flags land on the eight intended residues** (author/EU
241, 243, 244, 246, 260, 262, 264, 301 maps to `label_seq_id` 17, 19, 20, 22, 36,
38, 40, 77; upstream resolves spec res_ids against `label_seq_id`, per
`docs/reference/yaml-json.md`):

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m modal run docs/scoping/rung4-probe/rung4_app.py --mode cpu --spec-name spec_v4_chainA.yaml
```

Read `condition_columns` in the written `cpu_result.json`: the `C_HOT` column must
mark exactly those eight residues of chain A and nothing else. **If it does not,
stop — do not spend.**

Step 2, the one approved paid run (A100-80GB, `timeout=1020` s, which bounds the
spend at **$1.78 customer** at the §6 rate of $0.0017476 per customer-GPU-second;
the subprocess self-kills at 870 s so a Modal timeout cannot swallow the outputs):

```bash
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m modal run docs/scoping/rung4-probe/rung4_app.py --mode gpu --num-replicates 4 --spec-name spec_v4_chainA.yaml
```

It writes `gpu_result.json` next to the script with `wall_s`, `rc`, `timed_out`,
the per-file first-sighting times (for the first-design wall-clock and the
`"Compiling model for padded length N tokens"` log line), the full stdout/stderr,
and every output `.cif`/`.csv` inline — rung 3 lost the metrics CSV to a dead
container and needed a second paid run to get it back, so nothing is left behind
this time.

**One run only.** If it fails, stop and report the cost and the proposed change;
do not retry.

## Already prepared, no further work needed

- The comparator side is built with **no GPU spend**: `rung4-probe/comparator_seqs.py`
  and `comparator_seqs.json`, read from the recorded `proteina_direct_out/`
  coordinates (binder is chain C in all eight designs; lengths 113, 98, 74, 107,
  74, 113, 107, 98).
- §5.8 must carry the comparator's weakness: the longest exact shared block
  against target chain A is 51/98/4/54/74/51/80/98 residues, so **three of eight
  comparator binders are verbatim substrings of the target** and most of the rest
  are largely copied, and the recorded AF2 scores on that run are junk
  (af2_iptm 0.087–0.100, binder_scrmsd 34–41 A). A favourable head-to-head against
  this baseline would mean very little, and the write-up must not let it stand
  unqualified.

## Still open for §5.8

- Questions 1-4 all need the paid run; none can be answered from CPU.
- §10 item 4, the real `per_example_metrics.csv` header **with** `metrics_tags`
  set, is reachable oracle-free (the tag check above proves the selection, not the
  header) — it closes only if step 2 runs.
- Per-chain `molecule_iid` derivation: UNVERIFIED, see above. Not a blocker.
