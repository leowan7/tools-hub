# Atomic Tools — Build Spec

Per-primitive build recipe for standalone Modal apps (ProteinMPNN, AF2, ColabFold, ESMFold, AF2-IG, LigandMPNN, Boltz-2, RF2-standalone, RFdiff-standalone).

Each atomic tool gets its **own** Dockerfile.modal, its own `run_pipeline.py`, its own Modal app, and its own validation log entry. No shared images across primitives — see "Why each needs its own app" in [PRODUCT-PLAN.md](PRODUCT-PLAN.md).

This document is the **contract** every stream-D agent follows. Read it end-to-end before building a primitive.

---

## Common shape (all primitives inherit this)

```
llm-proteinDesigner/
├── docker/<name>/
│   ├── Dockerfile.modal          # minimal, pins CUDA + Python + the one binary
│   └── run_pipeline.py           # preflight() + smoke() + main entrypoint
├── infrastructure/modal/
│   └── <name>_app.py             # thin Modal wrapper: gpu, image, entrypoint
└── backend/pipelines/
    └── <name>.py                 # Python wrapper: smoke_preset, standalone_preset, gpu_sku, execution_timeout_ms
tools-hub/tools/<name>/
├── __init__.py                   # exports form_fields, submit, render_results
├── forms.py                      # WTForms or plain HTML form fields spec
├── results.py                    # result payload → display model
└── templates/
    ├── form.html                 # inherits tools-hub/templates/base.html
    └── results.html              # inherits tools-hub/templates/base.html
```

### Required elements in every primitive

**1. Dockerfile.modal Layer-1 checks** (at the end of the file):
```dockerfile
RUN python3 -c "import <primitive_module>" \
 && test -f /opt/weights/<name>.pt \
 && which <binary> \
 && echo "layer1 ok"
```
Failing any check fails `modal deploy` before any GPU is spent.

**2. `preflight()` in `run_pipeline.py`** — ≤60s, runs on GPU container before any real work. Must assert:
- Payload parses; all required keys present.
- Input file readable.
- `torch.cuda.is_available()` or `jax.devices("gpu")` reports expected SKU.
- Binary on `$PATH` with `--help` exit 0.
- `/tmp/smoke_results.json` writable.

On any failure, write `{"status":"FAILED","error":{"bucket":"preflight","check":"<name>","detail":"<stderr>"}}` to `/tmp/smoke_results.json` and `sys.exit(1)`. Copy shape from `docker/bindcraft/run_pipeline.py::startup_check()`.

**3. Smoke preset in `backend/pipelines/<name>.py`** — minimal inputs that complete in ≤3 minutes on the intended GPU SKU and produce real (non-stub) output. Example for MPNN: 1 PDB of ~100 residues, 1 sample, num_seq_per_target=2.

**4. Stub-score rejection** — the smoke preset's result parser must detect and reject the "silent stub" failure mode. PXDesign's ipTM=0.08 / pLDDT=0.96 incident (see [VALIDATION-LOG.md](VALIDATION-LOG.md)) is the cautionary tale. Add an assertion that rejects exact-repeat scores or implausible ranges; any hit writes FAILED to `/tmp/smoke_results.json`.

**5. Modal wrapper tier contract** (`infrastructure/modal/<name>_app.py`) — follow the same pattern as [`bindcraft_app.py`](../../llm-proteinDesigner/infrastructure/modal/bindcraft_app.py):
- `tier: "smoke"` → use `smoke_preset()`, return results inline via `/tmp/smoke_results.json`.
- `tier: "standalone"` (atomic default) → use `standalone_preset()`, return results inline.
- Heartbeats every 60s to `/webhooks/heartbeat`.

**6. Webhook roundtrip** — the tool-hub side must dispatch via `tools-hub/gpu/modal_client.submit(preset) → FunctionCall`, then poll status, then debit actual Modal cost × 2.85 credits on completion. This is the shape stream A publishes.

**7. Feature flag in tools-hub** — `FLAG_TOOL_<NAME>=off` by default; flip to `on` only after [VALIDATION-LOG.md](VALIDATION-LOG.md) records two consecutive staging smoke passes with current commit SHA.

---

## Per-primitive recipes

### D1 — ProteinMPNN standalone (pattern setter, Wave 2)

**Purpose:** user uploads backbone PDB + chain labels → receives N candidate sequences with MPNN scores and pLDDT. 1-credit loss leader.

**Dependency source:** copy the install recipe from [`docker/rfdiffusion/Dockerfile.modal`](../../llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal) (MPNN is already installed there to serve the RFdiffusion → MPNN pipeline step). Strip everything else (no RFdiffusion weights, no ColabFold, no AF2).

**Modal app:** `ranomics-mpnn-prod`. GPU: **A10G-24GB** (MPNN runs happily on a small GPU — do not pay A100 prices).

**Timeout:** 10 minutes.

**Input schema:**
- `backbone_pdb` (file) — required.
- `chains_to_design` (string, space-separated chain IDs) — required.
- `num_seq_per_target` (int, default 5, max 20) — how many sequences to return.
- `sampling_temp` (float, default 0.1) — MPNN sampling temperature.

**Output schema:**
```json
{
  "status": "COMPLETED",
  "sequences": [
    {"seq": "MDPLR...", "score": 1.23, "recovery": 0.52, "chain": "A"},
    ...
  ],
  "runtime_seconds": 47
}
```

**Smoke preset:** 1 PDB of ~100 residues, 1 chain, `num_seq_per_target=2`, `sampling_temp=0.1`. Expected runtime ≤60 s on A10G-24GB. Stub rejection: fail if all returned sequences are identical.

**Pricing:** 1 credit per run regardless of `num_seq_per_target` up to 20. Above 20, cap and charge 2 credits.

**Marketing angle:** "Run ProteinMPNN in 30 seconds. Upload a backbone, get sequences. $1 a run, 3 free per month on the Free tier."

**Why this is the pattern setter:** cheapest to build (1 day), smallest image, smallest GPU, highest demand per the competitor research. D2..Dn agents clone this shape and swap in their primitive.

---

### D2 — AF2 standalone (ColabFold-style, Wave 3)

**Purpose:** user uploads FASTA (single chain or multimer) → receives predicted structure + pLDDT + PAE. 2-credit tool.

**Dependency source:** JAX AF2 from [`docker/bindcraft/Dockerfile.modal`](../../llm-proteinDesigner/docker/bindcraft/Dockerfile.modal) (BindCraft uses FreeBindCraft which bundles AF2). MSA retrieval from the ColabFold MMseqs2 public server (free tier; cache on Modal Volume).

**Modal app:** `ranomics-af2-prod`. GPU: **A100-80GB** (AF2-multimer on sequences > ~400 AA needs the 80GB).

**Timeout:** 30 minutes (MSA + fold + relax).

**Input schema:**
- `fasta` (text) — required, single-chain or multimer with `>chain1` / `>chain2` headers.
- `model_preset` (enum: `monomer` | `multimer`, default inferred from FASTA).
- `num_recycles` (int, default 3, max 5).
- `use_templates` (bool, default true).

**Output schema:**
```json
{
  "status": "COMPLETED",
  "pdb_b64": "...",
  "plddt_per_residue": [...],
  "pae_matrix_b64": "...",
  "iptm": 0.82,
  "ptm": 0.79,
  "runtime_seconds": 420
}
```

**Smoke preset:** 50-residue monomer sequence, `num_recycles=1`, no templates. Expected runtime ≤90 s on A100-80GB. Stub rejection: fail if pLDDT array is all-identical or all-nan.

**Pricing:** 2 credits per fold. Multimer folds over 1500 AA total charge 4 credits.

---

### D3 — ColabFold (no-MSA fast fold, Wave 4)

**Purpose:** user uploads FASTA → receives fast prediction with minimal MSA. Speed tier, complements AF2 standalone.

**Dependency source:** ColabFold is already installed in [`docker/rfdiffusion/Dockerfile.modal`](../../llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal). Strip to minimal image.

**Modal app:** `ranomics-colabfold-prod`. GPU: **A100-40GB**.

**Timeout:** 15 minutes.

Schema as D2 but faster defaults, no templates by default. Stub rejection same.

**Pricing:** 2 credits.

---

### D4 — ESMFold (Wave 4)

**Purpose:** fastest single-sequence fold. No MSA, no JIT bottleneck.

**Dependency source:** fresh `pip install esm` (no Kendrew image to reuse).

**Modal app:** `ranomics-esmfold-prod`. GPU: **A100-40GB** (ESMFold's 3B model needs ~40GB for sequences > 500 AA).

**Timeout:** 10 minutes.

**Input schema:**
- `fasta` (text, single chain only) — required.

**Output schema:** PDB + pLDDT.

**Smoke preset:** 50-AA sequence. Expected runtime ≤30 s. Stub rejection: fail if pLDDT array is all-identical.

**Pricing:** 1 credit.

---

### D5 — AF2 Initial Guess (AF2-IG, Wave 4)

**Purpose:** for validation loops — user uploads designed PDB + template → AF2-IG scores the pre-designed backbone with the template as initial guess. Used to filter RFdiffusion/BindCraft candidates.

**Dependency source:** AF2 from PXDesign image after PXDesign cuDNN re-validation ([VALIDATION-LOG.md](VALIDATION-LOG.md) PXDesign section must be green first).

**Modal app:** `ranomics-af2ig-prod`. GPU: **A100-80GB**.

**Timeout:** 15 minutes.

**Input schema:**
- `designed_pdb` (file) — required.
- `template_pdb` (file) — required.
- `num_recycles` (int, default 1, max 3).

**Output schema:** ipTM, pAE matrix, RMSD to template.

**Pricing:** 2 credits.

---

### D6 — Boltz-2 (frontier, Wave 5)

**Purpose:** structure prediction + affinity in one model. Frontier gap — no competitor has a good hosted UI.

**Dependency source:** public Boltz repo. Verify current license + weight availability before starting.

**Modal app:** `ranomics-boltz2-prod`. GPU: **A100-80GB**.

**Timeout:** 30 minutes.

**Input schema:**
- `fasta` (text, multimer supported).
- `ligand_smiles` (string, optional) — for affinity prediction.

**Output schema:** PDB + pLDDT + optional affinity score.

**Smoke preset:** 50-AA monomer + short SMILES. Stub rejection: fail if affinity is exactly zero or unchanged across inputs.

**Pricing:** 4 credits (pilot). Higher because of the frontier-gap positioning.

---

### D7 — LigandMPNN (Wave 5)

**Purpose:** ligand-aware sequence design. Premium atomic.

**Dependency source:** fresh build — not installed in any Kendrew image.

**Modal app:** `ranomics-ligandmpnn-prod`. GPU: **A100-40GB**.

**Timeout:** 15 minutes.

**Input schema:** backbone PDB + ligand SMILES or bound-ligand PDB.

**Pricing:** 2 credits.

---

### D8 — RF2 standalone (antibody fold, Wave 5)

**Purpose:** antibody-specific folding. Lower volume than AF2 but higher quality for Fv/scFv.

**Dependency source:** RF2 from [`docker/rfantibody/Dockerfile.modal`](../../llm-proteinDesigner/docker/rfantibody/Dockerfile.modal).

**Modal app:** `ranomics-rf2-prod`. GPU: **A100-40GB**.

**Timeout:** 15 minutes.

**Input schema:** heavy + light chain FASTA (or VHH).

**Pricing:** 3 credits.

---

### D9 — RFdiffusion standalone backbone (Wave 5)

**Purpose:** backbone-only diffusion. Separates design (D) from sequence (MPNN) and validation (AF2) — useful for users iterating on just the backbone step.

**Dependency source:** RFdiffusion from [`docker/rfdiffusion/Dockerfile.modal`](../../llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal), strip MPNN + AF2.

**Modal app:** `ranomics-rfdiff-prod`. GPU: **A100-40GB**.

**Timeout:** 20 minutes.

**Input schema:** target PDB + hotspots + contig spec + num_designs (cap at 20 for atomic).

**Pricing:** 3 credits.

---

## Per-primitive Definition of Done

A D* stream is complete when ALL of:

- [ ] `docker/<name>/Dockerfile.modal` builds cleanly on Modal (Layer-1 checks pass).
- [ ] `docker/<name>/run_pipeline.py` implements `preflight()` per the contract above.
- [ ] `backend/pipelines/<name>.py` exposes `smoke_preset()`, `standalone_preset()`, `gpu_sku`, `execution_timeout_ms`, stub-score rejection.
- [ ] `infrastructure/modal/<name>_app.py` registers the Modal app with correct GPU SKU.
- [ ] Two consecutive staging smoke passes recorded in [VALIDATION-LOG.md](VALIDATION-LOG.md) with real scores.
- [ ] `tools-hub/tools/<name>/` form + results template live behind `FLAG_TOOL_<NAME>=off`.
- [ ] Pricing row exists in the "Credit rates" table of [PRODUCT-PLAN.md](PRODUCT-PLAN.md).
- [ ] Marketing MDX page drafted in `ranomics-website-2026/src/content/pages/tools/<name>.mdx` (can stub; stream F polishes).
- [ ] Orchestrator flips `FLAG_TOOL_<NAME>=on` in tools-hub env.
