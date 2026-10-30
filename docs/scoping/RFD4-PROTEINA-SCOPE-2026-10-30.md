# RFD4-Proteina — research and scoping pass

Date: 2026-10-30. Author: hub worker session (research only).
Scope of this document: research and scoping. **No product code was changed, no Modal app was deployed, and no GPU job was launched. No money was spent.**

Every runtime, cost, VRAM and size figure below names the file, command or URL that produced it. Anything else is labelled **UNVERIFIED**, **NOT FOUND** or **DERIVED**.

Cite convention: a bare `path:line` is this repository at `origin/main` (`045a61dc`). Upstream cites name the GitHub path in `RosettaCommons/RFD4-Proteina` at `main` = `aaccdd2ad0720c180468df5f9cf037a713d403e5`, 2026-10-22 (`gh api repos/RosettaCommons/RFD4-Proteina/commits/main`), or a URL.

---

## 0. Recommendation in one line

**Add RFD4-Proteina alongside `proteina` and `rfdiffusion` as a new slug `rfd4`, scoped at first release to protein-binder design only — but gate the build behind one cheap, non-negotiable compatibility probe (rung 1 below, $0), because the model requires a CUDA-13-capable driver and every GPU image we ship today is built on a CUDA-12 base.**

Three facts drive that shape:

1. **It is not a variant of our `proteina` tool.** Our `proteina` is NVIDIA's Proteina/Complexa line (`tools/proteina/Dockerfile.modal:54` pins `916eaaed` of that upstream). RFD4-Proteina is a different, later model that *benchmarks against* Complexa and beats it by a wide margin on the authors' own figures (§3). Keeping the two slugs separate is correct; sharing a slug would silently change what a repeat customer is buying.
2. **On the authors' own benchmark it is the strongest binder generator in the table, including against the three we already sell.** §3 has the numbers, with their caveats.
3. **The licence story is clean for the path we want and dirty for a path we do not need.** The core generation path is BSD-3. The optional AlphaFold2 / OpenFold3 refolding images carry separate upstream terms, and the upstream model card states plainly that neither oracle is needed for generation. We exclude them. That has a product consequence (§5) worth Leo's attention: without a refold oracle, our results page cannot show an AF2 ipTM, which is the number our `proteina` and `rfdiffusion` results pages lead with.

**§2A tests that one-preset recommendation against every modality the model supports** — eight with a runnable shipped config, six without — and the recommendation survives: the three commercially interesting additions (customer ligand, enzyme cofactor, DNA) all need the same piece of platform work on our side (a CIF-preserving staging path, §2A.2), and two of the three *also* lack a customer-input route upstream. §2A.7 carries the staged release map and the one narrow widening worth considering.

**Scoring is the one thing this shape costs us, and §7A is new:** RFD4 ships no default score, and every pass/fail criterion that bears on binder quality is computed from the refold oracle we exclude. The oracle-free interface metrics it does compute are real but carry no confidence number, so release 1 should chain a fold through a tool we already own.

Nothing below is an instruction to spend. Every GPU rung in §6 is a proposal for Leo to approve or refuse individually; rung 1 ($0, no GPU) was approved on 2026-10-30.

---

## 1. What RFD4-Proteina is

| | |
|---|---|
| Upstream | `RosettaCommons/RFD4-Proteina`, created 2026-09-25, 514 stars, **7 open issues** (`gh api "search/issues?q=repo:RosettaCommons/RFD4-Proteina+is:issue+is:open"` on 2026-10-30; the repo's `/issues` endpoint returns 21 because it counts pull requests too), no GitHub Releases, tags `v0.0.1`–`v0.0.3`, PyPI classifier "Development Status :: 3 - Alpha" (`gh api repos/RosettaCommons/RFD4-Proteina`; `pyproject.toml`) |
| Weights | `nvidia/RFD4-Proteina` on Hugging Face, ungated, BSD-3 per the model card |
| Lineage | Baker Lab × NVIDIA GenAIR. Successor umbrella over *both* the RFdiffusion line and the Proteina/Complexa line (README) |
| Licence | BSD-3 for code, model and weights (`LICENSE`; HF model card states it explicitly) |

**What it generates.** Sequence *and* all-atom structure together, in one model — the HF card: "generates protein sequences and all-atom structures together". **This is the single biggest difference from everything else we host.** `rfdiffusion` gives backbones that need ProteinMPNN; `proteina` runs its own downstream designability stack. RFD4 needs no MPNN step.

**Conditioning modes named upstream:** monomers, protein binders, DNA binders, antibodies and nanobodies, enzymes, motif scaffolding, small-molecule pockets (README). Also partial diffusion (re-noise an existing design), inverse folding, and a reward-guided beam search over generation paths.

**Symmetry: NOT FOUND** as a named conditioning mode. A `cyclic_peptide_binder` preset exists in `configs/design_tasks/presets/`, but upstream issue #62 asks whether cyclic peptide design is supported at all — treat it as unsupported until upstream answers.

### 1.1 Against what we already sell

| | our `proteina` | our `rfdiffusion` | RFD4-Proteina |
|---|---|---|---|
| Model family | NVIDIA Proteina / Complexa | RFdiffusion v1 | RFD4 (Baker × NVIDIA) |
| Output | backbone + downstream designability stack | backbone only, MPNN + AF2 in the container | **sequence + all-atom structure from one model** |
| Target input | PDB/CIF via our normalizer | PDB | **annotated CIF or a YAML/JSON condition spec; PDB input NOT FOUND in upstream docs** (§4 — this is a real integration cost) |
| In our stack | `tools/proteina/` — the one tool whose container lives in this repo | sibling repo `llm-proteinDesigner` | would be a new in-repo container, copying `proteina` |

**Does it supersede either one?** On paper it dominates both — see §3. In practice: **no, not yet, and not on the authors' numbers alone.** Three reasons to add rather than replace. (a) It is alpha software, four weeks old, with 7 open issues, one of them a binder-mode bug (upstream #81, alanine over-representation on the non-interacting face under the binder-design protocol). (b) Our `proteina` and `rfdiffusion` are live-charging and have run real paid customer jobs; RFD4 has run nothing here. (c) `rfdiffusion` has never produced a successful binder for us in the runs on record, so the case for retiring *it* is worth making separately and on our own data, not on an upstream leaderboard. Revisit replacement after RFD4 has a month of real runs.

---

## 2. Recommended first-release scope

**Ship one preset: `protein_binder`. Nothing else.**

In scope for release 1:
- Protein-target binder design, hotspot-directed or open-search.
- Single conditioning recipe: `C_CRDGRP` (target backbone coordinates) + `C_SEQ` (target sequence) + optional `C_HOT` (hotspots), which is exactly upstream's documented ten-line binder example.
- Inline metrics only — no refolding oracle (§5).

Explicitly out of scope for release 1, each a later slug or preset: DNA binders, small-molecule pockets, antibodies/nanobodies (we sell `rfantibody` and `iggm` there already), enzyme/atomic-motif scaffolding, partial diffusion, inverse folding, reward-guided beam search, cyclic peptides.

**§2A examines every one of those exclusions against the upstream configs and docs rather than asserting them, and adds a staged release map.** It confirms this scope, with one qualified addition (an unconditional-monomer preset, worth it only if it costs under a day, as a $0-upload smoke test for the slug). Read §2A.2 before costing any of the later stages: our own structure intake converts every upload to PDB and stages only those bytes, which is what actually blocks ligand, enzyme-cofactor and DNA design here.

Reward-guided search deserves a note, because it is the headline mode: on the upstream benchmark it is the strongest arm by a distance (28.1% pass vs 17.0% for one-shot medium, §3), but it costs ~27× the GPU seconds per design (62.5 s vs 2.35 s median, same source) and upstream issue #64 reports a multi-GPU beam-search reward bug. It is the obvious *second* release, priced as its own preset, once one-shot is earning.

---

## 2A. Every modality the model supports, and what each one would cost us

Added 2026-10-30 at the hub lead's request: §2 recommends one preset, so this section tests that recommendation against the *whole* capability surface rather than the binder slice.

Reads in this section are at the same upstream revision as the rest of the document (`main` = `aaccdd2ad0720c180468df5f9cf037a713d403e5`, re-confirmed 2026-10-30 via `gh api repos/RosettaCommons/RFD4-Proteina/commits/main`), by `gh api repos/RosettaCommons/RFD4-Proteina/contents/<path>?ref=main --jq .content | base64 -d`. Nothing was cloned, no weights or images were fetched, and **no GPU ran**.

**Provenance labels used below.** FIRST-HAND = I decoded and read the cited file myself in this session. SECOND-HAND = a delegated reader decoded it and quoted it; I did not re-read that file, so the citation is its verification, not mine. Every benchmark aggregate in §2A.4 that is not a website headline is SECOND-HAND.

### 2A.1 There is no `modality` switch — which changes what a "preset" means for us

The first finding reshapes the rest. Upstream has **no `modality:` key and no per-modality inference config**. A modality is the intersection of three things:

1. **The input itself.** `inputs:` is an annotated CIF (preferred), a directory of them, or a YAML/JSON condition spec — `configs/inference_experiment/TEMPLATE.yaml:65`, `inputs: ???  # Path to annotated CIF or directory of CIFs (preferred) or JSON input` (FIRST-HAND, quoted in §4.1 already). Whether the job is a PPI binder, a ligand binder, an enzyme or a DNA binder is encoded in that file's `contig` and its `C_*` condition blocks — not in any YAML switch.
2. **`metrics_tags`** — a flat list of strings. Every metric, oracle and pass criterion declares `required_tags_any` / `required_tags_all` / `prohibited_tags` and runs only if the run's tags satisfy it. This is the only place a shipped config names a modality.
3. **`search.checkpoints.<step>.{metrics,rewards}`** — which provider steers beam search, when search is on at all.

The default is inert: `configs/inference.yaml:71` is `metrics_tags: []`, and `search.checkpoints: null` (`:73-77`). So a plain default run **computes no metrics and fires no oracle** (FIRST-HAND).

The tag vocabulary that exists is readable off one file, `configs/metrics/pass_criteria/all_pass_criteria.yaml` (FIRST-HAND, tags and line numbers re-read directly):

| Pass criterion | Gate | Line |
|---|---|---|
| `unconditional` | `required_tags_any: [unconditional_refolding]` | :12 |
| `ppi` | `[ppi_refolding]` | :23 |
| `antibody` | `[antibody]` | :32 |
| `dna` | `[dna_refolding]` | :42 |
| `motif_scaffold` | `[motif_scaffold_refolding]` | :53 |
| `internal_coords` | `[internal_coordinate_motif_scaffold_refolding]` | :59 |
| `sm_binder` | `[sm_binder_refolding]` | :72 |
| `ame` | `[ame_refolding]`, `prohibited_tags: [no_ligand]` | :85-86 |
| `ame_no_ligand` | `[ame_refolding]` + `required_tags_all: [no_ligand]` | :95-96 |
| `disco` | `[disco_refolding]` | :105 |
| `developability` | `[developability]` | :120 |

**There is no peptide, cyclic-peptide, VHH-specific or RNA pass criterion in that file.** The VHH route reuses the `antibody` criterion, whose bars are *looser* than `ppi` (interface PAE ≤ 2.0 rather than ≤ 1.5, binder RMSD ≤ 2.0 rather than ≤ 2.5, and no pTM bar at all — `:25-32`).

Three consequences for us:

- **No modality needs a different checkpoint.** One denoiser + autoencoder pair serves all of them (`configs/inference.yaml:38-46`, already cited in §5.2). So "add a modality" never means "add weights" — a real saving against the shape of `rfantibody`, `bindcraft` and `iggm`.
- **A hub "preset" is a four-part bundle**, not a flag: a condition-spec template, a `metrics_tags` list, a recycle strategy, and optionally a `search` block. That is the unit of work for every row below.
- **Ten of the eleven pass criteria need a refolding oracle** (every tag ending `_refolding`, plus `antibody`); only `developability`, which thresholds SAP, GRAVY and instability index, runs without one. We excluded the AF2/OF3 images on licence grounds (§0, §5). So for *every* modality our results page shows inline metrics only — stated for release 1 in §5, and it generalises.

Pre-oracle filters carry a trap that applies to every row: *"Filters gate on the same tags as the metric they threshold — a filter whose metric never ran sees a missing key, which counts as a (non-deleting) failure and drops every design"* (`configs/metrics/pre_oracle_filters/all_pre_oracle_filters.yaml:11-12`, now FIRST-HAND, re-read directly). Read with `:2` of the same file — *"Rejected designs remain zero-pass attempts in per-example summaries"* — the precise hazard is **zero *passing* designs on a run we have fully billed**, not zero files: the filter is marked non-deleting, and the `delete_failing_pre_oracle: True` default applies *"only … when a pre-oracle design filter is active (i.e. its tag gate matched)"* (`configs/inference.yaml:41-44`, FIRST-HAND), which is the opposite of the mis-tagged case. So nothing here is evidence that designs are lost; the money exposure is a customer paying a full run for an all-zero pass summary. It belongs in the rung-3 checklist (§6.1) for **whichever** modality ships, and the harness must assert on delivered-design count, not on pass count.

### 2A.2 The decisive hub-side blocker: we stage PDB bytes, and RFD4 wants an annotated CIF

This is the finding that orders the staged map, and it is in our code, not upstream. An earlier draft of this section located the blocker in `shared/pipeline_normalize.py`; the code review of this delta showed that is the wrong file, and the corrected version below is both narrower and better news. **Read this paragraph before costing release 3 — a reader who acts on the old framing would edit a module a new RFD4 container never loads.**

**What does *not* block us.** `shared/pipeline_normalize.py:31-34` defines `STANDARD_AA` as exactly the twenty standard residues, and the per-residue loop drops anything else at `:346-349` (FIRST-HAND):

```python
# Standard-AA filter (after MODRES_MAP applied)
if resname not in STANDARD_AA:
    dropped_count += 1
    continue
```

But that copy of the module is only ever run **server-side in dry run**: `shared/pdb_preflight.py:628` calls `preview(tmp_in.name, None, …)` on a tempfile copy of the upload with a `None` output path — it produces a verdict and rewrites nothing. The copy that strips in-container is the sibling repo's `backend/pdb_utils/pipeline_normalize.py`, which the boltzgen/pxdesign images mount and which is *not* byte-identical to ours (`shared/pdb_preflight_rules.py:241-246`: *"Checking only the vendored copy would have been an argument about a file the GPU never loads"*). And the one container that lives in **this** repo — `tools/proteina/`, the one §8 and §2A.5 propose copying RFD4's from — never calls the normalizer at all (`shared/pdb_preflight_rules.py:248-249` and `:976-977`: *"proteina's container does not run `pipeline_normalize` at all"*). So a new in-repo RFD4 container inherits **no** standard-AA filter and no HETATM strip. There is nothing to remove.

**What does block us**, in-repo and unavoidably: intake converts every structure to PDB and stages only those bytes. `shared/pdb_intake.py:482-488` states that `data`/`filename` are post-conversion — an mmCIF arrives as `.cif` and leaves as PDB bytes under a `.pdb` name, *"because everything downstream (Storage, the presigned URL, the container) assumes PDB"* — with the conversion at `:563` via `convert_cif_to_pdb_bytes` → `shared/pdb_inspect.py:239-268`, a Biopython `MMCIFParser` → `PDBIO` round trip. Bonds and formal charges have no home in a PDB file and do not survive it. RFD4 wants exactly those: an annotated CIF.

**And disclosure already exists, as an aggregate.** The docstring at `shared/pdb_preflight.py:7-8` does say the cleanup happens *"silently, so by itself it's invisible to the user"* — but `:9-10` continues *"This module surfaces the cleanup as a user-facing verdict"*, and it does: `shared/pdb_preflight.py:1324-1334` emits `"{n} residue(s) dropped (waters / HETATM / incomplete backbone)"` and `"Other chain(s) dropped: …"` into `CleanupSummary.items`, *"the bulleted list rendered under 'Cleanup applied:' in the preflight panel"* (`:84-85`), wired at `blueprints/tools.py:1518-1551`. So the honest statement is narrower than "nothing tells the customer": the customer is told **a count**, not *which* ligand went. Itemising it is a smaller build than a new report, and §2A.5's "new preflight" columns should be costed that way.

Two tiers follow.

- **Protein-only modalities** (PPI binder, monomer, protein-motif scaffolding, VHH) survive in substance — the twenty amino acids are all PDB ever needed to carry. Regenerating a CIF from the staged PDB is work, but it is the work §4 already scopes for release 1.
- **Every non-protein modality** (ligand binder, enzyme active site with its cofactor, DNA binder, RNA, glycan, PTM, non-canonical) needs a **CIF-preserving staging path**: keep the original CIF bytes alongside the converted PDB so the container can be handed the annotated file with its bonds and charges intact, plus a preflight that can reason about a ligand and itemise what was kept. Still multi-PR — it changes what `TargetUpload` means and what every consumer of the staged object may assume — but it is additive to intake rather than surgery on each tool's normalizer, and it is shared across all of them, so it is paid once and then amortised.

A second, smaller constraint from our side, which bites in release 2 rather than release 1: `shared/score_legends.py:925-945` warns that the legend map is keyed on `(tool, column)` **with no preset dimension** — *"a bar may be mode-scoped, a legend may not."* So if two presets under one slug report different quantities under the same column name, the legend lies for one of them. Mitigation is cheap but must be deliberate: every preset's score column gets a name that carries exactly one quantity, or each modality gets its own slug.

### 2A.3 What ships, per modality

"Shipped config" below means a runnable config or example bundle in the repo, not merely a mentioned capability. By that definition the table splits **eight / six**, not ten / four: rows 1, 2, 4, 5, 7, 8, 9 and 10 have a shipped config; rows 3 and 6 have a pass criterion and (for 6) an oracle but **no inference config anywhere**, and rows 11–14 are named only. The six without a config are not runnable without writing one, so rows 3 and 6 are mechanism-plus-scoring, not products.

| # | Upstream name | Switched on by | User must supply | Needs anything we excluded? | Shipped config |
|---|---|---|---|---|---|
| 1 | **Protein binder (PPI)** | condition spec: `contig: "A278-382, /0, BINDER:55-75"` + `C_CRDGRP` (target backbone) + `C_SEQ` (reveal target sequence) + optional `C_HOT` (hotspots); `metrics_tags: [design, ppi_refolding]` | target structure, chain, residue range, optional hotspots, binder length range | **Oracle yes for the `ppi` pass bar** (`ppi_refolding` → `oracle_ppi`). Generation itself needs none. `ppi_novelty_of3_search.yaml` additionally requires **OpenFold3 weights** (`AF3_MODEL_DIR`) *and* an external Foldseek archive (`metrics.inline_metrics.novelty.archive_dir: ???`) | `configs/inference_experiment/ppi_novelty_of3_search.yaml`; plus `docs/examples/ppi_specificity_tutorial.ipynb` writing its own configs over `examples/trka_crossreactivity/targets/*.cif.zst` |
| 2 | **Unconditional monomer** | `metrics_tags: [design, unconditional_refolding]`; inputs = `data/benchmarks/monomer`, `subset_to_examples: monomer-short` | length only (no structure at all) | Oracle yes for the pass bar; `inference_beam_regression.yaml` is deliberately oracle-free, rewarding on clash count instead | `inference_beam_example.yaml` (AF3-steered), `inference_beam_regression.yaml` (oracle-free) |
| 3 | **Motif scaffolding (generic)** | `metrics_tags: [… motif_scaffold_refolding]` or `internal_coordinate_motif_scaffold_refolding`; motif carried in the spec | structure + which residues are the motif | Oracle yes for the pass bar | **None.** Pass criteria + four benchmark input sets (`data/benchmarks/motif_scaffolding/`), no inference config |
| 4 | **Enzyme active site (AME / M-CSA)** | `inference_experiment=inference_on_val_set +datasets/val@generation=mcsa/mcsa_subset`; `ame_refolding`, with `no_ligand` flipping which of the two criteria applies | active-site residues **and their cofactor/ligand** as atoms | Oracle yes. Plus our intake blocker (§2A.2): the cofactor is HETATM and is deleted today | Only via `inference_on_val_set.yaml` + an upstream val dataset — i.e. a curated benchmark route, not a customer-input route |
| 5 | **Small-molecule / ligand binder** | `metrics_tags: [interchain_interactions]` for the oracle-free H-bond route, or `sm_binder_refolding` / `disco_refolding` for the scored ones | target ligand with bonds and formal charges (an annotated CIF) | **Oracle-free route exists** — `sm_binder_hbond_search.yaml` rewards a geometric H-bond count, no refold. But our intake deletes the ligand (§2A.2) | `sm_binder_hbond_search.yaml` (input: `data/benchmarks/small_molecule_binder/SAM_hbonds.cif.zst`) |
| 6 | **DNA binder** | `metrics_tags: [… dna_refolding]`; DNA sequence always specified, coordinates optionally fixed | double-stranded DNA target, sequence ± coordinates | **Oracle yes, and AF3/OF3 only** — `af3.yaml` has `oracle_dna`; there is **no AF2 equivalent**, so the cheaper oracle cannot substitute. Plus the intake blocker | **None.** Pass criterion + oracle exist; no inference config anywhere |
| 7 | **VHH / nanobody** | `metrics_tags: [design, antibody, 3d_coordinate_conditioning, internal_coordinate_conditioning, hotspot_conditioning]`; `recycle_strategy: high/front_loaded`; `generation.dataset.sampling_transforms` = a `SampleNearbyHotspots` list | target structure + a VHH framework; eight example complexes ship | **Oracle expected, but escapable at a cost.** `examples/antibody_design/README.md:6`: *"Complete setup first, including AF3 for scoring."* The spec route is closed: `docs/how-to/cookbook.md:94-96` — loading the VHH inputs as a spec *"stops at a residue-numbering safeguard, so run them through the example config in `examples/antibody_design/` instead"*. That config *"scores designs with the AlphaFold 3 / OpenFold3 oracle, which needs that environment from setup (`AF3_IMAGE`); without it the run writes the designs and then fails"* — **but the same page's own command line passes `'~metrics'`, and `:107` says it *"skips scoring"***. So designs can be generated without the oracle; what is lost is every score and the `antibody` pass criterion (all FIRST-HAND) | `examples/antibody_design/{common,vhh,partial}.yaml` + 8 `.cif.zst` inputs — an **out-of-tree** bundle needing `hydra.searchpath` |
| 8 | **Partial diffusion (refine an existing design)** | `generation.args.partial_trajectory_fraction_steps: 0.4166666666666667` (resume at step 35 of 60) + `sampling_transforms: null` to retain the parent's hotspots | *a previous run's design plus its saved conditioning* | Inherits whatever the parent modality needed | `examples/antibody_design/partial.yaml` |
| 9 | **Inverse folding / fixed-backbone sequence design** | `generation=inverse_folding` (7 lines: `args.inverse_folding: true`, `args.inverse_folding_method: resampled_forward_marginal`) | a backbone | No oracle wired, and **no pass criterion exists** for it | `configs/generation/inverse_folding.yaml` |
| 10 | **Reward-guided beam search** (a mode across rows 1–9, not a modality) | the `search` block: `n_branch`, `beam_width`, `checkpoints.<step>.{metrics,rewards,continuation_criteria}` | nothing extra from the user | Depends on the chosen reward. Oracle-free rewards ship (clash count, H-bond count); oracle rewards cost one refold per candidate per checkpoint | four shipped configs use it |
| 11 | **RNA** | mechanism only | — | — | **None.** A training-side preset (`configs/design_tasks/presets/rna_design.yaml`) and pseudoknot benchmark *inputs*; no inference config, no tag, no pass criterion, no oracle |
| 12 | **Peptide / cyclic peptide** | mechanism only | — | — | **None.** Training-side `cyclic_peptide_binder.yaml` only; upstream issue #62 asks whether cyclic peptide design is supported at all (§1) |
| 13 | **Symmetric oligomer** | — | — | — | **Not implemented.** `docs/reference/annotations.md:76-97` "Status at a glance" marks every row Supported **except** `| **Symmetry** | Planned |` (FIRST-HAND) |
| 14 | **Structure prediction / side-chain packing** | — | — | — | Benchmark inputs only. Not a design product; ignore |

Two upstream facts that kill several hypothetical rows outright. First, `docs/concepts/conditioning.md:50-53`: *"Atomized tokens are always sequence conditioned. Non-standard residues, PTMs, ligands and small molecules (everything with `atomize` set true) are context the model gets told about and never generates. The model generates only the 20 standard amino acids; nucleic acid and small molecule generation are coming"* (FIRST-HAND). So RFD4 cannot *design* a ligand, a PTM, a nucleic acid or a non-canonical residue — it designs protein **around** them, and upstream says in the same breath that generating them is future work. Any "design me a glycan" or "design me a cyclic peptide with an NCAA" framing is outside the model, not merely outside our scope. Note this also sharpens what rows 5, 6 and 11 mean: a *ligand binder* is a protein designed around a ligand we supply, and "RNA" would be a protein designed around RNA, never RNA itself. Second, `configs/design_tasks/` — the one group whose filenames do read like a modality menu, 23 presets including `rna_design.yaml`, `cyclic_peptide_binder.yaml`, `ig_design.yaml`, `protein_from_dna.yaml` (listing re-read FIRST-HAND via `gh api .../contents/configs/design_tasks/presets`) — **is reached from the training entry point, not from `inference.yaml`**. It is absent from every inference `defaults:` block. Confirmed negatively; I did not read `training_proteina.yaml` to confirm the positive side, so treat "it is training-side" as verified-by-absence. Reading that filename list as a shipped inference menu would be the single easiest mistake to make here, and it would have put RNA and cyclic peptides in release 1.

### 2A.4 Benchmark numbers, per modality

Upstream publishes in-silico design-benchmark numbers for **exactly four** modalities. The headline sentences below are FIRST-HAND — I decoded `website/index.html` and read lines 520-537 myself:

> *"For protein binding, high-thinking, single-shot RFD4-Proteina achieves a 10.6% clustered pass rate versus 5.9% for the next-best model (BindCraft), at 42-fold higher throughput. For small molecules it is 6.1-fold higher than the next-best model (14.3% versus 2.4%). For enzyme scaffolding it reaches 12.1% versus 5.7%, and for DNA binding 3.5% versus 0.8% for BoltzGen and 0.5% for fixed-DNA RFD3."* — `website/index.html:529`

| Modality | Targets | Best published RFD4 figure | Next best | Caveat worth carrying |
|---|---|---|---|---|
| PPI binder | 24 | 10.6% clustered, high one-shot; 7.7% at low thinking (`:531`) | BindCraft 5.9% | The BindCraft baseline is **single-backbone**, with its three continuation gates disabled (SECOND-HAND, `website/benchmarks/protocols.mjs:17-18`) |
| Small-molecule binder | 77 | 14.3% clustered | 2.4% | Two non-nested criteria published side by side (`sm_binder` and the looser `disco`); the 14.3% is the `sm_binder` one |
| Enzyme (AME / M-CSA) | 37 of 41 | 12.1% clustered | 5.7% | 4 tasks excluded (3 covalent adducts + 1 with unobserved phosphate atoms) |
| DNA binder | **3** | 3.5% clustered | BoltzGen 0.8%, RFD3 0.5% | **Cross-arm**: the 3.5% is RFD4 on *unfixed* DNA while both baselines are on *fixed* DNA. Three targets is not a benchmark |

Shared basis, SECOND-HAND (`website/benchmarks/leaderboard.html:63-67`): 200 attempts per target, failures counted with no replacement; clustered = distinct passing folds ÷ 200, Foldseek TM 0.5, clustered within each model/protocol/target; the oracle is a **common AF3 refold, not the model's own filter**. Snapshot `2026-10-21`, 3,073 rows, `live_database: false`.

**Everything else has no number.** Explicitly: VHH/nanobody (a full workflow guide and zero published rate), peptide and cyclic peptide, unconditional monomer (no scTM anywhere — a repo code search for `scTM` returns zero hits, SECOND-HAND), generic motif scaffolding, RNA, inverse folding, side-chain packing, glycan. Several of those carry an asserted claim with no value attached, and the two assertions differ in strength (both now FIRST-HAND, re-read directly): `website/index.html:277` is a bare superlative — *"**State-of-the-art performance** on side-chain packing, sequence design, conformational sampling, and design refinement **without additional training**"* — while `:313`, in the abstract, is a qualified comparative: the same model *"performs equivalently to or better than state-of-the-art specialist methods for related tasks"*. Neither attaches a metric, value, baseline or table. Treat both as marketing until a number appears, and do not quote `:313` as a win.

**The benchmark GPU model is absent from the repository**, not merely missed: no H100/A100 designation and no wall-clock figure in `website/index.html`, `leaderboard.html`, anything under `website/benchmarks/`, or a repo-wide search for `H100` (which hits only install guidance). Throughput is published as designs per GPU-hour with a per-row `timing_basis` string taking seven values including `transferred_prior_run_estimate`. The preprint PDF was not opened. **UNVERIFIED** — and it means every "designs per GPU-hour" figure here, including the 42-fold claim, cannot be converted into a price on our rate card without the rung-3 measurement in §6.1.

Wet-lab affinities do exist for several modalities with no in-silico number (calmodulin, peptide–MHC, TrkA, glycan shielding, phosphotriesterase, a lenacapavir ligase) — SECOND-HAND, from `website/index.html`. They are campaign results with no attempts-per-target and mostly no baseline. **They are not benchmark numbers and must not be quoted as pass rates**, including in our marketing copy.

### 2A.5 Hub-side work, per modality, on top of the release-1 binder path

Taking the release-1 path of §2 and §8 as the baseline (new slug, new container copied from `tools/proteina/`, condition-spec builder, CIF regeneration, live charging, streaming, exports):

| Modality | New intake? | New form fields | New preflight | New score columns | New export shape | Verdict |
|---|---|---|---|---|---|---|
| PPI binder | the §4 CIF path | baseline | baseline | baseline | baseline | **release 1** |
| Unconditional monomer | **none** — no upload at all | length + count only; the form gets *smaller* | **none** (nothing to inspect) | same inline metrics | same (sequence + PDB per design) | cheapest addition in the list |
| Partial diffusion / refine | none — input is our own prior run | "refine this design" + a strength slider | none | none | none | cheap **but** needs the parent run's saved conditioning persisted, which release 1 must not throw away |
| Inverse folding | protein-only, as release 1 | "sequence only" toggle | baseline | fewer, not more | sequence-only (no structure) | cheap mechanically; **no pass criterion and no benchmark number upstream**, so we would be selling an unmeasured mode |
| Motif scaffolding (protein motif) | protein-only, as release 1 | motif residue picker | motif-coverage check (new) | motif RMSD columns | baseline | moderate; no upstream inference config to copy |
| VHH / nanobody | protein-only | framework chooser + hotspot sampler knobs | baseline | antibody metrics (different bars) | baseline | mechanically moderate, commercially weak: the only working route is an out-of-tree example config, and without the excluded oracle it delivers **unscored** designs with no pass criterion |
| Small-molecule binder | **new CIF-preserving staging path** | ligand upload + charge/bond handling | ligand-aware preflight (new) | interface-interaction columns | ligand copied into output | large — the shared intake build |
| Enzyme active site | **new CIF-preserving staging path** | active-site residue + cofactor picker | both of the above | motif all-atom + clash columns | cofactor in output | largest; also no customer-input route upstream |
| DNA binder | **new CIF-preserving staging path** | DNA sequence ± coordinates | nucleic-acid preflight (new) | DNA-specific bars | DNA in output | large, **and** the pass bar needs AF3/OF3 specifically — no AF2 fallback |
| Reward-guided search | none | reward preset + budget | none | reward column | none | cheap to wire, **expensive to run** (§2: ~27× GPU seconds per design on our own aggregation, and upstream issue #64 reports a multi-GPU beam-search reward bug) |
| RNA / peptide / cyclic / symmetry | — | — | — | — | — | nothing to build against |

### 2A.6 Overlap with what we already sell

| Our tool | What it owns | RFD4 overlap | Replace or compete? |
|---|---|---|---|
| `rfantibody` | *"You have a target structure and want nanobodies — single-domain antibodies you can carry straight into yeast display, mammalian display or a hybridoma workflow. Each candidate is refolded and scored against the target before you see it"* (`comparison_one_liner`, `tools/rfantibody/meta.py:45-52`) | row 7 — same job | **Neither, for now.** RFD4's VHH route has **no published pass rate**, and without the AF3/OF3 oracle we excluded it returns designs with no scores and no pass/fail at all — unsellable next to a tool that scores. `rfantibody` keeps this segment |
| `iggm` | antibody/Ig design (`tools/iggm/meta.py:16-23`) | row 7 | as above |
| `pxdesign` / `bindcraft` | protein binder design, with and without AF2 filtering | row 1 — same job | **Competes, and on upstream's figures wins** (10.6% vs BindCraft 5.9% clustered). But that baseline is a deliberately de-gated single-backbone BindCraft, and we have no RFD4 run of our own. Decide after a month of real runs, not now (§1.1) |
| `proteina` | NVIDIA Proteina/Complexa. Already has four variants: `protein_binder`, `ligand_binder`, `motif_ame`, `validate` — but *"the ligand and motif variants run curated tasks (their tasks resolve from separate upstream registries)"* (`tools/proteina/meta.py:270-290`) | rows 4 and 5 | **New capability, not duplication.** Our `proteina` ligand and motif variants run **bundled benchmark tasks only** — a customer has never been able to submit their own ligand. RFD4 rows 4–5 would be the first customer-ligand route we sell. That is the strongest argument in this section for eventually building the CIF intake path |
| `rfdiffusion` | backbone-only binder design, MPNN + AF2 in the container | row 1 | **Candidate for replacement eventually**, but on our data: it has never produced a successful binder in the runs on record. Argue that separately (§1.1) |
| `boltz2`, `esmfold2-design`, `af2`, `colabfold` | folding and scoring | row 14 | none — RFD4's structure-prediction benchmarks ship inputs only, and we are not buying a predictor |

Net: RFD4 **competes** with `pxdesign`/`bindcraft` on day one, **does not threaten** `rfantibody`/`iggm` (its antibody route is the one blocked on an excluded dependency), and is the only route to a **customer-supplied-ligand** product we have ever had.

### 2A.7 Staged release map, and whether §2 still holds

**It holds. Ship `protein_binder` alone in release 1 — now with a stronger reason than §2 had.**

§2 excluded the other modalities mostly on "one thing at a time" grounds. Having looked, the exclusions are load-bearing, not stylistic:

- The three commercially interesting ones (ligand, enzyme cofactor, DNA) all need the **same** piece of platform work — a CIF-preserving staging path that hands a container the annotated file instead of converted PDB bytes (§2A.2). That is multi-PR: it changes what `TargetUpload` means for every tool that consumes a staged structure, even though no tool's own normalizer has to change. Doing it under the pressure of a first release of an alpha-stage model is the wrong order. Two of the three are *also* gated upstream, not only by us: the enzyme route has no customer-input path at all (row 4 — a curated benchmark route), and DNA has no inference config anywhere (row 6). So "our platform work is the only blocker" holds for the ligand row alone.
- The one that looks adjacent, VHH, is the **worst** candidate: its spec route hits a residue-numbering safeguard, its only working route is an out-of-tree example config wired to the AF3/OF3 oracle we excluded on licence grounds — runnable without it, but then every design comes back unscored and the `antibody` pass criterion never evaluates — it has no published pass rate, and we already sell two tools in that segment.
- The headline mode, reward-guided search, is a pricing decision rather than a capability gap, and it has an open upstream multi-GPU bug.

So a wider release 1 is **not** the better call — with one qualified exception, which I recommend:

**Add the unconditional monomer preset to release 1 if and only if it costs under a day.** It is the only modality in the table that needs *no* new intake, *no* new preflight, *no* new score column and *no* new export shape — the form is strictly smaller than the binder form (a length and a count, no upload). It has two shipped configs, one of them deliberately oracle-free. Its value is not the product: it is a **$0-upload smoke test for the whole slug** that exercises generation, streaming, metering and exports without a customer structure anywhere in the loop, which is exactly what §6.1 rung 3 wants. The honest cost against it: there is **no published designability number for it at all** (no scTM anywhere upstream), so it ships as a utility, never as a headline — and if wiring it turns out to need its own condition-spec shape, drop it and keep §2 as written.

| Stage | Modalities | Why |
|---|---|---|
| **Release 1** | Protein binder (row 1). Optionally unconditional monomer (row 2) on the one-day test above | One conditioning recipe, one protein-only intake, direct competition with what we already sell. Confidence rests on **one** of the four published benchmarks (PPI); the monomer option has no published number at all, so it adds a smoke test, not evidence |
| **Release 2** (after one month of real release-1 runs) | Reward-guided search as its own priced preset (row 10); partial diffusion / "refine this design" (row 8) | Both reuse release 1 unchanged. Search is the strongest arm upstream and the clearest premium-price story; refine is the natural second purchase for a customer who liked one design. Gate search on upstream issue #64 closing. Release 1 must persist each design's conditioning or refine becomes a rebuild |
| **Release 3** (one platform project, then three presets) | Small-molecule binder (row 5), enzyme active site (row 4), DNA binder (row 6) | They share the CIF-preserving staging path. Build it once, land it on its own, then the three presets are each small. This is also where we first sell customer-ligand design at all. DNA goes last of the three: 3 benchmark targets, and its pass bar needs AF3/OF3 with no AF2 fallback |
| **Not scheduled** | VHH/nanobody (row 7), motif scaffolding (row 3), inverse folding (row 9) | VHH: only route is an out-of-tree config that without the excluded oracle returns unscored designs, no published number, segment already served by two tools. Motif scaffolding: no upstream inference config to copy and no published number. Inverse folding: one config flag, but no pass criterion and no number, so we would be selling something we cannot measure |
| **Skip** | RNA (11), peptide/cyclic peptide (12), symmetry (13), structure prediction (14) | Nothing runnable exists. Symmetry is *"Planned"* upstream. Revisit only when upstream ships an inference config |

**One question this section adds for Leo**, on top of §9: the CIF-preserving staging path in release 3 is the single largest piece of work in this document and it is not RFD4-specific — it would also let `proteina`, `boltz2` and `bindcraft` accept a customer's ligand for the first time. It may be worth scoping as its own platform project rather than as RFD4 release 3. That is a roadmap call, not a technical one.

---

## 3. The benchmark numbers, and what they are worth

All figures in this section are **my own arithmetic over the upstream benchmark CSV**, not quoted marketing numbers. Source: `website/benchmarks/source_data.csv` at `main`, fetched with `gh api` and aggregated with a throwaway script that is **not committed**, so a reader who wants to check these numbers must re-aggregate the CSV themselves. The CSV path and the group-by (arm x target, mean of the per-target success rates) are the whole method. Each arm is 24 targets × 200 attempts = 4,800 attempts.

| Arm | pass % | clustered % | gen GPU-s / design (median, range) |
|---|---|---|---|
| **RFD4 search (multimer)** | **28.10** | **14.92** | 62.5 (53.3–95.6) |
| **RFD4 high** (backbone cond.) | 19.29 | 10.62 | 3.85 (1.98–9.25) |
| **RFD4 medium** (backbone cond.) | 17.04 | 9.71 | 2.35 (1.20–5.60) |
| RFD4 high (all-atom cond.) | 16.94 | 9.58 | 4.05 (2.27–9.23) |
| RFD4 low (backbone cond.) | 13.69 | 7.73 | 1.60 (0.81–3.78) |
| BindCraft 1 | 9.94 | 5.85 | 113.0 (47.9–1155.1) |
| ESMFold2-design | 8.92 | 2.46 | 79.3 (53.7–214.4) |
| BindCraft 2 | 7.23 | 4.81 | 101.7 (51.0–318.4) |
| Protein-Hunter | 6.92 | 4.15 | 75.6 (63.2–153.1) |
| BoltzGen | 4.81 | 1.29 | 3.15 (2.10–7.64) |
| Proteina-Complexa (**our `proteina`**) | 4.50 | 1.85 | 2.95 (1.62–8.49) |
| **RFdiffusion v1 (our `rfdiffusion`)** | **2.60** | **0.75** | 29.2 (20.1–78.1) |

**Five caveats, all of which matter before anyone quotes these to a customer.**

1. **"Pass" is computational, not experimental.** Upstream says so itself. No wet-lab validation is claimed.
2. **Conditioning is not matched across arms.** RFD4's best PPI arms use *backbone* conditioning while several baselines are scored under all-atom conditioning. The RFD4 all-atom row (16.94%) is the like-for-like one and is the number to quote.
3. **The timing bases differ, in RFD4's favour.** RFD4 and the pipeline arms are `batch_amortized`; BindCraft, RFdiffusion, Complexa and others are `per_attempt` (the `timing_basis` column). Amortized timings exclude setup, compile and warmup. **Any "N× faster" claim built on this table is not safe to publish**, and the per-design seconds column above should never reach a customer-facing page without that caveat attached.
4. **Measured on the previous release pair.** Upstream states the published benchmark has not been re-measured on the current weights.
5. **GPU model for these timings: NOT FOUND.** Nothing in the CSV or its notes names the card. So the per-design seconds do not convert to dollars on our A100s without our own measurement.

The honest one-sentence read: *on the authors' own computational benchmark, under matched all-atom conditioning, RFD4 passes 16.9% against our Proteina's 4.5% and our RFdiffusion's 2.6% — unvalidated experimentally, and on timings we cannot yet price.*

---

## 4. Inputs, form fields and preflight

### 4.1 The upstream input contract

Binder design takes a YAML/JSON condition spec pointing at a CIF (`docs/how-to/binder-in-10-lines.html`). Shape:

- `input` — target structure, `.cif` or `.cif.zst`.
- `contig` — e.g. `"50-120, /0, A17-131"`: the expandable range is the binder length, `/0` is the chain break, `A17-131` is the target chain and residue range. A contig must preserve at least one segment.
- `conditions.C_CRDGRP` / `C_SEQ` / `C_HOT` — atom-level selection expressions, e.g. `sel('A/*/56/[CG,OH]')` for a hotspot.
- Residue ids resolve against the mmCIF `label_seq_id`, **not** author numbering.

CLI: `rfd4-generate inputs=<spec> num_replicates=N batch_size=B out_dir=... '~metrics'`. Hydra config system; `num_replicates` is designs per input, `seed` defaults to random, `skip_existing` defaults true.

**Three integration costs fall straight out of that:**

- **PDB input is NOT FOUND in upstream docs — CIF only.** Every binder tool we sell takes a PDB upload. We already carry a CIF path (`shared/exports.py::structure_chain_sequences:883` handles both), but the *input* side needs a PDB→CIF conversion step in the wrapper, and `label_seq_id` numbering must be derived from it. This is the one place where RFD4 is materially harder to wire than our `proteina`, and it is where I expect the integration bugs to be. Our existing memory on `proteina` hotspot semantics (a bare residue number hit the wrong protomer) is the same class of defect waiting to repeat.
- **Hotspots are atom-level**, not residue-level. Our hotspot picker (`static/js/hotspot_picker.js`) emits residues. The wrapper must expand a picked residue to a sensible atom set, and that choice is a scientific decision, not a formatting one.
- **Contig semantics are ours to own.** Our `proteina` already learned this the hard way (its contig must cover the whole chain or the CA count mismatches). Budget for the same care here.

### 4.2 Proposed form fields (release 1)

Mirror `tools/proteina/__init__.py` so the customer sees one consistent binder form:

| Field | Proposal | Basis |
|---|---|---|
| Target structure | PDB or CIF upload | our convention; wrapper converts to CIF |
| Target chain(s) | multi-chain allowed | upstream binder example is chain-scoped; multi-chain is an upstream validated case for this model family |
| Hotspots | optional, max 64 | matches `tools/proteina/__init__.py:224` (`_MAX_HOTSPOTS`) |
| Binder length | range, default `[60, 120]`, allowed 20–300 | matches `tools/proteina/__init__.py:263-265`; upstream's own example uses `50-120` |
| Number of designs | default 8 | matches `designs_per_run_baseline=8`, `shared/wallet_estimates.py:525` |
| Thinking level | **release 1: fixed at `medium`, not exposed** | three levels exist upstream; exposing a 3× cost knob before we have measured any of them is how a tool over-quotes |

### 4.3 Proposed preflight envelope — all **provisional, pending rung 3**

Model it on `_PROTEINA` (`shared/pdb_preflight_rules.py:844`) and replace each number the moment we measure:

- `min_target_aa` 30 — copy from `_PROTEINA`.
- `hard_cap_target_aa` — **start at 400, not 500.** `_PROTEINA`'s 500 is a policy number anchored on a measured VRAM curve up to 415 aa; RFD4 has no such curve here, and its own `compile_lengths` bucket list tops out at 1024 tokens with a BioIR CUDA-graph limit of 511 (`configs/inference.yaml:19,51`) — which is **INFERRED** to mean longer inputs run uncompiled and slower, not that they are refused. Tighten on measurement, loosen later.
- `hard_cap_combined_aa` — 520 (400 target + 120 binder), same logic.
- `runtime_base_min` / `runtime_alpha` — **leave explicitly unmeasured and labelled so until rung 3.** Do not copy `proteina`'s curve; it describes a different model.
- VRAM: upstream's only figure is "≥40 GB VRAM recommended" (`skills/rfd4-proteina-setup/skill-card.md:31`, verified by direct read this session). The HF card declines to give a number: peak memory "depends on token count, batch size and recycle depth rather than on the weights". So **A100-80GB, not 40GB**, until measured.

---

## 5. Modal image, GPU class and the CUDA-13 risk

### 5.1 The blocking technical question

Upstream requires **Linux x86_64, Python 3.12, and a CUDA-13-compatible driver** (README; macOS and Windows unsupported). Their `Dockerfile` takes `RFD4_ACCELERATOR=cu13|cu12|cpu`, builds on `python:3.12.12-slim-bookworm` pinned by digest, and ships **no CUDA base image** — CUDA runtime libraries come from the PyTorch wheels, so the host needs only a driver and the container toolkit.

Our `tools/proteina/Dockerfile.modal:35` builds on `nvcr.io/nvidia/pytorch:24.08-py3`, a CUDA-12 base. **Whether Modal's A100-80GB hosts present a CUDA-13-capable driver is the one thing that decides the shape of this work, and it is UNVERIFIED.** Two outcomes:

- **Driver is new enough** → build the `cu13` variant from upstream's Dockerfile. Best case.
- **Driver is CUDA 12 only** → build `RFD4_ACCELERATOR=cu12`. Upstream ships it as a first-class option, so this is a fallback rather than a blocker — but it is a different wheel set and must be probed, not assumed. Rung 1 proved cu12 builds and imports too — but it drops the BioIR inference backend, so it is a **different product**, not a drop-in fallback (§5.4).

Rung 1 answered half of this for $0 on 2026-10-30: the `cu13` wheel set resolves and the image builds and imports (§5.4). The driver half is still open and needs a GPU second — see §5.4.

### 5.2 Image plan

Follow the `proteina` pattern exactly, because its guard tests already enforce it:

- New `tools/rfd4/Dockerfile.modal`, **digest-pinned `FROM`** and **`ARG RFD4_SHA=<commit>`** with a `git checkout` of that SHA — same shape as `tools/proteina/Dockerfile.modal:35,54,58`. Pinned to a commit, never a branch.
- Add the `certifi` floor layer; `tests/test_gpu_image_certifi_floor.py` and `tests/test_dockerfile_digest_pins.py` glob `tools/*/Dockerfile.modal`, so a new file is picked up automatically and will fail the suite without these.
- **Do NOT include `alphafold2/runtime/` or `openfold3/runtime/`.** Per the HF model card those images package third-party software under its own upstream terms, some of which restrict commercial use or redistribution, and **neither oracle is needed for generation**. They are also 3.5–16.6 GB each.
- **The oracle exclusion has a config consequence that must be in code, not in a comment.** Upstream's default `inference_on_user_inputs` experiment composes a metrics group whose default oracle is AF3. The wrapper must pass `'~metrics'` (or an inline-only metrics tag) so no oracle is ever selected. A guard test asserting that our invocation disables the oracle group is the cheapest way to keep a future edit from quietly re-enabling a licence we did not clear.
- Weights on a Modal Volume seeded once, copying `tools/proteina/seed_volumes.py`. **Flow checkpoint and autoencoder are a matched pair** — the HF card warns that mixing releases "produces nonsense rather than an error", so the seeder must pin one HF revision for both and record it.
- Sizes from the HF file listing: `model.safetensors` 1.74 GB, flow checkpoint 1.75 GB, autoencoder 1.03 GB; upstream says `make setup` caches about 1.7 GB. Upstream prebuilt runtime `.sif` images (weights excluded): cu13 4.04 GB, cu12 4.41 GB. **Measure the real volume size at rung 2** — our `boltz2` weights volume was previously off by ~7× against its documented figure.
- GPU class **A100-80GB** (`_GPU` must be a module-level literal; `tests/test_gpu_class_drift.py:155-171` requires a matching `TOOL_SPECS` entry).

### 5.3 Pin list — what rung 1 must turn into code, not prose

Leo's instruction (2026-10-30): once we know how to run it, **pin those settings**. Eight images already carry digest pins (`tests/test_dockerfile_digest_pins.py`, which globs `tools/*/Dockerfile.modal`; esmfold2_design has no Dockerfile and is checked differently, `:68-75`), so the mechanism exists; what follows is the list this tool has to fill in, and the file each pin lives in. Every row is currently **UNSET** — rung 1 and rung 2 produce the values.

| What | Where it is pinned | Why a float here hurts |
|---|---|---|
| Upstream commit | `ARG RFD4_SHA=<sha>` + `git checkout`, same shape as `tools/proteina/Dockerfile.modal:54` | Upstream is alpha and moving; a branch checkout re-resolves on every rebuild |
| Base image | digest-pinned `FROM`, enforced by `tests/test_dockerfile_digest_pins.py` | Tag drift silently changes the driver/toolkit pairing |
| **Accelerator variant** | `ARG RFD4_ACCELERATOR=cu12\|cu13` with **no default** | This is rung 1's answer (§5.1). A default would let a rebuild silently pick the untested wheel set |
| Weights revision | one HF revision, both checkpoints, recorded in `seed_volumes.py` | The HF card warns a mismatched flow/autoencoder pair "produces nonsense rather than an error" — no exception is raised, so nothing would catch it |
| Checkpoint filenames | `rfd4-proteina-flow-release-2026-10-19.ckpt`, `rfd4-proteina-ae-release-2026-10-19.ckpt` (`configs/inference.yaml:38,46`) | Dated filenames; the next release will sit beside them in the same volume |
| GPU class | module-level `_GPU` literal + matching `TOOL_SPECS` entry (`tests/test_gpu_class_drift.py:155-171`) | Already guarded; a mismatch mis-bills |
| **The invocation** | one module-level constant holding the full Hydra override list — `metrics_tags`, `'~metrics'`, `nsteps`, `num_replicates` | Built ad hoc, an edit can silently re-enable the AF3 oracle group we excluded on licence grounds (§5.2). Assert on the constant |
| `certifi` floor | the floor layer, enforced by `tests/test_gpu_image_certifi_floor.py` | Globs `tools/*/Dockerfile.modal`; the suite fails without it |

**The pin that is easiest to get wrong is the accelerator variant**, because both variants build and only one will run. Rung 1 must record *which* variant it proved, in the Dockerfile, in the same commit.


### 5.4 Rung 1 RESULT — the cu13 image builds, 2026-10-30

Leo approved rung 1 on 2026-10-30. It ran the same day on a Modal CPU builder. **No GPU was attached, no weights were downloaded, nothing was deployed, and no GPU second was billed.** The probe script lives in this session's scratchpad, not in the repo; it mirrors upstream's `Dockerfile` (`aaccdd2a`) step for step but `git clone`s the pinned SHA instead of using a build context.

**Outcome: the `cu13` image builds and runs.** Built in **147.57 s**, nine steps, `python -m pip check` reporting "No broken requirements found", 215 packages installed, all from public PyPI and the public PyTorch index. Upstream's own build-time smoke test passed on CPU — including the compiled TMol LJ/LK operator import (`tmol.score.ljlk.potentials._compiled`), OpenBabel, and a `torch.compile(backend="inductor", fullgraph=True)` round trip. `rfd4-generate --help` exited 0.

Resolved versions, read from a container started on the built image:

| | |
|---|---|
| `rfd4-proteina` | 0.0.3 |
| `torch` | 2.14.1+cu130 |
| `torch.version.cuda` | 13.0 |
| cuDNN | 9.24 |
| `tmol-ops-cu13` | 0.2.0 |
| `bionemo-ir` | 0.2.1 |
| `certifi` | 2026.10.23 |

Two things the probe found that were not in the plan:

- **`rfd4-generate --help` succeeds without a driver, but it is not silent about it.** With no GPU present the CLI printed `Error while loading libcue_ops.so: libcuda.so.1: cannot open shared object file: No such file or directory` and still exited 0. **A `--help` that exits 0 is therefore not evidence of GPU readiness**, and that string is the exact signature to watch for at rung 2 if the host driver turns out to be too old. Do not build a readiness check on the exit code.
- **`atomworks` warns that `CCD_MIRROR_PATH` and `PDB_MIRROR_PATH` are unset** on every invocation, saying functions requiring them will be unavailable. CCD is the chemical component dictionary. UNVERIFIED whether a protein-only binder run needs either; it is a likely requirement for any ligand or modified-residue modality (§2A). Settle at rung 3.

**The `cu12` fallback also builds, and it is not a free swap.** Probed the same way, same day, same $0. It passes upstream's smoke test with `torch 2.14.1+cu126` (CUDA runtime 12.6, cuDNN 9.10.2) and `tmol-ops-cu12 0.2.0`. But it installs **166 packages against cu13's 215, and `bionemo-ir` is absent** (`ModuleNotFoundError` from a container on the built image). That matches upstream's own note on `Dockerfile:76`: "BioIR (`inference_backend=bioir`) comes with the all-cuda13 extra; cu12 and cpu stay on the native runtime." **So falling back to cu12 costs us an inference backend, not just a wheel set** — and the native runtime's speed relative to BioIR is UNMEASURED by us and unstated upstream. If the driver check forces cu12, the runtime figures in §6 need re-deriving, not adjusting.

One caveat on the probe's own output: the `accelerator` label it prints is read from a module global that the container re-imports with different arguments, so the cu12 run printed `cu13` in that one field. Every other value above was read from the running container and is correct for the image it ran on; the torch local version (`+cu126`) is the trustworthy discriminator.


**What rung 1 did not answer.** A CPU builder has no NVIDIA driver, so this proves the wheel set resolves and the code imports — **it does not prove Modal's A100-80GB hosts present a CUDA-13-capable driver**, which was §5.1's actual question. That remains UNVERIFIED and now costs about three cents of GPU time to settle, not $0; it should be folded into rung 2 rather than run on its own. An earlier draft of the §6.1 ladder said rung 1 would establish the host driver; that row and §5.1 are corrected above.


---

## 6. Cost: what a run will bill, and what validating it costs

**Our raw Modal A100-80GB rate is $0.001028/GPU-second** (`shared/wallet_estimates.py:74`). Live charging bills at the marked-up rate: `WALLET_MARKUP = 1.70` (`shared/wallet_estimates.py:54`) → **$0.0017476/customer-GPU-second ≈ $6.29/hour**. Computed this session:

| Run wall-clock | Customer charge |
|---|---|
| 300 s | $0.52 |
| 600 s | $1.05 |
| 900 s | $1.57 |
| 1800 s | $3.15 |
| 3600 s | $6.29 |

**What will an 8-design RFD4 binder run actually cost? UNKNOWN, and the benchmark does not answer it.** At the benchmark's 2.35 s/design median, eight designs is ~19 seconds of generation — about three cents. That is obviously not what a run bills, because a real run is dominated by fixed overhead the benchmark explicitly excludes: container cold start, weight load, `torch.compile` for the input's length bucket, CIF prep. For reference, our `proteina` 8-design shards measured 553–1343 s (`shared/wallet_estimates.py:501-506`), nearly all of it overhead of the same kind. **Expect RFD4's cost to be set by its startup, not its sampling** — which is good news for margin and bad news for anyone estimating from the benchmark. Rung 3 settles it.

### 6.1 Validation ladder — cheapest first

**Every rung is a proposal. Leo approves GPU spend per job. Nothing here has been launched.** Dollar figures are our raw A100-80GB cost, not customer price.

| Rung | What it answers | GPU | Cost | Notes |
|---|---|---|---|---|
| **1. CPU build probe** | Does the image build, does `rfd4-generate --help` run, and **which accelerator variant do we need?** | none | **$0** | Build `cu13` on a CPU builder with weights excluded; a `RUN` probe at build time is free. **DONE 2026-10-30, $0: `cu13` builds and imports (§5.4).** It does NOT establish the host driver — a CPU builder has none. Fold that check into rung 2. |
| **2. Volume seed + GPU import smoke** | Do the weights land, how big is the volume really, does CUDA initialise on our A100? | 1 × A100-80GB, ~10 min | **~$0.62** | Mirrors `proteina`'s seeding step. Deliverable: the true volume size and a `torch.cuda` init on our hardware. |
| **3. One 8-design binder run** | **The cost question.** Real wall-clock, real output tree, real per-design file timing | 1 × A100-80GB, ≤30 min | **~$1.85** | Upstream's own `5o45_cropped` binder example, `num_replicates=8`, `'~metrics'`. Deliverables: wall-clock → the `expected_gpu_seconds` figure; the real `per_example_metrics.csv` header → the score column set; **and whether structure files appear per design or only at the end** (§7). |
| **4. Our own target, like-for-like** | Does it work on a customer-shaped input, and how does it compare to our `proteina`? | 1 × A100-80GB, ≤30 min | **~$1.85** | The same target and hotspots as a recorded `proteina` run. First honest head-to-head on our own data. |
| **5. Size sweep** | What is the runtime/VRAM curve? | 3 × A100-80GB, ≤30 min each | **~$5.55** | ~130 / 260 / 415 aa, matching `proteina`'s measured points so the two curves are comparable. Replaces the provisional §4.3 caps. Defer until the tool is committed to — buys accuracy, not a go/no-go. |

**Ladder through rung 4: ≈ $4.30 of GPU.** Through rung 5: **≈ $9.90.** The go/no-go is fully answered at rung 4. This is an order of magnitude cheaper than the BindCraft 2 ladder (≈$32) because RFD4 generates in seconds, not hours.

---

## 7. Live charging, streaming and exports

### 7.1 Live charging

How a live tool works here (verified in code this session):

- The slug joins `_LIVE_ALL_PRESETS` (`shared/wallet_guard.py:81-91`), which today holds eight tools.
- A **start minimum** is the balance a run needs before it may start; nothing is debited at start. Per-tool constants sit beside that set — `PROTEINA_START_USD = $1.00` (`shared/wallet_guard.py:97`), `PXDESIGN_START_USD` and `RFANTIBODY_START_USD` = $2.00 (`:100-101`). The other live tools derive theirs instead of using a constant: `bindcraft` and `iggm` return `TOOL_SPECS[slug].base_hard_cap_usd`, `esmfold2-design` returns its base cap **times its seed count**, and `rfdiffusion` returns a `live_due_usd` figure computed from a runtime estimate (`shared/wallet_guard.py:199-213`). Of the eight tools in `_LIVE_ALL_PRESETS`, only `boltz2` falls through to $0 — so "$0 unless stated" is **not** the house default and `rfd4` needs an explicit decision (§9 Q4).
- The container sends a **start heartbeat the moment the GPU is live** (`tools/proteina/modal_app.py`, `_start_beat`), so a cancel bills from GPU start. Two guard tests constrain this, both parametrised over every in-repo live app (`tests/test_live_charging.py:1147-1161`): `_start_beat` must be **AST-identical to `af2`'s copy** (`ast.dump` equality — so comments and formatting may differ, the code may not), and the statement calling it must come **immediately after** `env = _merged_environment(payload)` in every function that builds the env. Landed in `3a8de1fc` (#464) for the **7 in-repo GPU apps** only — its own commit message says so; the out-of-repo containers (`rfdiffusion`, `pxdesign`, `rfantibody`) and non-live `mpnn` are not covered, so "every live tool sends a start beat" is **not** true today. A new app is picked up automatically once its slug joins the live sets, so `tools/rfd4/modal_app.py` must copy `af2`'s `_start_beat` body exactly rather than write its own.
- Billing itself runs from the Railway cron every 5 minutes, debiting wall-clock since `started_at`; when the balance falls short the run is cancelled, reconstructed and completed as a partial.

**Proposed start minimum for `rfd4`: $1.00**, pending rung 3 — **and this is a Leo decision, not a derivation.** The principle the other tools follow is "cover the stretch before the first design reaches storage". RFD4's generation is seconds per design, so if structures stream per design that stretch is container start + weight load + compile, and $1.00 (≈570 s of billing) is comfortable. If instead structures only appear at run end, the stretch is the whole run and no modest minimum covers it. **Rung 3 must answer which, before the figure is set.** Until it does, the figure is a guess.

### 7.2 Streaming / save-as-you-go

Copy the `proteina` mechanism: the container uploads each design via a presigned PUT, then sends a heartbeat carrying a `new_candidate` record; the hub appends it to the job's partial candidates, and `shared/job_recovery.py` rebuilds a result from those if the run stops. **Two RFD4-specific hazards:**

- **`delete_failing_pre_oracle: True`** (`configs/inference.yaml:44`) deletes structure files for designs failing a pre-oracle filter. Our uploader must either run after that filter or tolerate a vanished file. A naive per-design uploader will race it.
- **Score keys must be registered.** `shared/job_recovery.py:59` keeps only a fixed tuple of score keys when rebuilding a candidate from a partial, so a new tool's own metrics are dropped on a stop or cancel unless its keys are added. The tuple is `_PROTEINA_SCORE_KEYS`, named for and populated with proteina's metrics, so proteina itself is fine; the hazard is that a new tool silently loses any metric not on that list. Add `rfd4`'s keys in the same PR that adds its streaming.

### 7.3 Exports

Exports need each candidate to carry a `pdb_key` and a sequence. **RFD4 is unusually easy here** — it emits sequence and structure together, so `shared/exports.py::_candidate_sequence:441` finds a sequence directly and no structure parse is needed for FASTA. Since `b60ee72e` (#465) chain sequences are stored on the row at ingest, so a tool that supplies sequences gets correct CSV and FASTA for free. Outputs are `*_generated.cif` in `inference_samples/`, so the export path must treat **CIF as a first-class structure format**, not PDB — the same shape of change the BindCraft 2 scope anticipated.

**Score columns:** without a refold oracle there is no AF2 ipTM. What we *do* get is upstream's inline, no-oracle metric set: clash, bond length, hotspot obedience (`max_hotspot_distance: 4.5`), RASA adherence, sequence adherence, secondary-structure accuracy, contact molecular surface, amino-acid composition, developability metrics. The real column list comes from rung 3's `per_example_metrics.csv` header, not from reading upstream configs.

**This is the product decision §0 flagged.** Our binder results pages lead with ipTM; RFD4's will not have one. Options: (a) ship with inline metrics only and explain the difference in the results copy; (b) add a *separate* chained fold step through a tool we already license — we already sell `boltz2` and `af2` — and show that ipTM; (c) use OpenFold3's ported weights (Apache-2.0 per the HF card) while still excluding the restricted runtime image, which needs its own licence read before anyone relies on it. **Recommendation: (a) for release 1, (b) as the follow-up**, because (b) is a chained-tool product feature with its own billing question and should not block the first release.

---

## 7A. Scoring: what RFD4-Proteina ranks designs by

Leo asked (2026-10-30) what the model uses for ranking. The short answer is that **it ships no default score at all**, and the one quality bar it does publish is binary and needs the oracle we excluded. Everything in this section was read first-hand at upstream `aaccdd2ad0720c180468df5f9cf037a713d403e5`.

### 7A.1 A plain run computes nothing

`configs/inference.yaml:71` is `metrics_tags: []` and `:73-77` is `search.checkpoints: null`. Metrics are tag-gated, and `src/rfproteina/metrics/metric.py:417-421` passes a metric whose gate sets are empty, so an empty tag list leaves **exactly one metric running: `amino_acid_composition`**, the only entry in the default composition (`configs/metrics/inline_metrics/all_inline_metrics.yaml:1-6`) with no `required_tags_any` (`design_metrics.yaml:1-3`). It is a residue tally, not a score. **Every ranking column is silent.** Scoring is opt-in, per tag, per run — it is not a property of the model.

### 7A.2 The published bar is pass/fail, and every binder criterion needs the oracle

`configs/metrics/pass_criteria/all_pass_criteria.yaml` defines twelve criteria, each emitting one boolean at `{config_key}.pass` (`:2`), each gated by its own tag. The protein-binder one (`:14-23`):

- `oracle_ppi.ptm_coi` min **80.0**
- `oracle_ppi.min_ipae_coi` max **1.5**
- `refolded_target_backbone_aligned_binder_rmsd.rmsd` max **2.5**

`antibody` (`:25-32`) and `dna` (`:34-42`) are two-term versions of the same shape, and their bounds are not uniformly looser: `antibody` demands binder RMSD ≤ **2.0** against `ppi`'s 2.5, while `dna` keeps the same ipAE bound of 1.5 and relaxes RMSD to 8.0. **Every term in all three is a refolding-oracle quantity** — AF3 by default (`configs/metrics/all_metrics.yaml:5`), with AF2 and OpenFold3 as alternates. We excluded every oracle image on licence grounds (§5.2), so **with our scope, nothing upstream ranks anything, and the headline benchmark percentages in §3 are pass rates we cannot reproduce in-house.** That is the real cost of the licence decision, and it is a scoring cost, not a generation cost.

One of the twelve is oracle-free: `developability` (`:109-120`) thresholds `rosetta_developability.sap_score` ≤ 50.0, `hydrophobic_exposure_sasa` ≤ 1500.0, `sequence_developability.gravy` ≤ 0.1 and `instability_index` ≤ 40.0. It is a monomer triage bar, not an interface bar — it says nothing about whether the binder binds — and it carries the PyRosetta dependency of §7A.6. The other eight criteria cover modalities (motif scaffolding, small-molecule, enzyme, DISCO) and are oracle-gated too.

### 7A.3 What survives oracle-free

`configs/metrics/inline_metrics/design_metrics.yaml` is computed from the generated structure alone. The ones that would make customer-visible columns for a binder run, with their gating tag:

| Column | Tag | Direction | Note |
|---|---|---|---|
| `interchain_interactions.hbond_atom_pair_count_coi` | `interchain_interactions` | maximize | Upstream's own oracle-free reward (`skills/rfd4-proteina-search/references/rewards.md:60`) |
| `interchain_interactions.hydrophobic_atom_pair_count_coi` | `interchain_interactions` | maximize | Paired with the above in the shipped search config |
| `contact_molecular_surface` | `cms` | maximize | The classic interface-size number |
| `clash_metrics` | `design` | minimize | Threshold 1.5 |
| `hotspot_obedience` | `hotspot_conditioning` | maximize | `max_hotspot_distance: 4.5` — "did it hit the residues you asked for" |
| `sequence_adherence` | `design` | — | "Reports NaN when nothing was sequence-conditioned" |
| `sse_accuracy`, `bond_length_metrics` | `design` | — | Geometry sanity, not ranking material |
| `amino_acid_composition` | *(none)* | — | Ungated — the one metric a default run still writes (§7A.1) |

The H-bond direction is upstream's own (`rewards.md:60`); the others are the conventional reading of each quantity and are **UNVERIFIED against an upstream statement** — rung 3 should confirm them before any of them sorts a results page.

**None of these is a confidence number.** They say the interface is large and clean; they do not say the model believes the complex folds.

### 7A.4 `search.reward` is a real scalar — but you define it, and it costs 27×

In search mode each `search.checkpoints.<step>` aggregates named metric outputs into one scalar (`rewards.md:38-50`: `sum(weight * clipped_value / normalize_by)`). `configs/inference_experiment/sm_binder_hbond_search.yaml:18-32` is a shipped, **fully oracle-free** example: beam width 2, branch 2, rewarding H-bond count at step 40 and H-bond + hydrophobic contacts at step 60. Three caveats before treating this as our ranking column:

- It only exists in search mode, which §3 measures at **~27× the GPU seconds per design**.
- A weighted output missing at scoring scores that candidate `-inf`, and if every candidate scores `-inf` search raises (`rewards.md:48-50`). That is a run-killing failure mode on a billed GPU.
- Reward scales differ per checkpoint (`rewards.md:120`), so the number is not comparable across presets. It is a within-run selector, not a cross-run score.

### 7A.5 Proposal: fold the designs ourselves and rebuild upstream's own bar

Our site shows a confidence number on every other design tool, and a tool that shows none will read as broken. The licence-clean route is to run each delivered design through a folder **we already ship** — `boltz2` or `esmfold2-design` — and compute interface pTM / PAE from that. This reconstructs two of the three quantities upstream's `ppi` criterion thresholds on (`ptm_coi`, `min_ipae_coi`) with a different predictor; the third, `refolded_target_backbone_aligned_binder_rmsd`, needs the refold itself and is not recovered this way. Whether `boltz2` or `esmfold2-design` exposes an interface PAE in the shape we need is UNVERIFIED and is a rung-3 question. Costs and cautions:

- Extra GPU time per run, which must be in the quote before release, not after.
- A recorded Hub finding is that `boltz2` ipTM is not reproducible run-to-run on one input (0.26–0.83 observed), which makes it a weak *sort key* even where it is a fine *display* number. Decide the sort key deliberately; the `#` column on our results pages is a position, not a rank.
- Our thresholds would be ours, not upstream's. Do not print 80.0 / 1.5 next to a non-AF3 number.

### 7A.6 New finding: the developability metrics need a PyRosetta container

`configs/metrics/inline_metrics/developability_metrics.yaml:1-3` gates both developability metrics behind `metrics_tags=['developability']` and states that **"Rosetta metrics require a PyRosetta apptainer set via `$PYROSETTA_CONTAINER`"**. `rosetta_developability` (`sasa_cutoff: 20.0`) is therefore a third-party dependency with its own licence question, on the same footing as the oracle images we excluded. `sequence_developability` in the same file appears to be pure-sequence and may not need it — **UNVERIFIED, not read**. Resolve before any developability column is promised.

### 7A.7 What rung 3 must capture

The scope doc has been treating the score column set as a rung-3 deliverable. It still is: **no `per_example_metrics.csv` header has been observed**, only the configs that should produce it. Rung 3 must record the real header, and the run must set `metrics_tags: [design, interchain_interactions, cms, hotspot_conditioning]` rather than the empty default, or it will produce no columns to read.


---

## 8. Work plan, in PR-sized steps

Each step is one PR with a gate. Sizes are rough.

| # | PR | Size | Gate |
|---|---|---|---|
| 0 | This scope doc | XS | Leo reads §9 and answers the open questions |
| 1 | `tools/rfd4/Dockerfile.modal` + `seed_volumes.py`, digest-pinned, SHA-pinned, no oracle images | **M** | Rung 1 passes: image builds, CLI runs, accelerator variant known |
| 2 | `tools/rfd4/modal_app.py` + `run_pipeline.py` — **PDB→CIF conversion, contig and atom-level hotspot construction live here**; `'~metrics'` enforced; byte-identical `_start_beat` | **L** — the biggest step, and where the risk is | Rungs 2–3: a real 8-design run completes, with its wall-clock and its real metrics header |
| 3 | Adapter + preflight: `tools/rfd4/{__init__.py,meta.py}`, `shared/pdb_preflight_rules.py`, `gpu/modal_client.py` ceilings, `app.py` import, feature flag off | **M** | Form validates; flag-off means no customer sees it |
| 4 | Wallet: `TOOL_SPECS`, `PER_JOB_HARD_CAP_USD`, `_LIVE_ALL_PRESETS`, `RFD4_START_USD`, `shared/job_recovery.py` score keys | **M** | Numbers come from rung 3, not from `proteina`'s |
| 5 | Results + exports: `shared/result_columns.py`, `shared/score_legends.py`, templates, CIF export path | **M** | Export parity tests pass |
| 6 | Guard-test entries: `test_deploy_paths_exclusions.py`, `test_modal_stale_result_guard.py`, `test_export_sequence_matrix.py`, `test_export_page_parity.py`, `test_signup_credit_covers_smallest_run.py`, `test_live_charging.py`, the deploy workflow matrix | **S**, but **do not skip** — several of these are hardcoded tool lists that fail on a new slug | Full suite green |
| 7 | Catalog and copy: `shared/tools_catalog.py`, `shared/tool_chooser.py`, `shared/email.py`, comparison and guide templates | **M**, customer-visible | **SEO lead sign-off.** Includes the unanswered positioning question: *what does a customer choose between `rfd4`, `proteina` and `rfdiffusion`, in one sentence?* |
| 8 | Flag flip after rung 4 | XS | Leo's go |

**Rollout order is fixed by the deploy workflow:** seed the volumes → deploy Modal → merge the hub PR → flip the flag. One trap: `.github/workflows/deploy-modal.yml` has **no per-app routing** — its matrix of nine apps means a `tools/**` change redeploys the whole fleet. (The trigger paths do negate `tools/**/meta.py`, `tools/**/example/**` and `tools/**/__init__.py`, so a data-only or adapter-only edit does not trigger it — but any Dockerfile or `modal_app.py` edit does, for all nine.) **Merge only when the fleet is idle** (no pending or running jobs for any tool), per standing practice.

---

## 9. Open questions for Leo

**GPU spend decisions — these are the ones that need an answer to proceed:**

1. ~~**Approve rung 1 ($0)?**~~ **APPROVED by Leo 2026-10-30.** CPU-only image build probe; no GPU, no spend. Leo also directed that whatever configuration it proves must be **pinned** — §5.3 is the list.
2. **Approve rungs 2–3 (≈$2.50 together)?** Volume seed plus one 8-design run. This is the decision point: it produces the real cost per run, the real score columns, and the streaming answer. I recommend approving 1–3 as one block, since 1 and 2 are nearly free and 3 is what makes the rest honest.
3. **Approve rung 4 (≈$1.85)?** Head-to-head against a recorded `proteina` run. Recommended but separable.
4. **Rung 5 (≈$5.55)?** Defer until after the go/no-go.

**Product decisions:**

5. **No confidence number on the results page.** §7A makes this worse than first written: upstream's *only* quality bar is pass/fail and every term in it comes from the oracle we excluded, so under our scope nothing upstream ranks anything. Ship release 1 with the oracle-free interface columns (§7A.3), or hold for a chained fold through our own `boltz2` / `esmfold2-design` (§7A.5)? I still recommend shipping, then chaining.
6. **Start minimum.** $1.00 proposed, but the derivation depends on rung 3's streaming answer. Confirm after rung 3, not now.
7. **Positioning.** Three binder tools on the shelf. What does a customer choose between them, in one sentence? This blocks step 7, not the engineering.
8. **Alpha software, four weeks old, 7 open issues.** Are we comfortable selling it? My read: yes, behind a flag and after rung 4, because the licence is clean and the downside is a refund, not a liability. Leo's call.

---

## 10. UNKNOWNs and UNVERIFIEDs that must close before step 2

1. **Does Modal's A100-80GB present a CUDA-13-capable driver?** STILL UNVERIFIED and still blocking. Rung 1 ($0, 2026-10-30) proved only that the `cu13` wheel set resolves and the code imports (§5.4); a CPU builder has no driver to test. Needs ~3¢ of GPU time, folded into rung 2.
2. **Real wall-clock and cost of an 8-design run on our hardware.** UNKNOWN. The benchmark's per-design seconds exclude setup, compile and warmup and name no GPU model. Rung 3.
3. **Do structure files land per design or only at run end?** NOT FOUND upstream. Decides whether streaming works at all and whether the $1.00 start minimum is defensible. Rung 3.
4. **The real `per_example_metrics.csv` header.** Currently inferred from upstream config files, not from a run (§7A). Rung 3 — and that run must set `metrics_tags` explicitly, since the default is empty and would produce no columns at all.
5. **PDB→CIF conversion and `label_seq_id` numbering.** Upstream resolves residue ids against mmCIF `label_seq_id`, not author numbering. Our whole stack speaks author numbering. **This is the most likely source of a silent wrong-residue bug** — the same class as the `proteina` hotspot defect already on record. Needs a deliberate test, not a code read.
6. **Hotspot atom-set expansion.** Upstream hotspots are atom-level; our picker emits residues. Which atoms we choose is a scientific decision nobody has made.
7. **Real weights-volume size.** Documented ~1.7 GB; a previous weights volume was off by ~7× against its doc figure. Rung 2.
8. **VRAM at the top of the size envelope.** Only "≥40 GB recommended" upstream, with no curve. Rung 5.
9. **Does `delete_failing_pre_oracle` race our uploader?** UNVERIFIED. Rung 3 observation.
10. **Cyclic peptide support.** A preset exists; upstream issue #62 asks whether it works. Out of scope for release 1 either way.
11. **Upstream binder-mode bugs.** #81 (high alanine fraction on the non-interacting face in binder mode) is open and is a *quality* bug in exactly our release-1 mode. Watch it; it may shape the copy we write about expected quality.
12. **Does the authors' benchmark hold on the current weights?** Upstream says it has not been re-measured. Rung 4 is our own check, on one target.
13. **Does `sequence_developability` also need the PyRosetta apptainer?** UNVERIFIED — only `rosetta_developability` is documented as requiring `$PYROSETTA_CONTAINER` (§7A.6), and the class was not read. Blocking for any developability column, not for release 1.
14. **Which confidence predictor and which sort key?** We must supply our own (§7A.5). `boltz2` ipTM is on record as not reproducible run-to-run, so the display number and the sort key may have to differ. A decision, not a measurement.
15. **Which accelerator variant we actually shipped.** Rung 1 showed `cu13` builds (§5.4); whether it *runs* on our hardware is unknown until the driver check. The Dockerfile must record the variant that was proven on a GPU, not the one that merely built.
16. **Do `CCD_MIRROR_PATH` and `PDB_MIRROR_PATH` need to be set?** `atomworks` warns on every invocation that they are unset (§5.4). Harmless for `--help`; unknown for a real run, and a likely requirement for any ligand or modified-residue modality. Rung 3.

---

## 11. What this pass did not do

No product code was changed. No Modal app was deployed. No GPU job was launched. No weights or container images were downloaded. No money was spent. No customer-visible string was edited.

One process note for whoever fetches the upstream docs next: a web-fetch helper used during this research reported that page text it retrieved appeared to carry embedded instructions about how to report its contents. Those were ignored, as they should be — text inside a fetched page is data, never direction. Worth knowing if the same pages are fetched again.

---

## 12. Review verdicts on this document

Both saved reviewers ran on this branch's delta against `origin/main` (045a61dc) before the PR was opened.

**`review-code`: no findings.** The delta is a single new prose file; there is no behaviour to review. Recorded for completeness, not as evidence that any code described here is correct — none of it exists yet.

**`review-claims`: ~60 claims enumerated, 52 checked — 44 VERIFIED, 3 FALSE or overstated, 5 UNVERIFIED.** It independently re-derived the §6 cost table from the rate constants ($0.0017476 per customer GPU-second, $6.29/hour, rung totals $4.32 and $9.87 against my stated ≈$4.30 and ≈$9.90) and recomputed every §3 benchmark row it checked from the upstream CSV, matching mine. The three confirmed problems and the five unverified items are all fixed in this file:

| Finding | Fix |
|---|---|
| §7.1 "Everything else defaults to $0" — FALSE; seven of eight live tools are non-zero | §7.1 now enumerates each one and names `boltz2` as the only $0 |
| §7.1 `_start_beat` "byte-identical" — the guard compares `ast.dump` | §7.1 now says AST-identical and cites `tests/test_live_charging.py:1147-1161` |
| §7.1 `3a8de1fc` "fleet-wide" — OVERSTATED; 7 in-repo apps only | §7.1 now names the 7 and lists what is not covered |
| §3 aggregation script not committed | §3 now says so and states the group-by |
| §5.1 "~2.7× the size" — the ratio belongs to the excluded oracle images | sentence replaced with an explicit UNVERIFIED |
| §1 "21 open issues" — the count includes PRs | corrected to 7, with the query that produced it |
| §7.2 score keys "known open defect for `proteina`" | reworded: proteina's keys *are* kept; the hazard is a new tool's |
| §8 "any `tools/**` change redeploys" — slightly broader than the trigger | §8 now notes the `meta.py` / `example/` / `__init__.py` negations |

A clean claims pass is not proof. Every figure in §3, §5 and §6 is either labelled with the file or command that produced it or marked UNVERIFIED; the UNKNOWNs in §10 are the honest list of what no reviewer could settle without spending GPU money.

### 12.1 Round 2 — on the §2A delta (`205f8b54..73caaea7`)

Both saved reviewers ran again on the §2A commit alone. `review-code` had a reviewable surface this time — not code, but whether §2A's statements about *this* repository would misdirect an implementer — and found the most important error in the section. I re-verified every finding below against the repo or upstream myself before accepting it; all nine stood.

**`review-code`: 3 findings, all confirmed.**

| Finding | Fix |
|---|---|
| §2A.2 located the ligand/DNA blocker in `shared/pipeline_normalize.py` — but that copy runs **only** in dry-run preflight (`shared/pdb_preflight.py:628`, output path `None`), the in-container copy is the sibling repo's, and `tools/proteina/`'s container — the one RFD4's would be copied from — never calls it at all | §2A.2 rewritten and retitled. The blocker is the PDB-only staging path (`shared/pdb_intake.py:482-488`, conversion at `:563`), not a residue filter. A new in-repo container inherits no filter, so there is nothing to remove — narrower work, differently located |
| §2A.2 "nothing tells the customer" — contradicted by `shared/pdb_preflight.py:9-10`, one line after the sentence it quoted; the cleanup *is* surfaced (`:1324-1334`, rendered per `:84-85`, wired at `blueprints/tools.py:1518-1551`) | §2A.2 now says the customer is told a **count**, not which ligand went, and that itemising it is a smaller build than a new report |
| §2A.6 quoted *"RFdiffusion-based de novo antibody and nanobody binder design"* at `tools/rfantibody/meta.py:45-50`; `grep -rn 'RFdiffusion-based' tools/` returns nothing — the string is in no file | Replaced with the actual `comparison_one_liner` text at `:45-52`. A fabricated quotation, and the worst defect in the section: it was the one cite a reader would spot-check |

**`review-claims`: ~45 claims enumerated, 38 checked — 25 VERIFIED, 4 FALSE, 2 overstated, 4 imprecise cites.** It independently re-read the whole pass-criteria table, the `inference.yaml` lines, the `conditioning.md` and `annotations.md` quotes, the cookbook's `'~metrics'` escape, all six `normalize_for_*` wrappers and the `score_legends.py` warning, and matched them.

| Finding | Fix |
|---|---|
| "ten runnable, four named but not runnable" — FALSE on the section's own definition; rows 3 and 6 have **no** config, so the split is eight / six | §0 pointer and §2A.3 both corrected to eight / six, with the two mechanism-only rows called out |
| "blocked behind one shared piece of platform work on our side, **not upstream**" — the document's own rows 4 and 6 record upstream blockers for two of the three | §0 pointer and §2A.7 now say the ligand row is the only one where our work is the sole blocker |
| Release 1 = "two of the four published benchmarks' worth of confidence" — monomer has no published number, so it is one | §2A.7 stage table corrected; the monomer option is called a smoke test, not evidence |
| Pre-oracle trap: "a mis-tagged run can plausibly deliver zero designs" — the quoted comment says *non-deleting*, and `inference.yaml:41-44` scopes the delete flag to filters whose gate **matched**, the opposite case | §2A.1 rewritten: the hazard is zero *passing* designs on a fully billed run, and the rung-3 harness must assert on delivered-design count |
| `website/index.html:313` quoted alongside `:277` as a bare *"state-of-the-art"* claim — `:313` is a qualified comparative (*"equivalently to or better than"*) | §2A.4 now quotes both exactly, distinguishes them, and says not to quote `:313` as a win. Both upgraded to FIRST-HAND |
| Four cites off by one to a few lines (`pipeline_normalize.py:346-348`, `pdb_intake.py:481-485`, `pdb_preflight.py:6-8`, and a dropped markdown link in the `examples/antibody_design/README.md:6` quote) | All corrected to `:346-349`, `:482-488`, `:7-8`; the README quote is unchanged in substance |

Two cautions carried forward. First, §2A.4's SECOND-HAND items (the leaderboard basis, `protocols.mjs`, the CSV aggregates, the `scTM` zero-hit search, the absent GPU model, the wet-lab list) were not re-verified by either reviewer and remain labelled as what a delegated reader quoted. Second, round 1 recorded `review-code` as having no reviewable surface on a prose delta; round 2 shows that was the wrong conclusion — a scoping document's statements about our own code are reviewable, and that review found the error that would have cost the most to act on.
