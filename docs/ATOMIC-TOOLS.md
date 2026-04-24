# Atomic Tools — Build Spec

> **Note (2026-04-23):** This spec was written for D1..D9 atomic primitives.
> Wave-2 also exposes **pilot tiers** on the four composite Kendrew
> pipelines (BindCraft, RFantibody, BoltzGen, PXDesign). Those pilot
> tiers reuse the existing Kendrew Modal apps + the
> `tools-hub/tools/<name>/` adapter pattern; they are NOT atomic tools
> in the D-series sense. See `PRODUCT-PLAN.md` "Iterative binder design
> platform" for how the pilot tiers fit the iterative workflow.

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

#### D1 Status (as of 2026-04-24)

**Validated on `ranomics-mpnn-prod` with 2× consecutive smoke PASS (commit `cdc9e3a`).** Gated only on the operator flipping `FLAG_TOOL_MPNN=on` in the tools-hub Railway env. See [VALIDATION-LOG.md](VALIDATION-LOG.md) for the two PASS rows (`smoke-1777047396` cold 25.4 s, `smoke-1777047431` warm 5.3 s).

Post-validation Codex-review fixes also landed (commits `80270d9` `54f0cc7` `aa6c4f5` `072f679` `cdc9e3a`): `numpy<2` pin + Layer-1 `torch.from_numpy(numpy.zeros(1))` bridge check; handoff `pilot` preset remap so cross-tool handoffs don't silently run the baked 1HEW fixture; `/jobs/<id>/export.fasta` serializes the MPNN `sequences` schema; stub-rejection strengthened with near-clone Hamming and tight-cluster guards; `run_tool` signature annotated `Any` so `modal run --payload` works.

Shipped this commit:
- `tools-hub/tools/mpnn/Dockerfile.modal` — image derived from `docker/rfdiffusion/Dockerfile.modal` with everything but PyTorch + MPNN + weights stripped. Bakes `static/example/1HEW.pdb` as the smoke fixture. Layer-1 build-time validation in the final `RUN` block fails `modal deploy` if the MPNN repo, weights, or helper scripts are missing.
- `tools-hub/tools/mpnn/run_pipeline.py` — `preflight()` (GPU + MPNN + weights + /tmp writable) + `main()` that invokes `protein_mpnn_run.py`, parses the FASTA output into the atomic-tool sequence schema, and writes `/tmp/smoke_results.json`. Stub rejection rejects all-identical sequences and the MPNN equivalent of the PXDesign silent-stub (identical score+recovery across >= 3 samples).
- `tools-hub/tools/mpnn/modal_app.py` — Modal wrapper (`ranomics-mpnn-prod`, `A10G`, 10-min timeout). Self-contained per the Kendrew portability pattern.
- `tools-hub/tools/mpnn/__init__.py` — `ToolAdapter` with two presets: `smoke` (0 credits, baked 1HEW target) and `standalone` (1 credit, caller-uploaded PDB). Per-preset `requires_pdb` flag.
- `tools-hub/tools/mpnn/meta.py` — citation, repo link, runtime table.
- `tools-hub/templates/tools/mpnn_form.html` — dark-design-system form, preset picker, chain/num_seq/temp fields, About panel. Matches the RFantibody two-panel layout.
- `tools-hub/templates/tools/mpnn_results.html` — sequence table (no composite `candidates` shape because MPNN returns sequences directly). FASTA export link reuses `/jobs/<id>/export.fasta`.
- `tools-hub/gpu/modal_client.py` — `PRESET_CAPS` entries for `("mpnn","smoke")=120` and `("mpnn","standalone")=360`, plus a new `APP_NAME_OVERRIDES` map and `modal_app_name(tool)` helper that routes `"mpnn"` → `ranomics-mpnn-prod` while leaving every composite tool pointing at `kendrew-<tool>-prod`.
- `tools-hub/app.py` — `import tools.mpnn` added to the adapter-registration block.
- `tools-hub/tests/test_mpnn_smoke.py` — 32 offline tests covering adapter registration, validate/build_payload shape, form render (flag on → 200, flag off → 404), Modal payload shape (via offline-stub path), webhook roundtrip (accept COMPLETED + reject unknown-job / bad-token / replay), FASTA parser, stub rejection.

**Open / gated on user action:**
- Modal deployment: `modal deploy tools/mpnn/modal_app.py` has not been run. Image build time is ~8-12 min on first deploy (MPNN repo clone + weights download + PyTorch install); thereafter Modal caches.
- Staging smoke validation: two consecutive green smoke runs on `ranomics-mpnn-prod` are the gate to flipping `FLAG_TOOL_MPNN=on` per the atomic Definition of Done.
- The `modal_app.py` file imports the `modal` package at module top. When the `modal` CLI is not on PATH the import fails at app boot. This is intentional (the existing composite apps follow the same pattern) — see `llm-proteinDesigner/infrastructure/modal/bindcraft_app.py`.

**Definition-of-Done checklist (from top of this doc):**
- [x] `Dockerfile.modal` — Layer-1 checks wired.
- [x] `run_pipeline.py` — `preflight()` + `main()` + stub rejection.
- [x] `backend/pipelines/mpnn.py` — **NOT shipped**; D1 is self-contained under `tools-hub/tools/mpnn/` rather than mirroring the Kendrew `docker/ + infrastructure/ + backend/pipelines/` three-directory split. The spec in ATOMIC-TOOLS.md "Common shape" was written for tools that live in the Kendrew repo; D1 lives in tools-hub because it is its own Modal app, not a Kendrew composite. The fields `backend/pipelines/*.py` exposes (`smoke_preset`, `standalone_preset`, `gpu_sku`, `execution_timeout_ms`) are instead expressed in `tools/mpnn/__init__.py` (presets) + `tools/mpnn/modal_app.py` (gpu_sku=A10G, timeout=600 s).
- [x] `infrastructure/modal/<name>_app.py` → `tools-hub/tools/mpnn/modal_app.py` (see deviation above). Registers `ranomics-mpnn-prod` with GPU A10G.
- [x] Two consecutive staging smoke passes — `smoke-1777047396` (cold, 25.4 s) + `smoke-1777047431` (warm, 5.3 s) on commit `cdc9e3a`. Both PASS. See VALIDATION-LOG.md.
- [x] `tools-hub/tools/mpnn/` form + results template behind `FLAG_TOOL_MPNN=off`.
- [x] Pricing row in the "Credit rates" table of PRODUCT-PLAN.md (1 credit).
- [ ] Marketing MDX page drafted — **deferred to stream F** per the 1-day scope.
- [ ] Orchestrator flips `FLAG_TOOL_MPNN=on` — **user action** after validation.

**User action to validate D1 on Modal:**

```bash
# From tools-hub repo root
modal deploy tools/mpnn/modal_app.py

# Smoke-tier run (baked 1HEW target, should complete in <60 s):
modal run tools/mpnn/modal_app.py::run_tool --payload '{
  "tier": "smoke",
  "job_tier": "smoke",
  "job_id": "smoke-'$(date +%s)'",
  "job_spec": {"target_chain": "A", "parameters": {"num_seq_per_target": 2, "sampling_temp": 0.1}}
}'
```

Expected return: `smoke_result.status == "COMPLETED"`, `smoke_result.sequences` length == 2, each with distinct seq / score / recovery floats. Log two consecutive green runs in VALIDATION-LOG.md before flipping `FLAG_TOOL_MPNN=on` in the tools-hub Railway env.

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

**Timeout:** 10 minutes (the tools-hub agent deviated from the 15-min spec here — no-MSA ColabFold on <=600 aa runs 1-2 min once JIT is cached, so 600 s gives ~5x headroom while keeping the user's max-charge cap tight).

Schema as D2 but faster defaults, no templates by default. Stub rejection same.

**Pricing:** 2 credits.

#### D3 Status (as of 2026-04-24)

**CODE-COMPLETE on `feat/colabfold-standalone`.** Mirrors the D1 MPNN pattern. Awaits `modal deploy` + 2x consecutive staging smoke on `ranomics-colabfold-prod` before the operator flips `FLAG_TOOL_COLABFOLD=on`.

Shipped this commit:
- `tools-hub/tools/colabfold/Dockerfile.modal` — image derived from `docker/rfdiffusion/Dockerfile.modal` with RFdiffusion / SE3Transformer / DGL / MPNN stripped. Bakes `static/example/ubiquitin.fasta` (human ubiquitin, 76 aa monomer, UniProt P0CG47) as the smoke fixture. Layer-1 build-time validation fails `modal deploy` if ColabFold, AF2 weights, or the smoke fixture are missing.
- `tools-hub/tools/colabfold/run_pipeline.py` — `preflight()` (GPU + jax + colabfold + weights + /tmp writable) + `main()` that invokes `colabfold_batch --msa-mode single_sequence --num-recycle 1 --num-models 1 --rank iptm`, parses `*_scores_rank_001_*.json` for pLDDT + ptm + iptm + PAE, b64-encodes the matching `*_unrelaxed_rank_001_*.pdb`, and packs PAE as npz-compressed float16. Stub rejection catches uniform pLDDT, NaN pLDDT, implausible mean pLDDT, and zero ipTM (PXDesign's historical silent-stub signatures).
- `tools-hub/tools/colabfold/modal_app.py` — Modal wrapper (`ranomics-colabfold-prod`, A100-40GB, 600 s timeout). Self-contained per the Kendrew portability pattern. `run_tool` payload annotated `Any` so `modal run --payload` works.
- `tools-hub/tools/colabfold/__init__.py` — `ToolAdapter` with two presets: `smoke` (0 credits, baked ubiquitin fixture) and `standalone` (2 credits, inline FASTA text — no file upload, since FASTAs are tiny). Validates the canonical 20 aa + X alphabet, min 10 aa / max 600 aa per chain, 600 aa total complex cap.
- `tools-hub/tools/colabfold/meta.py` — citation, repo link, runtime table.
- `tools-hub/templates/tools/colabfold_form.html` — dark-design-system form, preset picker, FASTA textarea, recycles + templates controls, About panel. Includes the `pilot->standalone` handoff remap from the MPNN Codex P1 fix.
- `tools-hub/templates/tools/colabfold_results.html` — mean pLDDT + pTM + ipTM + length summary, collapsible per-residue pLDDT spark (CSS bars, no JS deps), clone link.
- `tools-hub/gpu/modal_client.py` — `PRESET_CAPS` entries `("colabfold","smoke")=120` and `("colabfold","standalone")=420`, plus `APP_NAME_OVERRIDES["colabfold"]="ranomics-colabfold-prod"`. Legacy `("colabfold","fast")=720` retained for pre-D3 planning code paths.
- `tools-hub/app.py` — `import tools.colabfold` added to the adapter-registration block.
- `tools-hub/tests/test_colabfold_smoke.py` — 45 offline tests covering adapter registration, validate / build_payload shape, form render (flag on -> 200, flag off -> 404), Modal payload shape, webhook roundtrip, FASTA parser, ColabFold output parser, stub rejection.

**Open / gated on user action:**
- Modal deployment: `modal deploy tools/colabfold/modal_app.py` has not been run. Image build time is ~10-15 min on first deploy (JAX + cuDNN wheels + AF2 multimer weights download + Layer-1 check); thereafter Modal caches.
- Staging smoke validation: two consecutive green smoke runs on `ranomics-colabfold-prod` are the gate to flipping `FLAG_TOOL_COLABFOLD=on` per the atomic Definition of Done.

**User action to validate D3 on Modal:**

```bash
# From tools-hub repo root
modal deploy tools/colabfold/modal_app.py

# Smoke-tier run (baked 76 aa ubiquitin, expect <=120 s warm / ~4 min cold):
modal run tools/colabfold/modal_app.py::run_tool --payload '{
  "tier": "smoke",
  "job_tier": "smoke",
  "job_id": "smoke-'$(date +%s)'",
  "job_spec": {"parameters": {"num_recycles": 1, "use_templates": false}}
}'
```

Expected return: `smoke_result.status == "COMPLETED"`, `smoke_result.plddt_per_residue` length == 76 with real non-uniform floats, `smoke_result.mean_plddt` in [70, 95], `smoke_result.ptm` in [0.5, 0.95], `smoke_result.pdb_b64` decodes to >=200 bytes of PDB text, stub rejection does not trip. Log two consecutive green runs in VALIDATION-LOG.md before flipping `FLAG_TOOL_COLABFOLD=on` in the tools-hub Railway env.

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

#### D4 Status (as of 2026-04-24)

**CODE-COMPLETE on `feat/esmfold-standalone`.** Mirrors the D3 ColabFold pattern, with monomer-only validation and a ``ptm=None`` tolerant results template. Awaits `modal deploy` + 2x consecutive staging smoke on `ranomics-esmfold-prod` before the operator flips `FLAG_TOOL_ESMFOLD=on`.

Shipped this commit:
- `tools-hub/tools/esmfold/Dockerfile.modal` — fresh image (no Kendrew image to reuse): PyTorch 2.1 CUDA 11.8 + HF transformers 4.35 + openfold helpers. Bakes ~15 GB ``facebook/esmfold_v1`` weights + the same 76 aa ubiquitin smoke fixture D3 uses. Layer-1 build-time validation fails `modal deploy` if transformers, the baked weights, or the smoke fixture are missing.
- `tools-hub/tools/esmfold/run_pipeline.py` — `preflight()` (GPU + HF cache + /tmp writable + ESMFold snapshot present) + `main()` that loads `EsmForProteinFolding` from HF, runs one forward pass, extracts per-residue pLDDT (handles both `(B, L, 37)` and `(B, L)` output shapes across transformers versions), generates PDB via `model.output_to_pdb`, and handles `ptm`/`pae` being `None` cleanly. Stub rejection catches uniform pLDDT, NaN pLDDT, implausible mean pLDDT, empty/degenerate PDB, and all-zero coordinates — but does NOT reject `ptm==0.0` because ESMFold v1 legitimately omits the pTM head on some checkpoints.
- `tools-hub/tools/esmfold/modal_app.py` — Modal wrapper (`ranomics-esmfold-prod`, A100-40GB, 600 s timeout). Self-contained per the Kendrew portability pattern. Stale `smoke_results.json` cleanup lifted from the D3 Codex P1 fix. `run_tool` payload annotated `Any` so `modal run --payload` works.
- `tools-hub/tools/esmfold/__init__.py` — `ToolAdapter` with two presets: `smoke` (0 credits, baked ubiquitin fixture) and `standalone` (1 credit, inline FASTA text). Validates the canonical 20 aa + X alphabet, 10-400 aa per sequence. **ESMFold v1 is monomer-only**, so the validator rejects (a) multi-record FASTA and (b) any `:` chain-separator in the sequence — both paths surface a pointed error suggesting ColabFold (D3) or AF2 (D2) for multimer work.
- `tools-hub/tools/esmfold/meta.py` — citation (Lin et al., Science 2023), repo link, runtime table.
- `tools-hub/templates/tools/esmfold_form.html` — dark-design-system form, preset picker, monomer-only FASTA textarea, About panel. Includes the `pilot->standalone` handoff remap from the MPNN/ColabFold Codex P1 fix.
- `tools-hub/templates/tools/esmfold_results.html` — mean pLDDT + length summary, pTM tile only rendered when `ptm is not none` (ATOMIC-TOOLS.md D4 gotcha), collapsible per-residue pLDDT spark, PDB + (optional) PAE download links as data-URIs.
- `tools-hub/gpu/modal_client.py` — `PRESET_CAPS` entries `("esmfold","smoke")=90` and `("esmfold","standalone")=360`, plus `APP_NAME_OVERRIDES["esmfold"]="ranomics-esmfold-prod"`. Legacy `("esmfold","fast")=360` retained for pre-D4 planning code paths.
- `tools-hub/app.py` — `import tools.esmfold` added to the adapter-registration block.
- `tools-hub/tests/test_esmfold_smoke.py` — 49 offline tests covering adapter registration, validate (including monomer-only enforcement for both multi-record FASTA + `:` separator), build_payload shape, form render (flag on -> 200, flag off -> 404), Modal payload shape, webhook roundtrip, `pilot->standalone` handoff remap, FASTA parser, ESMFold output shaping, stub rejection (uniform / NaN / implausible-mean / empty-PDB / all-zero coords), and the results template's `ptm=None` omit-the-tile behaviour.

**Codex review status:** external `codex review` blocked by usage-limit rate-limit on the workspace through 2026-05-01. One self-review finding filed + fixed in commit `536e73b` (`fix(esmfold): harden pLDDT extraction across transformers versions` — covers the `atom37_atom_exists` attribute drift across transformers minor versions plus a `.eval().cuda()` ordering tweak). No other self-review findings above P3.

**Open / gated on user action:**
- Modal deployment: `modal deploy tools/esmfold/modal_app.py` has not been run. Image build time is ~20-25 min on first deploy (PyTorch CUDA wheels + transformers + openfold + 15 GB esmfold_v1 weight download + Layer-1 check); thereafter Modal caches the image layers.
- Staging smoke validation: two consecutive green smoke runs on `ranomics-esmfold-prod` are the gate to flipping `FLAG_TOOL_ESMFOLD=on` per the atomic Definition of Done.

**User action to validate D4 on Modal:**

```bash
# From tools-hub repo root
modal deploy tools/esmfold/modal_app.py

# Smoke-tier run (baked 76 aa ubiquitin, expect <=90 s warm / ~3-4 min cold
# on first run while ESMFold-3B pages off the baked layer):
modal run tools/esmfold/modal_app.py::run_tool --payload '{
  "tier": "smoke",
  "job_tier": "smoke",
  "job_id": "smoke-'$(date +%s)'",
  "job_spec": {"parameters": {}}
}'
```

Expected return: `smoke_result.status == "COMPLETED"`, `smoke_result.plddt_per_residue` length == 76 with real non-uniform floats on a 0-100 scale, `smoke_result.mean_plddt` in [70, 95] for ubiquitin, `smoke_result.pdb_b64` decodes to >=200 bytes of PDB text with real ATOM coordinates, stub rejection does not trip. `smoke_result.ptm` may be `None` — that is valid for ESMFold v1 and the result template handles it. Log two consecutive green runs in VALIDATION-LOG.md before flipping `FLAG_TOOL_ESMFOLD=on` in the tools-hub Railway env.

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
