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

**Scoring is the one thing this shape costs us, and §7A is new:** RFD4 ships no default score, and every pass/fail criterion that bears on binder quality is computed from the refold oracle we exclude (the one oracle-free criterion grades monomer developability, not binding — §7A.2). The oracle-free interface metrics it does compute are real but carry no confidence number, so release 1 should chain a fold through a tool we already own.

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

### 4.3 Proposed preflight envelope — runtime now **measured**, the size and VRAM numbers still **provisional**

Model it on `_PROTEINA` (`shared/pdb_preflight_rules.py:844`) and replace each number the moment we measure:

- `min_target_aa` 30 — copy from `_PROTEINA`.
- `hard_cap_target_aa` — **start at 400, not 500.** `_PROTEINA`'s 500 is a policy number anchored on a measured VRAM curve up to 415 aa; RFD4 has no such curve here, and its own `compile_lengths` bucket list tops out at 1024 tokens with a BioIR CUDA-graph limit of 511 (`configs/inference.yaml:19,51`) — which is **INFERRED** to mean longer inputs run uncompiled and slower, not that they are refused. Tighten on measurement, loosen later.
- `hard_cap_combined_aa` — 520 (400 target + 120 binder), same logic.
- `runtime_base_min` / `runtime_alpha` — **measured at rung 3 (§5.7): `expected_gpu_seconds ≈ 65 + 12.3 × N`**, from 159.2 s over eight designs with two independent estimates of the marginal term agreeing to 4%. Three limits on using it: it was measured *eager* (`compile_lengths: null`), unbatched, and on a single 115 aa target with a 50–120 aa binder, so it is one point on a curve, not the curve. Still do not copy `proteina`'s numbers; they describe a different model.
- VRAM: upstream's only figure is "≥40 GB VRAM recommended" (`skills/rfd4-proteina-setup/skill-card.md:31`, verified by direct read this session). The HF card declines to give a number: peak memory "depends on token count, batch size and recycle depth rather than on the weights". So **A100-80GB, not 40GB**, until measured.

---

## 5. Modal image, GPU class and the CUDA-13 risk

### 5.1 The blocking technical question

Upstream requires **Linux x86_64, Python 3.12, and a CUDA-13-compatible driver** (README; macOS and Windows unsupported). Their `Dockerfile` takes `RFD4_ACCELERATOR=cu13|cu12|cpu`, builds on `python:3.12.12-slim-bookworm` pinned by digest, and ships **no CUDA base image** — CUDA runtime libraries come from the PyTorch wheels, so the host needs only a driver and the container toolkit.

Our `tools/proteina/Dockerfile.modal:35` builds on `nvcr.io/nvidia/pytorch:24.08-py3`, a CUDA-12 base. **Whether Modal's A100-80GB hosts present a CUDA-13-capable driver is the one thing that decides the shape of this work, and it is UNVERIFIED.** Two outcomes:

- **Driver is new enough** → build the `cu13` variant from upstream's Dockerfile. Best case.
- **Driver is CUDA 12 only** → build `RFD4_ACCELERATOR=cu12`. Upstream ships it as a first-class option, so this is a fallback rather than a blocker — but it is a different wheel set and must be probed, not assumed. Rung 1 proved cu12 builds and imports too — but it drops the BioIR inference backend, so it is a **different product**, not a drop-in fallback (§5.4).

Rung 1 answered half of this for $0 on 2026-10-30: the `cu13` wheel set resolves and the image builds and imports (§5.4). The driver half was settled the same day for ~3¢ and **passed** — see §5.5.

### 5.2 Image plan

Follow the `proteina` pattern exactly, because its guard tests already enforce it:

- New `tools/rfd4/Dockerfile.modal`, **digest-pinned `FROM`** and **`ARG RFD4_SHA=<commit>`** with a `git checkout` of that SHA — same shape as `tools/proteina/Dockerfile.modal:35,54,58`. Pinned to a commit, never a branch.
- Add the `certifi` floor layer; `tests/test_gpu_image_certifi_floor.py` and `tests/test_dockerfile_digest_pins.py` glob `tools/*/Dockerfile.modal`, so a new file is picked up automatically and will fail the suite without these.
- **Do NOT include `alphafold2/runtime/` or `openfold3/runtime/`.** Per the HF model card those images package third-party software under its own upstream terms, some of which restrict commercial use or redistribution, and **neither oracle is needed for generation**. They are also 3.5–16.6 GB each.
- **The oracle exclusion has a config consequence that must be in code, not in a comment.** Upstream's default `inference_on_user_inputs` experiment composes a metrics group whose default oracle is AF3. The wrapper must pass `'~metrics'` (or an inline-only metrics tag) so no oracle is ever selected. A guard test asserting that our invocation disables the oracle group is the cheapest way to keep a future edit from quietly re-enabling a licence we did not clear.
- Weights on a Modal Volume seeded once, copying `tools/proteina/seed_volumes.py`. **Flow checkpoint and autoencoder are a matched pair** — the HF card warns that mixing releases "produces nonsense rather than an error", so the seeder must pin one HF revision for both and record it.
- Sizes from the HF file listing: `model.safetensors` 1.74 GB, flow checkpoint 1.75 GB, autoencoder 1.03 GB; upstream says `make setup` caches about 1.7 GB. Upstream prebuilt runtime `.sif` images (weights excluded): cu13 4.04 GB, cu12 4.41 GB. **Measured at rung 2: the volume is 2.59 GiB** (§5.6), because it holds the matched pair, not one file — the kind of gap that put our `boltz2` weights volume ~7× off its documented figure.
- GPU class **A100-80GB** (`_GPU` must be a module-level literal; `tests/test_gpu_class_drift.py:155-171` requires a matching `TOOL_SPECS` entry).

### 5.3 Pin list — what rung 1 must turn into code, not prose

Leo's instruction (2026-10-30): once we know how to run it, **pin those settings**. Eight images already carry digest pins (`tests/test_dockerfile_digest_pins.py`, which globs `tools/*/Dockerfile.modal`; esmfold2_design has no Dockerfile and is checked differently, `:68-75`), so the mechanism exists; what follows is the list this tool has to fill in, and the file each pin lives in. Rungs 1 and 2 have now produced the accelerator variant, the weights revision and the filename mapping (§5.4, §5.6); the rest are still **UNSET**.

| What | Where it is pinned | Why a float here hurts |
|---|---|---|
| Upstream commit | `ARG RFD4_SHA=<sha>` + `git checkout`, same shape as `tools/proteina/Dockerfile.modal:54` | Upstream is alpha and moving; a branch checkout re-resolves on every rebuild |
| Base image | digest-pinned `FROM`, enforced by `tests/test_dockerfile_digest_pins.py` | Tag drift silently changes the driver/toolkit pairing |
| **Accelerator variant** | `ARG RFD4_ACCELERATOR=cu12\|cu13` with **no default** | This is rung 1's answer (§5.1). A default would let a rebuild silently pick the untested wheel set |
| Weights revision | the literal `a06e2509c7033b010fc43614e9fd840303588502`, both checkpoints, recorded in `seed_volumes.py` (§5.6) | The HF card warns a mismatched flow/autoencoder pair "produces nonsense rather than an error" — no exception is raised, so nothing would catch it |
| **Checkpoint filenames** | the flow checkpoint **must keep a name ending `-ema.ckpt`** (HF's own `rfd4_proteina_flow-ema.ckpt`); autoencoder `rfd4-proteina-ae-release-2026-10-19.ckpt` (`configs/inference.yaml:46`). The invocation pins a matching `ckpt_name=` | `rfproteina/utils/weights.py:20` dispatches on the filename suffix: without `-ema.ckpt` the load raises `has no SimpleEMA state`. Upstream's own `configs/inference.yaml:38` default fails this way on the native route. Proven at rung 3 (§5.7) |
| **Seed allow-list** | an explicit per-file list in `seed_volumes.py`, never `snapshot_download` | The pinned HF revision is 82.57 GB and ~71.4 GB of it is the `alphafold2/runtime` and `openfold3/runtime` images we exclude on separate terms. A snapshot pulls them (§5.6) |
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


**What rung 1 did not answer.** A CPU builder has no NVIDIA driver, so this proves the wheel set resolves and the code imports — **it does not prove Modal's A100-80GB hosts present a CUDA-13-capable driver**, which was §5.1's actual question. That cost about three cents of GPU time to settle rather than $0, and **it has now been settled — see §5.5.** An earlier draft of the §6.1 ladder said rung 1 would establish the host driver; that row and §5.1 are corrected above.


### 5.5 Rung 2 — the driver check: cu13 runs on Modal's A100-80GB hosts

**Run 2026-10-30, Leo's ~3¢ approval, app `ap-xGZQXncSKquxGYSQudwHNX`. Result: PASS.** The probe attached one A100-80GB for a few seconds and reported:

| | |
|---|---|
| Driver | **610.57.04**, NVIDIA A100-SXM4-80GB |
| Compute capability | **(8, 0)** — sm_80 |
| torch | **2.14.1+cu130** (the cu13 wheel set, not the cu126 fallback) |
| `torch.cuda.is_available()` | True |
| Real kernel launch (512×512 matmul) | finite, OK |
| `tmol.score.ljlk.potentials._compiled` | **loaded** |
| `rfd4-generate --help` | exit 0 |

That closes §5.1's question: **the cu13 wheel set and Modal's driver are a working pair, so the cu12 fallback described in §5.4 — which drops the BioIR backend — is not needed.** The decisive rows are the ones a CPU builder could not produce: a driver is present, `torch.cuda.is_available()` is True, and a real kernel launched and returned finite values. (An earlier draft of this section claimed the TMol operator had *failed* on the CPU builder and now loads. That is wrong and is corrected here: §5.4 records that the TMol import **passed** on CPU; the `libcuda.so.1` error was raised by `libcue_ops.so`, a different library, and whether *it* now loads was not probed.) Compute capability 8.0 is recorded because a cu13 build targeting only newer architectures would have failed here rather than on the builder.

**The `CCD_MIRROR_PATH` warning recurs with a GPU attached**, in the same words as on the CPU builder. It is not a new finding — §5.4 already recorded it and §10 item 16 tracks it — but it is now known not to be an artifact of running driverless. Still UNVERIFIED whether binder generation needs the variable; rung 3.

**The first attempt crash-looped and measured nothing.** The probe imported its image definition from the sibling rung-1 file, which Modal does not ship into the container, so every start died at import (`ModuleNotFoundError: No module named 'rfd4_cpu_probe'`) before a GPU did any work; Modal's retry loop re-tried it until the deployed-function alert fired. App `ap-TPERf9v6ZEHA2yPAG9AbFy`, stopped, 0 tasks. Fixed with `add_local_python_source`, plus `retries=0` so an import error cannot loop again. **No GPU work ran on the failed attempt**, so the ~3¢ approval was consumed once, by the run above.

### 5.6 Rung 2 RESULT — the weights land at 2.59 GiB and load on our A100, 2026-10-30

**Run 2026-10-30, Leo's ~$0.62 approval, app `ap-g8jGQ8Cft8pfeVvfl0bk16`. Result: PASS.** Script: `rfd4_weights_seed.py` (scratchpad; it is a probe, not product code). Volume `rfd4-proteina-weights`, pinned revision `a06e2509c7033b010fc43614e9fd840303588502`.

| File on the volume | Bytes | SHA-256 vs manifest |
|---|---|---|
| `flow_checkpoints/rfd4-proteina-flow-release-2026-10-19.ckpt` | 1,751,488,703 | **match** |
| `ae_checkpoints/rfd4-proteina-ae-release-2026-10-19.ckpt` | 1,029,340,246 | **match** |
| `release-checkpoints.json` | 3,755 | — |
| `config.json` | 7,270 | — |
| **Volume total** | **2,780,839,974 B = 2.59 GiB** | |

Loaded on an **NVIDIA A100-SXM4-80GB**, torch 2.14.1+cu130, every tensor moved to the device and **every floating-point tensor** checked finite (the probe's finiteness test skips integer and bool tensors):

| | Tensors | Elements | Peak GPU |
|---|---|---|---|
| Flow | 1,313 | 437,686,849 | 1.64 GiB |
| Autoencoder | 699 | 257,174,161 | 0.96 GiB |

Both state dicts are PyTorch-Lightning checkpoints (`epoch`, `global_step`, `hyper_parameters`, `pytorch-lightning_version`). **That closes §10 item 7:** the real volume is **2.59 GiB**, not the ~1.7 GB this document carried. §5.2 took that figure from upstream's note that `make setup` caches about 1.7 GB, alongside single-file sizes; whichever file it referred to, it was a single file, and the volume has to hold the flow checkpoint *and* the autoencoder, which upstream requires be used together.

**The licence trap this step avoided.** The pinned HF revision is **82.57 GB over 55 files**. The eight `alphafold2/runtime/**` and `openfold3/runtime/**` image files alone account for **~71.4 GB of it** — the images this project excludes on separate-terms grounds. A bare `snapshot_download` pulls them. **Seeding must use an explicit allow-list, never a snapshot**, and that belongs in the pin list in §5.3 as a hard requirement, not a convention. We fetched 2.78 GB of 82.57 GB.

**Two things the seeding code has to get right, both now known:**

1. **The filenames differ between HF and the config.** HF stores stable names (`flow_checkpoints/rfd4_proteina_flow-ema.ckpt`); `configs/inference.yaml:38,46` reads dated ones (`rfd4-proteina-flow-release-2026-10-19.ckpt`). The seed must rename on the way in. The mapping and the digests are in the repo's own `release-checkpoints.json`.
2. **The revision upstream pins and the revision upstream ships disagree.** `configs/inference.yaml:30` at RFD4-Proteina `aaccdd2a` names HF commit `a06e2509…` (2026-10-20). The `inference.yaml` stored *inside* that HF commit names `cf31b935…` (2026-10-19), which is also `release-checkpoints.json`'s `bundle_revision`. Both commits exist. **We pin `a06e2509…`** — it is what upstream's code reads and what the file list above was enumerated from. §5.3 records it as the literal to use; nothing enforces it yet, because `seed_volumes.py` does not exist. The seeder **will** carry it as a module-level constant and a guard test **should** assert on that constant, the way `tests/test_gpu_class_drift.py` already does for `_GPU`.

**Three caveats, so this is not read as more than it is.** (i) This loaded checkpoints; it did **not** instantiate the RFD4 model or run a sampling step, so `torch.compile`, the length buckets and real VRAM remain rung 3's job. (ii) The flow checkpoint holds **437,686,849** tensor elements against `release-checkpoints.json`'s `stored_parameters: 435622465` — a difference of 2,064,384. The manifest counts parameters and our probe counted every tensor in the state dict, so the two are not the same quantity; **UNVERIFIED** that the gap is only buffers. (iii) The download ran **unauthenticated** and the Hub warned about rate limits; production seeding should carry an `HF_TOKEN`. The exact GPU seconds billed were **not captured**. The only bound is the GPU function's own 600 s timeout, which at $0.001028/s caps *that function* at $0.617; container start and the CPU download are outside it. So the GPU half is bounded under the approved figure, the total is **unverified**, and a future rung should record the seconds rather than infer them.

### 5.7 Rung 3 RESULT — a real 8-design binder run, 2026-10-30

**Leo approved ~$1.85 on 2026-10-30. Three GPU functions ran; the ladder rung PASSED. Total raw spend $0.2908, 15.7% of the approval.** Scripts `rfd4_rung3_run.py` and `rfd4_rung3b_csv.py` (scratchpad probes, not product code).

| | App | Wall | Raw cost | Outcome |
|---|---|---|---|---|
| Attempt 1 | `ap-DunYOgRooNnkuhVaOsJrRQ` | 47.3 s | $0.0486 | **FAILED** at checkpoint load, 0 files |
| Rung 3 | `ap-f44S6NrCsPGSBFSAIJF38b` | 159.2 s | $0.1637 | **PASS**, `returncode 0`, 8/8 designs |
| Rung 3b | `ap-uZCAf7oVcR7l3D0s7WyC8h` | 76.4 s | $0.0785 | **PASS**, 1 design, recaptured the CSV and CIF |
| | | **282.9 s** | **$0.2908** | of $1.85 approved |

Rung 3b exists because rung 3's probe returned only file *sizes*, so the metrics-CSV header — a named deliverable — died with the container. Its only purpose was to fetch the two artefacts back.

**The failure is a real integration finding, and it belongs in the pin list.** Attempt 1 died with `ValueError: ... has no SimpleEMA state, but inference requires EMA weights` (`rfproteina/utils/weights.py:24`, reached via `generate.py:781 → :651 → :455 → :353`). The loader **dispatches on the filename**: `weights.py:20` treats a path ending `-ema.ckpt` as already-EMA and uses its `state_dict` directly, and anything else must carry a `SimpleEMA` callback. The HF publish strips exactly that — `release-checkpoints.json` records `training_state_stripped: true` for the flow checkpoint. Our rung-2 seed had renamed HF's `rfd4_proteina_flow-ema.ckpt` to the dated name `configs/inference.yaml:38` asks for, which removed the suffix the loader needs. **Upstream's own packaged default is self-defeating on the native route** for the same reason: `ckpt_name: rfd4-proteina-flow-release-2026-10-19.ckpt` does not end in `-ema.ckpt`, so `hf_ckpt_path=null` with the stock config cannot load the stock checkpoint. The autoencoder is unaffected — `proteina.py:464` uses a plain `AutoEncoder.load_from_checkpoint` with no EMA dispatch.

So **§5.6's instruction to rename on the way in was wrong for the flow checkpoint, and §5.3's "Checkpoint filenames" row is corrected accordingly**: the seeded flow checkpoint must keep a name ending `-ema.ckpt`, and the invocation must carry a matching `ckpt_name=` override. The fix cost nothing — a CPU-only `os.rename` inside the volume, no re-download. The volume's flow checkpoint is now `flow_checkpoints/rfd4_proteina_flow-ema.ckpt`, 1,751,488,703 bytes, the same file rung 2 digest-verified.

One route we did **not** take, and should not: setting `hf_ckpt_path` to the repo makes `generate.py:419-427` call `Proteina.from_pretrained(...)` with `ckpt_name="model.safetensors"`, which **re-downloads from the Hub and ignores the volume entirely**. Only `hf_ckpt_path=null` reads `${DATA_PATH}`.

**The invocation that worked:**

```
rfd4-generate inference_experiment=quickstart hf_ckpt_path=null \
  ckpt_name=rfd4_proteina_flow-ema.ckpt \
  inputs=/opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml \
  num_replicates=8 out_dir=/tmp/rfd4_run
```

**Cost: ~$0.28 customer for eight designs — one tenth of what this document assumed.** §6 predicted RFD4's cost would be set by startup rather than sampling. It is, but the startup is far cheaper than `proteina`'s 553–1343 s. Decomposing the eight per-design file timestamps:

| | Measured |
|---|---|
| Total wall, 8 designs | 159.2 s → **$0.2782 customer** |
| First design visible at | 68.1 s |
| Marginal, within-run (154.1 − 68.1)/7 | **12.29 s/design** |
| Marginal, between-run (159.2 − 76.4)/7 | **11.83 s/design** |
| Fixed overhead, by subtraction | **~61–65 s** |

Two independent estimates of the marginal cost agree to 4%. A working model for `expected_gpu_seconds` is **`65 + 12.3 × N` seconds**, which reproduces the measured 159.2 s to within 2%. At N=8 that is $0.28 customer; at N=100, $2.26 — but 100 replicates is a **12× extrapolation beyond anything measured**, and the per-design time is an average over a random 50–120 aa binder length draw (the example's contig), not a fixed-length figure. One run, no variance estimate.

**Three reasons this figure is not yet the production figure.** (i) `quickstart` sets `compile_lengths: null`, and its own comment says the `torch.compile` warm-up "costs minutes on a first run and pays off only across many designs, so drop this preset for production runs" — so 12.3 s/design is the **eager** number and ~61–65 s is a **floor** on startup, not a ceiling. (ii) `batch_size` was left at the preset's default, so the eight designs ran strictly one at a time; batching would change both numbers. (iii) VRAM was not captured.

**Streaming: designs land one at a time, so save-as-you-go is feasible.** First-sighting times from a 1 Hz poller: 68.1, 80.1, 92.1, 105.1, 117.1, 130.1, 142.1, 154.1 s — one design every 12–13 s, no end-of-run burst. **That closes §10 item 3.** Two caveats a watcher must handle:

1. **Files are written atomically via a hidden temp directory.** The poller caught `inference_samples/pdl1_binder_5/.generated-cif-w_d3wq6m/pdl1_binder_5_generated.cif` at 129.1 s, one second before the real path appeared at 130.1 s. Only one of eight was caught, which is what a 1 Hz sample of a sub-second window looks like — assume it happens on every design. A naive recursive glob would upload the temp path. Match the final filename and skip dot-directories.
2. **`per_example_metrics.csv` appears only at 154.1 s**, the same second as the last design. It is written once at the end, so nothing derived from it can stream.

**`per_example_metrics.csv` has 44 columns and not one of them is a score.** The real header, finally observed: `example_id`, `designed_structure_path`, `task`, `dataset_name`, `chain_of_interest`, `ckpt_path`, `ckpt_name`, `seed`, `input_json_path`, then 33 `generation.*` hyperparameters, `output_base_dir`, `metrics_tags`. Under a no-metrics configuration the file is a **run manifest, not a ranking**. This is hard confirmation of §7A.1 and §7B: with the oracle images excluded, nothing upstream ranks anything, and every score column on our results page has to come from our own Boltz-2 chain.

**This only partially closes §10 item 4.** §7A.7 asked that rung 3 set `metrics_tags: [design, interchain_interactions, cms, hotspot_conditioning]`; this run did **not** — it used `quickstart`, whose comment states the `metrics` group "is not composed (the same as `~metrics`)", and `metrics_tags: []`. So we now know the header for *our intended production configuration*, which is the one that matters, but we have **not** observed what the inline metrics would add. If a future rung wants those columns, it must set the tags explicitly and will need `$PYROSETTA_CONTAINER` for the developability pair (§7A.6).

**Runs carry a seed and record it.** The manifest's `seed` column reads `236859448.0` — drawn per run, written into the CSV, and therefore pinnable. That is the mechanism §7B.3 needs for reproducibility, on the RFD4 side at least. Note it is serialised as a **float**, which is lossy above 2^53; the value we observed is far below that, but a seed should be read as an integer and never round-tripped through the CSV.

**The output CIF is the complex, not the binder alone.** `pdl1_binder_0_generated.cif` holds two polymer entities: entity 0 is the 115 aa PD-L1 target on strand A (matching the example's A17-131 contig), entity 1 is the 67 aa generated binder on strand B. 1512 atom lines over 182 residues ≈ 8.3 atoms/residue, so it is all-atom, not backbone-only. Atom records appear in entity order `['1', '0']` — **binder first, target second**. Any downstream chain-splitting must key on the entity or strand id, never on file order.

**A quality signal that must not be read as a result.** The generated binder sequence is `EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEYEEELEKQEELEEDPEYQEAKEKAEEVIKELKEKA` — a 32-residue run of glutamate followed by a more plausible helical stretch, **70% glutamate overall (47 of 67)**. That is not a credible binder. I have **not** diagnosed why, and this document should not guess: candidate explanations include the eager no-compile preset, the manifest's `generation.args.inverse_folding,0.0`, and upstream issue #81 (already on record at §10 item 11 as a composition bug in binder mode). **What follows for us is narrow and firm: no `quickstart` output may be shown to anyone as evidence that the tool produces usable binders.** Rung 4, on our own target, must run a production configuration and must look at sequence composition before any head-to-head claim is made.

**Two smaller closes.** `CCD_MIRROR_PATH` and `PDB_MIRROR_PATH` were unset and the run completed — so they are **not** required for the protein-binder modality (§10 item 16 narrows to the ligand and modified-residue modalities, still unknown). And no file was deleted during the run: all 8 designs persisted, which is expected with no oracle composed, so §10 item 9 is **not applicable to our configuration** rather than answered.


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

**Rung 3 settled it (§5.7): ~61–65 s of fixed startup plus ~12.3 s per design, so eight designs billed ~$0.28 customer — an order of magnitude below the `proteina` shards, and still startup-dominated.**

### 6.1 Validation ladder — cheapest first

**Every rung is a proposal. Leo approves GPU spend per job. Nothing here has been launched.** Dollar figures are our raw A100-80GB cost, not customer price.

| Rung | What it answers | GPU | Cost | Notes |
|---|---|---|---|---|
| **1. CPU build probe** | Does the image build, does `rfd4-generate --help` run, and **which accelerator variant do we need?** | none | **$0** | Build `cu13` on a CPU builder with weights excluded; a `RUN` probe at build time is free. **DONE 2026-10-30, $0: `cu13` builds and imports (§5.4).** It does NOT establish the host driver — a CPU builder has none. Fold that check into rung 2. |
| **2. Volume seed + GPU import smoke** | Do the weights land, how big is the volume really, does CUDA initialise on our A100? | 1 × A100-80GB, ~10 min | **~$0.62** | Mirrors `proteina`'s seeding step. **DONE 2026-10-30 (§5.6): volume is 2.59 GiB, both digests match the manifest, both checkpoints materialise on an A100-80GB.** Exact seconds billed unverified; bounded below the budget. |
| **3. One 8-design binder run** | **The cost question.** Real wall-clock, real output tree, real per-design file timing | 1 × A100-80GB, ≤30 min | **~$1.85 approved; $0.2908 spent** | **DONE 2026-10-30 (§5.7): 159.2 s for eight designs = ~$0.28 customer; designs land one every ~12 s; the metrics CSV has 44 columns and no score.** One failed attempt and one artefact-recapture run are inside that $0.2908. |
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

**Proposed start minimum for `rfd4`: $1.00 — rung 3 has now removed the thing that made this a guess.** Structures *do* land per design, the first at 68.1 s (§5.7), so the stretch a start minimum has to cover is container start + weight load, not the whole run. At the customer rate $1.00 buys ≈572 s, roughly eight times that stretch, which is comfortable rather than tight. **It remains a Leo decision, not a derivation** — the principle the other tools follow is "cover the stretch before the first design reaches storage", and the measured stretch is a floor: a production configuration pays a `torch.compile` warm-up that `quickstart` skipped, which upstream describes as costing minutes. If that warm-up lands before the first design, $1.00 could stop covering it; rung 4 should re-measure the first-design time with compile on before the figure is fixed.

### 7.2 Streaming / save-as-you-go

Copy the `proteina` mechanism: the container uploads each design via a presigned PUT, then sends a heartbeat carrying a `new_candidate` record; the hub appends it to the job's partial candidates, and `shared/job_recovery.py` rebuilds a result from those if the run stops. **Two RFD4-specific hazards:**

- **`delete_failing_pre_oracle: True`** (`configs/inference.yaml:44`) deletes structure files for designs failing a pre-oracle filter. **Rung 3 showed this does not bite in our configuration** — no metrics group is composed, so no filter runs and all eight designs persisted (§5.7). The hazard is therefore conditional on enabling an oracle, which the licence exclusion rules out. Keep the uploader tolerant of a vanished file anyway; it costs one `try`.
- **Each design is written through a hidden temp directory, and the uploader must not see it.** Rung 3 caught `inference_samples/<id>/.generated-cif-w_d3wq6m/<id>_generated.cif` one second before the real path appeared (§5.7) — an atomic write-then-rename. A recursive glob for `*_generated.cif` would upload a partial file under a path no exporter expects. Match on the final filename and skip dot-directories.

- **Score keys must be registered.** `shared/job_recovery.py:59` keeps only a fixed tuple of score keys when rebuilding a candidate from a partial, so a new tool's own metrics are dropped on a stop or cancel unless its keys are added. The tuple is `_PROTEINA_SCORE_KEYS`, named for and populated with proteina's metrics, so proteina itself is fine; the hazard is that a new tool silently loses any metric not on that list. Add `rfd4`'s keys in the same PR that adds its streaming. Note rung 3 found RFD4 itself emits **no** score (§5.7), so every key on that list will come from our own Boltz-2 chain (§7B).

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

`configs/metrics/pass_criteria/all_pass_criteria.yaml` defines eleven criteria, each emitting one boolean at `{config_key}.pass` (`:2`), each gated by its own tag. The protein-binder one (`:14-23`):

- `oracle_ppi.ptm_coi` min **80.0**
- `oracle_ppi.min_ipae_coi` max **1.5**
- `refolded_target_backbone_aligned_binder_rmsd.rmsd` max **2.5**

`antibody` (`:25-32`) and `dna` (`:34-42`) are two-term versions of the same shape, and their bounds are not uniformly looser: `antibody` demands binder RMSD ≤ **2.0** against `ppi`'s 2.5, while `dna` keeps the same ipAE bound of 1.5 and relaxes RMSD to 8.0. **Every term in all three is a refolding-oracle quantity** — AF3 by default (`configs/metrics/all_metrics.yaml:6`), with AF2 and OpenFold3 as alternates. We excluded every oracle image on licence grounds (§5.2), so **with our scope, nothing upstream ranks anything, and the headline benchmark percentages in §3 are pass rates we cannot reproduce in-house.** That is the real cost of the licence decision, and it is a scoring cost, not a generation cost.

One of the eleven is oracle-free: `developability` (`:109-120`) thresholds `rosetta_developability.sap_score` ≤ 50.0, `hydrophobic_exposure_sasa` ≤ 1500.0, `sequence_developability.gravy` ≤ 0.1 and `instability_index` ≤ 40.0. It is a monomer triage bar, not an interface bar — it says nothing about whether the binder binds — and it carries the PyRosetta dependency of §7A.6. The other seven criteria cover modalities (motif scaffolding, small-molecule, enzyme, DISCO) and are oracle-gated too.

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

### 7A.5 Fold the designs ourselves and rebuild what we can of upstream's bar

**Decided 2026-10-30 — see §7B for the design, the BindCraft precedent and the reproducibility risk.**

Our site shows a confidence number on every other design tool, and a tool that shows none will read as broken. The licence-clean route is to run each delivered design through a folder **we already ship** — `boltz2` or `esmfold2-design` — and compute interface pTM / PAE from that. This reconstructs two of the three quantities upstream's `ppi` criterion thresholds on (`ptm_coi`, `min_ipae_coi`) with a different predictor; the third, `refolded_target_backbone_aligned_binder_rmsd`, needs the refold itself and is not recovered this way. Whether `boltz2` or `esmfold2-design` exposes an interface PAE in the shape we need is UNVERIFIED and is a rung-3 question. Costs and cautions:

- Extra GPU time per run, which must be in the quote before release, not after.
- A recorded Hub finding is that `boltz2` ipTM is not reproducible run-to-run on one input (0.26–0.83 observed), which makes it a weak *sort key* even where it is a fine *display* number. Decide the sort key deliberately; the `#` column on our results pages is a position, not a rank.
- Our thresholds would be ours, not upstream's. Do not print 80.0 / 1.5 next to a non-AF3 number.

### 7A.6 New finding: the developability metrics need a PyRosetta container

`configs/metrics/inline_metrics/developability_metrics.yaml:1-3` gates both developability metrics behind `metrics_tags=['developability']` and states that **"Rosetta metrics require a PyRosetta apptainer set via `$PYROSETTA_CONTAINER`"**. `rosetta_developability` (`sasa_cutoff: 20.0`) is therefore a third-party dependency with its own licence question, on the same footing as the oracle images we excluded. `sequence_developability` in the same file appears to be pure-sequence and may not need it — **UNVERIFIED, not read**. Resolve before any developability column is promised.

### 7A.7 What rung 3 captured

**Answered at rung 3 (§5.7), for the configuration we intend to ship.** The real 44-column header was observed and **contains no score column** — under a no-metrics configuration the file is a run manifest. What rung 3 did *not* do is set `metrics_tags: [design, interchain_interactions, cms, hotspot_conditioning]`, so what the inline metrics would add is still unobserved; a rung that wants those columns must set the tags and will need `$PYROSETTA_CONTAINER` for the developability pair (§7A.6).


---

## 7B. Decision: our own scoring, chained through Boltz-2

**Leo, 2026-10-30: "we want to use our own scoring, integrate with boltz2 refolding."** That settles §7A.5 and §9 item 5. This section is the design and, more usefully, the two things that make it harder than it sounds.

### 7B.1 The BindCraft precedent, and why it is two separate reroutes

Leo asked how BindCraft handled this for open source. The answer is that BindCraft had **two** licence problems, and only one of them is about folding — which matters, because RFD4-Proteina has the same two.

**Reroute 1 — the scoring/relax stack, not the folder.** BindCraft's own code is MIT, and its design loop (AlphaFold2 via ColabDesign, ProteinMPNN) is open; the non-commercial dependency was **PyRosetta**, used for the final relax and interface scoring. Rosetta is free to academics and charges commercial entities. The community fork **FreeBindCraft** removes it and substitutes OpenMM + PDBFixer for relax, FASPR for side-chain repacking, and `sc-rs` for shape complementarity ([Ariax write-up](https://www.ariax.bio/resources/freebindcraft-open-source-unleashed), [Tamarind](https://app.tamarind.bio/tools/freebindcraft)). Its equivalence to BindCraft is **vendor-reported and wet-lab validation is pending** — treat it as a precedent for the *shape* of the fix, not as a benchmarked drop-in.

This maps one-to-one onto §7A.6: RFD4's `rosetta_developability` needs a PyRosetta apptainer via `$PYROSETTA_CONTAINER`. It is the *same dependency BindCraft had*, and FreeBindCraft is the *same reroute* — swap the Rosetta terms for OpenMM/FASPR equivalents, or drop the group. We already run a `binder-developability` skill that computes dSASA, shape complementarity and sequence liabilities without Rosetta, so **we may already own reroute 1.** UNVERIFIED that its outputs line up with `rosetta_developability.sap_score` / `hydrophobic_exposure_sasa`; that is a desk check, not a GPU run.

**Reroute 2 — the folding oracle, where the licence line sits in a different place than I assumed.** AlphaFold2's *parameters* were CC BY-NC 4.0 at the 2021-07-14 release and were **relicensed to CC BY 4.0** from the 2022-12-06 release ([DeepMind commit](https://github.com/google-deepmind/alphafold/commit/8173117130e6df8749ab7349722ec465666df548)), which permits commercial use with attribution; ColabFold's code is MIT and inherits those terms. **AlphaFold3 is CC-BY-NC-SA 4.0** and is the restrictive one. RFD4 defaults its oracle to **AF3** (`configs/metrics/all_metrics.yaml:6`) and ships `af2` and `of3` as alternates.

So the picture in §7A.2 is right about the *default* and too broad about the rest: the reason to avoid upstream's oracle route is **AF3's terms plus the brief's exclusion of the optional `alphafold2/runtime` and `openfold3/runtime` images**, not a blanket licence problem with refolding. The hub already runs its own `af2` and `colabfold` tools. **Whether we could legitimately wire upstream's `af2` oracle to our own ColabFold weights is an open question worth asking, and it is a licence question for Leo, not a technical one.** It is not a blocker, because the Boltz-2 route below avoids it entirely.

### 7B.2 What Boltz-2 gives us, and the one thing it does not

Boltz-2 is MIT and we already ship it, so it is the clean route. Per `tools/boltz2/__init__.py:7-8` it returns, per design, the predicted complex PDB plus **ipTM, pTM, complex_pLDDT, complex_iplddt**, and a hotspot contact count. Legends already exist for `("boltz2", "ipTM")`, `("boltz2", "pTM")`, `("boltz2", "pLDDT")` and `("boltz2", "n_hotspot_contacts")` (`shared/score_legends.py:733-760`), with thresholds `STRICT_IPTM = 0.7` and `SOFT_IPTM = 0.5` (`tools/boltz2/run_pipeline.py:125,127`).

Against upstream's `ppi` pass criterion (§7A.2), that recovers one term and misses two:

| Upstream term | Boltz-2 equivalent | Status |
|---|---|---|
| `oracle_ppi.ptm_coi` min 80.0 | `ipTM` (0–1 here, 0–100 there) | **Covered**, after a scale conversion — the hub already has a pLDDT 0–1 vs 0–100 split to get wrong |
| `oracle_ppi.min_ipae_coi` max 1.5 | — | **Not emitted.** No `pae` key appears anywhere in `tools/boltz2/`. The hub does carry an interface-PAE field (`webhooks/modal.py::_sanitize_candidate`, used by `rfantibody`), so the column exists — Boltz-2 just does not currently fill it |
| `refolded_target_backbone_aligned_binder_rmsd` max 2.5 | — | Needs an alignment step we would have to write; the refolded PDB is returned, so this is arithmetic, not inference |

So the honest claim is: **chaining Boltz-2 gives customers a confidence number and a defensible sort key; it does not reproduce upstream's published bar.** Two of three terms need work, and the pass rates in §3 stay non-reproducible either way.

### 7B.3 The problem that decides whether this works at all

Our own measurement, in our own repo: five byte-identical copies of one input, folded in one Boltz-2 process, **spanned ipTM 0.263–0.830**, with 21 pairwise binder CA RMSDs at median 16.3 Å. No `--seed` is passed anywhere in the package, so the sampler is unseeded (`tools/boltz2/__init__.py:111-115`; full write-up in the 2026-09-23 rows of `docs/VALIDATION-LOG.md`). Worse for this purpose, rung B was a **negative discrimination result**: on three binders the cognate held at ipTM ~0.95 while the worst decoy climbed 0.619 → 0.801, collapsing the margin 37% and inverting two decoys' rank (`:118-124`).

**A scorer that moves 0.26–0.83 on identical input cannot rank designs.** This is the real risk in Leo's instruction, and it is ours, not upstream's. Three ways out, cheapest first:

1. **Seed the sampler.** No `--seed` is passed today. If Boltz-2 accepts one, a fixed seed makes the score reproducible per design — it does not make it *discriminative*, but it stops the same design scoring differently twice. UNVERIFIED that the CLI exposes a seed; one desk check.
2. **Fold each design *n* times and report the median plus the spread.** Honest, and the spread is itself information. Costs *n*× the fold.
3. **Use ESMFold-based `esmfold2-design` instead**, which is deterministic. Cheaper and stabler, but its interface confidence is weaker than a cofolder's.

**Recommendation: (1) then (2) with n = 3, and show the spread on the results page rather than hiding it behind a single number.** A single ipTM from an unseeded sampler would be the most misleading column we have ever shipped.

### 7B.4 Cost and shape

The chain runs as a second Modal call after generation, folding the delivered designs together in one batch, on the existing `boltz2` app — so it reuses the live-charging, streaming and recovery paths already in place and adds no new billing surface.

**What a triple fold costs, from our own measurements.** Boltz-2 runs on **A100-40GB** (`tools/boltz2/modal_app.py:54`), priced at **$0.000714 raw GPU-second** (`shared/wallet_estimates.py:73`) and billed to the customer through **`WALLET_MARKUP = 1.70`** (`shared/wallet.py:71`), i.e. **$0.0012138 per customer GPU-second**. Gate 1 rung A measured a **marginal ~69 s per design** on the `standalone` preset (81.9 s for the first, then 68.5 and 69.5 — the first carries the model load); rung C through the production web flow charged **261 GPU-s for 3 designs**, $0.3168, which is 87 s/design including load (2026-09-19 and 2026-09-23 rows of `docs/VALIDATION-LOG.md`).

| | GPU-s | Customer cost |
|---|---|---|
| One extra fold per design, marginal | 69 | **$0.08** |
| One extra fold per design, incl. amortised load | 87 | **$0.11** |
| **Triple fold per design (n = 3), marginal** | **207** | **$0.25** |
| Eight designs, folded once | ~565 | **$0.69** |
| **Eight designs, folded three times** | ~1,669 | **$2.03** |

The two batch rows assume **one call that loads the model once** and then folds at the marginal rate (81.9 + 23 × 69), which is how rungs A and C ran — 3 designs in a single call. Folding each design in its own call would pay the ~13 s load every time and cost more.

So the honest headline is **about 25 cents per design, roughly $2 on an eight-design run** — the n = 3 spread in §7B.3 costs a couple of dollars, not a reprice.

**Three caveats that could move it, each one a reason rung 3 still has to measure.** (i) The 69 s anchor was measured on a **~244 aa binder against a 107 aa antigen** (the three Gate 1 binders are trastuzumab-framework scFvs, per the 2026-09-21 row of `docs/VALIDATION-LOG.md`); the binder length we propose to offer is 60–120 aa (our `proteina` form default, `tools/proteina/__init__.py:263`, not an upstream default — upstream's own example uses 50–120), and a shorter complex folds faster, so this is more likely an over-estimate than an under-estimate — but it is an extrapolation, **UNVERIFIED for RFD4 output**. (ii) It holds **only on the `standalone` preset**; rung B measured `msa_server` at **214 s/design** against rung C's comparable 87 s/design including load — about **2.5×**, consistent with the validation log's own run-total comparison of ~2.9× (661 vs 229 GPU-s). That would put a triple fold near **$0.78 per design**. (iii) `expected_gpu_seconds = 180.0` for boltz2 (`shared/wallet_estimates.py:423`) over-reserves against the measured 87, so the *hold* a customer sees would be larger than the charge — safe on money, but it is the number they read.

### 7B.5 What this adds to the rung ladder

- **Desk check, $0 — DONE 2026-10-30, all three answered.** (i) **The Boltz-2 CLI does accept `--seed` at our pinned `boltz==2.2.1`** (`tools/boltz2/Dockerfile.modal:64`), **and we never pass it** — the argv built at `tools/boltz2/run_pipeline.py:460-468` has no seed flag. That is a plausible contributor to the ipTM spread at §7B.3, though seeding only makes a fold *reproducible*; the spread across seeds is real sampling uncertainty and does not go away because we fixed one. (ii) **`_sanitize_candidate` accepts a Boltz-2-derived interface PAE without complaint, and that is the problem** — `webhooks/modal.py` stores `"i_pae": _num(cand.get("i_pae"))` where `_num` is `round(float(value), 4)` with **no range gate and no unit check**, so a value on the wrong scale would be stored and rendered as if it were right. (iii) **Our `binder-developability` skill covers the two *sequence* terms of upstream's developability criterion and neither Rosetta term:** GRAVY and instability index are computed at `scripts/dev_analysis.py:125,130`, but `sap_score` has no counterpart, and `hydrophobic_exposure_sasa` is approximated only by a largest-contiguous-apo-patch figure that `scripts/build_report.py:696-697` itself calls "a single-snapshot proxy for SAP-style aggregation metrics". It is also a Claude skill outside this repo, so using it in the product means porting it, not calling it.
- **Licence question for Leo, $0:** is upstream's `af2` oracle route open to us given AF2 params are CC BY 4.0, or does the brief's exclusion of the `alphafold2/runtime` image close it regardless.
- **Rung 3, GPU — NOT DONE, carried to rung 4.** Rung 3 priced RFD4 generation only (§5.7); it folded nothing. Measuring the Boltz-2 fold cost per design, and the ipTM spread over *n* folds of one RFD4 design on RFD4 output rather than the 2026-09-23 inputs, is still unspent work and still the thing that decides whether the triple fold is affordable.

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

*Status 2026-10-30: rungs 1, 2 and 3 are all complete. Rung 1 ($0) proved the `cu13` image builds (§5.4). The driver check (~3¢) crash-looped on its first attempt and **PASSED** on the re-run (§5.5). Rung 2 (~$0.62) seeded the weights volume and **PASSED** (§5.6). Leo approved rung 3 (~$1.85) and it **PASSED** on the second attempt, for $0.2908 of the $1.85 — real cost, real streaming behaviour and the real metrics header are now measured (§5.7). The next spend is rung 4 (~$1.85), which has NOT been approved.*

1. ~~**Approve rung 1 ($0)?**~~ **APPROVED by Leo 2026-10-30.** CPU-only image build probe; no GPU, no spend. Leo also directed that whatever configuration it proves must be **pinned** — §5.3 is the list.
2. ~~**Approve rungs 2–3 (≈$2.50 together)?**~~ **Both APPROVED and DONE 2026-10-30 (§5.6, §5.7).** Rung 3 cost a measured **$0.2908** of its ~$1.85; rung 2's exact spend was never captured and is only bounded below $0.62 (§5.6), so the pair came in under budget but the total is not a measured figure. Rung 3 answered all three of its questions: ~$0.28 customer for eight designs, designs stream one at a time, and the metrics CSV carries no score column at all. The one thing it did not settle is design *quality* — see §5.7's glutamate caveat, which rung 4 must address before any head-to-head claim.
3. **Approve rung 4 (≈$1.85)? — this is now the open spend decision.** Head-to-head against a recorded `proteina` run, on our own target. It is no longer only a comparison: rung 3's binder came out 70% glutamate under the `quickstart` preset (§5.7), so rung 4 is also the first run that would tell us whether a production configuration produces credible sequences. Recommended.
4. **Rung 5 (≈$5.55)?** Defer until after the go/no-go.

**Product decisions:**

5. ~~**No confidence number on the results page.**~~ **DECIDED by Leo 2026-10-30: use our own scoring, chained through Boltz-2 refolding.** §7B is the design. It raises one new question that is genuinely Leo's: our own Boltz-2 measurement spans **ipTM 0.263–0.830 on byte-identical input** (§7B.3), so an unseeded single fold cannot rank anything either. I recommend seeding, then folding each design three times and showing the spread. That multiplies the scoring cost by three — confirm you want that before rung 3 prices it.
9. **Is upstream's `af2` oracle route open to us?** AF2 parameters have been CC BY 4.0 since the 2022-12-06 release, so the restrictive one is AF3, not AF2 (§7B.1). The brief excluded the optional `alphafold2/runtime` image on separate terms, which may or may not close the question. $0 to answer, and it would recover the `min_ipae_coi` term Boltz-2 does not give us.
6. **Start minimum.** $1.00 proposed, and rung 3 has now supplied the missing input: designs stream one at a time from 68.1 s, so $1.00 covers the pre-first-design stretch roughly eight times over (§7.1). Confirm the figure — with the caveat that a production run pays a `torch.compile` warm-up `quickstart` skipped, so rung 4 should re-check the first-design time before it is locked.
7. **Positioning.** Three binder tools on the shelf. What does a customer choose between them, in one sentence? This blocks step 7, not the engineering.
8. **Alpha software, four weeks old, 7 open issues.** Are we comfortable selling it? My read: yes, behind a flag and after rung 4, because the licence is clean and the downside is a refund, not a liability. Leo's call.

---

## 10. UNKNOWNs and UNVERIFIEDs that must close before step 2

1. ~~**Does Modal's A100-80GB present a CUDA-13-capable driver?**~~ **ANSWERED 2026-10-30, yes.** Driver 610.57.04 on an A100-SXM4-80GB, sm_80, a real kernel launch, `rfd4-generate --help` exit 0. Cost ~3¢ (§5.5).
2. ~~**Real wall-clock and cost of an 8-design run on our hardware.**~~ **ANSWERED 2026-10-30: 159.2 s, ~$0.28 customer, ≈ `65 + 12.3 × N` seconds (§5.7).** Measured eager, with no `torch.compile` warm-up and no batching, so the per-design figure is an upper bound and the startup a floor.
3. ~~**Do structure files land per design or only at run end?**~~ **ANSWERED 2026-10-30: per design**, one every ~12 s from 68.1 s onward (§5.7). Save-as-you-go is feasible. The watcher must skip the hidden `.generated-cif-*` temp directory each design is written through.
4. **The real `per_example_metrics.csv` header.** **PARTLY ANSWERED 2026-10-30 (§5.7):** observed for the no-metrics configuration we intend to ship — 44 columns, none of them a score. Still UNKNOWN for a run with `metrics_tags` set, which rung 3 did not exercise.
5. **PDB→CIF conversion and `label_seq_id` numbering.** Upstream resolves residue ids against mmCIF `label_seq_id`, not author numbering. Our whole stack speaks author numbering. **This is the most likely source of a silent wrong-residue bug** — the same class as the `proteina` hotspot defect already on record. Needs a deliberate test, not a code read.
6. **Hotspot atom-set expansion.** Upstream hotspots are atom-level; our picker emits residues. Which atoms we choose is a scientific decision nobody has made.
7. ~~**Real weights-volume size.**~~ **ANSWERED 2026-10-30: 2.59 GiB** (2,780,839,974 B), both digests verified against upstream's manifest (§5.6). The old ~1.7 GB figure was a single-file number; the volume must hold the matched flow/autoencoder pair.
8. **VRAM at the top of the size envelope.** Only "≥40 GB recommended" upstream, with no curve. Rung 5.
9. **Does `delete_failing_pre_oracle` race our uploader?** **NOT APPLICABLE to our configuration.** Rung 3 composed no metrics group, no oracle ran, and all eight designs persisted (§5.7). It would only matter if we later enabled an oracle, which the licence exclusion rules out.
10. **Cyclic peptide support.** A preset exists; upstream issue #62 asks whether it works. Out of scope for release 1 either way.
11. **Upstream binder-mode bugs.** #81 (high alanine fraction on the non-interacting face in binder mode) is open and is a *quality* bug in exactly our release-1 mode. Watch it; it may shape the copy we write about expected quality.
12. **Does the authors' benchmark hold on the current weights?** Upstream says it has not been re-measured. Rung 4 is our own check, on one target.
13. **Does `sequence_developability` also need the PyRosetta apptainer?** UNVERIFIED — only `rosetta_developability` is documented as requiring `$PYROSETTA_CONTAINER` (§7A.6), and the class was not read. Blocking for any developability column, not for release 1.
14. **Which confidence predictor and which sort key?** We must supply our own (§7A.5). `boltz2` ipTM is on record as not reproducible run-to-run, so the display number and the sort key may have to differ. A decision, not a measurement.
15. **Which accelerator variant we actually shipped.** Settled: `cu13`, now proven on a GPU as well as in a build (§5.5). The Dockerfile must record that variant, and must not silently fall back to cu12, which is a different product (§5.4).
16. **Do `CCD_MIRROR_PATH` and `PDB_MIRROR_PATH` need to be set?** **ANSWERED for release 1, no.** Both were unset for rung 3's run and it completed with 8/8 designs (§5.7). Still unknown for the ligand and modified-residue modalities, which release 1 does not ship.

---

## 11. What this pass did not do

No product code was changed. No Modal app was deployed. No customer-visible string was edited. GPU work was limited to the validation ladder Leo approved rung by rung: the free CPU build probe (§5.4), the ~3¢ driver check (§5.5), rung 2's weights seed (§5.6, bounded below $0.62, exact spend not captured) and rung 3's run (§5.7, a measured **$0.2908**). A Modal Volume named `rfd4-proteina-weights` holding 2.59 GiB now exists in Leo's account; **delete it if RFD4 is dropped**, and note its flow checkpoint was renamed to `rfd4_proteina_flow-ema.ckpt` during rung 3.

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
