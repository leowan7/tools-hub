# Design pilot brief — VHH + scFv against TIGIT and IL-4

**Status:** ready to execute. **Scope:** both targets, both formats.
**Prepared for:** a Claude Code session with execution access (local machine,
repo checked out, network, Railway CLI). Written 2026-08-23 against
`claude/tools-hub-vhh-scfv-design-98aeno`.

---

## 0. How to use this document

This is a handoff brief, not a code change. It contains no proposed edits to
`tools-hub`. It exists so a session with execution access can (a) verify the
platform is in the state this plan assumes, (b) dry-run every parameter set
before spending money, and (c) drive the pilot.

**Read §1 before doing anything** — it tells you what you can and cannot
automate. Several obvious-looking automation routes are dead ends and are
documented as such so you don't rediscover them.

### What is actually runnable locally

| Task | How |
|---|---|
| Verify which tools are live | `railway variables --kv \| grep FLAG_` |
| **Dry-run any tool's parameters** | Every adapter's `validate()` is a pure function — no Flask, no Supabase, no Modal. Import and call it. See §7. |
| Run the repo's test suite | `pytest tests/` |
| Inspect cost specs | Read `shared/wallet_estimates.py::TOOL_SPECS`, `shared/wallet.py:116` |
| Submit a GPU design job | **Web UI only** — see §1 |

### Three dead ends, documented so you skip them

1. **`/api/v1/*` is not a design API.** It is the wet-lab experiment ordering
   surface (Adaptyv Foundry-shaped: `POST /experiments`, quotes, results),
   gated behind `ENABLE_PLATFORM_API=1`, and it writes to
   `shared/campaigns.py` — the CRO "Lab projects" funnel. It **cannot submit
   GPU design jobs.** Do not try to drive the pilot through it.
2. **The calibrated antigen catalogue does not contain our targets.** It holds
   exactly five: HER2, PD-L1, CD3ε, VEGF-A, TNF-α
   (`tools/platform_api/calibrated_targets.py`). TIGIT and IL-4 would be
   custom targets requiring a human quote on the wet-lab side.
3. **There is no automatic design → fold → score chain.** It is explicit
   Phase 2 backlog (`docs/PRODUCT-PLAN.md:538`). Each stage is a separate job
   you launch. The one-click "refold" button is the only automation, and it
   does not cover the scFv arm (see §6).

GPU design jobs are submitted through the web UI (`tools.ranomics.com/tools/<slug>`
→ `/tools/<slug>/submit`), or the `/campaigns` and `/targets` pages for fan-out.

---

## 1. Findings — what the platform can do

Audited the whole `tools/` tree. Three things frame the pilot:

1. **VHH is well served (three tools); scFv rests on one tool** —
   `esmfold2-design` — which takes **no structure** and therefore **cannot be
   aimed at an epitope**. The arms are not symmetric; do not gate or budget
   them as if they were.
2. **Neither TIGIT nor IL-4 appears anywhere in the repo.** Zero hits for
   `TIGIT`, `IL-4`, `IL4`, `interleukin`. No stored targets, no presets, no
   prior runs. Fully greenfield.
3. **The epitope decision (blocking vs non-blocking) is deliberately open.**
   The runbook is sequenced so everything unblocked by that decision runs
   first, and the decision is made from real output rather than assumed.

### VHH — three tools, different jobs

| Tool | Role | Constraints that matter |
|---|---|---|
| **RFantibody** `/tools/rfantibody` | de novo VHH aimed at a chosen patch | `build_payload` hardcodes `"framework": "VHH"` (`tools/rfantibody/__init__.py:175`). `_CDR_BOUNDS` is H1/H2/H3 only — L-chain CDRs rejected. **Single target chain only** (`shared/pdb_preflight_rules.py:230`). **Hotspots required** → blocked until the epitope is chosen. Ranks by ipAE ascending; emits no ipTM. |
| **BoltzGen** `/tools/boltzgen` | `nanobody-anything` protocol | **Hotspots optional** (`tools/boltzgen/__init__.py:83-85`) → can run *before* the epitope is chosen. Multi-chain, handles glycans and modified residues. Emits ipTM / pLDDT / refolding_rmsd. |
| **IgGM** `/tools/iggm` | optimise a VHH you already have | `>H` alone = nanobody, `>L` optional. Epitope residues **required**. Presets: complex_prediction, cdr_design, fr_design, affinity_maturation, inverse_design. **Flag off.** Ranks only by `epitope_contacts` — no ipTM. |

In-repo precedent: `content/showcase/01-boltzgen-vhh-immune-coreceptor.md` —
BoltzGen, 2000 VHH designs against a lymphocyte surface receptor, best ipTM
0.974, **one recurring epitope across 32 independent designs**, narrowed to 12
and confirmed with AF2 + Boltz-2. That recurrence is why BoltzGen is usable as
an epitope-discovery instrument in Stage 1.

### scFv — one tool, and it cannot be aimed

**ESMFold2 design** (`/tools/esmfold2-design`, `scfv` preset) is the only tool
designing paired VH+VL with all six CDRs jointly
(`tools/esmfold2_design/__init__.py:11-14`).

- `requires_pdb=False` — **sequence-only. No epitope or hotspot steering.**
- Framework locked to trastuzumab / atezolizumab / ocankitug (humanised IgG1).
- Bundled targets are CD45, CTLA4, EGFR, PD-L1, PDGFR — both our targets are
  paste-your-own-sequence (30–800 aa, canonical residues only).
- Output is a **GS-linkered VH–VL fusion**. Strict pass =
  `cdr_distogram_iptm_proxy > 0.5`.
- Absent from `shared/compute_campaigns.py::SUPPORTED_TOOLS` → **atomic only**;
  cannot join `/targets` multi-tool launch or any campaign fan-out.

**Scheduling consequence: because it cannot be aimed, the entire scFv arm is
unblocked by the epitope decision and starts immediately.**

RFantibody's scFv support was **removed 2026-05-30**
(`docs/SESSION-HANDOFF-2026-05-30.md:9`) — the MPNN stage hardcodes
`--loops H1,H2,H3`, so selecting scFv silently degraded to a VHH-style run with
the wrong framework PDB. **`docs/PRODUCT-PLAN.md:45` still advertises
"VHH / scFv" for RFantibody; that line is stale — do not trust it.**

### Supporting chain

- **Epitope Scout** — PDB upload or 4-char RCSB ID (**no UniProt entry**,
  despite `templates/scout/index.html:4`). Ranks ~5-residue surface patches;
  feasibility tiers with `recommended_scaffold`, `design_scale_min/max`,
  `expected_hit_rate`; `ppi_interfaces` from other chains in the uploaded file;
  `known_binders` from SAbDab with VHH-vs-IgG/Fab classification and
  antigen-side contact residues. Handoff prefills chain + hotspots into
  **rfantibody / bindcraft / pxdesign / boltzgen only** (`scout/handoff.py:49-51`).
- **Boltz-2** — antibody-antigen-trained cofold, strongest orthogonal scorer.
  Built-in `classify()`: strict pass at ipTM > 0.7, complex pLDDT > 0.85,
  > 4 hotspot contacts (`tools/boltz2/run_pipeline.py:98-101`). ≤ 50 binders/job,
  binder 20–400 aa.
- **AF2 / ColabFold** — ipTM + full PAE. **ESMFold is monomer-only** and cannot
  score a complex at all.
- **OpenDDE** — has an `abag` antibody-antigen checkpoint, but scores are
  best-effort scraped and may return `None`.
- **Developability Scout** `/developability` — free, anonymous, instant, CPU.
- **Library Planner** `/library-planner` — delisted but live; natively speaks
  `scFv | VHH | Fab`.

---

## 2. Cost model

Rate card `shared/wallet.py:116-124`; `WALLET_MARKUP = 1.70`.

| GPU | $/s raw | $/s billed | $/hr billed |
|---|---|---|---|
| A100-40GB | 0.000714 | 0.0012138 | $4.37 |
| A100-80GB | 0.001028 | 0.0017476 | $6.29 |
| H100 | 0.002417 | 0.0041089 | $14.79 |

**Quoted figures are HOLDS, not charges.** Settlement is on actual GPU-seconds
with surplus released. The validated RFantibody pilot
(`docs/VALIDATION-LOG.md:51`, job `e29a462d`) **held $4.37 and charged $0.83**
for 474 GPU-s, returning 5 ranked candidates in ~8 min. Budget the hold; expect
to pay materially less.

| Tool | Hold basis | Per delivered design |
|---|---|---|
| BoltzGen | $8.74 flat/run (5000 s @ A100-80GB) | **$0.175** at budget=50 |
| RFantibody (single) | $4.37 per 2-design container | ~$2.19 |
| RFantibody (campaign) | $43.70 per 16-design chunk | ~$2.73 |
| ESMFold2-design | $9.87 per seed (2400 s @ H100) | **$1.65** at batch_size=6 |
| Boltz-2 | ~$0.22/design (180 s) | cap $0.40 |
| AF2 | ~$0.52/design (300 s) | cap $1.50 |
| Developability | free | free |

### Two free levers the default forms leave on the table

Both are documented in the repo's own pilot recipes. **Apply both.**

1. **BoltzGen `budget` does not change the price.** `build_payload` pins the
   generation pool at 200 regardless; `budget` only selects how many are
   returned, and the estimate is flat at $8.74 for budget 1→50. The shipped
   pilot recipe uses `budget: "4"` — its own comment concedes *"lowering the
   budget returns fewer designs for the same money, which is strictly worse."*
   **Always run budget=50.** 46 free designs per run.
2. **ESMFold2-design `batch_size` does not change the price.** Cost scales on
   `n_seeds` (one H100 container each); batch 1, 2, 3 all cost $9.87.
   **Always run batch_size=6.** 6 designs per seed instead of 1.

### Economics that drive the strategy

BoltzGen delivers VHH at **$0.175/design** vs RFantibody's **$2.73** — a 15×
gap. BoltzGen carries the volume; RFantibody earns its place as the
*epitope-steered* arm, since it requires hotspots and BoltzGen does not.

---

## 3. The epitope decision — deferred by design

Mechanism (blocking vs non-blocking) is **undecided**, and deferring is cheap:

- **BoltzGen needs no hotspots**, so VHH discovery proceeds now and itself
  reveals which faces the model favours.
- **ESMFold2-design cannot be aimed at all**, so the scFv arm is unaffected.

Only RFantibody (hotspots required) and IgGM are genuinely blocked.

### The co-crystal trap — read this before running Scout

Scout's feasibility composite includes `interface_competition` (weight 0.10,
`scout/feasibility.py:34-40`), which **penalises surfaces already occupied by a
natural partner**. For a *blocking* binder that is backwards — the
receptor-binding face is exactly the face you want.

**So: run Scout on the UNBOUND structure for patch ranking, and use the complex
separately to locate the functional face.** If you upload a co-crystal, read
`interface_competition` as a *label* of the functional face, not a verdict.

### Decision procedure (executed at the Stage 1 gate)

Per target, assemble four inputs and choose:

| Input | Source |
|---|---|
| Ranked patches + feasibility tier | Scout `/analyze` + `/feasibility` on the **unbound** structure |
| Which patch is the functional face | Scout `ppi_interfaces` on the **complex**, or by eye |
| Where existing antibodies bind | Scout `known_binders` → SAbDab antigen-side `contact_residues` |
| Where BoltzGen *wants* to bind | Stage 1a output — cluster the 50 designs by contact residue |

Choose **blocking** if the functional face scores Moderate or better *and*
BoltzGen already produces designs there. Choose **non-blocking** if the
functional face is Challenging/High-risk while a membrane-distal patch scores
well — the situation the existing showcase navigated (leads sat 11–18 Å clear).

### Targets

Both sit well inside every size envelope (RFantibody 600 aa target / 720
combined; BoltzGen 600/700). TIGIT IgV ectodomain ~120 aa; mature IL-4 ~130 aa.

| Target | Structures needed | Notes |
|---|---|---|
| TIGIT | unbound ectodomain for Scout; TIGIT–PVR/CD155 complex to locate the blocking face | Conventional cell-surface IgV domain. Closest analogue to the existing showcase. Well-precedented blocking mechanism. Run this one first. |
| IL-4 | unbound cytokine for Scout; IL-4/IL-4Rα complex to locate site 1 | Soluble 4-helix bundle, **not** a surface receptor. Blocking means occluding the IL-4Rα interface. Expect the harder read. |

**Action before any GPU spend: pick and verify actual PDB entries.** This brief
deliberately names none — they were not verifiable from the authoring
environment, and a wrong accession silently costs a whole stage. Two supported
routes: type a 4-character RCSB ID into Scout, which fetches server-side
(`scout/routes.py:761-780`) and fails loudly on a bad ID; or upload an
AlphaFold-DB model, which Scout detects via pLDDT in the B-factor column
(`scout/scoring.py:20`).

---

## 4. Runbook

Total hold **~$266**; expected settlement **~$120–180**.

### Stage 0 — Target prep (free)

Per target (TIGIT first):
1. Verify/fetch the unbound structure. Run Scout `/analyze`; pick the chain.
2. Run `/feasibility` on top patches. Record tier, `recommended_scaffold`,
   `expected_hit_rate`, `risk_factors`.
3. Run Scout on the **complex**; capture `ppi_interfaces` and `known_binders`
   contact residues.
4. Record candidate hotspot sets for **both** a blocking and a non-blocking face.

**Gate:** if every patch on a target returns High-risk, stop and reconsider
before spending.

### Stage 1 — Unblocked work, both in parallel (~$77)

**1a — VHH discovery, epitope-agnostic.** BoltzGen, one run per target:

```
preset            = pilot
protocol          = nanobody-anything
target_chain      = <chain>
hotspot_residues  = (leave empty — this is the point)
binder_length_min = 110
binder_length_max = 130
budget            = 50
```
→ 100 designs, **$17.48**. Cluster by contact residue to see which faces the
model favours. Feeds the epitope decision. (110–130 aa is the form's own stated
nanobody range, `templates/tools/boltzgen_form.html:229`.)

**1b — scFv framework screen.** ESMFold2-design, 3 frameworks × 2 targets:

```
preset          = scfv
target_mode     = paste
target_sequence = <antigen sequence, 30–800 aa>
binder_framework = trastuzumab_framework_vhvl | atezolizumab_framework_vhvl | ocankitug_framework_vhvl
n_seeds         = 1
batch_size      = 6
```
→ 6 runs, 36 designs, **$59.22**. Framework is the only real lever on this
tool, so screen it before scaling.

### ⟶ EPITOPE DECISION POINT

Execute §3's procedure using Stage 0 + Stage 1a output. Record the chosen face
and hotspot residues per target. Everything downstream depends on this.

### Stage 2 — VHH steered control (~$9)

RFantibody, one run per target, using Scout's handoff to prefill:

```
preset           = pilot
target_chain     = <chain>
hotspot_residues = <chosen face>
cdr_lengths      = H1:8,H2:7,H3:10-16   (the default)
num_designs      = 2
```
→ 4 designs, **$8.74**. Two designs answer *"is this epitope reachable by a VHH
at all"* — not whether it is a good one.

### Stage 3 — Scale the VHH arm (~$87)

BoltzGen campaign on the better target, now hotspot-steered at the chosen face,
500 designs (10 sub-jobs × 50). **$87.40**. Use `/targets` so chunking,
admission control and fund-and-drain pausing are handled for you.

### Stage 4 — Scale the scFv arm (~$59)

ESMFold2-design, winning framework, `n_seeds=6`, `batch_size=6`.
→ 36 designs, **$59.22**. Standalone atomic jobs — it cannot join `/targets`.

### Stage 5 — Orthogonal scoring (~$22)

- **VHH:** one-click refold, top 10 per run → Boltz-2.
- **scFv:** `esmfold2-design` is **not** a refold source. Export FASTA from the
  job and submit manually to Boltz-2 with the antigen PDB (≤ 50 binders/job).
- Target ~50 designs per arm.

### Stage 6 — Independent confirmation (~$12)

AF2 on the final ~24-design panel. Mirrors the showcase, which confirmed its
panel of 12 with **two** predictors independent of the design step; ten of
twelve held up under both.

### Stage 7 — Developability triage (free)

Every panel member through `/developability`. **For scFv, split at the GS linker
and score VH and VL separately** as chain types `VH` and `VL` — see §6.

---

## 5. Gates and scoring caveats

Repo rule of thumb (`templates/help/getting_started.html`): **roughly 1 in 5
designs clears the in-silico filter on a tractable target.**

| Arm | Gate | Source |
|---|---|---|
| RFantibody | pAE ≤ 5 and ipAE ≤ 6 | `tools/rfantibody/meta.py:99` |
| BoltzGen | ipTM (showcase best 0.974; shortlist band 0.93–0.98) | `content/showcase/02-*.md` |
| ESMFold2 scFv | `cdr_distogram_iptm_proxy > 0.5` | `tools/esmfold2_design/run_pipeline.py:119` |
| Boltz-2 confirm | ipTM > 0.7 **and** complex pLDDT > 0.85 **and** > 4 hotspot contacts | `tools/boltz2/run_pipeline.py:98-101` |

**Kill criterion:** if fewer than 1 in 10 designs clears its own tool's gate at
Stage 1 or 2, the epitope is probably wrong. Return to Stage 0 and pick a
different face rather than buying more designs on the same one.

**Do not cross-compare headline metrics between tools.** No metric is comparable
across tools (`shared/score_legends.py`); the target results page ranks by
within-tool percentile and deliberately carries no composite score. There is
also a documented scar: a generic all-chain-pairs `iptm` was once shipped where
the binder→target `design_iptm` was meant, ~2× too high, across 460 BoltzGen
designs. **Do not assume `iptm` means the same quantity in every payload.**

---

## 6. Gaps and workarounds

| Gap | Workaround |
|---|---|
| scFv designs cannot use one-click refold — `esmfold2-design` and `iggm` are not in `refold.SOURCE_TOOLS` | Export FASTA, submit manually to Boltz-2 with the antigen PDB (≤ 50 binders/job) |
| Developability Scout misreads scFv — its CDR heuristic assumes one ~120 aa domain, so spans and severity escalation are meaningless on a ~250 aa construct (`tools/developability/dimensions/liabilities.py:74-80`) | **Split at the GS linker; score VH and VL separately** as `VH` / `VL` |
| `VHH` chain type aliases to the VH germline pool — no FR2 hallmark tetrad, no VHH germline (`tools/developability/data/germlines.py:90`) | Treat humanness as indicative only for nanobodies |
| No Tm, no immunogenicity/MHC-II anywhere in the platform | Out of scope; handle externally |
| ANARCI/abnumber exist in the repo but only inside `tools/esmfold2_design`'s Modal image | Not importable by the developability scorer; do not expect IMGT numbering |
| `esmfold2-design` cannot join `/targets` multi-tool launch | Run the scFv arm as standalone atomic jobs |
| No automatic design → fold → score chain | Each stage is a manual job; refold is the only automation |
| Scout advertises UniProt input it does not have | Use a 4-char RCSB ID or upload coordinates |
| RFantibody is single-target-chain only | Fine for both targets here; relevant only for a future dimeric target |

### Follow-on decision, not in this budget

**`FLAG_TOOL_IGGM` is off.** IgGM is the only affinity-maturation path once you
have a VHH hit (affinity maturation from a wild-type reference, plus framework
humanisation). Decide whether to enable it *before* Stage 3 produces hits you
then cannot mature.

---

## 7. Pre-flight — do this before Stage 0

### 7.1 Verify live flag state (blocking)

All `FLAG_TOOL_*` are **fail-closed** (`shared/feature_flags.py`); nothing
in-repo sets any of them. `docs/VALIDATION-LOG.md` records **no flag flip for
`esmfold2-design`, `boltz2`, or `opendde`**. `esmfold2-design` is the only scFv
tool; `boltz2` is the primary orthogonal scorer.

```
railway variables --kv | grep FLAG_
```

`shared/compute_campaigns.py:63-71` is explicit that a stale "(off)" comment in
that file was previously wrong and load-bearing — **read the live value, do not
infer it.**

**If `esmfold2-design` is off:** Stages 1b and 4 are blocked; the pilot drops to
the VHH arm only (~$135). Decide whether to flip it or descope.

### 7.2 Dry-run every parameter set (free, catches typos before they cost money)

Adapter `validate()` functions are pure — no Flask, no Supabase, no Modal — so
every parameter block in §4 can be checked locally first:

```python
from tools.boltzgen import validate as bg_validate
from tools.esmfold2_design import validate as e2_validate
from tools.rfantibody import validate as rfab_validate

inp, err = bg_validate({
    "preset": "pilot", "protocol": "nanobody-anything",
    "target_chain": "A", "hotspot_residues": "",
    "binder_length_min": "110", "binder_length_max": "130",
    "budget": "50",
}, {})
assert err is None, err
```

Do this for all four blocks in §4 before touching the web UI. A rejected
parameter after a 30–60 min GPU run is the expensive failure mode this avoids
(cf. the `cdr_lengths` gap that motivated `tests/test_rfantibody_cdr.py`).

### 7.3 Re-check the cost table

`shared/wallet_estimates.py::TOOL_SPECS` bootstrap values are superseded by
historical p90 once ≥ 20 runs land, so §2 drifts. Re-read it and
`shared/wallet.py:116` before quoting a budget to anyone.

### 7.4 Confirm Stage 1a produced a usable distribution

Before trusting any epitope clustering built on Stage 1a, confirm the run
cleared the repo's own PASS bar (`docs/VALIDATION-LOG.md`): **median ipTM > 0.7,
score variance non-degenerate, no all-stub batch.**

---

## 8. Open items for the requester

1. **Blocking or non-blocking** — deferred to the Stage 1 gate by design (§3),
   but if there is already a programme-level answer, it collapses Stage 0 work.
2. **Structures** — verified RCSB IDs or AlphaFold models for both targets are
   the one hard prerequisite for Stage 0.
3. **`FLAG_TOOL_IGGM`** — enable before Stage 3, or accept no maturation path.
