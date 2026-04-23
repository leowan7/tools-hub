# Orchestrator Arbitration Log

Append-only record of cross-stream decisions the orchestrator makes when streams cross file-ownership boundaries or need a shared contract changed. Prevents the rationale from evaporating.

Streams are defined in [PRODUCT-PLAN.md](PRODUCT-PLAN.md#parallel-agent-workstreams): A (Hub), B (CPU tools), C (Pipeline integrations), D (Atomics factory, with sub-streams D1..D9), E (Kendrew validation), F (Marketing), G (Enterprise hardening).

## How to append

One entry per decision. Format:

```
### <YYYY-MM-DD> — <short title>

- **Requested by:** stream <X>
- **Affects:** stream(s) <Y>, file(s) <path>
- **Question:** <what needed deciding>
- **Decision:** <what the orchestrator ruled>
- **Rationale:** <why — one or two sentences>
- **Follow-up:** <commit SHA of the resulting change, or "n/a">
```

Never edit past entries. If a decision is reversed, append a new entry referencing the earlier one.

---

## Entries

### 2026-04-23 — ATOMIC-TOOLS spec locked (D1..D9)

- **Requested by:** orchestrator (Leo)
- **Affects:** stream D (all sub-streams D1..D9), file `docs/ATOMIC-TOOLS.md`
- **Question:** Is the atomic-tools build contract stable enough for Stream D agents to start work?
- **Decision:** **Locked.** Common shape (7 required elements), per-primitive recipes D1 through D9, and the Definition-of-Done checklist are all normative. Future edits must be additive — changes to input/output schemas, GPU SKUs, timeouts, or pricing rows must either keep existing D* recipes intact or land as a new recipe in a later wave.
- **Rationale:** Spec has been stable since the Wave-0 commit (`4e9eaa1`). Every element downstream agents need is present: Dockerfile Layer-1 checks, ≤60s `preflight()`, smoke preset, stub-score rejection, Modal wrapper tier contract, webhook roundtrip, `FLAG_TOOL_<NAME>` feature flag. D1 (ProteinMPNN, pattern setter) has full input/output schema, GPU SKU (A10G-24GB), timeout, smoke preset, pricing, and marketing angle. Waiting longer adds no information.
- **Follow-up:** Stream D1 scaffold starts on the Kendrew side (`docker/mpnn/`, `infrastructure/modal/mpnn_app.py`, `backend/pipelines/mpnn.py`). D2..D9 clone D1's shape.

### 2026-04-23 — Wave 2 vision: iterative binder design platform (revenue-ready)

- **Requested by:** orchestrator (Leo) — direct product call after Wave-1 demo tier shipped
- **Affects:** stream C (extends scope), `docs/PRODUCT-PLAN.md`, `tools-hub/tools/{rfantibody,pxdesign,boltzgen,bindcraft}/__init__.py`, `tools-hub/templates/tools/*`, new `tools-hub/shared/email.py`, `tools-hub/shared/jobs.py` (prorated refund), `tools-hub/app.py` (per-preset PDB requirement)
- **Question:** What does "fully production-ready, revenue-generating" look like for the GPU tools surface?
- **Decision:** **`tools.ranomics.com` is an iterative binder design platform, not a demo suite.** Every one of the 4 GPU tools (RFantibody, BindCraft, PXDesign, BoltzGen) gets a **pilot tier** that takes user-uploaded PDB + user-selected hotspots + user parameters and runs against the user's actual target via the webhook flow. Demo tiers (smoke / preview, baked PD-L1 target) stay as the free-tier funnel and the schema-preview UX, but pilot tiers are where revenue lives. Per-job pricing is a pre-authorisation; actual debit prorates against `gpu_seconds_used`.
- **Rationale:** A demo suite cannot generate the recurring revenue Ranomics needs. Users on Scout Pro / Lab tiers come for "design binders against my target", not "watch a tool run on PD-L1". The infrastructure shipped today (Storage upload → presigned URL → Modal spawn → webhook callback → job-detail polling) already supports the real-target flow; we just need pilot-tier adapters + email notifications + prorated billing to expose it.
- **Follow-up:** Wave 2 (this session) ships pilot tiers + email + prorated refund. Wave 3 ships Epitope Scout handoff + clone-job + cross-run comparison. Wave 4 ships 3D hotspot picker + atomic primitives. Spec recorded in PRODUCT-PLAN.md "The product — iterative binder design platform" and "Wave structure (revised)".

### 2026-04-23 — Stream C scope expanded: launch 4 existing Kendrew pipelines

- **Requested by:** orchestrator (Leo)
- **Affects:** stream C; files `tools-hub/tools/{bindcraft,rfantibody,boltzgen,pxdesign}/`, `tools-hub/gpu/modal_client.py`, `tools-hub/app.py`, new `tools-hub/shared/jobs.py`, new migration `0005_tool_jobs.sql`
- **Question:** Which Kendrew GPU pipelines can ship behind `tools.ranomics.com` in the immediate next wave?
- **Decision:** **Four tools ship behind feature flags (default off):** BindCraft, RFantibody, BoltzGen, PXDesign. RFdiffusion stays off pending one fresh mini_pilot GPU run per its FLAG verdict in VALIDATION-LOG.
- **Rationale:** All four targets have code-check PASS on current HEAD plus two real-scored greens on record (VALIDATION-LOG.md). PXDesign's two post-cuDNN9-fix smokes live in `blocker-pxdesign.md` not this log; status line is "🟢 GREEN on 5f22eec". The outstanding "add a fresh smoke entry before flipping" is bookkeeping, not missing evidence. RFdiffusion's code-check is FLAG (not PASS) because the JAX-cache blocker fix materially changed the execution path; the formal gate requires a fresh mini_pilot run before flip.
- **Follow-up:** The modal_client stub is replaced with a real Modal `Function.spawn` + `FunctionCall.get` implementation; tools-hub gains a `tool_jobs` Supabase table and a `/webhooks/modal/<tool>/<job_id>` callback so the Kendrew pipelines' webhook-roundtrip contract receives end-to-end. Flags default OFF until the operator flips `FLAG_TOOL_<NAME>=on` after verifying an end-to-end run in production.
