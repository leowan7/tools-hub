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

Nothing below is an instruction to spend. Every GPU rung in §6 is a proposal for Leo to approve or refuse individually.

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

Reward-guided search deserves a note, because it is the headline mode: on the upstream benchmark it is the strongest arm by a distance (28.1% pass vs 17.0% for one-shot medium, §3), but it costs ~27× the GPU seconds per design (62.5 s vs 2.35 s median, same source) and upstream issue #64 reports a multi-GPU beam-search reward bug. It is the obvious *second* release, priced as its own preset, once one-shot is earning.

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
- **Driver is CUDA 12 only** → build `RFD4_ACCELERATOR=cu12`. Upstream ships it as a first-class option, so this is a fallback rather than a blocker — but it is a different wheel set and must be probed, not assumed. UNVERIFIED whether the cu12 path is as well exercised upstream as cu13; the published `cu12`/`cu13` runtime image sizes are within 10% of each other (§5.2), so size tells us nothing either way.

Rung 1 answers this for $0.

### 5.2 Image plan

Follow the `proteina` pattern exactly, because its guard tests already enforce it:

- New `tools/rfd4/Dockerfile.modal`, **digest-pinned `FROM`** and **`ARG RFD4_SHA=<commit>`** with a `git checkout` of that SHA — same shape as `tools/proteina/Dockerfile.modal:35,54,58`. Pinned to a commit, never a branch.
- Add the `certifi` floor layer; `tests/test_gpu_image_certifi_floor.py` and `tests/test_dockerfile_digest_pins.py` glob `tools/*/Dockerfile.modal`, so a new file is picked up automatically and will fail the suite without these.
- **Do NOT include `alphafold2/runtime/` or `openfold3/runtime/`.** Per the HF model card those images package third-party software under its own upstream terms, some of which restrict commercial use or redistribution, and **neither oracle is needed for generation**. They are also 3.5–16.6 GB each.
- **The oracle exclusion has a config consequence that must be in code, not in a comment.** Upstream's default `inference_on_user_inputs` experiment composes a metrics group whose default oracle is AF3. The wrapper must pass `'~metrics'` (or an inline-only metrics tag) so no oracle is ever selected. A guard test asserting that our invocation disables the oracle group is the cheapest way to keep a future edit from quietly re-enabling a licence we did not clear.
- Weights on a Modal Volume seeded once, copying `tools/proteina/seed_volumes.py`. **Flow checkpoint and autoencoder are a matched pair** — the HF card warns that mixing releases "produces nonsense rather than an error", so the seeder must pin one HF revision for both and record it.
- Sizes from the HF file listing: `model.safetensors` 1.74 GB, flow checkpoint 1.75 GB, autoencoder 1.03 GB; upstream says `make setup` caches about 1.7 GB. Upstream prebuilt runtime `.sif` images (weights excluded): cu13 4.04 GB, cu12 4.41 GB. **Measure the real volume size at rung 2** — our `boltz2` weights volume was previously off by ~7× against its documented figure.
- GPU class **A100-80GB** (`_GPU` must be a module-level literal; `tests/test_gpu_class_drift.py:155-171` requires a matching `TOOL_SPECS` entry).

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
| **1. CPU build probe** | Does the image build, does `rfd4-generate --help` run, and **which accelerator variant do we need?** | none | **$0** | Build `cu13` on a CPU builder with weights excluded; a `RUN` probe at build time is free. Also establishes whether Modal's driver supports cu13 — if not, rebuild as cu12. **Blocking for every rung below.** |
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

1. **Approve rung 1 ($0)?** CPU-only image build probe. Answers the CUDA-13 question and gates everything.
2. **Approve rungs 2–3 (≈$2.50 together)?** Volume seed plus one 8-design run. This is the decision point: it produces the real cost per run, the real score columns, and the streaming answer. I recommend approving 1–3 as one block, since 1 and 2 are nearly free and 3 is what makes the rest honest.
3. **Approve rung 4 (≈$1.85)?** Head-to-head against a recorded `proteina` run. Recommended but separable.
4. **Rung 5 (≈$5.55)?** Defer until after the go/no-go.

**Product decisions:**

5. **No ipTM on the results page.** Ship release 1 with inline metrics only, or hold for a chained fold step? (§7.3 — I recommend shipping, then chaining.)
6. **Start minimum.** $1.00 proposed, but the derivation depends on rung 3's streaming answer. Confirm after rung 3, not now.
7. **Positioning.** Three binder tools on the shelf. What does a customer choose between them, in one sentence? This blocks step 7, not the engineering.
8. **Alpha software, four weeks old, 7 open issues.** Are we comfortable selling it? My read: yes, behind a flag and after rung 4, because the licence is clean and the downside is a refund, not a liability. Leo's call.

---

## 10. UNKNOWNs and UNVERIFIEDs that must close before step 2

1. **Does Modal's A100-80GB present a CUDA-13-capable driver?** UNVERIFIED. Blocking. Rung 1.
2. **Real wall-clock and cost of an 8-design run on our hardware.** UNKNOWN. The benchmark's per-design seconds exclude setup, compile and warmup and name no GPU model. Rung 3.
3. **Do structure files land per design or only at run end?** NOT FOUND upstream. Decides whether streaming works at all and whether the $1.00 start minimum is defensible. Rung 3.
4. **The real `per_example_metrics.csv` header.** Currently inferred from upstream config files, not from a run. Rung 3.
5. **PDB→CIF conversion and `label_seq_id` numbering.** Upstream resolves residue ids against mmCIF `label_seq_id`, not author numbering. Our whole stack speaks author numbering. **This is the most likely source of a silent wrong-residue bug** — the same class as the `proteina` hotspot defect already on record. Needs a deliberate test, not a code read.
6. **Hotspot atom-set expansion.** Upstream hotspots are atom-level; our picker emits residues. Which atoms we choose is a scientific decision nobody has made.
7. **Real weights-volume size.** Documented ~1.7 GB; a previous weights volume was off by ~7× against its doc figure. Rung 2.
8. **VRAM at the top of the size envelope.** Only "≥40 GB recommended" upstream, with no curve. Rung 5.
9. **Does `delete_failing_pre_oracle` race our uploader?** UNVERIFIED. Rung 3 observation.
10. **Cyclic peptide support.** A preset exists; upstream issue #62 asks whether it works. Out of scope for release 1 either way.
11. **Upstream binder-mode bugs.** #81 (high alanine fraction on the non-interacting face in binder mode) is open and is a *quality* bug in exactly our release-1 mode. Watch it; it may shape the copy we write about expected quality.
12. **Does the authors' benchmark hold on the current weights?** Upstream says it has not been re-measured. Rung 4 is our own check, on one target.

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
