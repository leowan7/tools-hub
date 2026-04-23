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

### 2026-04-23 — Stream C scope expanded: launch 4 existing Kendrew pipelines

- **Requested by:** orchestrator (Leo)
- **Affects:** stream C; files `tools-hub/tools/{bindcraft,rfantibody,boltzgen,pxdesign}/`, `tools-hub/gpu/modal_client.py`, `tools-hub/app.py`, new `tools-hub/shared/jobs.py`, new migration `0005_tool_jobs.sql`
- **Question:** Which Kendrew GPU pipelines can ship behind `tools.ranomics.com` in the immediate next wave?
- **Decision:** **Four tools ship behind feature flags (default off):** BindCraft, RFantibody, BoltzGen, PXDesign. RFdiffusion stays off pending one fresh mini_pilot GPU run per its FLAG verdict in VALIDATION-LOG.
- **Rationale:** All four targets have code-check PASS on current HEAD plus two real-scored greens on record (VALIDATION-LOG.md). PXDesign's two post-cuDNN9-fix smokes live in `blocker-pxdesign.md` not this log; status line is "🟢 GREEN on 5f22eec". The outstanding "add a fresh smoke entry before flipping" is bookkeeping, not missing evidence. RFdiffusion's code-check is FLAG (not PASS) because the JAX-cache blocker fix materially changed the execution path; the formal gate requires a fresh mini_pilot run before flip.
- **Follow-up:** The modal_client stub is replaced with a real Modal `Function.spawn` + `FunctionCall.get` implementation; tools-hub gains a `tool_jobs` Supabase table and a `/webhooks/modal/<tool>/<job_id>` callback so the Kendrew pipelines' webhook-roundtrip contract receives end-to-end. Flags default OFF until the operator flips `FLAG_TOOL_<NAME>=on` after verifying an end-to-end run in production.
