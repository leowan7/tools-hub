# Ranomics Tools — Product Plan

Source of truth for the `tools.ranomics.com` business: strategy, pricing, waves, gates, moat. Living document — edit as waves complete.

Companion docs in this directory:
- [VALIDATION-LOG.md](VALIDATION-LOG.md) — append-only per-tool smoke / mini-pilot pass records
- [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md) — per-primitive build spec (Dockerfile, Modal app, preflight, pricing)
- [ORCH-LOG.md](ORCH-LOG.md) — append-only orchestrator arbitration decisions across streams

---

## Context

Supplement Ranomics CRO income with self-serve computational tools — Tamarind Bio / Rowan / Ariax-style SaaS. Five findings shape the plan:

1. **CPU tools are shippable now.** [`epitope-scout/`](../../epitope-scout/) is live at scout.ranomics.com (CPU, no paywall). [`tools-hub/`](../) is a Flask+Supabase scaffold already running on Railway via `Procfile`+`nixpacks.toml`. [`tools-hub-prototype/`](../../tools-hub-prototype/) contains a Binder Developability Scout (~70%) and Yeast Display Library Planner (~60%) — both CPU scoring engines a form away from shipping.
2. **Kendrew's Modal GPU layer is stronger than the README alone suggested.** Code + git-log audit: BindCraft, RFantibody, BoltzGen, and PXDesign are all GREEN with 2× consecutive passes on record. RFdiffusion is smoke-only pending JAX-cache remediation (`064266f` + `97ec005` landed; fresh mini_pilot run required because execution path changed). PXDesign is GREEN on `5f22eec` (cuDNN 9 fix + N=1 mini_pilot tweak) with real non-stub AF2-IG scores. **Re-validation for already-green pipelines means code-check against HEAD, not fresh GPU runs** — fresh runs are only owed where the execution path materially changed. Full verdicts in "Asset audit" below.
3. **Users want primitives, not just pipelines.** A big chunk of Tamarind/Neurosnap revenue is standalone AF2, standalone ProteinMPNN, standalone ColabFold, standalone Boltz-2. None of these is a standalone Modal endpoint today — they are *installed* inside Kendrew's BindCraft / RFdiffusion / RFantibody images but have never been invoked alone. Each atomic tool is real engineering work: its own Dockerfile.modal, its own `run_pipeline.py` with preflight + smoke contract, its own Modal app, and its own staging validation. 1–2 days per primitive, not half a day. Dependencies are a solved problem because they already install and run inside the pipeline images.
4. **The pricing gap is real** — $49–$599/mo sits empty between Neurosnap ($7–$80) and Tamarind ($50k+/yr).
5. **The real moat is the wet lab.** Tamarind's Apr-2026 A-Alpha Bio partnership exists because they need what Ranomics already is. Every tool terminates in "Validate in Ranomics' lab" — that's the CRO handoff Ranomics already operates.

**Locked decisions:** Ranomics-branded (`tools.ranomics.com`). Wedge and monetization optimize for fastest path to first dollar — CPU tools behind Stripe first, then BindCraft + RFantibody + BoltzGen + MPNN standalone, then atomics and validation-gated additions.

---

## The product — iterative binder design platform

**Updated 2026-04-23. This is the north star.**

`tools.ranomics.com` is an **iterative binder design workspace**, not a demo
suite. A scientist signs in, uploads their target structure, picks
hotspots (typed today, 3D-clicked later, imported from Epitope Scout
soon), picks a generative tool, picks how many binders + parameters,
submits. They get a job-detail page that polls to completion, an email
when the run finishes, ranked candidates with real scores + downloadable
PDBs. They iterate — clone a job, tweak parameters, run it through a
second tool to validate, compare results across runs, repeat until they
have a binder set worth ordering peptide-synthesis or sending to the
Ranomics wet lab.

### Tool roles in the loop

| Tool | Role | Tier exposed |
|---|---|---|
| **RFantibody** | Generate VHH / scFv against target (RoseTTAFold-2 validated) | smoke (PD-L1 demo, 2cr) · preview (PD-L1 demo, 8cr) · **pilot (real target, ~15cr)** |
| **BindCraft** | De novo binder design, structure-based, AF2 multimer + ColabDesign | **pilot (real target, 22cr, ~45min)** |
| **PXDesign** | Generate + AF2-IG validate. Best for targets where AF2 confidence matters. | smoke (PD-L1 demo, 8cr) · mini_pilot (PD-L1 demo, 16cr) · **pilot (real target, ~15cr)** |
| **BoltzGen** | Boltz-2 backbone gen + refold-RMSD scoring | smoke (PD-L1 demo, 3cr) · mini_pilot (PD-L1 demo, 10cr) · **pilot (real target, ~10cr)** |

**Demo tiers stay** because they let a user see the output schema +
score quality + UX before committing to a 45-min real-target run. They
also serve the free-tier funnel.

**Pilot tiers are where revenue lives.** Real-target runs are user-PDB
+ user-hotspots + user-parameters and pay accordingly. Per-job pricing
is a pre-authorisation; actual credit burn is prorated against
``smoke_result.gpu_seconds`` so a pipeline that finishes early returns
unused credits.

### What "fully production-ready" means

1. ✅ Tool form for each of the 4 tools accepting (a) PDB upload, (b)
   chain selector, (c) hotspot text input, (d) numeric parameters
   (binder length range, num designs, framework where applicable),
   (e) preset selector covering both demo and pilot tiers.
2. ✅ Submission validates inputs, debits a pre-authorisation against
   the user's credit balance, uploads PDB to `tool-inputs` Storage,
   spawns the Modal function with a presigned URL + signed webhook URL.
3. ✅ Job detail page polls `/jobs/<id>/status.json` until terminal,
   renders a tool-specific results partial showing per-candidate scores
   + downloadable PDBs.
4. ✅ For long-running pilots (> 5 min) the user can close the tab —
   they receive a `tools-hub job complete` email when the run lands
   (subject: "Your <tool> run is ready" — link to /jobs/<id>).
5. ✅ Modal callback webhook updates the job row idempotently, refunds
   unused GPU-seconds back to the credit ledger, sends the email.
6. ✅ Epitope Scout has a "Design binders from this scout run" button
   that deep-links into the tools-hub form with target PDB +
   pre-selected hotspots already filled in.
7. ✅ User can clone any past job from the `/jobs` list, edit one
   parameter, re-submit. Iteration UX.
8. ✅ Cross-run results comparison: pick N past jobs, see a stacked
   table of top candidates ranked by composite score.

Items 1–5 are **Phase 1 — shipping in this session**.
Items 6–8 are **Phase 2 — next session**.

### Wave structure (revised)

| Wave | Scope | Status |
|---|---|---|
| Wave 0 | Hub foundation + paywall plumbing | ✅ shipped |
| Wave 1 | Scout paywall + Developability + Library Planner | ✅ shipped |
| Wave 1.5 | Modal client + jobs + webhook + Storage + 4 demo-tier tool adapters | ✅ shipped |
| **Wave 2** (**this session**) | Pilot tiers on all 4 tools + email + prorated billing | 🔴 in progress |
| Wave 3 | Epitope Scout handoff + clone-job + cross-run compare | planned |
| Wave 4 | 3D hotspot picker + atomic primitives D1..D9 | planned |

---

## Competitive snapshot

| Company | Funding | Catalog | Pricing | Moat |
|---|---|---|---|---|
| **Tamarind Bio** (YC W24) | $13.6M Series A | 200+ models | Free-limited → ~$50k+/yr | Breadth, pharma logos (8 of top 20), A-Alpha wet-lab partner |
| **Rowan Scientific** | $2.1M pre-seed | ~10 tools (chem-first) | 500 free credits + 20/wk; $0.04/credit | Medicinal chem, Corin Wagen brand |
| **Ariax Bio** | undisclosed | 4–5 tools (BindCraft, BoltzGen, Germinal, FreeBindCraft) | Pure PAYG GPU, 25–40% under AWS | Aaron Ring (Fred Hutch), academic adoption |
| **Neurosnap** | ~$300k | 100+ hosted OSS models | $7 / $14 / $25 / $80 tiers | Cheapest mid-market, clear tiers |
| **Cradle.bio** | $73M Series B | Enterprise platform | Custom ($100k+ inferred) | Own wet lab + 6 big-pharma |

Also in space: Chai Discovery, 310.ai, Profluent, BioLM, Ginkgo Model API, NVIDIA BioNeMo, Benchling, Isomorphic (internal), Iambic (partnership-only).

### Gaps
- **Pricing hole** at $49–$599/mo.
- **Frontier models with no UI:** Boltz-2 (affinity), Chai-2 (antibody), ESM3 (gated Forge).
- **Underserved workflow:** target → epitope → binder → affinity → developability, chained.
- **Underserved segments:** small biotechs, academic PIs without GPU.
- **Tools + wet-lab in one funnel** — no competitor offers this natively.

### Clone
- Neurosnap's tiered price clarity.
- Ariax's credits = dollars PAYG.
- Tamarind's form-per-tool UX.

### Do NOT clone
- Tamarind's 200-model breadth.
- Proprietary foundation models.
- Kendrew as a full enterprise platform (but **extract its Modal layer** — that's the unlock).

---

## Asset audit (with validation verdicts)

| Asset | State | Validation | Revenue role |
|---|---|---|---|
| [`epitope-scout/`](../../epitope-scout/) | Live on Railway at scout.ranomics.com, CPU, free | 🟢 in production | **v1 subscription tool** |
| [`tools-hub/`](../) | Flask + Supabase on Railway, shared auth with Scout | 🟢 hub live, tools stubbed | **v1 hub shell** |
| [`tools-hub-prototype/`](../../tools-hub-prototype/) Developability Scout | 70% scoring engine, CPU, CLI only | 🟢 algorithm works, no UI yet | **v1 free CPU tool** |
| [`tools-hub-prototype/`](../../tools-hub-prototype/) Library Planner | 60% scoring engine, CPU, CLI only | 🟢 algorithm works, no UI yet | **v1 free CPU tool** |
| [`llm-proteinDesigner/`](../../llm-proteinDesigner/) **Kendrew Modal apps** | 5 Modal apps deployed, shared `base_image.py`, smoke/mini_pilot tier contract | See next rows per tool | **v1+v2 paid GPU tools** |
| ↳ BindCraft ([`bindcraft_app.py`](../../llm-proteinDesigner/infrastructure/modal/bindcraft_app.py)) | A100-80GB; full `pilot_preset()`, 4h timeout | 🟢 **GREEN** — validated end-to-end, no blockers | **Wave 2** |
| ↳ RFdiffusion ([`rfdiffusion_app.py`](../../llm-proteinDesigner/infrastructure/modal/rfdiffusion_app.py)) | A100-40GB; smoke + mini_pilot presets | 🟡 **YELLOW** — smoke passes; mini_pilot blocked by JAX XLA JIT cold-start. Commits `064266f` + `97ec005` may have resolved it — re-validate. [`blocker-rfdiffusion.md`](../../llm-proteinDesigner/docs/blocker-rfdiffusion.md). | Wave 2 smoke-tier; Wave 3 full |
| ↳ RFantibody ([`rfantibody_app.py`](../../llm-proteinDesigner/infrastructure/modal/rfantibody_app.py)) | A100-40GB; full presets, Dockerfile Layer-1 checks validate RF2+RFdiff+MPNN | 🟢 **GREEN** — commit `64c4ab0` verifies 2× consecutive smoke (210s, 62s) + 2× consecutive mini_pilot (264s, 166s) with real pAE/ipAE/pLDDT floats and ~3600-ATOM PDBs. | **Wave 2** |
| ↳ BoltzGen ([`boltzgen_app.py`](../../llm-proteinDesigner/infrastructure/modal/boltzgen_app.py)) | A100-40GB; smoke + mini_pilot presets, cuequivariance baked | 🟢 **GREEN** — commit `4e9eaa1` verifies smoke PASS twice (~4.5 min) + mini_pilot PASS twice (~6 min) with real ipTM/pLDDT floats. | **Wave 2** |
| ↳ PXDesign ([`pxdesign_app.py`](../../llm-proteinDesigner/infrastructure/modal/pxdesign_app.py)) | A100-80GB; presets exist | 🟢 **GREEN** — commit `5f22eec` (stacked on `f41e17e` cuDNN 9 + preflight fix) verifies 2× consecutive mini_pilot with real AF2-IG scores: run 1 ipTM=0.75/pLDDT=94.0/pAE=6.21 filter=pass (918 s cold); run 2 ipTM=0.09/pLDDT=95.0/pAE=25.94 filter=fail (204 s warm). Both decode to 1243-ATOM parseable PDBs; scoring discriminates correctly. One smoke-tier entry still outstanding. [`blocker-pxdesign.md`](../../llm-proteinDesigner/docs/blocker-pxdesign.md) resolved. | **Wave 2** |
| [`backend/pipelines/`](../../llm-proteinDesigner/backend/pipelines/) | Per-tool Python wrappers with `pilot_preset()`, `smoke_preset()`, `mini_pilot_preset()`, typed error taxonomy | 🟢 Ready; importable via `PYTHONPATH` hop | Portable pipeline layer |
| [`backend/gpu/modal.py`](../../llm-proteinDesigner/backend/gpu/modal.py) | `ModalProvider.submit_job`, RunPod-emergency fallback, webhook signing | 🟢 Ready | GPU dispatch layer |
| [`rfantibody-workspace/`](../../rfantibody-workspace/), [`boltzgen-workspace/`](../../boltzgen-workspace/) | Bash + RunPod scripts | **Deprecate** — Modal apps supersede | — |
| [`RANOMICS-DESIGN-SYSTEM.md`](../../RANOMICS-DESIGN-SYSTEM.md) | Complete design tokens | Ready | Style foundation |
| Supabase auth | Shared Scout ↔ tools-hub | Ready | Auth |
| Stripe Payment Links (bold48 pattern) | In production elsewhere | Ready | Billing |

**Key insight:** four of five pipelines are GREEN on commit (BindCraft, RFantibody, BoltzGen, PXDesign). Wave 2 can ship four GPU pipelines plus the ProteinMPNN standalone atomic. RFdiffusion alone is the remaining validation track — its blocker-fix commits materially changed the execution path, so one fresh mini_pilot GPU run is still owed before ship.

---

## Atomic primitives — the real tool catalog

Users want the primitives (AF2, MPNN, ColabFold, Boltz-2) as standalone tools alongside the composite pipelines. None exists as a standalone Modal endpoint today. Each atomic is a minimal image + a Modal app + its own validation. Full per-primitive build spec lives in [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md). Summary here:

| Primitive | Dependency source | Effort | Demand | Pricing |
|---|---|---|---|---|
| **ProteinMPNN standalone** | Install recipe from `docker/rfdiffusion/Dockerfile.modal` | 1 day | **Very high — loss-leader** | 1 credit |
| **AF2 standalone (ColabFold-style)** | JAX AF2 from `docker/bindcraft/Dockerfile.modal`; MSA from ColabFold MMseqs2 public server | 1.5 days | Very high | 2 credits |
| **ColabFold (no-MSA fast fold)** | ColabFold already installed in RFdiffusion image | 1 day | High | 2 credits |
| **ESMFold** | New — pip install `esm` | 1 day | High | 1 credit |
| **AF2 Initial Guess (AF2-IG)** | JAX AF2 from PXDesign image (cuDNN 9 fix validated on `5f22eec`) | 1 day | High | 2 credits |
| **RF2 standalone (antibody fold)** | RF2 from `docker/rfantibody/Dockerfile.modal` | 1.5 days | Medium | 3 credits |
| **RFdiffusion standalone backbone** | Already atomic, already in RFdiffusion image | 1 day | High | 3 credits |
| **LigandMPNN** | Not installed anywhere | 2 days | High | 2 credits |
| **Boltz-2** (structure + affinity, frontier gap) | Not installed anywhere; public weights + inference via Chai/Boltz repo | 2–3 days | **Very high — frontier gap** | 4 credits (pilot) |

### Why each atomic needs its own Modal app (not a `mode` flag)
1. **Image bloat on wrong boundary** — standalone MPNN on a 40 GB BindCraft image pays cold-start time for AF2 weights it never uses.
2. **GPU SKU mismatch** — MPNN runs happily on an A10G; BindCraft's image forces A100-80GB.
3. **Failure domain contamination** — a bad BindCraft deploy shouldn't break MPNN.
4. **Observability** — Modal FunctionCall metrics roll up per-app; separate apps give per-primitive SLOs.

Rollout order: MPNN (Wave 2, pattern setter) → AF2 (Wave 3) → ColabFold + ESMFold + AF2-IG (Wave 4) → Boltz-2 + LigandMPNN + RF2-standalone + RFdiff-standalone (Wave 5).

---

## Pricing — subscriptions for CPU, credits for GPU

Actual timeouts from Kendrew's pipeline code:

| Tool | GPU SKU | Typical runtime | Timeout |
|---|---|---|---|
| BindCraft | A100-80GB | 30 min – 4 h | 4 h |
| BoltzGen | A100-40GB | 15 – 60 min | 2 h |
| RFantibody | A100-40GB | 15 – 60 min | 1 h |
| RFdiffusion | A100-40GB | 10 – 30 min | ~1 h |
| PXDesign | A100-80GB | 8 min – 1 h | 2 h |

Binder campaigns chain dozens of batches into hundreds of GPU-hours; any fixed job quota is mispriced 40×. Credits for GPU, subscriptions for CPU + perks.

### Tier structure

| Tier | Price | Includes | Target |
|---|---|---|---|
| **Free** | $0 | Scout ×3/mo, Developability ∞, Library Planner ∞, ProteinMPNN ×3/mo, ColabFold ×3/mo, no full pipelines | Academics, lead gen |
| **Scout Pro** | $49/mo | Unlimited Scout + Developability + Library Planner + atomics + API key + **10 GPU credits/mo** | Indie scientist |
| **Lab** | $299/mo | All above + **150 GPU credits/mo** + 20 GB storage + priority + email support | Small biotech |
| **Lab+** | $999/mo | All above + **600 GPU credits/mo** + batch API + Slack support | Mid biotech |
| **Credit top-ups** | $50 / $250 / $1000 | 50 / 275 / 1200 credits (10% and 20% volume discount). Never expire. | Anyone exceeding tier |
| **Enterprise** | Custom | Dedicated A100 pools, SSO, invoicing, SLA, on-prem option | Pharma / CDMO |
| **Binder Pilot handoff** | CRO pricing | "Validate in our lab" CTA on every results page | Existing Ranomics funnel |

### Credit rates (shown in UI before submit)

1 credit = $1 retail. Modal compute cost ≈ 35% of retail → **~65% gross margin**.

| Preset | Max GPU-hrs | SKU | Modal cost | **Price (credits)** |
|---|---|---|---|---|
| ProteinMPNN | 0.1 | A10G-24GB | ~$0.11 | **1** |
| ColabFold | 0.2 | A100-40GB | ~$0.40 | **2** |
| AF2 fold | 0.2 | A100-80GB | ~$0.75 | **2** |
| ESMFold | 0.1 | A100-40GB | ~$0.20 | **1** |
| RFdiffusion pilot | 0.5 | A100-40GB | ~$1.05 | **3** |
| RFdiffusion full | 1 | A100-40GB | ~$2.10 | **6** |
| RFantibody pilot | 0.5 | A100-40GB | ~$1.05 | **3** |
| RFantibody full | 1 | A100-40GB | ~$2.10 | **6** |
| BoltzGen pilot | 1 | A100-40GB | ~$1.85 | **6** |
| BoltzGen full | 2 | A100-40GB | ~$4.20 | **11** |
| BindCraft pilot | 2 | A100-80GB | ~$7.40 | **22** |
| BindCraft full | 4 | A100-80GB | ~$14.80 | **44** |
| PXDesign pilot (post-fix) | 1 | A100-80GB | ~$3.70 | **11** |
| Boltz-2 (when live) | 0.5 | A100-80GB | ~$1.85 | **6** |

Jobs that finish early refund unused GPU-minutes as credits (prorate on actual Modal wall-clock). Timeouts charge full preset price.

### Implementation
- Stripe Payment Link for Scout Pro ($49/mo recurring).
- Stripe Checkout + webhook for Lab / Lab+ / credit packs.
- Supabase columns: `tier`, `credit_balance_cents`, `credits_granted_this_period`, `period_ends_at`.
- Pre-job authorization: deduct preset price; refund delta on completion using Modal's actual billed seconds.
- Phase-2: migrate to Stripe Billing metered usage if warranted.

---

## Seamless integration with ranomics.com

| Layer | ranomics.com (Astro 6 / Vercel) | tools.ranomics.com (Flask / Railway) |
|---|---|---|
| Role | Marketing, blog, SEO, per-tool landing pages | The tools themselves, auth, billing, job dispatch |
| URL | `ranomics.com/tools`, `ranomics.com/tools/<slug>` | `tools.ranomics.com/<slug>` |
| SEO value | High (static, fast) | None (app behind login) |
| Design | `ranomics-website-2026/DESIGN.md` + Tailwind v4 | Same tokens ported to `tools-hub/static/style.css` |

### Rules
1. **Shared visual identity** — port Astro site's header + footer verbatim into `tools-hub/templates/base.html`.
2. **Shared auth cookie** — Flask session cookie `domain=".ranomics.com"`, `secure=True`, `samesite="Lax"`.
3. **Marketing lives on Astro, app lives on Flask** — per-tool MDX pages with "Launch tool →" CTA.
4. **Nav additions** — "Tools" in ranomics.com primary nav; "Services" on tools subdomain points back.
5. **Two-way funnel** — results pages CTA to `ranomics.com/binder-pilot`; binder-pilot CTAs "Try it yourself →" into tools hub.
6. **Shared OG / favicon / meta.**
7. **Analytics** — one PostHog/Plausible project across both domains.

**Not reverse-proxied, not ported to Astro.** Subdomain + shared identity is the standard pattern and ships fastest.

---

## Enterprise hardening gates

### Must fix before ANY paying customer
- [x] **PXDesign cuDNN re-validation** — ✅ resolved by `f41e17e` (cuDNN 9 + preflight GPU init) + `5f22eec` (mini_pilot N=1). 2× consecutive mini_pilot PASS on Modal with real non-stub AF2-IG scores recorded in [VALIDATION-LOG.md](VALIDATION-LOG.md). One smoke-tier entry still owed before UI-exposure.
- [ ] **Stripe webhook signature verification** — audit flagged as unverified in `backend/billing/stripe_client.py`. Add integration test using Stripe's signed test payloads.
- [ ] **Stripe idempotency-key dedup** — currently missing. Store `event.id` from Stripe webhook payloads in a `stripe_events` table; reject duplicates.
- [ ] **Job submission idempotency** — `backend/jobs/dispatch.py` has no dedup; double-click could charge twice. Add `(user_id, tool, input_hash)` unique check within a 60-second window.
- [ ] **Smoke pass per GPU tool before it goes live** — 2 consecutive passes on Modal `staging`, recorded in [VALIDATION-LOG.md](VALIDATION-LOG.md).

### Must fix within 2 weeks of launch
- [ ] **Supabase RLS audit** — every `public.*` user-scoped table has `USING (auth.uid() = user_id)`.
- [ ] **`/metrics` endpoint** — Prometheus format: job counts by tool+status, GPU seconds, credit burn, error rates.
- [ ] **Modal FunctionCall metrics → Sentry** — wire Modal exceptions + timeouts via `backend/jobs/errors.py`.
- [ ] **Smoke tests in CI** — workflow hits Modal `staging` on every PR and blocks merge on failure.
- [ ] **Reverse-proxy security headers** — HSTS, X-Frame-Options, X-Content-Type-Options, CSP documented in Railway config.

### Nice-to-have (Phase 2)
- [ ] Unit tests for `run_pipeline.py` modules.
- [ ] Secrets manager instead of `.env.local`.
- [ ] Per-user rate limiting at signup / top-up.
- [ ] Status page at status.ranomics.com.
- [ ] GDPR deletion workflow.

### Already ready per audit (no action)
JWT verification, secrets loading (env-only), error taxonomy, structured logging w/ PII scrubbing, CORS/CSRF, migrations.

---

## Build–Validate–Deploy protocol

Every GPU tool passes this gate **before** public traffic. Named waves, not dates — wave doesn't advance until gates pass.

### Staging environment (prerequisite, Wave 0)
Modal supports `MODAL_ENVIRONMENT=main | staging`. Set it up as a gate:
1. `.github/workflows/deploy-modal.yml` with a `workflow_dispatch` input `environment: staging | main`.
2. Every push deploys to `staging`. CI runs smoke-tier payloads against staging.
3. Manual promotion to `main` only if smoke passes.
4. tools-hub mirrors: env var `GPU_ENVIRONMENT=staging|production`; test accounts hit staging.

### Per-tool gate (must pass to ship)
1. **Dockerfile Layer-1 checks build cleanly** (`modal deploy` succeeds).
2. **`preflight()` passes** — GPU SKU correct, CLIs on `$PATH`, writable `/tmp/smoke_results.json`.
3. **Smoke tier** — 2 consecutive runs on Modal `staging`, real scores (not stubs).
4. **Mini-pilot tier** (full pipelines only) — 2 consecutive runs, real candidate count matches preset.
5. **Webhook roundtrip** — result arrives in tools-hub with correct `job_id`; credits = actual Modal cost × 2.85.
6. **Manual UX sanity** — Leo runs the tool once in staging UI against the PD-L1 IgV fixture.

Only after all six pass does the tool's UI route unlock in production (feature flag: `FLAG_TOOL_<NAME>=on`).

### Rollback per tool
- Break-glass: `GPU_PROVIDER=runpod_emergency` flips to preserved RunPod path in `backend/gpu/runpod.py`.
- Feature flag off: `FLAG_TOOL_<NAME>=off` hides the route; in-flight jobs finish normally.

---

## Parallel agent workstreams

Seven concurrent streams + one orchestrator. Kendrew Phase-4 proved this pattern (4 parallel agents, strict file ownership per `docs/SMOKE-TEST-SPEC.md`). Stealing it.

### A — Hub Foundation (blocker for most others)
- **Mission:** polish `tools.ranomics.com`. Design tokens, shared nav/auth, Supabase schema, Stripe webhooks + idempotency, credits middleware. Own the contracts other streams consume.
- **ALLOW:** `tools-hub/app.py`, `tools-hub/shared/**`, `tools-hub/webhooks/**`, `tools-hub/templates/base.html`, `tools-hub/templates/_header.html`, `tools-hub/templates/_footer.html`, `tools-hub/static/**`, `tools-hub/gpu/modal_client.py` (contract owner), `tools-hub/billing/**` (new), `tools-hub/supabase/migrations/`.
- **DENY:** `tools-hub/tools/*/`, anything under `llm-proteinDesigner/`, anything under `ranomics-website-2026/`, `epitope-scout/` app logic beyond paywall hook (that's B).
- **Depends on:** nothing.
- **Publishes:** design tokens, `base.html` partials, `modal_client.submit(preset) → FunctionCall` contract, credits ledger API, Stripe webhook idempotency pattern.
- **Done when:** stub tool route renders with shared nav + tier/credits; Stripe test subscription flips tier + grants credits; webhook replays dedup cleanly.

### B — CPU Tools
- **Mission:** Scout paywall, Developability Scout UI, Library Planner UI.
- **ALLOW:** `epitope-scout/app.py` paywall decorator + `/upgrade` route, `epitope-scout/templates/index.html` paywall UI, `tools-hub/tools/developability/**`, `tools-hub/tools/library_planner/**`, imports of scoring from `tools-hub-prototype/`.
- **DENY:** A's files, any GPU/Modal, Kendrew, other streams' tool dirs.
- **Depends on:** A.
- **Done when:** Scout 3-run cap → upgrade flow → unlimited; Developability + Library Planner forms return results with Binder Pilot CTA.

### C — Pipeline Integrations (BindCraft + RFantibody + BoltzGen)
- **Mission:** wrap the three GREEN pipelines in tools-hub routes. Form → Modal submit → webhook → ranked results + PDB downloads.
- **ALLOW:** `tools-hub/tools/bindcraft/**`, `tools-hub/tools/rfantibody/**`, `tools-hub/tools/boltzgen/**`.
- **DENY:** the Modal apps themselves (E), other `tools/*/`, hub infra, Kendrew outside `backend/pipelines/` read API.
- **Depends on:** A, E (staging re-validation logged).
- **Sub-stream:** BindCraft builds first as template; RFantibody + BoltzGen clone the pattern once reviewed.
- **Done when:** staging user submits each of the three pilots → Modal round-trip → credits debit + prorated refund → results render.

### D — Atomic-primitives Modal factory
- **Mission:** build each atomic Modal app end-to-end. ProteinMPNN first (D1, pattern setter); AF2 / ColabFold / ESMFold / AF2-IG / LigandMPNN / Boltz-2 / RF2-standalone / RFdiff-standalone follow.
- **ALLOW per primitive `<name>`:** `llm-proteinDesigner/infrastructure/modal/<name>_app.py` (new), `llm-proteinDesigner/docker/<name>/**` (new), `llm-proteinDesigner/backend/pipelines/<name>.py` (new), `tools-hub/tools/<name>/**` (new).
- **DENY:** other primitives' files, existing Modal apps (E's turf), `infrastructure/modal/base_image.py` (orchestrator-gated), hub infra, webhooks.
- **Depends on:** A, Orchestrator publishes [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md) spec.
- **Done when per primitive:** gate passes; entry in [VALIDATION-LOG.md](VALIDATION-LOG.md); pricing row locked in this file.
- **D1 (MPNN) is the pattern setter** — don't fork D2..Dn until D1 is committed and reviewed.

### E — Kendrew Validation & Blockers
- **Mission:** code-check GREEN pipelines against HEAD; resolve RFdiffusion JAX cache (fresh mini_pilot owed); stand up Modal `staging` + CI deploy workflow; own the validation log. PXDesign blocker is now closed.
- **ALLOW:** `llm-proteinDesigner/docker/rfdiffusion/**` (JAX cache), `llm-proteinDesigner/docs/blocker-*.md`, `llm-proteinDesigner/.github/workflows/deploy-modal.yml` (new), [VALIDATION-LOG.md](VALIDATION-LOG.md).
- **DENY:** application code, web hub, new Modal apps (D), Kendrew backend outside smoke-run scripts.
- **Depends on:** nothing.
- **Done when:** BindCraft / RFantibody / BoltzGen / PXDesign each have a code-check note against HEAD in [VALIDATION-LOG.md](VALIDATION-LOG.md); RFdiffusion has 1 fresh mini_pilot PASS (execution path changed post `064266f` + `97ec005`). Current commits-on-record: BindCraft GREEN (pre-audit), RFantibody GREEN (`64c4ab0`), BoltzGen GREEN (`4e9eaa1`), PXDesign GREEN (`5f22eec`).

### F — Marketing Site Integration
- **Mission:** Astro presence. Per-tool MDX explainers, `/tools` index, `/binder-pilot` upsell landing, primary-nav "Tools" item, shared OG/favicon pointers.
- **ALLOW:** `ranomics-website-2026/src/content/pages/tools/**`, `ranomics-website-2026/src/pages/binder-pilot.astro`, `ranomics-website-2026/src/pages/tools/**`, nav component files.
- **DENY:** anything outside `ranomics-website-2026/`.
- **Depends on:** B/C/D for tool names + URLs (can stub and refine).
- **Done when:** `ranomics.com/tools` lists each live tool with CTA; `/binder-pilot` accepts tool-hub traffic.

### G — Enterprise Hardening
- **Mission:** all Must-Fix items above.
- **ALLOW:** `tools-hub/middleware/idempotency.py` (new), `tools-hub/middleware/metrics.py` (new), RLS policy migrations coordinated with A, `tools-hub/docs/HARDENING.md` (Phase-2 checklist), Sentry config in `llm-proteinDesigner/backend/main.py` (arbitrated).
- **DENY:** tool implementations, Modal apps, anything that breaks a published contract without orchestrator sign-off.
- **Depends on:** A's schema + Stripe contract.
- **Done when:** idempotency prevents double-charges under stress test; `/metrics` returns counters; RLS present on user-scoped tables; smoke-in-CI blocks merge on failure.

### Orchestrator
Human (Leo) + a standing orchestrator agent.
- Holds this file as source of truth.
- Maintains [VALIDATION-LOG.md](VALIDATION-LOG.md), owns [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md) spec.
- Arbitrates cross-stream contract changes via [ORCH-LOG.md](ORCH-LOG.md).
- Gates Wave transitions; rolls up per-stream status into a weekly readout.
- **Arbitration rules:**
  - Stream B needs a template partial only A owns → B opens PR-style request; A edits.
  - Stream D1 needs a change to `base_image.py` → Orchestrator writes the change; D* rebase.
  - Two streams contend for the same file → Orchestrator splits or sequences.

### Stream × Wave matrix

| Stream | Wave 0 | Wave 1 | Wave 2 | Wave 3 | Wave 4 | Wave 5 |
|---|---|---|---|---|---|---|
| **A — Hub** | Deploy, design, schema, Stripe, credits, idempotency | Stripe Payment Link for Scout Pro | Stripe Checkout for Lab + top-ups | maintenance | maintenance | maintenance |
| **B — CPU tools** | — | Scout paywall, Developability, Library Planner | maintenance | maintenance | — | — |
| **C — Pipeline integrations** | — | — | Build + ship BindCraft, RFantibody, BoltzGen | maintenance | maintenance | — |
| **D — Atomics factory** | — | — | D1: MPNN (pattern-setter) | D2: AF2 standalone | D3: ColabFold, D4: ESMFold, D5: AF2-IG | D6: Boltz-2, D7: LigandMPNN, D8: RF2-standalone, D9: RFdiff-standalone |
| **E — Kendrew validation** | Staging env, CI deploy workflow. BindCraft/RFantibody/BoltzGen/PXDesign GREEN on commits | Code-check the four GREEN pipelines against HEAD; confirm deploy to staging | RFdiffusion fresh mini_pilot (post JAX-cache fix) — the one case the execution path changed | maintenance | maintenance | — |
| **F — Marketing** | Shared header/footer HTML extracted for A | Per-CPU-tool MDX | BindCraft + MPNN MDX | Wave-3 tool MDX | Wave-4 tool MDX | Wave-5 tool MDX |
| **G — Hardening** | Idempotency + RLS plan | Idempotency middleware shipped | /metrics + Sentry | smoke-in-CI | HARDENING.md Phase-2 items | — |
| **Orchestrator** | Writes ATOMIC-TOOLS.md spec | Wave-1 gate | Wave-2 gate | Wave-3 gate per tool | Wave-4 gate per tool | Wave-5 gate per tool |

### Agent spawn recipe

For each stream, the orchestrator spawns a standalone Claude session with:
1. One-paragraph mission (copy from above).
2. ALLOW / DENY file lists verbatim (these are the guard rails).
3. Current Wave goal.
4. Published contracts the stream depends on (file paths + commit SHAs).
5. Pointers to this file + [VALIDATION-LOG.md](VALIDATION-LOG.md) + [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md) + [ORCH-LOG.md](ORCH-LOG.md).

Streams A and E are critical path, start first. B and F can run in parallel with A from day one. C and D1 start once A publishes `modal_client` + credits API. D2..Dn spawn once D1 is committed and reviewed. G runs with A from day one on hardening items that don't depend on schema.

---

## Ship plan (waves, not weeks)

### Wave 0 — Hub polish + enterprise hardening (≈ Week 1)
- Confirm `tools.ranomics.com` routes to Railway (custom domain in Railway dashboard).
- Seamless nav — port Astro header/footer into `tools-hub/templates/base.html`; CSS tokens from [RANOMICS-DESIGN-SYSTEM.md](../../RANOMICS-DESIGN-SYSTEM.md).
- Seamless auth — cookie domain `.ranomics.com`.
- Supabase schema — add `tier`, `credit_balance_cents`, `credits_granted_this_period`, `period_ends_at`, `stripe_events`, `validation_log`.
- Stripe webhook idempotency + job submission idempotency (the Must-Fix items).
- Modal staging environment + CI deploy workflow.

### Wave 1 — Scout Pro + CPU tools live (≈ Week 2)
- `epitope-scout/app.py` paywall: monthly counter, Stripe Payment Link for Scout Pro $49/mo, webhook flips `tier=scout_pro` + grants 10 credits.
- Wire Binder Developability at `tools-hub/tools/developability/`.
- Wire Library Planner at `tools-hub/tools/library_planner/`.
- All three CPU tools show "Validate in our lab →" CTA linking to `ranomics.com/binder-pilot`.
- 3 MDX explainers under `ranomics-website-2026/src/content/pages/tools/`.

**Gate:** Scout Pro signup → paywall flip → unlimited runs, verified with Stripe test subscription.

### Wave 2 — BindCraft + RFantibody + BoltzGen + ProteinMPNN standalone (≈ Week 3)
- BindCraft full pipeline at `tools-hub/tools/bindcraft/`.
- RFantibody full pipeline at `tools-hub/tools/rfantibody/` — GREEN per commit `64c4ab0`.
- BoltzGen full pipeline at `tools-hub/tools/boltzgen/` — GREEN per commit `4e9eaa1`.
- All three use the same `modal_client.submit(preset)` contract from A; all three go staging-first.
- **ProteinMPNN standalone** as new Modal app — pattern setter for [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md).
- Lab tier ($299/mo) live via Stripe Checkout. Credit pre-auth middleware. Credit top-ups.

**Gate:** Lab-tier user submits pilot on each of BindCraft + RFantibody + BoltzGen → each round-trips → credits debit and prorated-refund correctly. Free user submits MPNN → credits deducted → results in <2 min.

### Wave 2.5 — Announce
- LinkedIn post (Leo's voice, no dashes): "We're releasing the computational stack Ranomics uses in-house. Free tier for academics, $49/mo indie, $299/mo small biotech. Validate in our lab when ready."
- X thread w/ screenshots.
- Email Ranomics lead list.
- Show HN, r/bioinformatics, r/proteindesign.
- Monitor: signup → Scout Pro, Scout Pro → Lab, tool use → Binder Pilot CTA clicks.

### Wave 3 — RFdiffusion + AF2 standalone (validation-gated, ≈ Weeks 4–5)
- RFdiffusion full pipeline (smoke-tier; mini_pilot after JAX cache commits `064266f` + `97ec005` re-validated).
- AF2 standalone Modal app — pattern from MPNN applied to AF2. A100-80GB. 2 credits.

### Wave 4 — More atomics (≈ Weeks 6–8)
- PXDesign moved up to Wave 2 (GREEN on `5f22eec`).
- ColabFold standalone — new Modal app (binary already in RFdiffusion image). A100-40GB. 1 credit.
- ESMFold — new minimal Modal image. A100-40GB. 1 credit.
- AF2-IG standalone — new Modal app on PXDesign's (now-validated) cuDNN 9 image. A100-80GB. 2 credits.

### Wave 5 — Frontier + premium atomics (≈ Weeks 9–12)
- Boltz-2 — if public weights + inference available, 2–3 day build. Frontier gap no competitor has UI for.
- LigandMPNN — new Modal app. 2 days. 2 credits.
- RF2 standalone — new Modal app from RFantibody image recipe. 1.5 days. 3 credits.
- RFdiffusion standalone backbone — separate from full pipeline. 1 day. 3 credits.

---

## Phase 2 (post-launch)

Only if Phase 1 has revenue signal:
1. API keys for Scout Pro / Lab — batch scripting.
2. Multi-step workflow builder — one-click "Epitope Scout → RFdiffusion → ProteinMPNN → Developability → rank". The DBTL gap.
3. Boltz-2 if not shipped in Wave 5.
4. Stripe Billing metered usage if pre-auth credit model shows friction.
5. Internal design-data feedback loop — Ranomics pilot results tune scoring. Long-term moat.
6. Remaining enterprise hardening.

**Explicit non-goals:** Kendrew-as-a-full-platform completion, hosting AF3/Chai-2/ESM3, own foundation models.

---

## Moat
- **Ranomics CRO brand** — scientists trust tools shipped by a real lab.
- **Wet-lab handoff** — single-click upsell Tamarind had to partner for.
- **Multi-step workflow** (Phase 2) — the DBTL chain nobody has built.
- **Internal design-data feedback** (Phase 2+) — pilot outcomes tune scoring. Hard to replicate.

---

## Verification

### Hard criteria (Wave-level gates)
- [ ] **Wave 0:** tools.ranomics.com resolves on Railway w/ Ranomics design; Supabase schema migrated; idempotency middleware; Modal staging reachable.
- [ ] **Wave 1:** Free user hits Scout limit → upgrade → Stripe → unlimited; Developability + Library Planner return results; all three show Binder Pilot CTA.
- [ ] **Wave 2:** BindCraft + RFantibody + BoltzGen pilots round-trip; credits debit + refund correctly; MPNN atomic passes its gate.
- [ ] **Wave 3/4/5:** each tool passes the per-tool gate before UI route unlocks.
- [ ] First paying Scout Pro subscriber within 2 weeks of Wave-2.5 announce.
- [ ] First paying Lab subscriber within 4 weeks of announce.
- [ ] First CRO lead attributable to tools-hub CTA within 4 weeks of announce.

### End-to-end smoke (Wave 2 gate)
```
1. Sign up via Supabase at tools.ranomics.com
2. Run 3 Scout jobs → 4th hits paywall
3. Upgrade to Scout Pro ($49 test) → unlimited Scout + 10 credits
4. Run ProteinMPNN standalone (1 credit) → results in <2 min
5. Upgrade to Lab ($299 test) → 150 credits + GPU pipelines unlock
6. Submit BindCraft pilot against PD-L1 IgV fixture
7. Modal app logs show kendrew-bindcraft-prod running
8. Webhook delivers results; credits debited 22, prorated refund applied
9. Results page renders ranked designs + PDB download + "Validate in our lab" CTA
```

### Soft criteria (8-week retrospective)
- CAC per tier (LinkedIn vs organic vs email).
- Free → Scout Pro conversion (Neurosnap benchmark ~5%).
- Scout Pro → Lab conversion.
- Tool use → Binder Pilot booking rate — the wedge hypothesis.
- [VALIDATION-LOG.md](VALIDATION-LOG.md) shows zero customer-facing silent-failure incidents across waves (the anti-PXDesign outcome).
