# RFD4-Proteina rung 4 — PARKED before any GPU spend, 2026-11-02

**GPU spend so far on rung 4: $0.00 of the ~$1.85 Leo approved. No GPU function
has ever been invoked** — `gpu_run` in `docs/scoping/rung4-probe/rung4_app.py`
was only ever *created* by the Modal client, never called; every probe ran through
`cpu_probe` or `variant_probe` (`cpu=4`, no `gpu=` and no weights volume mounted).
The evidence is that the committed console logs show only `Created function
gpu_run` with no invocation of it, and that `rung4-probe/` holds no gpu-mode
result file at all (`ls` of the directory: only `cpu_result.json` and
`variants_result_v5v7.json`). The CPU probe containers are themselves not free — just
small, and not against this budget. The $0.00 is specifically the approved GPU
budget.

The CPU side is now measured rather than left open. `modal billing report --start
2026-10-28 --json`, filtered to the four rung-4 probe app ids, totals
**$0.04012393**, every row dated 2026-11-02 (ap-2uZ3k5ao3MUNNBtbPZ441m
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
never been dereferenced remotely — `Image.from_dockerfile` reads its argument
lazily, at local declare time. `_REPO_RFD4` now searches upward for a real
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

**The cost to question 2, stated plainly:** V4 designs against one protomer, while
the recorded `proteina` comparator designed against the A+B dimer. The head-to-head
is therefore **not** like-for-like on target composition, and §5.8 must say so.

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
- §5.8 must carry the comparator's weakness. Pairing each binder length with its
  longest exact shared block against target chain A: 113/51, 98/98, 74/4, 107/54,
  74/74, 113/51, 107/80, 98/98. **Three of the eight comparator binders are
  verbatim substrings of the target.** Of the other five the copied fractions are
  45%, 5%, 50%, 45% and 75%, so "largely copied" fits one of the five, not most —
  an earlier draft of this line overstated it. The recorded AF2 scores on that run
  are also junk (af2_iptm 0.087–0.100, binder_scrmsd 34–41 A). A favourable
  head-to-head against this baseline would mean very little, and the write-up must
  not let it stand unqualified.
- **Evidence status for the bullet above:** the eight binder lengths are verified
  from `rung4-probe/comparator_seqs.json`. The shared-block lengths and the AF2
  scores were computed in-session from `proteina_direct_out/` and
  `.proteina_direct_call_state.json`, **neither committed here**, so those two
  figures are UNVERIFIED from this commit alone.

## Still open for §5.8

- Questions 1-4 are the four the rung-4 brief set, not a numbering from the scope
  doc: (1) sequence credibility under a production configuration, against rung 3's
  ~70% glutamate; (2) head-to-head against the recorded `proteina` run; (3)
  first-design wall-clock where the compile warm-up is actually paid; (4) §10 item
  4, the real `per_example_metrics.csv` header with `metrics_tags` set. All four
  need the paid run; none can be answered from CPU.
- §10 item 4, the real `per_example_metrics.csv` header **with** `metrics_tags`
  set, is reachable oracle-free (the tag check above proves the selection, not the
  header) — it closes only if step 2 runs.
- Per-chain `molecule_iid` derivation: UNVERIFIED, see above. Not a blocker.
