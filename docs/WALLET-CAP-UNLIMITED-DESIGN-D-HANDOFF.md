# Workstream D handoff: lift design caps to "unlimited" (canary-gated)

Status: RESEARCH COMPLETE, NOT SHIPPED. The copy alignment (wallet cap + unlimited
design) shipped separately. This doc specifies the remaining FUNCTIONAL cap-lift so it
can land as its own PR with a per-tool live GPU canary. Each item has a validation step
only an operator with Modal access + a fundable wallet can clear, which is why it is not
auto-committed.

Context: the campaign path (`/campaigns/new` -> `compute_campaign_create`,
`shared/compute_campaigns.py`) is already unlimited / wallet-bounded. Making a tool
"unlimited" means getting it onto the chunker, or (for cheap/fast tools) raising the
single-job cap. `SUPPORTED_TOOLS = ("rfdiffusion", "bindcraft", "boltzgen")`
(`compute_campaigns.py:59`). A campaign child IS an ordinary `tool_jobs` row using the
unchanged `reserve_hold`/settle path, so per-job money mechanics apply uniformly.

Key mechanic confirmed in code: `compute_campaign_create` validates tool params by
injecting a placeholder design count of `1` for `plan.design_param_key`
(`app.py:5263-5264`), so a tool's single-job per-design validator cap (e.g. pxDesign's
`num_designs <= 24`) does NOT block the campaign path. The real per-chunk count is
injected by the driver at dispatch.

---

## D2a — pxDesign campaign support  [SAFE, ready to implement; canary before enabling]

Why safe: a pxDesign campaign chunk sized at 24 designs is byte-for-byte the existing,
validated pxDesign pilot single job (the single-job form already allows `num_designs`
1..24, pilot container 3600s per `preset_gpu_seconds`). The chunker just fans those out.
No new Modal behavior, no bigger container.

Changes in `shared/compute_campaigns.py`:
1. `SUPPORTED_TOOLS = ("rfdiffusion", "bindcraft", "boltzgen", "pxdesign")`
2. `_DESIGN_PARAM_KEY["pxdesign"] = "num_designs"`
3. Chunk size: the spec-derived `_chunk_size_for("pxdesign")` returns ~2 (mis-calibrated:
   `expected_gpu_seconds=3600`, `designs_per_run_baseline=2` -> 1800 gpu_s/design, wrong
   because the pilot actually does 24 designs in that container). Add an explicit override
   so a chunk == a full validated pilot job:
   ```python
   _CHUNK_SIZE_OVERRIDE = {"pxdesign": 24}   # matches the single-job pilot's own max
   ```
   and honor it at the top of `_chunk_size_for` before the spec math.
4. Add `pxdesign` to the campaign form tool `<select>` — it is driven by
   `cc.SUPPORTED_TOOLS` (`app.py:5244`, `templates/runs/new.html:54`), so step 1 wires the
   UI for free. Confirm the boltzgen-only `protocol` field stays hidden for pxdesign
   (the form JS keys off `tool.value === 'boltzgen'`).

Tests (pure, no GPU): extend `tests/test_compute_campaigns.py` — `plan_chunks("pxdesign", N)`
returns `chunk_size == 24`, `total_subjobs == ceil(N/24)`, a positive `budget_usd`, and
`design_param_key == "num_designs"`; and `plan_chunks("pxdesign", 0)` raises.

Canary (Leo): one small real pxDesign campaign (e.g. 48 designs -> 2 sub-jobs) end-to-end
on GPU; confirm both children succeed, designs download, and `balance == SUM(ledger)`.

## D2b — RFantibody campaign support  [BLOCKED on Modal timeout confirmation + canary]

Do NOT ship with the pilot container. `preset_gpu_seconds("rfantibody","pilot") == 1800`,
but the spec implies ~1800 gpu_s/design (`3600s / baseline 2`), so a naive 2-design chunk
needs ~3600s and would TIME OUT the 1800s container. This is the exact case the codebase
deferred (`compute_campaigns.py:55-58`: "rfantibody (~1 design/pilot container) ... gated
behind Phase 4's campaign_chunk preset").

Required before implementing, mirroring how BindCraft was scaled
(`_BINDCRAFT_CAMPAIGN_CONTAINER_S = 36000` + `_total_budget_hours`):
1. Confirm the real Modal function timeout of `ranomics-rfantibody-prod` and whether its
   entrypoint accepts a budget-hours input (Leo / Modal access).
2. Add `_RFANTIBODY_CAMPAIGN_CONTAINER_S` and wire `_campaign_container_seconds` /
   `_campaign_session_inputs` for rfantibody (as bindcraft does), then add it to
   `SUPPORTED_TOOLS` + `_DESIGN_PARAM_KEY` ("num_designs").
3. Verify `gpu/modal_client.py:_build_payload` passes the session input for rfantibody.
4. Canary: one small rfantibody campaign end-to-end; confirm no container timeout and
   `balance == SUM(ledger)`.

## D3 — MPNN single-job cap  [BLOCKED on a cap-chain inconsistency to resolve]

The user chose "raise MPNN's single-job sequence cap" (it is fast/cheap, L4/A10G, and does
not fit the target-PDB campaign form). BUT there is a live inconsistency to resolve first:
- Validator + form cap: `NUM_SEQ_MAX = 200` (`tools/mpnn/__init__.py:27`,
  `templates/tools/mpnn_form.html:72` `max="200"`, hint "1 to 200").
- Pipeline clamp: `NUM_SEQ_MAX = 20` (`tools/mpnn/run_pipeline.py:76`, applied at line 494
  "Defensive clamping").

Confirm which path production uses (adapter `build_payload` -> `ranomics-mpnn-prod` vs the
local `run_pipeline.py` clamp) and whether prod is silently capping at 20. Resolve the two
constants to a single source of truth, THEN raise the cap. MPNN sequence sampling is O(n)
and the diversity filter in `run_pipeline.py` is O(n^2) pairwise Hamming, so a safe
single-container ceiling is ~1000 (5 min-ish, ~1e6 comparisons). Update all three: the
validator constant, `mpnn_form.html` `max` + hint, and the pipeline clamp (or make it
import the validator constant). The `mpnn` absolute hard cap ($150) already bounds cost.

## D1 — auto-route over-threshold single-job submits into a campaign  [money-path; canary]

Goal: on rfdiffusion/bindcraft/boltzgen (and pxDesign once D2a lands) single-job forms,
requesting a count above a per-tool single-container threshold should transparently become
a campaign instead of a doomed single job.

Constraint found: the `requires_wallet` decorator (`app.py:892`) reserves the single-job
hold BEFORE `tool_submit` runs, and a plain redirect cannot carry the PDB upload. So do NOT
branch inside `tool_submit` after the hold. Safe design:
- Client-side: on the single-job form, when `num_designs` exceeds the tool's threshold and
  the tool is campaign-supported, show a "this runs as a campaign" notice and POST to a
  thin new endpoint (or `compute_campaign_create` with field mapping). The single-job
  `requires_wallet` hold is never taken; the campaign's own preauth/hold path handles money.
- Field mapping needed: `num_designs -> requested_designs`, add hidden `tool`, and carry
  `target_pdb`, `target_chain`, `hotspot_residues`, `target_name` (+ boltzgen `protocol`,
  binder length). Reuse the existing `compute_campaign_create` validation.
- Update each form hint to "unlimited; large requests fan out as a campaign" and, on
  pxDesign, replace the `templates/tools/pxdesign_form.html:247` overflow copy ("hundreds
  to thousands ... AI Binder Sprint or a custom campaign. Contact us.") with the campaign
  pointer once D2a is live.
- Canary: submit an over-threshold single-job form and confirm it creates a campaign (not a
  single job), settles cleanly, and no orphan hold is left.

Note: the shared estimate panel copy already points scale at `/campaigns/new` (shipped in
the copy pass, `templates/wallet/_partials.html`), so the messaging is ready for D1.

---

## Canary checklist (per tool, before enabling in prod)
- Run one small real campaign end-to-end on GPU (real spend).
- All children reach a terminal state via the normal poll/webhook/heartbeat/sweeper path.
- Produced designs are downloadable; `balance == SUM(ledger)` holds across all rows.
- `PUBLIC_BASE_URL=https://tools.ranomics.com` when driving from a local script (the
  localhost value gets stamped into the Modal webhook and strands the job — see the
  2026-07-06 canary note in the compute-campaigns handoff).

## Files touched by this workstream
- `shared/compute_campaigns.py` (SUPPORTED_TOOLS, _DESIGN_PARAM_KEY, chunk sizing/overrides,
  container + session inputs for rfantibody)
- `gpu/modal_client.py` (`_build_payload` session inputs, if needed)
- `tools/mpnn/__init__.py`, `tools/mpnn/run_pipeline.py`, `templates/tools/mpnn_form.html`
- `app.py` (D1 endpoint / field mapping)
- `templates/tools/pxdesign_form.html` (D1 overflow copy, with D2a)
- `tests/test_compute_campaigns.py` (pure chunk-planning tests)

Money-path code (`reserve_hold`/settle, `balance == SUM(ledger)`) is NOT modified anywhere
in D — only routing, chunk presets, input caps, and copy.
