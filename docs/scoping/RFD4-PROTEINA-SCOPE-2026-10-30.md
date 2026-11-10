# RFD4-Proteina — research and scoping pass

Date: 2026-10-30. Author: hub worker session (research only).
Scope of this document: research and scoping, plus the sections later passes have appended. **The 2026-10-30 research pass changed no product code and deployed no Modal app**; later passes recorded in §10A do add code (`tools/rfd4/numbering.py`) and have themselves deployed nothing. "Deploys nothing" is a statement about these passes, NOT about the merge: merging the PR redeploys all ten GPU Modal apps (nine when this line was written; #505 added rfd4's row), so the merge is Leo's to make after an idle check, not a worker session's. GPU work was limited to the validation ladder Leo approved one rung at a time, which has now spent a measured **$0.2908** through rung 3, plus rung 2's uncaptured seeding bounded under $0.62 and rung 4's one GPU run whose billing read has **not yet landed** (estimated ~$0.22 raw, UNVERIFIED) (§§5.4–5.8, §11).

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
| Target chain(s) | ~~multi-chain allowed~~ **ONE chain. Corrected 2026-11-04 by step 3.** | The proposal was wrong for this tool. The landed container refuses a multi-chain target outright (`tools/rfd4/run_pipeline.py::resolve_target_chain`) because a multi-chain rigid target is **not expressible through the YAML spec format we invoke upstream with**. Of the six variants run on free CPU — tabulated in `docs/scoping/RFD4-RUNG4-RESUME-2026-11-02.md`, **not** here in §5.8, which an earlier draft of this row cited — **five name two target chains**; V4 names chain A alone and is the one-chain control, so it is evidence the harness works rather than evidence about either rule. Of the two-chain five, **V1 and V5 carry a committed `index_group_molecule_must_be_connected` traceback** — V1's quoted in that document, V5's in `rung4-probe/variants_result_v5v7.json` — while V2 returned rc 1 with no CIF and had its result JSON overwritten, so reading V2 as the same invariant failure is inference from the exit code and not from a trace. V3/V7 returned 0 and built a CIF — and those two are exactly the ones carrying the second chain **unindexed** (V3 on both chains, V7 on B only), which is what makes the measured rule **at most one *indexed* target chain** rather than one chain. An unindexed chain is not a conditioned target, and `C_IDXGRP` is Python-API only. Still UNVERIFIED: the mechanism, and the V2-V4 rows, whose result JSON the V5/V7 sweep overwrote. `tools/rfd4/__init__.py::validate` refuses more than one chain **named on the form** (`len(target_chains) > 1`) one layer earlier, and `_RFD4` in `shared/pdb_preflight_rules.py` carries both multi-chain flags False. Neither refuses a *file* that holds extra chains while naming one — `validate` never opens the upload and `_multi_chain_block` counts the chains named, not the chains present. That gap WAS §9 item 11; it is CLOSED 2026-11-10 -- the container crops a PDB upload to the named chain rather than refusing the file, and the gates are deliberately unchanged, so a multi-chain *file* naming one chain now runs. Pinned by `tests/test_rfd4_adapter.py::test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops`. **Unknown, not absent:** the exit condition is one two-chain run upstream accepts |
| Hotspots | ~~optional~~ **REQUIRED, max 64. Corrected 2026-11-04 by step 3.** | Release 1 runs upstream's hotspot-conditioned configuration, which has nothing to condition on without them, and the container `_fail`s an empty token list (`tools/rfd4/run_pipeline.py::build_hotspot_selections`). `hotspots_required=True` on `_RFD4`; `tools/base.py::parse_hotspot_residues` already refuses an empty field. The 64 ceiling is the container's own `MAX_HOTSPOTS`, which matches `tools/proteina/__init__.py:224` |
| Binder length | range, default `[60, 120]`, allowed 20–300 | matches `tools/proteina/__init__.py:263-265`; upstream's own example uses `50-120` |
| Number of designs | default 8 | matches `designs_per_run_baseline=8`, `shared/wallet_estimates.py:525` |
| Thinking level | **release 1: fixed at `medium`, not exposed** | three levels exist upstream; exposing a 3× cost knob before we have measured any of them is how a tool over-quotes |

### 4.3 Proposed preflight envelope — runtime now **measured**, the size and VRAM numbers still **provisional**

Model it on `_PROTEINA` (`shared/pdb_preflight_rules.py:844`) and replace each number the moment we measure:

- `min_target_aa` 30 — copy from `_PROTEINA`.
- `hard_cap_target_aa` — **start at 400, not 500.** `_PROTEINA`'s 500 is a policy number anchored on a measured VRAM curve up to 415 aa; RFD4 has no such curve here, and its own `compile_lengths` bucket list tops out at 1024 tokens with a BioIR CUDA-graph limit of 511 (`configs/inference.yaml:19,51`) — which is **INFERRED** to mean longer inputs run uncompiled and slower, not that they are refused. Tighten on measurement, loosen later.
- `hard_cap_combined_aa` — 520 (400 target + 120 binder), same logic.
- `runtime_base_min` / `runtime_alpha` — **rung 3 measured the runtime, but the measurement does not fit the field that prices a job, and this is the single most important implementation note in this document.** The fit is affine: **`≈ 65 + 12.3 × N` seconds**, from 159.2 s over eight designs, with two independent estimates of the marginal term agreeing to 4%. `ToolSpec.expected_gpu_seconds` (`shared/wallet_estimates.py`, the `ToolSpec` dataclass) is one scalar, and the only scaling applied to it is **purely proportional with no intercept** — `_scale_seconds` (same file; cited by symbol because #482's own insertion into `TOOL_SPECS` moves every line below it) returns `base_seconds * max(actual / designs_per_run_baseline, 1.0)`. A 65 s fixed term, 41% of the measured run, **has no field to live in**, and both ways of forcing it in are wrong in opposite money directions:

  - Register a default-size total (`designs_per_run_baseline=8`) and N=100 over-quotes. **TAKEN, 2026-11-03** (#482), at the fit's own value at N=8 rather than the measured total: `expected_gpu_seconds=163.4`, so N=100 quotes 163.4 × 12.5 = 2042.5 s = **$3.57** against this document's own $2.26 — a 58% over-quote, which `cushioned_hold_usd` then reserves 1.5× of. (The measured 159.2 s would quote 1990 s = $3.48, a 54% over-quote; the direction and the conclusion are the same either way.)
  - Register the marginal-only 98.4 s so large N lands, and N=8 — the default — quotes **$0.172** against the $0.278 a run actually consumes. Under live charging the customer is debited actual, so every default-size run bills ~62% above its quote.

  The field that *does* carry an intercept is the preflight `SizeEnvelope`'s `runtime_fixed_min` (`shared/pdb_preflight_rules.py:1068-1082`), which is advisory copy and **not** what prices a job. **Decide which of the two registrations to take, and record the resulting error, before `rfd4` goes in `TOOL_SPECS`.** Separately, `runtime_alpha` is still unmeasured: it is a required exponent (`pdb_preflight_rules.py:1068`), one 115 aa target cannot set it, and `proteina`'s must not be copied.
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

1. **The filenames differ between HF and the config.** HF stores stable names (`flow_checkpoints/rfd4_proteina_flow-ema.ckpt`); `configs/inference.yaml:38,46` reads dated ones (`rfd4-proteina-flow-release-2026-10-19.ckpt`). **Do not rename the flow checkpoint on the way in — rung 3 proved this caveat wrong for that file** (§5.7): `rfproteina/utils/weights.py:20` dispatches on the `-ema.ckpt` suffix, so the seeded name must keep it and the invocation must carry a matching `ckpt_name=` override instead. The autoencoder is unaffected and may take the dated name. The mapping and the digests are in the repo's own `release-checkpoints.json`.
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

**Cost: ~$0.28 customer for eight designs — ten times what the benchmark implied, and a third of the comparable `proteina` shard.** §6 called the benchmark's ~19 s of generation ("about three cents") obviously wrong because a real run is dominated by fixed overhead, and predicted RFD4's cost would be set by startup rather than sampling. Both hold: the measured $0.28 is an order of magnitude *above* the benchmark figure, and still below `proteina`'s 553–1343 s shards. Decomposing the eight per-design file timestamps:

| | Measured |
|---|---|
| Total wall, 8 designs | 159.2 s → **$0.2782 customer** |
| First design visible at | 68.1 s |
| Marginal, within-run (154.1 − 68.1)/7 | **12.29 s/design** |
| Marginal, between-run (159.2 − 76.4)/7 | **11.83 s/design** |
| Fixed overhead, by subtraction | **~61–65 s** |

Two independent estimates of the marginal cost agree to 4%. A working model for `expected_gpu_seconds` is **`65 + 12.3 × N` seconds**, which gives 163.4 s against the measured 159.2 s — **2.6% high**, and deliberately so, since over-reserving is the safe direction on money. The 61 s end of the fixed-overhead range would reproduce it to 0.1%. At N=8 that is $0.28 customer; at N=100, $2.26 — but 100 replicates is a **12× extrapolation beyond anything measured**, and the per-design time is an average over a random 50–120 aa binder length draw (the example's contig), not a fixed-length figure. One run, no variance estimate.

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

### 5.8 Rung 4 RESULT — the production configuration fixes the sequences, 2026-11-03

**Leo approved ~$1.85 for this one run on 2026-11-03. One GPU function ran; the rung PASSED.** App `ap-19HVd6i5HKpXr5tke3hs4E`, `returncode 0`, no timeout, 217.4 s wall, 8/8 designs plus both metrics CSVs. Probe `docs/scoping/rung4-probe/rung4_app.py`, result artefact `docs/scoping/rung4-probe/gpu_result_20261103T114437.json` (committed — the local client is the only thing that writes it). The app is **stopped**, read from `modal app list` at 2026-11-03 17:01 UTC (`ap-19HVd6i5HKpXr5tke3hs4E` and the pre-flight `ap-nBkWyfPz7zJbLE0xAmIUsN`, both `stopped`, 0 containers). That read is a session observation; no artefact of it is committed.

**The target changed, on Leo's instruction, and that is the single most important thing to know when reading this section.** Rung 4 was scoped against our own Fc target. Leo withdrew question 2 (the `proteina` head-to-head) on 2026-11-03 — *"we dont need the head to head comparison"* — and then redirected the run: *"switch to the same as rung 3, so we see if the settings give us a protein without poly Glu"*. So rung 4 ran **rung 3's exact input**, `/opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml`, with the configuration as the only variable. The full decision record is in `docs/scoping/RFD4-RUNG4-RESUME-2026-11-02.md`. **No comparison with our existing `proteina` tool was run and none may be inferred from this section.** The Fc hotspot-numbering readback was banked separately on a free CPU probe (`docs/scoping/rung4-probe/cpu_result_20261103T103209.json` — the `112936` file is the PD-L1 probe, not the Fc one) and cost nothing. It is re-derivable by running `docs/scoping/rung4-probe/check_hotspot_readback.py`, which asserts the flagged set matches the intended one by number and by residue identity.

**The invocation that ran:**

```
rfd4-generate inference_experiment=inference_on_user_inputs hf_ckpt_path=null \
  ckpt_name=rfd4_proteina_flow-ema.ckpt \
  inputs=/opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml \
  num_replicates=8 metrics_tags=[design,interchain_interactions,hotspot_conditioning] \
  delete_failing_pre_oracle=False out_dir=/tmp/rfd4_run
```

**Three** deltas from rung 3, and nothing else: `inference_experiment=quickstart` → `inference_on_user_inputs`, `metrics_tags` set, and `delete_failing_pre_oracle` set to `False`, a change of value — the default is `True` (`configs/inference.yaml:44` — an **upstream** file, not in this repository; read first-hand and recorded at §5.1 and line 693 of this document, and the `cfg_dump` in the probe artefacts shows only the overridden value, so it is not second evidence for the default), not a value this override merely spells out. The §5.3 pins are unchanged and remain the configuration of record; the variant that ran is cu13 on A100-80GB with the renamed `-ema.ckpt` flow checkpoint, as at rung 3.

#### Question 1 — sequence credibility. **The poly-glutamate is gone.**

Composition computed from the CIFs with `gemmi`, selecting the designed chain by **sequence identity** against the revealed 115 aa target (the target chain is byte-identical in all eight files); a length threshold is not safe here, because `pdl1_binder_4`'s designed chain is 116 aa and would be mistaken for the target by any length rule.

| Design | Designed length | Glu | Glu fraction | Most common residue | Distinct residues |
|---|---|---|---|---|---|
| `pdl1_binder_0` | 108 | 3 | **2.8%** | S 20% | 19 |
| `pdl1_binder_1` | 101 | 3 | **3.0%** | T 12% | 18 |
| `pdl1_binder_2` | 75 | 9 | **12.0%** | L 17% | 16 |
| `pdl1_binder_3` | 109 | 3 | **2.8%** | T 17% | 19 |
| `pdl1_binder_4` | 116 | 12 | **10.3%** | L 12% | 19 |
| `pdl1_binder_5` | 58 | 5 | **8.6%** | L 22% | 16 |
| `pdl1_binder_6` | 68 | 7 | **10.3%** | S 13% | 15 |
| `pdl1_binder_7` | 99 | 1 | **1.0%** | S 28% | 18 |
| | | | **min 1.0%, max 12.0%, mean 6.3%** | | |
| **Rung 3, same input, `quickstart`** | 67 | 47 | **70.1%** | E 70% | — |

**Answered plainly: yes, this is a credible binder set, and rung 3's was not.** Glutamate runs **1.0% to 12.0% with a mean of 6.3%** — four designs at or below 3.0% (1.0, 2.8, 2.8, 3.0) and four between 8.6% and 12.0% (8.6, 10.3, 10.3, 12.0) — against **70.1% on rung 3's single design from the same input**. The comparison that carries the verdict is that in-document one, not a proteome background frequency: no amino-acid background distribution is recorded anywhere in this repository, so any such comparison here would be unverifiable. No design is dominated by any single residue (the highest single-residue fraction anywhere is 28%); every design uses 15–19 of the 20 standard amino acids. The designed lengths spread 58–116 aa across the example's 50–120 contig, so the length draw is working too.

A cross-check from an independent source agrees: `amino_acid_composition.n_standard_residues` in the metrics CSV reads 173, 183, 190, 214, 216, 223, 224, 231, which is exactly each designed length plus the 115 aa target. The structural quality columns are clean across all eight — `clash_metrics.frac_atoms_clashing_1p5A` 0.0, `bond_length_metrics.bond_length.frac_unreasonable` 0.0, `amino_acid_composition.n_generated_unknown_residues` 0.0, `sequence_adherence.sequence_adherence` 1.0 with 0 violations — and `sse_distribution` splits the designs into two families of **four and four**, and no design mixes the two. Helical, with `sheet_fraction_coi` 0.0: design 2 at `helix_fraction_coi` 0.84, design 4 at 0.8793, design 5 at 0.8966, design 6 at 0.7206. Sheet, with `helix_fraction_coi` 0.0: design 0 at `sheet_fraction_coi` 0.4074, design 1 at 0.3861, design 3 at 0.3394, design 7 at 0.5152.

**This narrows §10 item 11, and it eliminates one of §5.7's three candidate explanations.** `generation.args.inverse_folding` reads `0.0` in this run's manifest — **identical to rung 3** — so that hyperparameter cannot be what caused rung 3's 70% glutamate. What differed was the experiment preset, which is also what changed the `torch.compile` behaviour below. Upstream issue #81 (high alanine fraction on the non-interacting face) is a separate claim and is **not** addressed here: `amino_acid_composition.ala_fraction` averages 0.0692 over the eight designs, which is unremarkable, but that is a whole-chain figure and #81 is about one face, which we did not compute.

**One caveat that must travel with this result.** This is sequence *composition* and upstream's own geometric checks, not a binding result. Nothing here says these eight designs bind PD-L1. §7A.1 and §7B still hold in full: the CSV contains no score, and whether any of these is a binder is a question only our own Boltz-2 chain can begin to answer.

#### Question 3 — first-design wall-clock, and where the warm-up is paid

| | Rung 3 (`quickstart`, eager) | Rung 4 (`inference_on_user_inputs`) |
|---|---|---|
| Total wall, 8 designs | 159.2 s | **217.4 s** |
| First design visible at | 68.1 s | **128.2 s** |
| Designs after the first | one every ~12.3 s | **4.0–8.0 s, or 17.5–29.1 s — see below** |
| Tail after last design | 5.1 s | **21.1 s** |

First-sighting times from the probe's watcher, which polls every 0.5 s (`rung4-probe/rung4_app.py:268`, `while not stop.wait(0.5)`): 128.2, 128.2, 145.7, 153.7, 182.8, 186.8, 190.8, 196.3 s. The two at 128.2 s are a genuine same-second pair, not a rounding artefact.

**The warm-up is paid per padded-length bucket, not once per run, and it is not cached between runs.** The log records three compile events — `rfproteina.utils.torch_compile` "Compiling model for padded length N tokens (batch size 1)" at log-relative t+18.7 s (N=224), t+57.3 s (N=192) and t+89.5 s (N=256) — and one further line: *"This shape may need recompiling in later runs; set `TORCH_COMPILE_CACHE_BLOB` to a persistent file path to preserve the cache."* That variable was **not** set, so every run pays its compiles again.

The bucket mapping is consistent and explains the whole cadence. Each design's total token count is `n_standard_residues`; the smallest bucket at or above it is the one it needs:

| Bucket | Designs | First needed by |
|---|---|---|
| 224 | `_0` (223), `_1` (216), `_3` (224), `_7` (214) | `_0` — compile #1 |
| 192 | `_2` (190), `_5` (173), `_6` (183) | `_2` — compile #2 |
| 256 | `_4` (231) | `_4` — compile #3 |

Three distinct buckets, three compile events, in exactly the order the designs first needed them. And the sighting gaps separate cleanly into two populations: a design that reuses an already-compiled bucket lands **0.0, 8.0, 4.0, 4.0 or 5.5 s** after the previous one, while the two designs that forced a new compile — `_2` and `_4` — land **17.5 s** and **29.1 s** after theirs. By subtraction a compile costs roughly **13–25 s** and marginal sampling under a compiled model is roughly **4–8 s/design**, against rung 3's eager **12.29 s/design**.

**So the preset's own promise holds: `torch.compile` pays for itself, and §5.7's warning that 12.3 s/design was an upper bound is confirmed.** A working model, with B = number of distinct padded-length buckets:

> `gpu_seconds ≈ 128 + 4.3 × (N − 1) + 20 × (B − 1) + 21`

At N=8, B=3 that gives **219.1 s against the measured 217.4 s — 0.8% high**, and high is the safe direction on money. **One run, three buckets, no variance estimate**; the 128 s constant itself contains the first compile, so it is not separable from startup on this data.

**Verdict on the $1.00 start minimum: it still covers the pre-first-design stretch, with the headroom roughly halved.** At the $0.0017476/customer-GPU-second rate (§6), 128.2 s to the first design is **$0.2240 of customer money**, so a $1.00 start minimum has **4.46× headroom** — down from rung 3's 68.1 s / $0.1190 / **8.40×**. §7.1 records the $1.00 figure as a Leo decision, and it survives this measurement. **Leo confirmed the $1.00 on 2026-11-04 (§9 item 6); step 4 carries it in PR #487, open and not merged.**

**But the headroom is no longer a fixed quantity, and that is the finding to carry into §7.1.** Because compiles scale with the *spread* of binder lengths rather than with N, a customer run whose length range covers more buckets pays more before its first design. The arithmetic is still comfortable — after the measured $0.2240, a $1.00 minimum leaves $0.7760, which is 444 further GPU-seconds, or room for roughly **18 to 34** more compiles at the 13–25 s each measured here — so **no change to the $1.00 figure is needed**. Two cheap follow-ups are worth noting and neither is a blocker: setting `TORCH_COMPILE_CACHE_BLOB` to a path on the weights volume would remove the compile cost from every run after the first, and clamping the form's binder-length range to fewer buckets would reduce it directly. **Both are unmeasured proposals, not results.**

#### Question 4 — the real header with `metrics_tags` set. **§10 item 4 is now CLOSED.**

**With `metrics_tags=[design,interchain_interactions,hotspot_conditioning]` the file has 89 columns and 8 rows** — against 44 columns for rung 3's no-metrics run. **The net +45 is 46 columns added and one lost.** Rung 4's header carries all 11 of rung 3's named columns but only **32** `generation.*` columns against the 33 §5.7 recorded, so exactly one `generation.*` column present at rung 3 is absent here; **which one cannot be recovered, because §5.7 recorded only the count and no rung-3 header artefact is committed.** The 46 added columns are:

| Group | Columns | Added |
|---|---|---|
| `amino_acid_composition.*` | 5 | `n_generated_unknown_residues`, `n_standard_residues`, `ala_fraction`, `val_fraction`, `gly_fraction` |
| `bond_length_metrics.*` | 5 | `frac_unreasonable` plus 4 breakdowns |
| `clash_metrics.*` | 6 | `frac_atoms_clashing_1p5A` plus 5 breakdowns |
| `hotspot_obedience.*` | 1 | `hotspot_obedience` |
| `interchain_interactions.*` | 12 | hbond / pistack / pication / saltbridge / hydrophobic / metal_coord, each as a count and a pair-count, all `_coi` |
| `sequence_adherence.*` | 2 | `sequence_adherence`, `n_violations` |
| `sequence_probability_adherence.*` | 6 | zero/one adherence, condition and violation counts |
| `sse_distribution.*` | 8 | helix / sheet / loop fraction and element count, whole-structure and `_coi` |
| `(flat)` | 1 | `pre_oracle_pass` |

**Still not one refold, confidence or ipTM column, and that is the point.** The tag-gated set contains no `refolding_oracles.*` and no `pass_criteria.*` — the free CPU pre-flight predicted exactly this and the paid run confirms it. **So §7B stands unchanged: every score on our results page must come from our own Boltz-2 chain.** `aggregated_metrics.csv` is a 5-column long-format roll-up (`dataset,metric,reduction,value,n`) of the same metrics with `reduction=mean, n=8`; it adds no new quantity.

Three smaller readings, each narrow:

1. **`hotspot_obedience` is populated and non-trivial**: 1.0 on two designs, 0.8333 on five, 0.6667 on one. The values quantise in sixths against three conditioned hotspot residues, so the denominator is 6 and **I have not identified what the six units are** — do not read these as "5 of 6 hotspots hit" without reading the metric's implementation.
2. **`pre_oracle_pass` is 1.0 on all eight**, and `sequence_adherence` 1.0 with 0 violations. The pre-flight showed `pre_oracle_filters.sequence_adherence_filter` armed, so `delete_failing_pre_oracle=False` was a live protection — but **no design failed, so the flag was never exercised**. §10 item 9 stays "not applicable to our configuration" on stronger ground than before: the filter now genuinely runs, and still deleted nothing.
3. **One seed per run, shared by all replicates.** `seed` reads `575229289.0` on all eight rows — same shape as rung 3, and the same float-serialisation caveat applies: read it as an integer, never round-trip it through the CSV.

#### Question 2 — withdrawn

Leo withdrew the `proteina` head-to-head on 2026-11-03, on the grounds that customers choose between tools and we do not need to. **No comparator run was made, no comparator number appears in this section, and §9 q3 should be read as closed by that decision rather than by evidence.**

#### Spend

| | App | Wall | Cost | Source |
|---|---|---|---|---|
| Free pre-flight (CPU) | `ap-nBkWyfPz7zJbLE0xAmIUsN` | ~70 s | **$0.00475879** | billing report, itemised read 2026-11-03 (uncommitted CLI output) |
| Local-guard abort, 0 containers | `ap-EsMINGnFqAI1Hx9kqmOonA` | — | $0, nothing started | app list: stopped the same minute it was created |
| **Paid run (A100-80GB)** | `ap-19HVd6i5HKpXr5tke3hs4E` | **217.4 s** | **$0.18659948** | billing report, itemised read 2026-11-03 (uncommitted CLI output) |
| Earlier CPU probe, same app name | `ap-URDXzh8fvVRksAPNWLjbcC` | ~100 s | **$0.00466825** | billing report, read 2026-11-03 |
| Earlier CPU probe, same app name | `ap-TiIWZoy6v2LJU4r1OXU5dN` | ~52 min | **$0E-8** | billing report, read 2026-11-03 |

**The paid run's exact cost was not captured in the session that ran it; it has since been read.** `modal billing report` was read **seven times** between the run and 17:01 UTC — about **16 minutes** after the run finished, taking the finish from the result artefact's own clock (`gpu_result_20261103T114437.json`, written 11:44:37 local; the session's `date` printed `12:06:35 EST` = `17:06:35 UTC`, so local is UTC−5 and the finish was ~16:45 UTC). The app-stop time itself is **not recorded in any committed artefact**; an earlier version of this paragraph read "1 h 17 min after the app stopped", which the 16:4x finish contradicts. The reads included `--resolution h` and the explicit window `--start 2026-11-03T16:00 --end 2026-11-03T17:00`, the complete hour containing the run. On every read the GPU app and the 11:28 pre-flight were **absent from the table**; the explicit-hour query returned an empty table. A **ninth** read at 17:20 UTC — about **35 minutes** after the run — repeated the empty explicit-hour result and, read at **day** resolution, listed only the two CPU probe apps (`ap-URDXzh8fvVRksAPNWLjbcC` at $0.00466825 and `ap-TiIWZoy6v2LJU4r1OXU5dN` at $0) plus `ranomics-proteina-prod`: the GPU app is absent from the **day** total as well, not merely from the hourly breakdown. So the lag is at least ~35 minutes past the run at every resolution the CLI offers, and it is not something further polling in the session that ran rung 4 could resolve; a later session read it the same day, below. **None of the per-app billing figures in this table are committed artefacts; they are readings of uncommitted CLI output.** **FILLED 2026-11-03 from a billing read, later the same day: $0.18659948.** The itemised report (`modal billing report --for today -r h --tz local --json`) lists `ap-19HVd6i5HKpXr5tke3hs4E`, `ranomics-rfd4-rung4-probe`, 11:00 local hour, **$0.18659948** - **10% of the $1.85 approved**, so the approval was never near being exceeded. Two qualifiers, because neither is obvious and both change what the figure is worth. **(i) The attribution is by app id; what is unverified is the evidence class, not the identity.** The session that fired rung 4 *did* record the app id - `ap-19HVd6i5HKpXr5tke3hs4E`, read from `modal app list` and written into the PASS paragraph above and the table above - and the billing report lists that same id, so identity rests on an **id match**, not on magnitude or hour. The other three `rfd4-rung4-probe` lines ($0, $0.00466825, $0.00475879) corroborate by magnitude but are not what carries the identification. What remains unverified is that **neither end is a committed artefact**: both the `modal app list` read and the billing read are uncommitted CLI output, as this table's own note says. An earlier version of this paragraph called the attribution inferential and said the id had not been recorded - that was **wrong**, and the PASS paragraph and the table above contradict it. **(ii) Modal billing lines settle upward**, so a figure read soon after a run is a lower bound: the rung-5 app's line rose $0.00487 between a read shortly after its run and one about an hour later (§7C.10). This $0.18659948 was read hours after the run and read again later unchanged - **two reads of this line**, against **three of the rung-5 line** - so both appear to have stopped moving, but "appears settled" is what is claimed, not "final". For context, the earlier arithmetic estimate at the raw A100-80GB constant ($0.001028/GPU-second × 217.4 s) was ~$0.22, and the measured line is **below** it; §7C.10 works that gap and concludes only that the constant is too high for this class.

Both apps that could have carried GPU time ran under the one approval. **No retry was made and none is requested here.** The pre-flight functions carry no `gpu=` and no `volumes=`, which is what kept them off GPU time - but the itemised read prices the rung-4 pre-flight at **$0.00475879** of CPU and container time, so "free" was true of GPU seconds only.

#### What rung 4 does not settle

- **VRAM was not captured** — §10 item 8 is untouched, still rung 5.
- **`batch_size` was left at the preset default**, so the eight designs still ran one at a time. Batching remains unmeasured.
- **Nothing was run on our own Fc target on a GPU.** The hotspot-numbering readback for it is closed on CPU; its runtime, its sequences and its VRAM are not measured. §4.3's size envelope is still anchored on a 115 aa target.
- **No binding evidence of any kind.** Eight plausible sequences with clean geometry is not eight binders.


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

**Rung 3 settled it (§5.7): ~61–65 s of fixed startup plus ~12.3 s per design, so eight designs billed ~$0.28 customer — about a third of the comparable `proteina` protein-binder shard, which measured ~553 s for the same eight designs and charged ~$0.97 (`shared/wallet_estimates.py:501-506`), and still startup-dominated.**

### 6.1 Validation ladder — cheapest first

**Every rung is a proposal. Leo approves GPU spend per job. Nothing here has been launched.** Dollar figures are our raw A100-80GB cost, not customer price.

| Rung | What it answers | GPU | Cost | Notes |
|---|---|---|---|---|
| **1. CPU build probe** | Does the image build, does `rfd4-generate --help` run, and **which accelerator variant do we need?** | none | **$0** | Build `cu13` on a CPU builder with weights excluded; a `RUN` probe at build time is free. **DONE 2026-10-30, $0: `cu13` builds and imports (§5.4).** It does NOT establish the host driver — a CPU builder has none. Fold that check into rung 2. |
| **2. Volume seed + GPU import smoke** | Do the weights land, how big is the volume really, does CUDA initialise on our A100? | 1 × A100-80GB, ~10 min | **~$0.62** | Mirrors `proteina`'s seeding step. **DONE 2026-10-30 (§5.6): volume is 2.59 GiB, both digests match the manifest, both checkpoints materialise on an A100-80GB.** Exact seconds billed unverified; bounded below the budget. |
| **3. One 8-design binder run** | **The cost question.** Real wall-clock, real output tree, real per-design file timing | 1 × A100-80GB, ≤30 min | **~$1.85 approved; $0.2908 spent** | **DONE 2026-10-30 (§5.7): 159.2 s for eight designs = ~$0.28 customer; designs land one every ~12 s; the metrics CSV has 44 columns and no score.** One failed attempt and one artefact-recapture run are inside that $0.2908. |
| **4. Our own target, like-for-like** | Does it work on a customer-shaped input, and how does it compare to our `proteina`? | 1 × A100-80GB, ≤30 min | **~$1.85** | The same target and hotspots as a recorded `proteina` run. First honest head-to-head on our own data. |
| **5. Size sweep** | What is the runtime/VRAM curve? | 3 × A100-80GB, ≤30 min each | **~$5.55** | ~130 / 260 / 415 aa, matching `proteina`'s measured points so the two curves are comparable. Replaces the provisional §4.3 caps. Defer until the tool is committed to — buys accuracy, not a go/no-go. |

**Ladder through rung 4: ≈ $4.30 of GPU** — an estimate. Rung 4's actual spend has since been read ($0.18659948, §5.8, matched on app id but from uncommitted CLI output), but rung 2's never was, so no total on this ladder is a measured figure. Through rung 5: **≈ $9.90.** This section said *"the go/no-go is fully answered at rung 4"*. **It is not.** Rung 4 answered the science question that mattered most and §5.8 reads it as a conditional go, with three things still gating a paid tool: which route §7B takes for scoring, the two edits that make the $1.00 start minimum bind, and the licence exclusion on the oracle path. This is an order of magnitude cheaper than the BindCraft 2 ladder (≈$32) because RFD4 generates in seconds, not hours.

---

## 7. Live charging, streaming and exports

### 7.1 Live charging

How a live tool works here (verified in code this session):

- The slug joins `_LIVE_ALL_PRESETS` (`shared/wallet_guard.py:81-91`), which today holds eight tools.
- A **start minimum** is the balance a run needs before it may start; nothing is debited at start. Per-tool constants sit beside that set — `PROTEINA_START_USD = $1.00` (`shared/wallet_guard.py:97`), `PXDESIGN_START_USD` and `RFANTIBODY_START_USD` = $2.00 (`:100-101`). The other live tools derive theirs instead of using a constant: `bindcraft` and `iggm` return `TOOL_SPECS[slug].base_hard_cap_usd`, `esmfold2-design` returns its base cap **times its seed count**, and `rfdiffusion` returns a `live_due_usd` figure computed from a runtime estimate (`shared/wallet_guard.py:199-213`). Of the eight tools in `_LIVE_ALL_PRESETS`, only `boltz2` falls through to $0 — so "$0 unless stated" is **not** the house default and `rfd4` needs an explicit decision (§9 Q4).
- The container sends a **start heartbeat the moment the GPU is live** (`tools/proteina/modal_app.py`, `_start_beat`), so a cancel bills from GPU start. Two guard tests constrain this, both parametrised over every in-repo live app (`tests/test_live_charging.py:1147-1161`): `_start_beat` must be **AST-identical to `af2`'s copy** (`ast.dump` equality — so comments and formatting may differ, the code may not), and the statement calling it must come **immediately after** `env = _merged_environment(payload)` in every function that builds the env. Landed in `3a8de1fc` (#464) for the **7 in-repo GPU apps** only — its own commit message says so; the out-of-repo containers (`rfdiffusion`, `pxdesign`, `rfantibody`) and non-live `mpnn` are not covered, so "every live tool sends a start beat" is **not** true today. A new app is picked up automatically once its slug joins the live sets, so `tools/rfd4/modal_app.py` must copy `af2`'s `_start_beat` body exactly rather than write its own.
- Billing itself runs from the Railway cron every 5 minutes, debiting wall-clock since `started_at`; when the balance falls short the run is cancelled, reconstructed and completed as a partial.

**Start minimum for `rfd4`: $1.00 — CONFIRMED BY LEO 2026-11-04, coded as `shared/wallet_guard.py::RFD4_START_USD` in PR #487 (open, not merged).** Rung 4 discharged the measurement caveat the pre-confirmation paragraph raised: the `torch.compile` warm-up does land before the first design, but it is paid once per padded-length bucket and $1.00 still covers the stretch (§5.8). The **two conditions** that paragraph left on the figure are the two listed below it, and **step 4 settled both, not rung 4** — the scoring leg stays a separate `boltz2` job with its own gate, and both edit sites are coded in that same PR (§8 step 4). What does not go away is that the headroom varies with the spread of binder lengths.

*Pre-confirmation reasoning (superseded by the line above, kept for its measurement caveat).* **Proposed start minimum for `rfd4`: $1.00 — rung 3 removed the thing that made this a guess for *generation*, and left two conditions on it.** Structures *do* land per design, the first at 68.1 s (§5.7), so the stretch a start minimum has to cover is container start + weight load, not the whole run. At the customer rate $1.00 buys ≈572 s, roughly eight times that stretch. **— RE-MEASURED AT RUNG 4, AND THIS "EIGHT TIMES" IS NOW THE `quickstart` FIGURE, NOT THE PRODUCTION ONE.** Under the production preset the first design lands at 128.2 s, so the headroom is **4.46×, not 8.40×**, and the figure survives but with half the margin (§5.8 Question 3). Read §5.8 before fixing the number. **It was a Leo decision, not a derivation** — the principle the other tools follow is "cover the stretch before the first design reaches storage", and the measured stretch is a floor: a production configuration pays a `torch.compile` warm-up that `quickstart` skipped, which upstream describes as costing minutes. If that warm-up lands before the first design, $1.00 could stop covering it; rung 4 should re-measure the first-design time with compile on before the figure is fixed. *(It did, and the figure was then fixed — see the line above this paragraph.)* **Rung 4 did re-measure it: the warm-up does land before the first design, it is paid once per padded-length bucket rather than once per run, and $1.00 still covers it (§5.8). The open part is that the headroom is no longer a fixed quantity, because compiles scale with the spread of binder lengths.**

**Two things the $1.00 depends on that the figure above does not cover.**

1. **It prices generation only, and §7B adds scoring.** Every score column on the results page comes from a chained Boltz-2 refold at ~25 cents per design, ~$2 on an eight-design run and ~$6 if Leo takes the 3-fold recommendation (§9 item 5). **Which route §7B takes decides whether that matters, and this document has not said.** `shared/refold.py` spawns a *separate* Boltz-2 job per candidate (`:41`), each with its own wallet gate — take that route and $1.00 is sound. Fold the refold *inside* the `rfd4` job instead and a run admitted on a $1.00 balance owes ~$2.28 or ~$6.28, the live meter stops it mid-refold, and the customer keeps eight structures with zero scores: unjudged, which is the failure §7B exists to prevent. This is the hole `live_start_usd`'s own docstring already records for `pxdesign`. **Pick the route before the figure.** **— SETTLED BY STEP 4: the route is the separate per-candidate `boltz2` job (`shared/refold.py:41`), so the fold is billed on a job of its own rather than inside an rfd4 run admitted on $1.00, and the $1.00 is sound for generation. The gate on that leg does not exist yet: `boltz2`'s start minimum is still $0 and `SOURCE_TOOLS` has no `rfd4`, so nothing refuses a chained fold on a short balance. §8 step 4 records the figures that gate will need and why it belongs in the step that builds the chain.**
2. **Nothing holds the figure unless two places are edited.** `live_start_usd` (`shared/wallet_guard.py:195-213`) returns `Decimal("0")` for any slug without an explicit branch, and `tests/test_live_charging.py:259-262` asserts exactly that for every live tool outside its `STARTS` map. **Adding `rfd4` to `_LIVE_ALL_PRESETS` alone therefore ships a $0 start minimum with the whole live-charging suite green.** The $1.00 needs a branch in `live_start_usd` *and* an entry in that test's `STARTS` dict; §5.3's pin list does not currently mention either. **— BOTH CODED IN PR #487 (open, not merged): the `live_start_usd` branch, pinned by `::test_rfd4_starts_at_one_dollar`, and the `STARTS` entry, pinned by the three tests parametrized over `STARTS` (`tests/test_live_charging.py:269`, `:280`, `:288`) — that test reads the `RFD4_START` constant, not the dict, so it would still pass with the dict entry deleted.**

### 7.2 Streaming / save-as-you-go

Copy the `proteina` mechanism: the container uploads each design via a presigned PUT, then sends a heartbeat carrying a `new_candidate` record; the hub appends it to the job's partial candidates, and `shared/job_recovery.py` rebuilds a result from those if the run stops. **Two RFD4-specific hazards:**

- **`delete_failing_pre_oracle: True`** (`configs/inference.yaml:44`) deletes structure files for designs failing a pre-oracle filter. **Rung 3 showed this does not bite in our configuration** — no metrics group is composed, so no filter runs and all eight designs persisted (§5.7). The hazard is therefore conditional on enabling an oracle, which the licence exclusion rules out. Keep the uploader tolerant of a vanished file anyway; it costs one `try`.
- **Each design is written through a hidden temp directory, and the uploader must not see it.** Rung 3 caught `inference_samples/<id>/.generated-cif-w_d3wq6m/<id>_generated.cif` one second before the real path appeared (§5.7) — an atomic write-then-rename. A recursive glob for `*_generated.cif` would upload a partial file under a path no exporter expects. Match on the final filename and skip dot-directories.

- **Score keys must clear two allowlists, not one.** The first is `_sanitize_candidate` (`webhooks/modal.py:508-542`), a **literal dict of ~24 named keys**, each pulled by an explicit `cand.get("<name>")`: anything not named there is discarded at heartbeat time, so it never reaches the partial. The second is `_PROTEINA_SCORE_KEYS` (`shared/job_recovery.py:59`), which the rebuild then filters by. **Adding a key to the second alone silently loses it on exactly the stop/cancel path the edit exists to protect** — the heartbeat drops it, `_candidate_from_partial` (`:102-104`) finds it `None`, and the PR looks like it covered the case. Add `rfd4`'s keys to **both**. Note the three keys §7B's Boltz-2 chain would most likely emit — `iptm`, `plddt`, `i_pae` — are already carried for **every** tool at `job_recovery.py:83-96` (each gated on `part.get(...) is not None`, so present-but-null still drops), so for those three the second edit is a no-op and the first is the only one that matters. Rung 3 found RFD4 itself emits **no** score (§5.7), so every key on that list will come from our own Boltz-2 chain (§7B).

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

Our own measurement, in our own repo: five byte-identical copies of one input, folded in one Boltz-2 process, **spanned ipTM 0.263–0.830** (that one input is the `il4_decoy`, not a representative design; the cognate reproduced to 0.0026 - **see §7C.8, which corrects this sentence**), with 21 pairwise binder CA RMSDs at median 16.3 Å. No `--seed` is passed anywhere in the package, so the sampler is unseeded (`tools/boltz2/__init__.py:111-115`; full write-up in the 2026-09-23 rows of `docs/VALIDATION-LOG.md`). Worse for this purpose, rung B was a **negative discrimination result**: on three binders the cognate held at ipTM ~0.95 while the worst decoy climbed 0.619 → 0.801, collapsing the margin 37% and inverting two decoys' rank (`:118-124`).

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
- **Licence question for Leo, $0:** is upstream's `af2` oracle route open to us given AF2 params are CC BY 4.0, or does the brief's exclusion of the `alphafold2/runtime` image close it regardless. **Leo closed this on 2026-11-03 (*"we already checked license"*, §7C.6 item 3); the reading itself is not recorded in this document, so what is closed is the question, not the answer.**
- **Rung 3, GPU — NOT DONE, carried to rung 4.** Rung 3 priced RFD4 generation only (§5.7); it folded nothing. Measuring the Boltz-2 fold cost per design, and the ipTM spread over *n* folds of one RFD4 design on RFD4 output rather than the 2026-09-23 inputs, is still unspent work and still the thing that decides whether the triple fold is affordable.

---

## 7C. Scoring decision record

**Purpose: turn §7B from a direction into a decision Leo can answer in one sitting, and price the one GPU measurement that is still missing.** Nothing here was run. No code was written, no Modal app deployed, no GPU second spent. Every figure is either cited to a `file:line` in this repository, to a named rung result in this document, or labelled **UNVERIFIED**.

### 7C.1 The chain, end to end

Generation and scoring are **two Modal calls on two apps**, not one container.

1. **`rfd4` generates.** The `rfd4` container streams each design as it lands — a presigned PUT then a `new_candidate` heartbeat — exactly as `proteina` does (§7.2). Each candidate carries `pdb_key`, the designed `sequence`, and RFD4's own inline metrics. **No score** (§5.8 Question 4: 89 columns, not one refold, confidence or ipTM column).
2. **`boltz2` scores.** When the `rfd4` job completes, the delivered designs are folded on the existing `ranomics-boltz2-prod` app (`tools/boltz2/modal_app.py:193`, GPU `A100-40GB` at `:54`). The `boltz2` pipeline takes an antigen PDB plus a list of binder sequences (`tools/boltz2/__init__.py:5-9`) — which is exactly the shape `rfd4` delivers, since RFD4 emits sequence and structure together (§7.3). Per design it returns the predicted complex PDB plus `iptm`, `ptm`, `complex_plddt`, `complex_iplddt` and a hotspot contact count (`tools/boltz2/run_pipeline.py:862-865, 883-889`).
3. **The hub ingests the fold candidates** through the paths already in place: upload URLs, per-design heartbeat (`run_pipeline.py:896-901`), `_sanitize_candidate` (`webhooks/modal.py:419-542`), and `shared/job_recovery.py` for a stopped run.

**Preset: `standalone`, not `msa_server`.** `standalone` is single-sequence cofold and is the documented right choice for designed binder sequences with no informative MSA (`tools/boltz2/__init__.py:16-23`); `msa_server` costs ~3x and, on the one comparison that exists, bought *worse* discrimination (`tools/boltz2/__init__.py:24` and `:121-127`, Rung B). `msa_server` is also exempt from batching (`run_pipeline.py:749`), which would multiply the fixed cost per fold.

**It is a batch call, which the pipeline already does.** `main` folds `FOLD_CHUNK = 10` designs per `boltz predict` process (`tools/boltz2/run_pipeline.py:120`, chunk loop `:749-756`), paying the ~60-70 s process fixed cost once per chunk rather than once per design. Eight designs at n=1 is therefore **one process**.

**Which hub paths this reuses, and the two that need an edit.**

| Path | `file:line` | Reused as-is? |
|---|---|---|
| Live charging membership | `shared/wallet_guard.py:89-104` (`_LIVE_ALL_PRESETS`) | `boltz2` is already in it. `rfd4` must be added (step 4). |
| Start minimum | `shared/wallet_guard.py:122-213` | **No.** See §7C.4 — `boltz2` falls through to `Decimal("0")` at `:218`. |
| Streaming / per-design upload | `tools/boltz2/run_pipeline.py:800-901` | Yes. |
| Heartbeat candidate allowlist | `webhooks/modal.py:419-542` | **No** for any new key. It is a literal dict of explicit `cand.get("<name>")` reads; an unnamed key is discarded at heartbeat time. |
| Stop/cancel rebuild | `shared/job_recovery.py:78-96` | Yes for `iptm`, `plddt`, `i_pae` — those three are carried for every tool, each gated on `is not None`. A spread key is not carried at all. |
| Legends | `shared/score_legends.py:733-767` | Yes. `("boltz2", "ipTM")`, `"pTM"`, `"pLDDT"`, `"n_hotspot_contacts")` all exist. |
| Uniform bar | `shared/score_legends.py:1539` | `boltz2` maps to `("ipTM", "pLDDT", "n_hotspot_contacts")`. An `rfd4` row is a new entry. |
| Campaign columns + sort key | `shared/result_columns.py:25-72` | **No entry for `boltz2` or `rfd4` today.** `_TOOL_RESULT_COLUMNS` and the primary-metric map (`:71-72`) both need an `rfd4` row. |
| Exports | `shared/exports.py:441` | Yes for sequence (RFD4 supplies one directly, §7.3). CIF-as-structure is still the step-5 change. |

**Columns on the results page, and which one sorts (§10 item 14).**

| Column | Source | Role |
|---|---|---|
| **ipTM** | Boltz-2 `iptm`, `run_pipeline.py:862` | **DISPLAYED and SORT key.** One fold (§7C.6 item 1), so labelled as a single fold, not as a confidence score. |
| ~~ipTM range (min-max of n)~~ | our arithmetic over the n folds | **Not built for release 1** - n=1 leaves nothing to range over (§7C.6 item 1). Reinstate with the median column if n>1 is ever revisited. |
| pLDDT | `complex_plddt`, `:864` | Displayed. |
| hotspot contacts | `n_hotspot_contacts`, `:887` | Displayed. |
| hotspot_obedience | RFD4's own metrics CSV (§5.8 Q4) | Displayed. **Do not rank on it** — its denominator of 6 against 3 conditioned residues is unexplained (§5.8 reading 1). |
| binder RMSD | derived, §7C.3 | Displayed if we build it. |

**The sort key and the displayed number must be the same quantity. At the decided n=1 (§7C.6 item 1) that is the single fold's ipTM and the constraint is satisfied by construction; the median-plus-spread form below applies only if n>1 is revisited.** The alternative considered and rejected: sort on the *worst* of the n folds (a conservative "does it hold up under resampling" reading). It is defensible but it ranks a design by its unluckiest draw, and with n=3 that is one sample. Median plus a visible range says the same thing without pretending the single number is stable.

**How n folds' spread reaches the page — not needed for release 1.** At n=1 each design yields one record and no aggregation step exists, so none of the following is step-5 work unless n>1 returns; it is kept because it is the part that would have to be built. n folds of one design produce n candidate records from the `boltz2` job. The aggregation to one row per design is **hub-side after the fold job completes**, not in the container: the container knows nothing about which records are replicates of the same design. That is a step-5 piece of work and it has no code today. Two consequences worth stating before anyone plans it: (i) the per-design heartbeat during the fold reports a *position*, not a rank (`run_pipeline.py:767-783`), so the live page during an n=3 fold shows 24 rows, not 8; (ii) `_sanitize_candidate` has **no key for a spread**, so carrying one means adding it to `webhooks/modal.py:508-542` *and* to the rebuild in `shared/job_recovery.py` — the "two allowlists, not one" trap already recorded at §7.2. The cheapest shape that avoids both: store the median under the existing `iptm` key (existing legend, existing recovery path, no allowlist edit) and add exactly one new key for the range.

### 7C.2 Seeding: how, with what seeds, and what it does not buy

**The flag exists and we do not pass it.** `boltz predict` takes `--seed` (an `int`, default `None`, "no seeding") at our pinned `boltz==2.2.1` (`tools/boltz2/Dockerfile.modal:64`); the option is declared at `main.py:919-922` of upstream `v2.2.1` and applied as `seed_everything(seed)` at `main.py:1101-1103` (read first-hand at upstream tag `v2.2.1`, commit `cb04aeccdd480fd4db707f0bbafde538397fa2ac`; not a file in this repository). Our argv (`tools/boltz2/run_pipeline.py:460-468`) carries `--out_dir`, `--no_kernels`, `--output_format pdb`, `--override` and no seed flag (`--use_msa_server` is the only other append, at `:469-470`, and `standalone` does not take it), so the sampler runs unseeded today.

**Exactly how it would be passed:** one more `cmd.append`/pair in `run_boltz` (`tools/boltz2/run_pipeline.py:444-470`), which is the only place the argv is built.

**The constraint that decides what "n-fold seeds" can mean.** `seed_everything` is a **process-global** call made once per `boltz predict` invocation, and our pipeline folds a whole chunk of up to 10 records in one process. So **one seed per process, shared by every record in it** — there is no per-record seed. n replicates therefore means **n processes, each folding the same design list under its own seed**, paying the ~60-70 s fixed cost n times. A seed per design is not available without one process per design, which is the arrangement batching exists to avoid.

Proposed seeds: **fixed and derived, not random** — replicate *k* of a job uses a constant offset (e.g. `1 + k`), identical across jobs, so two runs of the same design list are byte-comparable. Nothing in the hub currently stores a seed for a `boltz2` job, so the seed must also be recorded on the candidate or the job row, or the reproducibility is unverifiable from the result alone. **UNVERIFIED: no such field exists in `_sanitize_candidate` today** (read `webhooks/modal.py:508-542` — there is no seed key).

**Stated plainly: seeding gives reproducibility, not discrimination.** A fixed seed makes the same design score the same number twice. It does **not** narrow the 0.263-0.830 band in §7B.3 (which, read correctly, is seven draws of one **decoy** input - see §7C.8) — that band is sampling uncertainty across the diffusion sampler's own distribution, and fixing a seed selects one draw from it rather than shrinking it. A seeded single fold is a *stable* number, not a *trustworthy* one. This is why §7B.3's recommendation is seeding **and** n>1, not seeding instead of it.

**One hazard for the existing tool.** `run_boltz` is shared with the production `boltz2` tool that customers already buy. Adding `--seed` unconditionally changes that tool's output for every future run (and would make its scores no longer comparable with scores already in the database). It should arrive as a parameter with the existing unseeded behaviour as the default, or be gated on the caller being the `rfd4` chain.

### 7C.3 The three upstream `ppi` terms, one at a time

Upstream's protein-binder pass criterion has three terms (§7A.2). Status of each against the Boltz-2 chain:

| Upstream term | Status | What it costs |
|---|---|---|
| `oracle_ppi.ptm_coi` min 80.0 | **Covered** by Boltz-2 `iptm` (`run_pipeline.py:862`), after a 0-1 vs 0-100 scale conversion | $0, already in the result |
| `refolded_target_backbone_aligned_binder_rmsd.rmsd` max 2.5 | **Derivable at $0** | CPU arithmetic |
| `oracle_ppi.min_ipae_coi` max 1.5 | **Probably derivable at $0, from output we already park** | see below |

**Binder RMSD — arithmetic, not inference.** RFD4's generated complex (`*_generated.cif`) and Boltz-2's predicted complex (`<id>_model_<n>.pdb`, `run_pipeline.py:498`) are both returned, so the quantity is: superpose on the **target** backbone, then compute binder CA RMSD. Pure CPU, no new GPU second. Two cautions. (i) It needs a chain correspondence between a CIF and a PDB across two different writers; the §5.8 lesson applies directly — pick the designed chain by **sequence identity, not by length**, because `pdl1_binder_4`'s designed chain is 116 aa against a 115 aa target and any length rule mis-identifies it. (ii) It is **not** upstream's number: upstream aligns an *oracle refold* against the generated structure, we would align a *Boltz-2 cofold*. Same shape, different predictor. Do not print it next to upstream's 2.5 bound.

**i_pAE — what the pinned package actually writes.** Read first-hand at upstream `boltz` tag `v2.2.1` (`cb04aecc`), which is the version pinned at `tools/boltz2/Dockerfile.modal:64`:

- The writer saves a **full token-pair PAE matrix** as `pae_<id>_model_<n>.npz`, and at `v2.2.1` that write is gated on **`"pae" in prediction`** (`src/boltz/data/write/writer.py:231-238`) — *not* on the `--write_full_pae` CLI flag. `write_full_pae` is declared (`src/boltz/main.py:890-894`) and threaded into `predict_args` (`:1305`), but the writer body contains no reference to it.
- `prediction["pae"]` is populated when the model's **`alpha_pae > 0`** (`src/boltz/model/models/boltz2.py:1100-1101`). `alpha_pae` is a model hyperparameter (`:73`, default `0.0`) read from the checkpoint, **not** a CLI flag — `main.py` never sets it.
- ~~**So whether the npz exists for *our* run turns on what the released `boltz2_conf.ckpt` sets `alpha_pae` to, which this session did not read. UNVERIFIED.**~~ **RESOLVED 2026-11-03 by existence, not by reading the checkpoint - the npz is there. See §7C.9.** At `v2.2.1` nothing in the *CLI* suppresses the PAE write, and our pipeline passes no flag that would.
- **And if it is written, we already have it.** `archive_raw_outputs` tars the **entire** work tree before teardown (`tools/boltz2/run_pipeline.py:550-575`) onto the raw Volume `ranomics-boltz2-raw` (`tools/boltz2/modal_app.py:97`), precisely because the pipeline keeps only four confidence scalars and the function's own docstring names "the PA(E)/PDE npz" among what it would otherwise lose. **So the $0 check is: pull one parked tar from a past `boltz2` run and look for `pae_*.npz`.** That is a Modal volume read, free, no approval needed, and it answers the question without folding anything. It was not done in this session.
- **A second free quantity we currently throw away.** The confidence JSON at `v2.2.1` also carries `chains_ptm` and the full **`pair_chains_iptm`** matrix (`writer.py:202-213`). Our pipeline reads four scalars from that file (`run_pipeline.py:862-865`) and discards the rest. ~~Per-chain-pair ipTM is the *binder-to-target* confidence specifically, which is closer to upstream's `_coi` ("chain of interest") framing than the global `iptm` we use.~~ **That was wrong, and §7C.9 corrects it: for a two-chain complex the global `iptm` we already store *is* `pair_chains_iptm['1']['0']`, bit for bit, so there is no extra chain-pair signal to recover here.** It would matter for a multi-chain target. `chains_ptm` is still discarded and is still free.

**The `_num` hazard, restated because it is the one that would ship a wrong number silently.** `_sanitize_candidate` stores `"i_pae": _num(cand.get("i_pae"))` where `_num` is `round(float(value), 4)` with **no range gate and no unit check** (`webhooks/modal.py:437-441, 517`). PAE is an Angstrom-like quantity and ipTM-like scores are 0-1; the hub has already been bitten by a 0-1 vs 0-100 split on pLDDT, and `shared/score_legends.py:1533-1536` records a deliberate decision to leave `pAE` out of the `pxdesign` bar for exactly this reason — "a leg that can silently pass everything is worse than no leg". **If we compute an interface PAE from a Boltz-2 matrix, the unit must be fixed at the point of computation and asserted there**, because nothing downstream will catch it.

### 7C.4 Cost, and what it does to the $1.00 and the cap

Boltz-2 runs on **A100-40GB** (`tools/boltz2/modal_app.py:54`) at **$0.000714 raw GPU-second** (`shared/wallet_estimates.py:73`), billed through **`WALLET_MARKUP = 1.70`** (`shared/wallet.py:71`) = **$0.0012138 per customer GPU-second**.

Two measured anchors exist, and they disagree by ~2.5x because one predates batching. Both are in `tools/boltz2/__init__.py`:

- **Per-design (pre-batching), `:30-35`:** 81.9 s first fold then ~69 s marginal, measured 2026-09-19 on A100-40GB.
- **Batched, `:76-88`:** fixed process cost **60.3-69.8 s** paid once per chunk, each record's own fold **14.1-23.5 s**, measured 2026-09-23 on two throwaway probes.

Since the chain is a batch call, the batched anchor is the right one and the per-design anchor is the ceiling. **Both are given, because the batched figure has never run through prod, the wrapper or the wallet (`:90-99`), and the largest batch ever folded is 5 records, not 10.**

| | Processes | Records | GPU-s (batched anchor) | Customer | GPU-s (per-design ceiling) | Customer |
|---|---|---|---|---|---|---|
| **n=1, one design** | 1 | 1 | 74-93 | **$0.09-0.11** | 82 | **$0.10** |
| **n=1, 8 designs** | 1 | 8 | 173-258 | **$0.21-0.31** | 565 | **$0.69** |
| **n=3, one design** | 3 | 3 | 223-280 | **$0.27-0.34** | 246 | **$0.30** |
| **n=3, 8 designs** | 3 | 24 | 519-773 | **$0.63-0.94** | 1,695 | **$2.06** |

Arithmetic: batched = `processes x (60.3-69.8) + records x (14.1-23.5)`; per-design = `processes x 81.9 + (records - processes) x 69`. n=3 is three processes because a seed is process-global (§7C.2), and 8 records fit one chunk at `FOLD_CHUNK = 10`.

**So scoring an 8-design run costs between about 20 cents and about $2, and the n=1 vs n=3 decision is worth roughly $0.40 to $1.40 per run.** **That decision went to n=1 (§7C.6 item 1), so the cheaper row is the live one.** That is a rounding error against the $1.00 start minimum, and it is the reason this decision should be made on what is *honest*, not on what is cheap.

**Three things that could move those numbers, each UNVERIFIED for RFD4 output.** (i) Both anchors were measured on **242-246 aa binders against a 107 aa antigen** (`tools/boltz2/__init__.py:37-40`). Rung 4's actual designs are **58-116 aa binders against a 115 aa target** (§5.8 Question 1) — a materially smaller complex, so the anchors are more likely high than low, but the direction is an extrapolation and not a measurement. (ii) The batched anchor's fixed term moved 60.3 -> 69.8 s between two probes, so read it as a band. (iii) Nothing has measured a 10-record chunk, let alone three of them.

**Effect on the $1.00 start minimum — this is the part that needs a decision, not a number.** §7.1 already records the hole: `shared/refold.py` spawns a **separate** Boltz-2 job per candidate (`:41`), and a separate job passes its own wallet gate. `boltz2` has **no start-minimum branch** in `live_start_usd`, so it falls through to `return Decimal("0")` (`shared/wallet_guard.py:218`). Consequences of each route:

- **Separate `boltz2` job (recommended).** `rfd4`'s $1.00 prices generation only, which is what §5.8 measured it against ($0.2240 to the first design, 4.46x headroom). But the fold job starting at $0 means a customer whose balance is exhausted by generation gets **eight structures and no scores** — the exact "unjudged" outcome §7B exists to prevent. The fix is not a bigger `rfd4` minimum; it is to **refuse to spawn the fold unless the balance covers it**, or give the chained fold its own non-zero minimum. $1.00 then stands unchanged.
- **Fold inside the `rfd4` job.** A run admitted on $1.00 owes $1.21-$3.06 all-in and the live meter stops it mid-fold. Do not do this.

**Effect on the per-job cap: none binding, but the *hold* is a display problem at n=3.** `PER_JOB_HARD_CAP_USD["boltz2"]` is $50.00 (`shared/wallet.py:147`) and `base_hard_cap_usd` is $0.40 (`shared/wallet_estimates.py:428`) scaling on `n_designs_total` (`:425`), so a 24-record fold caps at $9.60 against an actual $0.63-$2.06 — comfortable. The problem is the quote: `expected_gpu_seconds = 180.0` (`:423`) is a deliberate over-guess covering both presets, so 24 records quote 4,320 GPU-s ≈ $5.24, cushioned 1.5x to roughly **$7.87 of hold for about $2 of folding**. Safe on money (settlement on completion "releases any surplus or debits any variance up to a per-tool hard cap", `shared/wallet.py:5-6`, implemented at `settle_hold:717`) but it is the number the customer reads, and at n=3 it is nearly 4x the charge. **`rfd4`'s own cap is a step-4 number and must come from rung 4's measurement, not from `proteina`'s.**

### 7C.5 Rung 5: the smallest GPU run that prices this

~~**This is a proposal. It is not approved and must not run until Leo approves this specific job.**~~ **SUPERSEDED 2026-11-03.** Leo approved a paid run, but not this shape: the measurement he asked for is the per-design ipTM spread on all eight designs, not a length sweep on three. The approved design, the cap and the result are in **§7C.10**. The reasoning below stands; the record table does not.

**What it measures, and why it cannot be derived.** Two quantities, both load-bearing and neither available on CPU or on paper: (i) **Boltz-2 fold wall-clock on RFD4-sized complexes** — every anchor we have is a ~244 aa binder against a 107 aa antigen, and the whole §7C.4 table rests on extrapolating to 58-116 aa; (ii) **the ipTM spread across seeds on real RFD4 output** — §7B.3's 0.263-0.830 was measured on 2026-09-23 inputs (trastuzumab-framework scFvs), and whether RFD4 designs behave the same way is the question that decides n=1 vs n=3.

**Inputs are already in the repository, at $0.** All eight rung-4 designs are embedded as full CIF text in the committed result artefact `docs/scoping/rung4-probe/gpu_result_20261103T114437.json` under its `files` map (keys `/tmp/rfd4_run/inference_samples/pdl1_binder_<k>/pdl1_binder_<k>_generated.cif`, 517-691 KB each). The target chain is byte-identical across all eight (§5.8 Question 1), so one target and eight binder sequences are extractable locally with no GPU and no download.

**The smallest run that answers both.** Three designs spanning the length range — `pdl1_binder_5` (58 aa), `pdl1_binder_2` (75 aa), `pdl1_binder_4` (116 aa) — folded under three seeds, plus **one repeat of the first seed on one design** to confirm that seeding actually fixes the number:

| | Processes | Records |
|---|---|---|
| seeds 101 / 202 / 303 x 3 designs | 3 | 9 |
| seed 101 repeat, `pdl1_binder_5` | 1 | 1 |
| **total** | **4** | **10** |

**Expected cost:** `4 x (60.3-69.8) + 10 x (14.1-23.5)` = **382-514 GPU-s** on A100-40GB = **$0.27-$0.37 raw**, **$0.46-$0.62 customer**. Ask for a **$0.75 raw ceiling** — the headroom covers the batched anchor being optimistic, since the per-design ceiling arithmetic for the same shape (`4 x 81.9 + 6 x 69` = 742 s) is $0.53 raw.

**What it would return:** per-design fold seconds at RFD4 sizes; ipTM at three seeds on three designs (the spread); and one seeded duplicate pair (reproducibility, pass/fail). That is enough to settle n=1 vs n=3 and to replace the §7C.4 table's extrapolation with a measurement.

**Where the fold runs, and why it is not `ranomics-boltz2-prod`.** This paragraph was missing from the first draft of §7C.5, which named the production app and left the rest to be read as "the existing app". It is not. The measurement runs in **a separate ephemeral Modal app that reuses the production boltz2 image** - `modal.Image.from_dockerfile(tools/boltz2/Dockerfile.modal)` plus the production `tools/boltz2/run_pipeline.py`, same `boltz==2.2.1` pin (`tools/boltz2/Dockerfile.modal:64`), calling production's own `make_yaml_sseq:396`, `_record_id:427` and `collect_outputs:478-498`. `modal run` creates such an app for one invocation and tears it down; nothing is deployed and the nine production apps are untouched.

**The reason it has to be a separate app is the one-line difference, and that difference is load-bearing.** Measuring per-seed spread needs `--seed` on the `boltz predict` argv, and production `run_boltz` (`tools/boltz2/run_pipeline.py:444-473`) does not pass it. Adding it there unconditionally would change the live `boltz2` tool's output for **every future run** of a paid tool - the same input would stop returning the same number it returns today - and would make new scores non-comparable with every score already in the database. That is a migration question, not a probe. So the flag lives in a throwaway app until step 5 decides how it should arrive in production: as a parameter defaulting to today's unseeded behaviour, or gated on the caller being the `rfd4` chain.

**The $0 / CPU path that must run first.** Folding on CPU is not the dry run — `boltz predict --accelerator cpu` on a ~230-token complex is not a sane use of anyone's afternoon, and it would measure nothing we want. The free rehearsal is everything *around* the fold, and it de-risks the paid run: extract the eight designed chains and the target from the committed artefact **by sequence identity, not by length** (§5.8's own trap); build the per-design input YAMLs through the real code path (`tools/boltz2/run_pipeline.py::make_yaml_sseq:396`); check the record ids are the fixed-width `d_000` form that keeps one record's glob off its neighbour's files (`_record_id:427`, `collect_outputs:478-498`); and implement and self-check the target-aligned binder RMSD of §7C.3 on the eight CIFs against each other, where no fold is needed. **Every one of those is a place a paid run could return unusable output, and all four are free.**

### 7C.6 Questions for Leo

Each has a recommendation. Items 1 and 2 are needed before step 5 is written; 3 and 4 can follow. **Leo answered on 2026-11-03. Item 4: drop them. Items 1 and 2: **one fold**, decided by Leo directly on 2026-11-03 - and he stated that the *"confirm three folds per design"* this document previously attributed to him was **not his words**. Item 3: closed, licence already checked. Every item is now answered; see each line, and the withdrawn attribution under item 1.**

1. **Fold each design once, or three times?** -> **DECIDED 2026-11-03 by Leo, in his own words: once.** *"previous work shows that a same design folded three times give different iptm scores. I am not convinced of that so I am okay with folding just once in scoring."* **The condition this section already attached to n=1 stands and is now load-bearing: the ipTM column is labelled as a single fold, not as a confidence score.**

    **Attribution withdrawn.** An earlier version of this line read *"DECIDED 2026-11-03 by Leo ... 'confirm three folds per design'"*, relayed through the hub lead. Leo has since said that was **not his words**. The quote is withdrawn and n=3 was never his decision; this document asserted it at four commits - `ab7bcb4a`, `27d8ee09`, `9309a9e6`, `8eac9bfb` (counted with `git cat-file -p <rev>:<path> | grep -c`) - and the error is recorded rather than quietly removed. The commit message of `2f4a3812` says two commits; that count was wrong and this line supersedes it.

    **The measured fact Leo is overriding, stated once and not re-argued.** §7C.10 folded rung 4's eight designs three times each on **real RFD4 output**, not on the decoy that prompted the original doubt: `pdl1_binder_7` read **0.7369 at seed 2 and 0.8873 at seed 3**, so at n=1 it falls either side of an 0.8 shortlist cut on the draw alone, and its rank against `pdl1_binder_5` flips with it. One *shortlist-changing* inversion in eight designs, at $0.30 a measured run - not the only reordering: §7C.10 also records the top four permuting among themselves between seeds 1 and 3, inside a 0.007 band. The call is n=1 and this section records n=1 (this is a scoping document; step 5 is where it is built); what that carries is that a shortlist cut near 0.8 is not reproducible run-to-run, and the labelling condition above is what keeps the page honest about it. Original objection that prompted the probe, kept for the record. Leo: *"i find it weird that the design scores from 0.26 to 0.83 on the same designs, i don't believe that"* - a fair objection, and §7C.8 is the answer to it. He approved a paid run to settle the question on real RFD4 output instead of on the decoy; see §7C.10. Original recommendation: **Recommendation: three, and show the range.** Cost is $0.63-$0.94 against $0.21-$0.31 on an 8-design run (§7C.4) — under a dollar either way. Our own Boltz-2 spans ipTM 0.263-0.830 on byte-identical input - but **only on a decoy**; the cognate held to 0.0026 across the same three runs (§7C.8). So a single number is one draw from a distribution whose width is itself unknown until you fold twice. Three folds with a visible range is the honest version of the same column. **If you want n=1 for release 1 to keep the page simple, the condition is that the column is labelled as a single fold** — not as a confidence score.
2. **Sort key.** -> **DECIDED 2026-11-03, following item 1: the single fold's ipTM.** With n=1 there is no median and no range, so the displayed number and the sort key are the same quantity by construction - the property the recommendation below was protecting, obtained for free at n=1. **pLDDT remains ineligible, and that half does not depend on n:** it rests on the measurement, not on the fold count. Original recommendation, which the measurement upheld: **Recommendation: median ipTM over the n folds, and display that same median with the min-max range beside it.** Sorting on something other than the number shown invites "why is #1 below #2". Rejected alternative: sort on the worst fold, which is defensible but ranks a design by its unluckiest draw. **Do not** sort on `hotspot_obedience` — its denominator is unexplained (§5.8 reading 1). **Nor on pLDDT:** §7C.10 measured all eight rung-4 designs inside a 0.058-wide complex-pLDDT band while their ipTM ran 0.2651 to 0.9262, and the pLDDT order ranks the best of the weak designs last of eight, below two designs that do not bind.
3. **The `af2`-oracle licence question (§9 item 9).** -> **CLOSED 2026-11-03 by Leo: *"we already checked license"*.** The reading itself is **not recorded in this document**, so this line claims only that the question is closed, not what the answer was - anything that turns on the outcome must cite where it is recorded. Original recommendation, now moot: **ask, but do not wait for it.** AF2 parameters have been CC BY 4.0 since the 2022-12-06 release (§7B.1), so the restrictive oracle is AF3, not AF2; whether the brief's exclusion of the optional `alphafold2/runtime` **image** also closes the route to our own ColabFold weights is a licence reading, not a technical one. It would recover `min_ipae_coi` directly. But §7C.3 says that term is **probably already in a tar we have parked**, at $0 — so this question is now worth less than it was when §9 raised it. Spend the free volume read first.
4. **Drop the developability columns for release 1?** -> **ANSWERED TWICE, AND THE SECOND ANSWER IS THE ONE THAT HOLDS. Split the group.** First answer, 2026-11-03: *"leave them out"*, all four. **Reversed by Leo, relayed through the hub lead and recorded here 2026-11-04:** the two **sequence** terms -- GRAVY and the instability index -- **are in release 1**; the two **Rosetta** terms -- `sap_score` and `hydrophobic_exposure_sasa` -- **stay out**, on the licence question of §7A.6 and because we have no counterpart for either in our own script (§7B.5: GRAVY and the instability index are computed at `scripts/dev_analysis.py:125,130`, `sap_score` has no counterpart, and `hydrophobic_exposure_sasa` has only a patch proxy that `scripts/build_report.py:696-697` itself calls "a single-snapshot proxy"). **Those two `scripts/` paths are not in this repository** — they are the `binder-developability` Claude skill's, as §7B.5 records, so step 5 ports the two formulas rather than calling them.

    **Never call that surviving pair "developability" bare.** It is 2 of upstream's 4, both pure-sequence, and it says nothing about whether the binder binds. Upstream's own criterion is a monomer triage bar (§7A.4); half of one is less than that. Write "the two sequence developability terms", or name them.

    **Whose step this is: 5, not 3.** Both terms compute hub-side at result ingest, from the sequence we already have, so no GPU column and no upstream config change is involved, and §10 item 13 (does `sequence_developability` need the PyRosetta apptainer?) does not gate them -- we are not running upstream's class. **Two traps step 5 will hit:**

    * **`_sanitize_candidate` is a literal allowlist.** `webhooks/modal.py` copies named keys out of each candidate at heartbeat time, so a new score key that is not listed there is **silently discarded** -- the design streams, the column is simply never populated, and nothing errors. Add the keys in the same PR that computes them.
    * **`shared/job_recovery.py`'s rebuild has no rfd4 keys.** `_candidate_from_partial` carries `iptm`/`plddt`/`i_pae` for every tool (each gated on `is not None`) *and* the nine `_PROTEINA_SCORE_KEYS`, plus `target_numbering` — and nothing else. So it is an allowlist with no rfd4 entry rather than a three-key list: a cancelled or recovered job loses any key on neither list, and the two terms need an entry there too or they vanish on exactly the runs a customer is most likely to ask about.

    Original recommendation, superseded in part: yes, drop them. The Rosetta pair needs a PyRosetta apptainer with its own licence question (§7A.6), our `binder-developability` skill covers only the two *sequence* terms and lives outside this repository so using it means porting it (§7B.5), and whether `sequence_developability` also needs the apptainer is still unread (§10 item 13). Nothing in the chain above depends on them.

**One thing to note rather than decide: the start-minimum hole in §7C.4.** The recommended route (a separate `boltz2` fold job) keeps `rfd4`'s $1.00 correct but lets the fold start at $0, so a drained balance delivers structures with no scores. That is an engineering fix in step 4 — refuse the spawn, or give the chained fold its own minimum — and it does not need Leo's input unless he wants the other route.

### 7C.7 What this section did not verify

- **Whether `alpha_pae > 0` in the released `boltz2_conf.ckpt`**, i.e. whether a `pae_*.npz` is actually written for our runs. The CLI does not suppress it at `v2.2.1` and we pass no flag that would; the checkpoint hyperparameter was not read. The $0 resolution is a read of one parked tar on `ranomics-boltz2-raw`, not a fold.
- **Whether any parked `boltz2` tar in fact contains `pae_*.npz` or the `pair_chains_iptm` block.** Nothing was pulled from the raw Volume in this session.
- **Every cost figure for RFD4-sized complexes.** All of §7C.4 extrapolates from 242-246 aa binders against a 107 aa antigen to 58-116 aa binders against a 115 aa target. The direction (smaller is faster) is reasoning, not measurement. That is what rung 5 is for.
- **The batched anchor through production.** It comes from two throwaway probes on a non-identical image, and the largest batch ever folded is 5 records (`tools/boltz2/__init__.py:90-99`).
- **That `_sanitize_candidate` has no seed field** was read (`webhooks/modal.py:508-542`); what it would take to add one was not designed.
- **No GPU ran, no app was deployed, no money was spent for this section.**

### 7C.8 Correction: the 0.263-0.830 spread is a decoy property, not a tool property

Raised by Leo 2026-11-03 - "I find it weird that the design scores from 0.26 to 0.83 on the same designs, I don't believe that." The doubt is well placed and the figure needed splitting.

**The spread is real, and it is one input: `il4_decoy`.** Seven draws of that single byte-identical input span ipTM 0.263-0.830 - five folds in one `boltz predict` process (0.263, 0.470, 0.725, 0.802, 0.830) plus Rung A's 0.7187 and a three-record batch's 0.2849 (`docs/VALIDATION-LOG.md`, "Decoy correction (appended 2026-09-24)").

**The cognate, over the same runs, is stable to three decimal places: 0.9527 / 0.9501 / 0.9509, spread 0.0026** (same file, the 2026-09-23 ship-gate note, which calls the cognate "the one stable quantity across all of it"; the Rung A -> Rung C pair is recorded as `tigit_cognate` **0.9509** against **0.9527**, a move of -0.0018, on input proven byte-identical by sha256).

**So the honest statement is the opposite of the alarming one.** Boltz-2 is *not* unreliable on things it is confident about - a 0.95 cognate reproduces to 0.3%. It is unreliable on things it is *unsure* about, which is exactly where a weak design sits. §7B.3 says "five byte-identical copies of one input" without naming that input as a decoy, and §7C's first draft repeated the 0.263-0.830 figure as though it applied to any design. **Both readings were too pessimistic.**

**This changes what rung 5 is for, and makes it a better experiment.** The question is no longer "how noisy is Boltz-2" but **"is the per-design spread tight for confident designs and wide only for weak ones?"** If it is, the spread is not noise to be averaged away - it is a *discrimination signal in its own right*, and a design whose three folds disagree is telling us something a single median would hide. If instead a confident-looking RFD4 design also swings 0.5, the column cannot be shipped at n=1 at all. One measurement separates those, which is why n=1 vs n=3 and the sort key were left undecided pending it.

### 7C.9 The $0 archive read: the PAE matrix is there, and it discriminates

Done 2026-11-03, before the paid run and as a condition of it. Free: one `modal volume get` from `ranomics-boltz2-raw` plus local arithmetic. The script and its output are committed beside this doc as `docs/scoping/rung5-probe/pae_from_parked_tar.py` and `pae_check_result.json`; every figure below is read from that JSON.

**The npz exists.** `gate1-standalone-1789842854.tgz` holds 83 members, among them **3 `pae_*.npz`, 3 `pde_*.npz` and 3 `plddt_*.npz`** - one each for the three records of that run. So `alpha_pae > 0` in the released `boltz2_conf.ckpt`, established by the file being written rather than by reading the checkpoint, and §7C.3's UNVERIFIED is closed. Every past `boltz2` run has been parking this.

**It is Angstrom-scale, which makes the `_num` hazard real rather than theoretical.** `pae` is a single `float32` (n, n) matrix - 351x351 here - with values from **0.25 to 29.8**. Nothing in `_num` would reject that if it were ever stored in a field a 0-1 legend reads (§7C.3, `webhooks/modal.py:437-441`).

**Interface PAE separates the cognate from both decoys, more cleanly than ipTM does.** Taking both off-diagonal blocks (PAE is asymmetric) with the split read from the run's own `chains.res_num` rather than guessed from a length:

| record | chains | i_pAE mean | i_pAE min | ipTM |
|---|---|---|---|---|
| `cd45_decoy_0p102` | 244 + 107 | **12.62 A** | 4.40 | 0.6187 |
| `IL4_decoy_0p621` | 242 + 107 | **10.45 A** | 4.12 | 0.7187 |
| `TIGIT_cognate_0p949` | 246 + 107 | **4.16 A** | 1.26 | 0.9527 |

Cognate to nearest decoy is **2.5x on i_pAE** against **1.33x on ipTM**. **Caveat, and it is exactly what rung 5 exists to answer: one stochastic draw each, so this is a separation, not a stability claim.** Upstream's bound is `min_ipae_coi` max 1.5 and the cognate's block minimum is 1.26, so the sign is right - but a block mean is not upstream's statistic and must not be printed against their 1.5.

**A correction to §7C.3.** The global `iptm` we store is **exactly** `pair_chains_iptm['1']['0']` for all three records (0.6187190413475037, 0.7187153100967407, 0.9527387619018555 - identical values, not rounded matches). The matrix is asymmetric (`['0']['1']` is 0.5330 where `['1']['0']` is 0.6187) and its diagonal holds each chain's own ptm. So for a **two-chain** complex there is nothing extra to recover; §7C.3 claimed otherwise and was wrong.

**One bonus.** Rung A's `tigit_cognate` ipTM reproduced from the parked archive to all sixteen digits (0.9527387619018555), independently corroborating `docs/VALIDATION-LOG.md` on the one number §7C.8 rests on.

### 7C.10 Rung 5, measured: the spread is tight for confident designs and wide for weak ones

**Leo's question, answered on real RFD4 output.** One paid run, approved 2026-11-03 at a $1.00 cap: rung 4's eight designs, each folded three times with fixed seeds 1 / 2 / 3, three `boltz predict` processes (a seed is process-global, so replicates cannot be records in one chunk). All three processes returned `rc=0`. Raw artefact: `docs/scoping/rung5-probe/gpu_result_20261103T135955.json`; the probe is `rung5_app.py` in the same directory and the inputs it was given are `rung5_inputs.json`.

| design | binder aa | seed 1 | seed 2 | seed 3 | median | **spread** |
|---|---|---|---|---|---|---|
| `pdl1_binder_6` | 68 | 0.9233 | 0.9210 | 0.9260 | 0.9233 | **0.0050** |
| `pdl1_binder_3` | 109 | 0.9226 | 0.9293 | 0.9251 | 0.9251 | **0.0067** |
| `pdl1_binder_2` | 75 | 0.9262 | 0.9320 | 0.9227 | 0.9262 | **0.0093** |
| `pdl1_binder_4` | 116 | 0.9229 | 0.9124 | 0.9195 | 0.9195 | **0.0105** |
| `pdl1_binder_5` | 58 | 0.8787 | 0.8628 | 0.8834 | 0.8787 | **0.0206** |
| `pdl1_binder_7` | 99 | 0.8241 | 0.7369 | 0.8873 | 0.8241 | **0.1504** |
| `pdl1_binder_0` | 108 | 0.2651 | 0.2253 | 0.3228 | 0.2651 | **0.0975** |
| `pdl1_binder_1` | 101 | 0.4412 | 0.3590 | 0.5378 | 0.4412 | **0.1788** |

**The answer is yes, as a two-band split - not as a trend.** The four tightest designs, medians **0.9195 to 0.9262**, reproduce to within **0.0105** and the tightest to 0.0050. The three weakest, medians **0.8241, 0.4412 and 0.2651**, spread **0.0975 to 0.1788** - nine to thirty-six times the tight band. `pdl1_binder_5` sits between them at median 0.8787, spread 0.0206. Spread does **not** rise monotonically as median falls, and the table should not be read that way: inside the tight band the spreads run 0.0093, 0.0067, 0.0050, 0.0105 against falling medians, and at the bottom `pdl1_binder_0` has the lowest median of all eight with a spread (0.0975) *smaller* than `pdl1_binder_1`'s 0.1788, which is what a metric compressed against its own floor looks like. What the eight rows support is the band claim - **above about 0.92 the number reproduces, below about 0.83 it does not** - and nothing finer. So §7C.8's reading is confirmed on real RFD4 output and not just on the `il4_decoy`: **Boltz-2 is reproducible about designs it is confident in and unreliable about designs it is unsure of.** Leo's instinct that a 0.26-0.83 swing could not be the whole story was right.

**It also means the spread is itself a signal, not just an error bar.** A wide range is a statement that the model does not know, which is information a single number destroys - and release 1 ships a single number (§7C.6 item 1), so that information is not collected rather than collected and hidden.

**What it says about n=1 versus n=3, concretely.** **Leo decided n=1 on 2026-11-03 (§7C.6 item 1), so what follows is the measured case this section put, not the decision - it is recorded because it is the known cost of shipping one fold, and because the shortlist cut it names is the thing to watch.** The case is not "n=3 is more rigorous"; it is one measured rank inversion:

- **`pdl1_binder_7` straddles a 0.8 shortlist cut.** Seed 2 gives 0.7369 and seed 3 gives 0.8873. At n=1 that design is above or below an 0.8 threshold **depending on which seed ran**, and the page would show no sign of it either way.
- **Its rank against `pdl1_binder_5` flips with the seed.** At seed 2, 0.7369 < 0.8628; at seed 3, 0.8873 > 0.8834. Two adjacent rows swap on nothing but the sampler's draw.
- **The top four stay the top four, but their internal order does not.** Seed 1 orders them `_2, _6, _4, _3` (0.9262, 0.9233, 0.9229, 0.9226); seed 3 orders them `_6, _3, _2, _4` (0.9260, 0.9251, 0.9227, 0.9195). All four sit inside a 0.007 median band, narrower than several of their own spreads, so the four *names* at the top of the page are stable while the order among them is noise. That is the precise sense in which n=1 is defensible: *if* the column is labelled a single fold and the top rows are not presented as a ranking. The shortlist-changing error is not in the strong designs, it is in the middle of the list, which is exactly where a cut falls.

**pLDDT must not be the sort key - though not because it promotes a non-binder to the top.** All eight designs fall inside a **0.058-wide** complex-pLDDT band, median 0.8997 to 0.9577, while their median ipTM runs 0.2651 to 0.9262. `pdl1_binder_0` reads complex pLDDT 0.9152 / 0.9128 / 0.8857 against ipTM 0.2651 / 0.2253 / 0.3228, and `pdl1_binder_1` 0.9072 / 0.9029 / 0.9131 against 0.4412 / 0.3590 / 0.5378: the two worst binders in the set are confidently *folded* and not *bound*. Ranked by median pLDDT the order is `_6, _5, _2, _3, _4, _0, _1, _7`, so the two non-binders land 6th and 7th rather than at the top - but `pdl1_binder_7`, the best of the weak designs at median ipTM 0.8241, is ranked **last of eight** beneath both of them, and `pdl1_binder_5` rises from 5th on ipTM to 2nd. pLDDT compresses the whole set into six hundredths and then orders it by something that is not binding. ipTM is the discriminating column.

**Cost, measured per app rather than derived.** `modal billing report --for today -r h --tz local` is itemised, and the measurement app `ap-8SYu3bEIFtALXWwICDaRsD` reads **$0.29720974**. The two free CPU probes alongside it: `ap-C1InHcJeCBjqyUgXLrIozL` (the `--seed` check that passed) $0.00019549, and `ap-SDCoVQ3FcVK9hQbNhcUY1W` (an earlier attempt whose containers died at module import, requesting no GPU) $0.00004704. **Session total $0.29745227 against a $1.00 approval.** **The measurement line settles upward after the run ends:** the same command read `$0.29233967` for that app shortly after it finished and `$0.29720974` about an hour later, a rise of $0.00487. So a billing figure read immediately after a run is a lower bound, not the bill - which also means the first reconciliation attempt below was done against an unsettled number. Fold wall time was 166.3 + 162.0 + 161.1 = **489.4 s** across the three processes, so ~163 s for an 8-record batch of 58-116 aa binders against a 115 aa target.

**The cap, as arithmetic.** The ceiling was the function's `timeout=1150`, which kills the container rather than letting it keep billing. `shared/wallet_estimates.py:73` prices A100-40GB at **$0.000714 per second** under the comment "Modal GPU rate card (USD per second)", i.e. raw Modal cost before `WALLET_MARKUP` (`:54`, 1.70). This probe runs on our own Modal account with no wallet and no customer, so raw is the correct basis: **1150 x $0.000714 = $0.8211**, and the measured $0.29721 is 36% of it. Worth stating because it is not obvious: at the 1.70 markup the same timeout would be **$1.3959** of customer-billed spend, so the $1.00 fits only because this is account spend, and a customer-facing job of this shape would need a tighter timeout. The paragraph below bounds the real A100-40GB rate at or below $0.00060729/s, which makes the real ceiling at most 1150 x $0.00060729 = **$0.6983** - the cap was tighter than it was set to be, in the safe direction. (From the measured bound, not from the not-yet-effective $2.19/hr list.)

**Both GPU constants we have measured are too high, and that is the only conclusion the two lines carry.** Wall clock here is taken *inside* the container (`rung5-probe/rung5_app.py:141-145`, `rung4-probe/rung4_app.py:262-300`), so billed GPU-seconds can only be greater than or equal to it. Cost divided by wall is therefore an **upper bound on the $/s rate**, and any candidate rate above that bound is excluded. Rung 5 requested `A100-40GB` and spent $0.29720974 over 489.4 s of fold wall clock: real rate **<= $0.00060729/s = $2.1863/hr**, against the $0.000714 ($2.5704/hr) our constant encodes (`shared/wallet_estimates.py:73`), which is excluded - it would imply 416.3 billed s, 73 s less than the folds demonstrably ran. Rung 4 requested `A100-80GB` (`docs/scoping/rung4-probe/rung4_app.py:251`), its subprocess ran 217.4 s (`rung4-probe/gpu_result_20261103T114437.json`, `wall_s`) and the only large rung-4 line in the same itemised report is $0.18659948 (`ap-19HVd6i5HKpXr5tke3hs4E`, 11:00 hour - the same app id §5.8 records for that run from `modal app list`, so this is an id match; what is uncommitted is the CLI output at both ends): real rate **<= $0.00085832/s = $3.0900/hr**, against our $0.001028 ($3.7008/hr), also excluded (181.5 billed s, below that run's wall). **Two of seven classes, both over-quoted at the request level.** That is the defensible consequence and it is direction-safe: for a job that asks for either class, our estimate charges more per second than Modal did, so on those two classes we over-quote and never under-quote - the safe direction, and not urgent. It says nothing about the five classes no run has measured, so the `A10G` constant the hub lead holds for the refresh batch is still unmeasured in either direction.

**What the numbers do NOT establish.** Leo's forwarded Modal pricing mail (hub lead, 2026-11-03) gives new rates from **2026-12-01**, A100-40GB at $2.19/hr = $0.00060833/s. That rate is *above* rung 5's ceiling: it implies 488.6 billed s against 489.4 s of wall, 0.8 s short, and rung 4 shows about 32 s of container start-up and weights loading sitting on top of a subprocess, which should push billed seconds tens of seconds *above* wall, not below it. So "our rate card is one price list behind" is **not** established by this measurement; $2.19/hr is merely the nearest published rate to a bound that sits just under it, on a list that is second-hand and not in effect until 2026-12-01. Nor is the wrong-GPU-class alternative closed. The old `L40S` constant ($0.000597, `shared/wallet_estimates.py:77`) implies 497.8 billed s on rung 5 - 8.4 s above wall, which is **possible**, and on this evidence fits the start-up overhead better than $2.19/hr does. On rung 4 the old A100-40GB and old L40S constants imply 261.3 s and 312.6 s, both above that run's wall and so both possible. At least three readings survive; the measurement excludes the two old constants for the classes requested, and nothing finer. Settling adds a caveat in the same direction: the rung-5 line rose $0.00487 between two readings an hour apart, then held at $0.29720974 on a third read later the same day (`modal billing report --for today -r h --tz local --json`, 2026-11-03; a session observation, no artefact committed). So it appears to have stopped, but the ceilings above are bounds either way: any further settling can only move the real rate down. **No constant is changed on this PR**; the hub lead holds the rate-card refresh as its own board item for after 2026-12-01, because those numbers move live customer prices and because what to set them to is not yet measured.

**What this does not measure.** One target. Eight designs from one rung-4 run. Three seeds is enough to see that spread tracks confidence and enough to catch one rank inversion; it is not enough to put a number on the sampling distribution's width, and no claim above does so. Seeds 1/2/3 are arbitrary and fixed; nothing here says a different triple would give the same medians to four decimals.

## 8. Work plan, in PR-sized steps

Each step is one PR with a gate. Sizes are rough.

| # | PR | Size | Gate |
|---|---|---|---|
| 0 | This scope doc | XS | Leo reads §9 and answers the open questions |
| 1 | `tools/rfd4/Dockerfile.modal` + `seed_volumes.py`, digest-pinned, SHA-pinned, no oracle images | **M** | Rung 1 passes: image builds, CLI runs, accelerator variant known |
| 2 | `tools/rfd4/modal_app.py` + `run_pipeline.py` — **PDB→CIF conversion, contig and atom-level hotspot construction live here**; `'~metrics'` enforced; byte-identical `_start_beat` | **L** — the biggest step, and where the risk is | Rungs 2–3: a real 8-design run completes, with its wall-clock and its real metrics header |
| 3 | Adapter + preflight, **built but NOT registered**: `tools/rfd4/__init__.py` (defines and exports `adapter`), `shared/pdb_preflight_rules.py` envelope, `shared/pipeline_normalize.py::normalize_for_rfd4` + its mandatory `shared/pdb_preflight.py::_PREVIEW_FN` entry, `gpu/modal_client.py` ceiling, feature flag off. No `meta.py` (see §9 item 10), no `register(adapter)`, no `app.py` import — both are step 7A | **M** | `validate()` coerces the form into the container's `job_spec` and refuses what `run_pipeline.py` would refuse (a subset claim only — the converse still fails in general; the one case it was costing money on, a multi-chain file naming one chain, is CLOSED 2026-11-10 by the container crop, §9 item 11); the slug is unreachable twice over — not in the registry, and `FLAG_TOOL_RFD4` unset |
| 4 | Wallet: `PER_JOB_HARD_CAP_USD`, `_LIVE_ALL_PRESETS`, `RFD4_START_USD` and their pinning tests. **`TOOL_SPECS` and `shared/job_recovery.py` needed no edit** — see the note below | **S**, not M | Numbers come from rungs 3–4, not from `proteina`'s |
| 5 | Results + exports: `shared/result_columns.py`, `shared/score_legends.py`, templates, CIF export path, **plus the two sequence developability terms (GRAVY, instability index) that §7C.6 item 4 put back into release 1** -- they compute hub-side at ingest, so watch the `_sanitize_candidate` allowlist in `webhooks/modal.py` and the score-key allowlist in `shared/job_recovery.py` (which has no rfd4 entry — see §7C.6 item 4) | **M** | Export parity tests pass |
| 6 | Guard-test entries: `test_deploy_paths_exclusions.py`, `test_modal_stale_result_guard.py`, `test_export_sequence_matrix.py`, `test_export_page_parity.py`, `test_signup_credit_covers_smallest_run.py`, `test_live_charging.py`, the deploy workflow matrix | **S**, but **do not skip** — several of these are hardcoded tool lists that fail on a new slug | Full suite green |
| 7 | Catalog and copy: `shared/tools_catalog.py`, `shared/tool_chooser.py`, `shared/email.py`, comparison and guide templates | **M**, customer-visible | **SEO lead sign-off.** Includes the unanswered positioning question: *what does a customer choose between `rfd4`, `proteina` and `rfdiffusion`, in one sentence?* |
| 7A | **Registration.** Add `register(adapter)` to `tools/rfd4/__init__.py` and `import tools.rfd4` to `app.py`'s adapter block, and flip the registry assertion in `tests/test_rfd4_adapter.py::test_the_adapter_is_built_and_the_flag_is_off`. Needs `tools/rfd4/meta.py`, which waits on §9 item 10. **Date open.** | **S** if steps 4–7 are complete; otherwise it is their remainder | Full suite green with rfd4 in `all_adapters()`: no guard left demanding a registry row rfd4 does not have, **and §9 item 11 closed** (DONE 2026-11-10, the container crop) |
| 8 | Flag flip after rung 4 | XS | Leo's go |

**What step 4 codes, in PR #487 — open and not merged as of 2026-11-04 — and the two rows of its own scope that turned out empty.** Three code changes, each pinned by a test in `tests/test_live_charging.py`: `PER_JOB_HARD_CAP_USD["rfd4"] = $25.00`, mirroring the `absolute_cap_usd` already in `TOOL_SPECS` (`::test_the_rfd4_cap_mirrors_its_spec`, which also re-derives a full 2700 s session at the A100-80GB rate as **$4.72** of marked-up compute, so $25 bounds a pricing bug rather than a run); `rfd4` added to `_LIVE_ALL_PRESETS`, its one preset `binder_design` being the only tier the adapter admits; and `RFD4_START_USD = $1.00` with its `live_start_usd` branch (`::test_rfd4_starts_at_one_dollar`). **§9 item 6's $1.00 is decided: Leo confirmed it on 2026-11-04**, after this step's first commit was pushed. It was first coded as proposed-and-unconfirmed and the comments were corrected in the same PR. The three places that disagreed about its status now agree: §9 item 6 is marked ANSWERED and §7.1 leads with the confirmation and its date, each keeping its pre-confirmation reasoning after that lead in a paragraph labelled superseded, because each carries a caveat that outlived the answer; §5.8's verdict paragraph closes with the same date. Its derivation, from rung 4's measured figures (§5.8) and not `proteina`'s: the first design lands at 128.2 s = **$0.2240**, so $1.00 is 4.46× headroom on the first metered design; the whole 8-design run at 217.4 s = **$0.3799**; and the minimum does not scale with `nsamples`, so at the 200-design ceiling the §4.3 fit `65 + 12.3 × N` = 2,525 s = $4.41 is admitted on $1.00 and stopped by the meter, not the gate.

Two rows of the step's own scope needed no edit, and the reason is recorded here rather than discovered again in step 5. **`TOOL_SPECS`**: the `rfd4` spec landed in step 2 (`0572c571`) already carrying rung 4's numbers. **`shared/job_recovery.py` score keys**: the landed container streams no score at all — `run_pipeline.py::_upload_design` returns `{"rank", "name", "pdb_key"}` — and every score column rfd4 will show comes from the separate Boltz-2 leg, whose keys `_candidate_from_partial` already carries. There is no rfd4 score key to allowlist in this step. (Noted, not fixed, because it is step 5's: `_candidate_from_partial` drops `name` for *every* tool, so an rfd4 recovery loses the design name that `_upload_design` streamed.)

**The Boltz-2 scoring leg, settled (closes §7C.4's $0-start hole with a decision, not a constant).** The fold stays a separate `boltz2` job, as §7C.4 requires. Three parts, all three recorded in `shared/wallet_guard.py::live_start_usd`'s docstring and pinned by `::test_the_rfd4_scoring_fold_is_covered_by_boltz2s_existing_cap`: (1) **no cap change** — `compute_hard_cap("boltz2", {"n_designs_total": 8})` is **$3.20** against §7C's measured $0.21–0.31 and a $0.69 ceiling, so boltz2's existing cap already covers the leg; (2) **boltz2's start minimum stays $0**, because a non-zero branch there would refuse standalone boltz2 folds no one asked to refuse; (3) **the hole closes at the spawn**, in the step that builds the chain — the gate needs $0.11 for the first fold record (93 GPU-s) and $0.31 for the whole 8-design fold. No constant is left behind for it: `shared/refold.py::SOURCE_TOOLS` has no `rfd4` entry, so no spawn path exists yet and a constant with no caller would be dead code.

**Why registration is a step of its own (split out 2026-11-04, during step 3).** `register(adapter)` is one line, and it is the line that turns rfd4 into a row every guard iterating `tools.base.all_adapters()` reads. Measured on step 3's branch in both directions over the **whole** suite — `venv/Scripts/python.exe -m pytest -q -p no:randomly`, with `register(adapter)` and the `app.py` import temporarily added and then reverted — nothing else touching the tree: **with** the call, **66 failed, 10147 passed** across **14 modules**; **without** it, on the committed tree with nothing else running, **0 failed, 10212 passed, 33 skipped, 3 xfailed in 481.28s**, exit 0. Registration changes the collected total by exactly **one**: `--collect-only -q` gives **10248** without and **10249** with, and diffing the two id lists names the single addition, `tests/test_guide_bar_matches_legend.py::test_stated_threshold_is_a_number_its_own_legend_publishes[rfd4]`. (The diff also shows one id of `tests/test_gpu_class_drift.py::test_prose_gpu_labels_match_the_wallet_spec` *renamed*: that test is parametrized on the line number of each prose GPU label it finds, and the temporary `register,` line shifted this file's by one. An id, not a test — and its number moves again with any edit above that line, which is why it is described here rather than quoted.) So **65 of the 66 are pre-existing cases flipping from pass to fail**, not new ones appearing. Two earlier drafts of this paragraph are replaced rather than restated: the first said 63 across 14 modules and 24+23 templates, from a partial module set I could not reproduce; the second said `10193 passed` and that the registry adds 20 parametrized cases, which came from a run that overlapped two other pytest processes and collected 10229, 19 short of the committed tree's 10248. Every one of the 66 failure banners is accounted for — and this table is itself a third draft, because classifying pytest output is a trap this milestone has now fallen into twice. A banner split on `_{3,}` silently drops the long test names pytest pads to fewer underscores (54 of 66 found), and relaxing to `_+` over-matches the `_ _ _ _` separators *between nested traceback frames* (169 blocks). The pass below walks banner **lines**, keeps nested frames with their parent block — the real `E` line is in the last frame, not the first — and keys on the first `E` line naming an exception; it returns 66 blocks against the 66 `FAILED` ids with no duplicates, no gaps and no block lacking an `E` line, and the per-module tally sums to 66 across the same 14 modules. A `FAILED` id for a class-based test prints as `TestClass::test_name` while its banner prints `TestClass.test_name`, which is why a naive cross-check reports 28 phantom gaps:

```
 24  TemplateNotFound tools/rfd4_form.html
 23  TemplateNotFound tools/rfd4_results.html
  7  AssertionError: expected 14 adapters, got [...13 slugs...]
  2  AssertionError: discovery missed /tools/rfd4
  1  AssertionError: _tool_label returns the bare slug for: ['rfd4']
  1  AssertionError: no workflow band for ['rfd4']; add them to _TOOL_CATEGORIES
  1  AssertionError: adapters with no _TOOL_CATEGORIES entry (they render under "Other")
  1  AssertionError: catalog bands with no glyph: ['Other']
  1  AssertionError: catalog entries with no metadata resolved
  1  AssertionError: meta.py declares no PILOT for: ['rfd4']
  1  AssertionError: meta.py declares no EXAMPLE for: ['rfd4']
  1  AssertionError: assert 14 == 13
  1  AssertionError: assert {...14 slugs...} == {...13 slugs...}
  1  AssertionError: rfd4 is in the adapter registry  (this PR's own test)
```

So 47 of the 66 are the two templates steps 7 and 5 own, and the rest demand a registry row's worth of customer-visible copy: a catalog band (and the glyph for the "Other" band rfd4 otherwise falls into), an email label, a pilot card, a worked example, and the two `meta.py` maps. Most of that is Leo's and the SEO lead's. One of the 66 is this PR's own `tests/test_rfd4_adapter.py::test_the_adapter_is_built_and_the_flag_is_off`, which asserts rfd4 is **absent** from the registry — it is what goes red if a later step registers the slug without landing the rest.

The house precedent allowed the other reading and is stale: `tools/proteina/__init__.py` landed registered and flag-off in `c03aa1df` naming `form_template="tools/proteina_form.html"`, a file that did not arrive until `ee9c1121` three weeks later, with no proteina entry in `shared/email.py::_tool_label` either. But **not one of the 13 pre-existing guard tests that now go red existed at `c03aa1df`**: 12 of their modules are absent from that tree (checked per path with `git cat-file -e c03aa1df:tests/<name>.py`), and the thirteenth, `tests/test_email_failure_copy.py`, existed there without `test_every_registered_slug_gets_a_label` (`git cat-file blob` on that revision of the file finds no such def). The fourteenth failing module is this PR's own. They have accumulated since, precisely to stop a half-registered tool, so in practice `register()` has come to mean "every registry and every string is complete". Note that this reading is narrower than `shared/feature_flags.py`'s own docstring, which still says the route, form and submission plumbing ship in one commit behind the flag.

**Rollout order is fixed by the deploy workflow:** seed the volumes → deploy Modal → merge the hub PR → flip the flag. One trap: `.github/workflows/deploy-modal.yml` has **no per-app routing** — its matrix of ten apps means a `tools/**` change redeploys the whole fleet. (The trigger paths do negate `tools/**/meta.py`, `tools/**/example/**` and `tools/**/__init__.py`, so a data-only or adapter-only edit does not trigger it — but any Dockerfile or `modal_app.py` edit does, for all ten.) The matrix held nine apps when this section was written; #505 added rfd4's row, and `tests/test_deploy_paths_exclusions.py::test_deploy_matrix_is_the_ten_known_apps` is what holds the count at ten. **Merge only when the fleet is idle** (no pending or running jobs for any tool), per standing practice.

---

## 9. Open questions for Leo

**GPU spend decisions — these are the ones that need an answer to proceed:**

*Status 2026-10-30: rungs 1, 2 and 3 are all complete. Rung 1 ($0) proved the `cu13` image builds (§5.4). The driver check (~3¢) crash-looped on its first attempt and **PASSED** on the re-run (§5.5). Rung 2 (~$0.62) seeded the weights volume and **PASSED** (§5.6). Leo approved rung 3 (~$1.85) and it **PASSED** on the second attempt, for $0.2908 of the $1.85 — real cost, real streaming behaviour and the real metrics header are now measured (§5.7). The next spend is rung 4 (~$1.85), which has NOT been approved.*

1. ~~**Approve rung 1 ($0)?**~~ **APPROVED by Leo 2026-10-30.** CPU-only image build probe; no GPU, no spend. Leo also directed that whatever configuration it proves must be **pinned** — §5.3 is the list.
2. ~~**Approve rungs 2–3 (≈$2.50 together)?**~~ **Both APPROVED and DONE 2026-10-30 (§5.6, §5.7).** Rung 3 cost a measured **$0.2908** of its ~$1.85; rung 2's exact spend was never captured and is only bounded below $0.62 (§5.6), so the pair came in under budget but the total is not a measured figure. Rung 3 answered all three of its questions: ~$0.28 customer for eight designs, designs stream one at a time, and the metrics CSV carries no score column at all. The one thing it did not settle is design *quality* — see §5.7's glutamate caveat. **Rung 4 settled it: on rung 3's own input, the production preset gives glutamate 1.0–12.0%, mean 6.3%, against 70.1% (§5.8 Question 1). The head-to-head that caveat gated was withdrawn by Leo on 2026-11-03 and was never run.**
3. **Approve rung 4 (≈$1.85)? — this is now the open spend decision.** Head-to-head against a recorded `proteina` run, on our own target. It is no longer only a comparison: rung 3's binder came out 70% glutamate under the `quickstart` preset (§5.7), so rung 4 is also the first run that would tell us whether a production configuration produces credible sequences. Recommended.
4. **Rung 5 (≈$5.55)?** Defer until after the go/no-go.

**Product decisions:**

5. ~~**No confidence number on the results page.**~~ **DECIDED by Leo 2026-10-30: use our own scoring, chained through Boltz-2 refolding.** §7B is the design. It raises one new question that is genuinely Leo's: our own Boltz-2 measurement spans **ipTM 0.263–0.830 on byte-identical input** (§7B.3), so an unseeded single fold cannot rank anything either. I recommend seeding, then folding each design three times and showing the spread. That multiplies the scoring cost by three — confirm you want that before rung 3 prices it.
9. **Is upstream's `af2` oracle route open to us?** AF2 parameters have been CC BY 4.0 since the 2022-12-06 release, so the restrictive one is AF3, not AF2 (§7B.1). The brief excluded the optional `alphafold2/runtime` image on separate terms, which may or may not close the question. $0 to answer, and it would recover the `min_ipae_coi` term Boltz-2 does not give us.
6. **Start minimum — ANSWERED. Leo confirmed $1.00 on 2026-11-04**, and step 4 codes it as `RFD4_START_USD` in `shared/wallet_guard.py`, with the `live_start_usd` branch and the `STARTS` test entry that make it bind (PR #487, open and not merged). One thing rung 4 added survives the answer: the headroom is not a fixed quantity, because it varies with the spread of binder lengths.

    *Pre-confirmation reasoning (superseded by the line above, kept for that caveat).* $1.00 proposed, and rung 3 has now supplied the missing input: designs stream one at a time from 68.1 s, so $1.00 covers the pre-first-design stretch roughly eight times over (§7.1). **Rung 4 re-checked it (§5.8 Question 3): the production first design lands at 128.2 s, $0.2240 of customer money, so $1.00 holds at 4.46× headroom against rung 3's 8.40×.**
7. **Positioning.** Three binder tools on the shelf. What does a customer choose between them, in one sentence? This blocks step 7, not the engineering.
8. **Alpha software, four weeks old, 7 open issues.** Are we comfortable selling it? My read: yes, behind a flag and after rung 4, because the licence is clean and the downside is a refund, not a liability. Leo's call.
10. **There IS a preprint; this document's earlier "never a paper" was false. The citation URL is DECIDED (2026-11-04, Leo); the author/year half and the guard's identifier shape are not.** Corrected 2026-11-04 during step 3, by reading the two upstream sources at the revisions we actually pin rather than this document's own summary of them.

    **What the two upstream sources say, and they do not agree.** The code repo's README has a `## Citation` section — `@article{corley2026rfd4proteina}`, *Biomolecular design through latent reasoning with RFD4-Proteina*, `year = {2026}`, `note = {Preprint}`, authors Corley, Didi, Kazman, Brent and others, `url = {https://rfd4-proteina.org/assets/paper/RFD4-Proteina.pdf}`. It is present **at the SHA we pin**, not something upstream added afterwards: read with `gh api 'repos/RosettaCommons/RFD4-Proteina/contents/README.md?ref=aaccdd2ad0720c180468df5f9cf037a713d403e5'` (the SHA at `tools/rfd4/Dockerfile.modal:34`). The §1-§3 research pass simply missed it, which is the first of the two possibilities this item used to name. The Hugging Face model card at the weights revision we pin (`a06e2509c7033b010fc43614e9fd840303588502`, `tools/rfd4/seed_volumes.py:81`) asks for something else: `@software{rfd4_proteina_2026}`, authored by the two **organisations** with no individuals named, `url` the HF model page, and a `note` pinning model revision `cf31b935…` — which is not the revision we pin. That card also links its code as `github.com/baker-laboratory/RFD4-Proteina-dev`, which `gh api` returns 404 for, while the code we pin is `RosettaCommons/RFD4-Proteina`.

    **Who publishes it.** Not one of the two: the weights, the training dataset, the Diffusers pipeline and both Spaces are all under the Hugging Face org `nvidia/`; the code is under `RosettaCommons`; the model card's licence line is BSD 3-Clause, "Copyright (c) 2026, Institute for Protein Design, University of Washington"; and both the README and the card carry the byline "Baker Lab × NVIDIA GenAIR". The project's own page, declared in the repo's `homepage` field, is a third thing — `https://rfd4-proteina.org/` — and it returned **503** when checked on 2026-11-04, though the preprint PDF on that same domain returned 200 (54.8 MB).

    **DECIDED (2026-11-04, Leo), in two parts.** The first decision, taken before the premise above was checked, was to cite the upstream project's own web page and to leave `FIRST_AUTHOR` and `PAPER_YEAR` empty because there is no paper. Once the pinned README's `@article` block was read that premise fell, the question went back to Leo, and he decided again the same day: **cite the preprint** — Corley et al. 2026, *Biomolecular design through latent reasoning with RFD4-Proteina*, as the pinned README's BibTeX gives it. So `FIRST_AUTHOR` is **`Corley`** and `PAPER_YEAR` is **`2026`**, and the model card's organisation-only `@software` entry is not what we cite. **Still OPEN, and the registration step's:** the preprint has no DOI, no bioRxiv id and no arXiv id, so a `REQUIRED_URL_TOKENS` entry of the shape the guard's other 14 carry does not exist for this tool; whether the guard grows an affordance for a citation with no persistent identifier, or takes a project-page token, changes what it proves for the other tools and is not a worker's call. **Step 3 still ships no `tools/rfd4/meta.py`**: the guard keys off that file's existence, so not creating it remains the only state that invents nothing, and the two decided values wait in this document until the step that writes that file.
11. **An extra polymer chain in the upload passes every hub gate and then fails in the container. Which side gives?** `run_pipeline.py::resolve_target_chain` `_fail`s when the upload holds any polymer chain besides the named one ("Crop the structure to one chain and resubmit"). Nothing hub-side mirrors that: `validate` never opens `files`, and `shared/pdb_preflight.py::_multi_chain_block` keys on the count of chains NAMED IN THE FORM, not on the chains present in the file. Measured on step 3's branch: a two-chain PDB with one chain named returns `READY` for rfd4 — **and for `rfantibody` and `bindcraft` too**, so the preflight is behaving to house convention; what diverges is the container, whose siblings' normalizer crops to the named chain instead of refusing the file. The customer case is the ordinary one for binder design: upload a crystal dimer, pick the antigen chain, get funded for a run that cannot start. Pinned by `tests/test_rfd4_adapter.py::test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops`, which fails the moment either side moves. Step 3 did not close it because both fixes are outside it — refusing hub-side needs new customer-visible copy (and `_multi_chain_block`'s existing wording says "named", which would be false here), and cropping in the container edits `tools/rfd4/run_pipeline.py`, which redeploys the whole GPU fleet. **DECIDED 2026-11-10 by Leo: the CONTAINER crops** -- matching what the sibling tools’ normalizers already do -- **with no hub-side refusal and no preflight change.** Direction of the fix, so it is not re-litigated: the hub keeps staging the whole converted upload and keeps returning `READY`, and `tools/rfd4/run_pipeline.py::load_target` crops a PDB upload to the named chain before `pdb_to_cif` converts it, so `resolve_target_chain` sees one polymer chain. The refusals that stay are the ones that genuinely cannot proceed: no polymer chain at all, a named chain the file does not hold, a multi-chain upload with NO chain named, and a chain split across asym units. An mmCIF upload is not cropped -- there is no CIF writer in that container and the four hub paths that stage a target convert `.cif`/`.mmcif` to PDB before staging (`blueprints/tools.py:1773`, `:2193`, `blueprints/prep.py:132`, `shared/pdb_intake.py:563` -- those four are cited, not enumerated) -- so the multi-chain mmCIF refusal remains. Hotspot identity is unaffected by construction: `pdb_to_cif` counts `label_seq_id` per asym from 1 over that asym’s own polymer residues, so dropping other chains cannot move the kept chain’s label ids, and the crop does not renumber (matching the `renumber_residues=False` of the rfdiffusion / proteina / rfantibody / bindcraft / rfd4 presets; `normalize_for_pxdesign` and `normalize_for_boltzgen` do renumber, so it is those five and not a house-wide rule). Both halves are now pinned by the renamed `tests/test_rfd4_adapter.py::test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops` (gates still `READY`, container crops) and by `tests/test_rfd4_pipeline.py::test_cropping_a_dimer_leaves_the_kept_chains_label_numbering_identical`. **No longer a blocker for step 7A.** The crop ships in a PR of its own, which redeploys all **ten** Modal GPU apps -- rfd4 included -- and needs an idle-fleet window. The count was nine while this item was being written; #505 (merged, main `613aa2e2`) added rfd4's matrix row, pinned by `tests/test_deploy_paths_exclusions.py::test_deploy_matrix_is_the_ten_known_apps`, so the crop now deploys itself rather than only the other tools. CLOSED. Two residuals are recorded rather than fixed, both outside this decision's direction: (a) a multi-chain upload with the chain field left BLANK is still staged, funded and then refused in the container ("no target chain was named"), because cropping on the `"A"` that `validate` substitutes would design against whichever protomer is chain A -- `templates/tools/rfd4_form.html:177` carries no `required`, and closing it hub-side is exactly the refusal this decision rules out. What that residual costs was measured rather than assumed: `_fail("input", ...)` writes bucket `input`, which `_ERROR_BUCKET_TO_FAILURE_CLASS` does not carry, so `classify_terminal_state` returns `"unclassified"` (`shared/jobs.py:831-858`, `:917`), and `"unclassified"` is not in `_BILLED_FAILURE_CLASSES` (`shared/jobs.py:807-818`), so `is_billed_failure_class` is False (`:931-933`) and the class settles as a refund. The wallet hold and the GPU start are still spent, and whether the settle path honours that class on this row is not something this change exercised -- no run has taken this path; (b) a multi-chain mmCIF is still refused rather than cropped, and a CIF writer in that container is the fix if a customer path to one ever opens.

---

## 10. UNKNOWNs and UNVERIFIEDs that must close before step 2

1. ~~**Does Modal's A100-80GB present a CUDA-13-capable driver?**~~ **ANSWERED 2026-10-30, yes.** Driver 610.57.04 on an A100-SXM4-80GB, sm_80, a real kernel launch, `rfd4-generate --help` exit 0. Cost ~3¢ (§5.5).
2. ~~**Real wall-clock and cost of an 8-design run on our hardware.**~~ **ANSWERED 2026-10-30: 159.2 s, ~$0.28 customer, ≈ `65 + 12.3 × N` seconds (§5.7).** Measured eager, with no `torch.compile` warm-up and no batching, so the per-design figure is an upper bound and the startup a floor.
3. ~~**Do structure files land per design or only at run end?**~~ **ANSWERED 2026-10-30: per design**, one every ~12 s from 68.1 s onward (§5.7). Save-as-you-go is feasible. The watcher must skip the hidden `.generated-cif-*` temp directory each design is written through.
4. ~~**The real `per_example_metrics.csv` header.**~~ **ANSWERED 2026-11-03 (§5.8).** 44 columns with no metrics group composed (rung 3), **89 columns with `metrics_tags=[design,interchain_interactions,hotspot_conditioning]`** (rung 4). The **46** added columns are the composition, bond-length, clash, hotspot-obedience, interchain-interaction, sequence-adherence, sequence-probability-adherence and SSE groups plus `pre_oracle_pass` (net +45: rung 4 carries 32 `generation.*` columns against rung 3's 33, and which one dropped is unrecoverable) — and **still no refold, confidence or ipTM column**, which is hard confirmation of §7B.
5. **PDB→CIF conversion and `label_seq_id` numbering.** Upstream resolves residue ids against mmCIF `label_seq_id`, not author numbering. Our whole stack speaks author numbering. **This is the most likely source of a silent wrong-residue bug** — the same class as the `proteina` hotspot defect already on record. Needs a deliberate test, not a code read.
6. ~~**Hotspot atom-set expansion.**~~ **DECIDED 2026-11-04 by Leo: option B**, sidechain heavy atoms with `CA` for glycine, intersected with the atoms the structure actually contains. That is what `RFD4_HOTSPOT_ATOM_SET = "sidechain_heavy"` and `hotspot_atoms_for_residue(resname, observed_atom_names)` in `tools/rfd4/numbering.py` already implement, so the decision confirms the landed code rather than changing it. **D is the upgrade path** if B is ever shown to over-constrain; **C must not ship.** §10A keeps the options table and the reasoning as the record of what was weighed. Still true and unchanged by the decision: **no run has compared the options**, so B is chosen on reasoning, not on measurement.
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

## 10A. §10 item 6: which atoms a hotspot should name -- **DECIDED**

Added 2026-11-02 by the numbering/conversion pass (`tools/rfd4/numbering.py`), which
laid out the options so the decision could be made deliberately. **Leo decided on
2026-11-04: option B, intersected with observed atoms** (§10 item 6). Everything below
is the record of what was weighed, kept because the decision rests on it -- the options
table and its reasoning are not superseded by the choice. What changed is the framing
only: this section no longer asks the question, and the "Decision" heading below
replaces what read "Recommendation ... this is a recommendation, not a decision".

### What the gap actually is

Our picker emits residues. A customer clicks a residue on the viewer and we carry
`"B52"` — a chain plus an author residue number, and nothing finer
(`shared/pdb_inspect.py:315` parses exactly that shape). Upstream's condition spec
addresses atoms: `sel('A/*/56/[CG,OH]')` (§4.1). So something between the picker and
the container has to invent an atom set per picked residue. Whatever it invents is a
scientific choice about how much of the target surface the model is told to bury.

Nothing below has been compared on a GPU. There is no run, ours or upstream's, that
measures one option against another on the same target.

### Where it is decided in code

One constant and one function, both in `tools/rfd4/numbering.py`:

* `RFD4_HOTSPOT_ATOM_SET` — the name of the policy. Currently `"sidechain_heavy"`.
* `hotspot_atoms_for_residue(resname, observed_atom_names)` — the policy itself.

Changing the policy means editing both: the function refuses any value of the constant it does not implement, so the constant on its own is a label, not a switch. No caller spells out atom names, so
no caller needs to change. Step 2 will call it once per picked residue and pass the
result to `format_hotspot_selection`.

### The options

| Option | What it emits for one picked residue | Consequence |
|---|---|---|
| **A. `CA` only** | One atom | Weakest steering, and it cannot over-constrain. (The only upstream example this document records is `sel('A/*/56/[CG,OH]')`, §4.1 — two atoms, not one, so upstream's own usage is no argument for A.) But a 4-residue epitope becomes 4 points, which is a very thin description of a surface patch, and the customer's mental model ("bind *here*") is a patch |
| **B. Sidechain heavy atoms, `CA` for glycine** | 1–10 atoms, by residue type | What the code does today. Names the part of the residue that actually makes the contact and skips the backbone, which in a well-packed target is not solvent-facing. Costs: a buried or poorly-resolved sidechain contributes atoms that cannot be contacted, and the atom count varies 10-fold across residue types (TRP yields 10, ALA and GLY 1 — measured by calling `hotspot_atoms_for_residue` on a full residue, 2026-11-02), so a TRP-heavy epitope is weighted far more heavily than a GLY/SER one |
| **C. All heavy atoms** | 4–14 atoms | Most literal reading of "this residue". Strongest constraint, and the one most likely to over-specify: it asks the binder to approach backbone atoms that may be inaccessible, and §10 item 11 already records an open upstream quality bug in binder mode. Highest risk of a design that satisfies the spec and is nonsense |
| **D. Curated functional group per residue type** | 1–3 atoms, from a hand-written table (TYR `OH`; ARG `NE,NH1,NH2`; ASP `OD1,OD2`; …) | Closest to how a structural biologist would describe an epitope, and the most defensible per-residue weighting. Costs a table that is a scientific artefact needing its own review, and it has a judgment baked in for all 20 residue types rather than one |
| **E. Expose it to the customer** | Their choice | Pushes an undecided scientific question onto someone with less context than us, and adds a form field, a preflight rule and copy. Not for release 1 |

A cross-cutting consideration for every option except A: the atom set must be
intersected with the atoms the structure **actually contains**, or a selection names
an atom upstream cannot resolve. `hotspot_atoms_for_residue` takes the observed atom
names for exactly that reason.

### Does upstream error on an unresolvable atom name, or silently drop it? **SILENT DROP** (answered 2026-11-04)

**Answered by reading the source at our own pin -- no container was started and no GPU
second was spent.** It drops, and nothing is reported.

* **The call site.** `src/rfproteina/inference_utils/condition_spec.py::_apply_conditions`
  at our pinned `RFD4_SHA` `aaccdd2ad0720c180468df5f9cf037a713d403e5`
  (`tools/rfd4/Dockerfile.modal`) resolves each entry's `select` string through
  `resolve_mask(atom_array, select)` and writes at the resulting indices. An empty
  index array is a no-op write. There is no emptiness check on either the
  `C_CRDGRP` path or the per-value path, and `raise_on_empty` does not appear in
  `condition_spec.py` or `mask_spec.py`.
* **Why it cannot raise.** `src/rfproteina/utils/mask_spec.py::resolve_mask` returns
  `QueryExpression(query).mask(atom_array)`, and in `atomworks`
  `src/atomworks/io/utils/query.py::QueryExpression._build_functions` the `sel` entry
  is `lambda selection_str: AtomSelectionStack.from_query(selection_str).get_mask(atom_array, raise_on_empty=False)`,
  with upstream's own comment that it is non-raising on an empty match so it composes
  inside boolean expressions. The flag's **default** is `True` and
  `get_mask_from_atom_selection` does raise `ValueError("No atoms found for
  selection: ...")` when it is set -- but the `sel(...)` query path, which is the path
  RFD4 uses, never reaches that default. The behaviour is a property of the call site,
  and our call site is the non-raising one.
* **Per-token and whole-selection misses behave identically.** `from_query` expands
  `[CG,OH]` into two members and unions them, passing the same flag to each: if `CG`
  is absent and `OH` present, `OH` alone is selected; if both are absent the mask is
  all False. Either way nothing is reported.
* **Read at:** `atomworks` commit `1a9fa25fec3709b73493edfaba31d131dabddbbc`, which
  PyPI gives as the 3.0.4 publish commit. Upstream's `pyproject.toml` at our pinned
  SHA constrains `atomworks[ml,openbabel]>=3.0.4,<4` and carries no `uv.lock`, so
  **the version resolved into our built image is not pinned and was not read**;
  3.0.5 and later are **UNVERIFIED**. Also unread, and so also unverified:
  `annotator.py` (whether `sel` is in `ANNOTATOR_REGISTRY`), any post-run validation
  in `conditions_reporting.py` that might flag an empty selection, and upstream's own
  docs on empty-selection behaviour. Locations above are by symbol, not line: the
  fetch returned processed text.
* **This is the same failure shape as §10 item 5**, and it confirms the precedent:
  proteina's upstream drops an unmatched residue token without complaint
  (`tools/proteina/run_pipeline.py:706-715`), and RFD4's does the same for atoms.
  **It is not a defect in the landed path**, because option B's intersection with
  `observed_atom_names` means our code cannot name an absent atom by construction.
  It is a robustness finding: the intersection is load-bearing, since removing it
  would fail silently rather than loudly.
* **One thing the same read turned up, worth more than the question was.**
  `_apply_conditions` swaps `res_id` back to the **pre-contig** numbering before
  evaluating selections, so **a residue number that a contig renumbered is also a
  silent miss** -- no error, just a hotspot that conditions nothing. That is §10
  item 5's failure mode arriving by a second route, and it is **UNVERIFIED against a
  run**: it is read off the source, not observed.

### Decision (Leo, 2026-11-04)

**B, intersected with observed atoms, with D as the upgrade path if B ever proves to
over-constrain. C must not ship.** The reasoning below was the recommendation and is
now the record of why the decision went that way. It was decided on reasoning, not on
measurement: the comparison experiment described at the end of this subsection **was
not run**, and no GPU spend for it has been approved or requested, so nothing here
measures B against A or C.

The reasoning, unchanged from when it was a recommendation: B is what a contact actually involves, it needs no
new scientific artefact to review, and it degrades safely — a buried sidechain adds
atoms the model cannot reach, which weakens the constraint rather than corrupting it.
A is the safe fallback if any evidence appears that multi-atom selections
over-constrain. C should not ship, and the decision above makes that binding.

**§10 item 6 is closed by the decision above; do not re-open it.** What would
legitimately move it is evidence that B over-constrains, which would make it D's turn.
The measurement that was never run: one target, one epitope, the same seed, three runs under A, B and
C, scored through our Boltz-2 chain (§7B) and compared on whether the designed binder
contacts the picked residues. That is GPU spend nobody has approved.

### What else stays open

§10 item 5 (PDB→CIF and `label_seq_id`) is **not closed** by the numbering pass
either. That pass tests our mapping on a corpus -- three deposited structures plus
crafted cases -- and the suite fails under an identity mapping; the polymer-eligibility
conformance test says of itself that it is a corpus, not a proof. It does not prove upstream's reader agrees with the mmCIF
we write — nothing produced by `tools/rfd4/numbering.py` has been through
`rfd4-generate`. Accepting the generated CIF in the container is a step-2 gate.

---
## 11. What the 2026-10-30 research pass did not do

This section is about that pass only; §10A records a later one that does add code. The research pass changed no product code, deployed no Modal app and edited no customer-visible string. GPU work was limited to the validation ladder Leo approved rung by rung: the free CPU build probe (§5.4), the ~3¢ driver check (§5.5), rung 2's weights seed (§5.6, bounded below $0.62, exact spend not captured), rung 3's run (§5.7, a measured **$0.2908**) and rung 4's single run (§5.8, one A100 container, 217.4 s, **$0.18659948 raw** from a billing read, matched on the app id §5.8 records — UNVERIFIED only in that no billing artefact is committed). Rung 4 also ran two CPU-only probe functions that carry no `gpu=` and no `volumes=`. A Modal Volume named `rfd4-proteina-weights` holding 2.59 GiB now exists in Leo's account; **delete it if RFD4 is dropped**, and note its flow checkpoint was renamed to `rfd4_proteina_flow-ema.ckpt` during rung 3.

One thing it got wrong, found 2026-11-04 during step 3: it read the upstream README and reported that upstream cites no paper. The README has a `## Citation` section, and it is there at the SHA we pin. §9 item 10 carries the correction and the two conflicting upstream citation blocks; the lesson for the next pass is that a "never" about a document we hold is a grep, not a reading impression.

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

### 12.2 Rounds 1 and 2 — on the step-3 delta (`0572c571..` the adapter branch)

Step 3 is the first delta in this milestone that is mostly code: the hub-side adapter, the preflight rules row, the ceilings, and the corrections to §4.2, §7C.6, §8, §9 and §10A that the implementation exposed. Both saved reviewers ran twice, base `0572c571` each time. Every finding below was re-verified against the repo before it was accepted; two were rejected on that re-check and are recorded as such, because a reviewer's cite is a claim.

**`review-code` round 1: 2 findings, 1 confirmed, 1 rejected on re-check.**
This table is the dated record of that round; the first row's "reaching a
container that refuses it" was true on 2026-11-05 and is not now. Item 11 was
CLOSED 2026-11-10 -- see §9 item 11 for the decision and what replaced the
refusal.

| Finding | Fix |
|---|---|
| A PDB holding a second polymer chain passes `validate` (which refuses a multi-chain *naming*, not a multi-chain *file*) and passes `_multi_chain_block` (`shared/pdb_preflight.py:1074`, which counts the chains **named on the form**), reaching a container that refuses it — so once the slug registers, a customer pays the start minimum for zero designs | Confirmed and pinned rather than fixed: closing it is a decision about which layer crops, which is Leo's. §9 item 11, a blocker on step 7A's `register(adapter)`, and `tests/test_rfd4_adapter.py::test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops` fails the moment the gates start refusing it |
| The runtime terms under-quote large jobs | REJECTED. Reading §5.8's production fit gives ≈1005 s at N=200 against a 2670 s ceiling, so the envelope is conservative at the top of the range, not optimistic. The terms are still worth re-fitting (Follow-ups), but not for this reason |

**`review-claims` round 1: 5 FALSE, all confirmed and repaired, plus 4 UNVERIFIED.** The repair of UNVERIFIED #4 was itself wrong in its first draft — I wrote the two runtime fits as diverging in one direction when they cross near N=10 — which is the fourth time in this milestone that fixing a claim introduced a new one.

**`review-claims` round 2: ~45 claims enumerated, 2 FALSE, 4 UNVERIFIED, round 1's nine not re-raised.** Both FALSE held, and the first was wrong in a *second* way the reviewer did not catch.

| Finding | Fix |
|---|---|
| The multi-chain comment cited §5.8 for the two-chain variant sweep; §5.8 carries no such table | The sweep is in `docs/scoping/RFD4-RUNG4-RESUME-2026-11-02.md`. Reading it to fix the cite showed the substance was also wrong: there are **six** variants, **V1/V2/V5** failed upstream's `index_group_molecule_must_be_connected` invariant and **V3/V4/V7** returned 0 and built a CIF. So the measured rule is **at most one *indexed* target chain** — "one chain" is contradicted by V3 and V7 passing with the second chain unindexed. And the stronger reason sat unused in the same document: `C_IDXGRP`, the condition that would express a multi-chain rigid target, is Python-API only and absent from the YAML condition table, so the case is **not expressible** through the spec format we drive upstream with, rather than merely unknown. Corrected at all three sites — the `_RFD4` comment, the `tests/test_pdb_preflight.py` bullet, and §4.2's row. Two items stay UNVERIFIED: the mechanism that judges the two protomers one molecule, and the V2–V4 rows, whose result JSON the V5/V7 sweep overwrote |
| "a default-size job estimates 2.7 min" — size-specific and undefined | Eight designs give **2.7217 min at the measured 115 aa target** and **2.8400 min at the 120 aa anchor the terms are expressed against**; both fall under `runtime_estimate_min`'s closing `max(5.0, est)`, so the conclusion held at both anchors while the number named neither. Both are now named |
| §9 item 10 said the citation guard's docstring records five past misattributions, and that each token is "a real DOI or arXiv id" | Both undercounts. The docstring records **six**; enumerating all 14 `REQUIRED_URL_TOKENS` entries shows publisher DOI suffixes, bioRxiv dated ids **and** arXiv ids — `iggm`'s is bioRxiv `2024.09.19.613838`, matching that docstring's own note that IgGM has no arXiv version |
| The `63`/`0` registration figures were labelled as measured, but the prose never listed the modules, so "9 of the 10" could not be re-derived | Re-measured rather than restated, over the whole suite with `register(adapter)` and the `app.py` import temporarily added. The replacement figures and the command that produced them are in §8's registration note; the old numbers are gone because I could not reproduce them |

**`review-code` round 2: 2 findings — 1 confirmed (the same gap), 1 resolved as cross-run interference — and one observation from its *clean* list that earned a code change.** It did not open `tests/test_preflight_panel_contract.py` (+116 lines) and treated this document as out of scope, so this is an incomplete read of the delta rather than a clean one.

| Finding | Fix |
|---|---|
| The multi-chain gap again, with the money mechanism spelled out: the staged bytes are the whole converted upload, not a chain-cropped file, so the sequence on registration is hold taken → GPU starts → `resolve_target_chain` fails → start-minimum charge, zero designs | Already pinned and recorded (§9 item 11). The §4.2 row's claim that `validate` "refuses it one layer earlier" was tightened — a claim neither reviewer raised and I found by re-reading the neighbour text of my own repair |
| "the full suite is not green — 4 failures, cause UNVERIFIED" | Resolved: two full-suite runs overlapped on the repo-root `tmp/`. The reviewer's four and mine are **disjoint** sets; each of the eight passes when its module runs alone; all **28** `tests/test_scout*.py` together give 792 passed, 8 skipped (re-measured 2026-11-04 after review-claims round 3 flagged the count: `ls tests/test_scout*.py | wc -l` gives 28, and the pass/skip figures reproduced exactly, so only the module count was ever wrong); and the delta touches no `scout/` file, no `tests/test_scout*` and not `tests/conftest.py`. The shared state is `TMP = Path("tmp")`, and `test_the_sweep_leaves_the_repo_alone` asserts exactly what a second pytest process violates. Follow-up: that shared relative path makes the scout modules order- and concurrency-sensitive |
| *From the clean list:* step 2's stamp guard, `tests/test_rfd4_pipeline.py::test_the_adapter_stamps_the_key_the_wallet_scales_on`, is a source-text grep for the literal `"n_designs_total"` — it would pass even if the key were stamped where `_effective_scaling_value` never looks | The brief said make it pass without weakening it, so it is untouched and an executing guard was added beside it: `tests/test_rfd4_adapter.py::test_the_wallet_actually_reads_the_stamped_key_off_the_job_spec` runs `validate(num_designs="200")`, asserts the wallet lookup returns `200.0`, and pairs that with a control that strips the key and asserts the silent fallback to the baseline is still reachable |

Two cautions. First, round 2 found two false claims that round 1's claims pass had read and not flagged, and the first of those was wrong in a second way round 2 also missed — only reading the cited source document exposed it. Rounds 3, 4 and 5 each then surfaced a defect that an earlier round's repair had introduced — the pattern starts at the first repair, not at the first round. Second, `tests/test_preflight_panel_contract.py` has not been opened by a code reviewer in either round, and is the known hole in this delta's coverage.

### 12.3 Rounds 3 and 4 — the same delta, after the round-2 repairs

**`review-code` round 3: zero findings.** It read all eleven code and test hunks in full, including `tests/test_preflight_panel_contract.py` (+116), which was round 2's known coverage hole and is now closed. It recorded twelve attack attempts that each failed, and ran two test batches itself (397 passed / 10 skipped, and 349 passed). Its own caveat: coverage is weakest on `normalize_for_rfd4`'s rfd4 branch and on everything behind `FLAG_TOOL_RFD4`, neither of which a flag-off delta exercises — recorded as a follow-up for the step that flips the flag, not as a finding here.

**`review-claims` round 3: ~70 claims counted, 46 checked, and verdicts reported for 39 — 31 VERIFIED, 4 FALSE, 4 UNVERIFIED.** Round 5 caught the arithmetic in the sentence that stood here: 31+4+4 is 39, not 46, so seven of the checked claims carry no verdict in that round's report, and these figures are quoted from it rather than re-derived. The gap cannot be closed after the fact — the file that round wrote is 0 bytes, so its report survives only in the session transcript. All four FALSE were confirmed against the repo and repaired in `c836f3db`.

| Finding | Fix |
|---|---|
| "Six **two-chain** variants", at three sites | Five of the six name two target chains; **V4 names chain A alone** and is the one-chain control, so it is evidence the harness works and not evidence about either rule. The at-most-one-*indexed*-chain rule is now sourced to **V3 and V7**, which are exactly the two that passed carrying a second chain unindexed (V3 on both chains, V7 on B only) |
| `validate()`'s docstring concluded that a run the container cannot start is "never funded" | The subset claim is true, the conclusion is not — and the test that pins the gap is in this same delta. The docstring now states the converse explicitly, names `tests/test_rfd4_adapter.py::test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops`, and points at §9 item 11. §8's step-3 row is tightened the same way |
| The module docstring said every bound is "quoted from it by symbol rather than restated" | They are restated literals with the container's symbol in a trailing comment — two files, not one import. The docstring now says so and names `test_adapter_bounds_are_the_containers_bounds` as the drift guard |
| "all 20 `tests/test_scout*.py` give 792 passed, 8 skipped" | Re-measured rather than restated: `ls tests/test_scout*.py | wc -l` gives **28**, and the pass/skip figures reproduced exactly. Only the module count was ever wrong |

Four claims stayed UNVERIFIED, and **one of them turned out to hide two real defects** that neither reviewer named. Re-deriving the 66-banner table instead of restating it gives **24** `rfd4_form.html` failures, not 25, and shows the table omitted a tenth one-count row — this PR's own registry test. Those two errors cancelled, which is why a wrong table still summed to 66; "48 of the 66" is **47**. The first draft of that table had "24+23" right, so my own earlier edit to 25 was the regression. The table now records how it was derived, because classifying pytest output has been a trap twice in this milestone: splitting banners on `_{3,}` silently drops the long test names pytest pads to fewer underscores (54 of 66 found), and relaxing to `_+` over-matches the `_ _ _ _` separators *between nested traceback frames*, where the real `E` line actually lives (169 blocks). Three uses of "unconditionally" about `shared/job_recovery.py` are also tightened: `iptm`, `plddt` and `i_pae` are each gated on `is not None` at `shared/job_recovery.py:78-100`.

**`review-code` round 4: no defect meeting its evidence bar, on a read that closed round 2's hole.** It read all eleven changed files, widened only to the callers and consumers of the changed symbols, and — unlike either earlier code round — published what it verified rather than only what it found, so the clean verdict is auditable. It reproduced the container/adapter constant table item by item (including `runtime_ceiling_s=2670` against `PRESET_CAPS[("rfd4","binder_design")]=2700`), confirmed that the stamped `n_designs_total` is read by `_effective_scaling_value` so the silent eight-design fallback is **not** taken, confirmed that `_parse_preflight_size_params` feeds real numbers to the size and runtime gates on the **submit** path and not only the AJAX panel, reproduced the 2.7217 min / 163.3 s runtime arithmetic against §4.3's 163.4 s fit, and attacked the claim that the envelope errs safe against §5.8's production fit without finding an `N` that breaks it — the bucket term `B` is drawn from upstream's fixed `compile_lengths` list and is bounded near ten, not by `N`. It ran the full suite in this worktree: **10212 passed, 33 skipped, 3 xfailed**.

It re-raised the two pre-conditions this delta already records rather than finding new ones, and both are unreachable while the flag is off and `register()` is absent: the multi-chain money gap (§9 item 11), and the two template paths `tools/rfd4/__init__.py:273-274` names — `templates/tools/rfd4_form.html` and `rfd4_results.html` do not exist (`ls templates/tools/` matches no rfd4 file), so registering the adapter without them 500s the form route. Round 5 found two errors in the sentence that stood here: the lines are **273-274**, not 277-278, and it claimed that pre-condition was "now listed with the first as a registration blocker" when no such listing exists — §9 ends at item 11 and holds decisions for Leo rather than work items, and §8's step-7A gate column (`:1102`) names no template pre-condition — it asks for a green suite with rfd4 in `all_adapters()` and for §9 item 11 to be closed, and nothing else. The templates are recorded here and in step 3's PR body; giving them a gate of their own in the step-7A row is step 7's edit, not this one's. It also judged this delta's one deliberately weakened guard acceptable: `tests/test_preflight_panel_contract.py` went from `len(_CIF_SLUGS) == 7` to `>= 7`, because the exact count was an import-time failure on a new slug while the assertion's stated purpose — never let an empty parametrize pass silently — survives the relaxation.

**`review-claims` round 4: ~45 claims enumerated, 39 checked — 33 VERIFIED, 1 FALSE, 5 UNVERIFIED.** The one FALSE was mine and was introduced by round 3's own repair: the `_RFD4` comment, having just corrected its own mis-citation of §5.8, went on to assert that `tools/rfd4/run_pipeline.py::resolve_target_chain`'s docstring "repeats the same wrong citation". It does not. Read first-hand — `sed -n '638,662p' tools/rfd4/run_pipeline.py` — that docstring cites **scope §4.2**, and `grep -n "5\.8"` in that file hits nothing related. It *is* stale, differently: it names only V1 and V5 as failing, and it asserts that "no committed artefact shows upstream accepting one", which V3 and V7 contradict. All three sites now say that instead, and the PR body's two restatements of the same false claim were corrected with them.

One UNVERIFIED was worth more than its label. The three comment sites stated V2's `index_group_molecule_must_be_connected` failure as fact, and the reviewer could not find the trace. Neither could I, because there isn't one. Two runs have a committed traceback: V1's is in `rung4-probe/cpu_result.json` (`write_cifs_rc: 1`, `built_cifs: []`, which the rung-4 resume note attributes to V1), and V5's is in `rung4-probe/variants_result_v5v7.json` under `spec_v5_nocrdgrp.yaml`. Round 5 corrected how an earlier draft of this line argued that. The traceback the resume note quotes is labelled `ValueError: fc_binder:` — and **that label does not identify the run**: V5's log tail carries the same label with the same 23088-pair body, which is what the resume note means by "byte-identical". The attribution rests on the artefact, not the label. `grep -rl` finds the invariant's name in nine committed files, of which two are run artefacts and the rest are specs, code and prose, so "the only other committed occurrence" was wrong as well. V2 returned rc 1 and built no CIF, but **its result JSON was overwritten by the V5/V7 sweep**, so reading V2 as the same invariant failure is inference from an exit code. The three sites now show the inference as inference. The remaining four UNVERIFIED were checked and left: one was the §7C.6 provenance of two `scripts/` paths that are not in this repository — §7B.5 does disclose it, and item 4 now forwards that disclosure rather than relying on the cross-reference.

The pattern across these rounds is the finding to keep, stated as narrowly as the evidence allows: **rounds 3, 4 and 5 each surfaced a defect that an earlier round's repair had introduced** — round 3's by way of an UNVERIFIED that was then chased, round 4's and round 5's as outright FALSE findings. Round 1 had no previous repair to inherit one from and round 3's code pass found nothing, so neither is an instance, and round 5 caught the sentence that stood here claiming otherwise. What the pattern does rule out is reading a clean round as proof: round 3's zero findings are one round, on one reading, of a delta no customer can reach while the flag is off.
