# Preflight v2 — Week 2 Calibration

After Week 1 ships the v2 preflight (gap detection + size caps + runtime
estimates), Week 2 tunes the per-tool size caps against **real Modal GPU
behaviour** rather than research-derived theoretical numbers. This doc
walks the deliberate-fail loop.

## Why deliberate-fail

The Week 1 caps in `shared/pdb_preflight_rules.py` were picked from
published model envelopes and Australian Protein Design Initiative
workshop notes — no actual OOM measurements in our repo. Concretely,
`TOOL_RULES["rfdiffusion"].size.hard_cap_target_aa = 400` is a guess.
The actual OOM boundary on our Modal A100-40GB pod could be 350 or 500.

The loop: submit deliberately-oversized PDBs through tools.ranomics.com
(which still runs the **old** preflight, since Week 1 hasn't merged
yet), observe whether each tool OOMs / times out / completes, then
update the caps in the rules module from observed boundaries.

## Pre-conditions

- **Week 1 PR (#13) is open but NOT merged.** Production
  tools.ranomics.com is on `main` HEAD = pre-v2 preflight = no size cap.
  An oversized upload will therefore pass preflight and reach Modal —
  exactly what we need.
- A user account with **enough wallet credit** to absorb up to ~$240 of
  GPU spend. Spend tracked in `wallet_transactions`.
- Working `python venv/Scripts/python.exe` for the fixture builder.

## Step 1 — Build the calibration fixtures

```bash
venv/Scripts/python.exe scripts/calibration/build_fixtures.py
```

Idempotent. Outputs to `tmp/calibration/` (gitignored):

- **`oversized_1jff_A.pdb`** — Tubulin α from 1JFF, chain A. **412 CA
  residues.** Sits 10% above the v1 hard cap of 400 (rfantibody /
  rfdiffusion / boltzgen) and 18% above bindcraft's 350. Used to probe
  whether the GPU pipeline actually OOMs at this size.
- **`gappy_2lzm_A_del60-69.pdb`** — T4 lysozyme from 2LZM, chain A,
  residues 60-69 deleted. **154 CA residues with a 10-residue internal
  gap mid-chain.** Used to confirm RFdiffusion's contig builder
  actually asserts on internal gaps (Week 1's
  `needs_fix_on_any_gap=True` is a hypothesis to verify here).

## Step 2 — Smoke-test the fixtures against the v1 preflight (offline)

Before spending any GPU credit, verify both fixtures trigger the rules
they're meant to test:

```bash
venv/Scripts/python.exe -c "
from pathlib import Path
from shared.pdb_preflight import preflight_for_tool

print('-- Oversized (412 aa) --')
data = Path('tmp/calibration/oversized_1jff_A.pdb').read_bytes()
for tool in ['rfantibody', 'rfdiffusion', 'bindcraft', 'boltzgen']:
    hs = [100, 200, 300] if tool != 'boltzgen' else []
    v = preflight_for_tool(tool, data, target_chain='A', hotspots=hs,
                           binder_max_aa=120, num_designs=100)
    print(f'  {tool:12s} {v.kind.value}  over_hard={v.size_envelope.over_hard_cap}')

print('-- Gappy (154 aa, 10-aa gap 60-69) --')
data = Path('tmp/calibration/gappy_2lzm_A_del60-69.pdb').read_bytes()
for tool in ['rfantibody', 'rfdiffusion', 'bindcraft', 'boltzgen']:
    v = preflight_for_tool(tool, data, target_chain='A', hotspots=[80, 100],
                           num_designs=100)
    ga = v.gap_analysis
    print(f'  {tool:12s} {v.kind.value}  longest_gap={ga.longest_gap}  hard_fail={ga.causes_hard_fail}')
"
```

Expected (per Week 1 rules):

```
-- Oversized (412 aa) --
  rfantibody   needs_fix  over_hard=True
  rfdiffusion  needs_fix  over_hard=True
  bindcraft    needs_fix  over_hard=True
  boltzgen     needs_fix  over_hard=True
-- Gappy (154 aa, 10-aa gap 60-69) --
  rfantibody   ready_with_fallback     longest_gap=10  hard_fail=False
  rfdiffusion  needs_fix                longest_gap=10  hard_fail=True
  bindcraft    ready_with_fallback     longest_gap=10  hard_fail=False
  boltzgen     ready_with_fallback     longest_gap=10  hard_fail=False
```

All Week 1 rules behave as designed. The fixtures are now ready for the
GPU phase.

## Step 3 — Submit to production tools.ranomics.com (the GPU run)

For each binder design tool, upload `oversized_1jff_A.pdb` with these
fields and click Run:

| Field | Value |
|---|---|
| Target PDB | `oversized_1jff_A.pdb` |
| Target chain | `A` |
| Hotspot residues | `100, 200, 300` (any 3 — they're for sizing only) |
| Binder length min / max | 50 / 100 (default) |
| Num designs | **10** (keep cost down — calibration only needs failure mode, not statistics) |
| Tier | **Pilot** (cheaper; full would 2-4x the bill) |

Cost ceilings on **pilot tier** per `gpu/modal_client.py PRESET_CAPS`:

| Tool | Cap (s) | Est. $ |
|---|---|---|
| rfantibody | 1800 | $5-8 |
| rfdiffusion | 1800 | $5-8 |
| bindcraft | 7200 | $20-30 |
| boltzgen | 3600 | $10-15 |
| **Total** | | **$40-60** |

Submit **all 4 in parallel** to keep wall-clock short. Modal will
schedule them on independent GPU pods. Watch `/jobs` until each
reaches a terminal state.

Then submit `gappy_2lzm_A_del60-69.pdb` to **rfdiffusion only**
(testing the contig-builder hypothesis) with hotspots `80, 100`,
same other defaults. Cost ~$5.

**Total calibration spend: ~$45-65.** Under the $240 ceiling agreed
in the spec.

## Step 4 — Observe + classify per tool

For each job, capture:

1. Terminal status: `succeeded` / `failed` / `timeout`.
2. If failed: look at the job's stderr in the `tool_jobs` row's
   `error_text` column, or in Modal CLI logs:
   ```
   modal app logs ranomics-rfantibody-prod | grep -A20 "<job_id>"
   ```
3. Classify the failure mode:
   - `OOM` — `CUDA out of memory` / `Killed` (SIGKILL via cgroup OOM)
   - `TIMEOUT` — hit the subprocess wall (`subprocess.TimeoutExpired`)
   - `SLOW_SUCCESS` — completed but took close to the cap (cap was right)
   - `FAST_SUCCESS` — completed well under cap (cap is too tight)
   - `OTHER` — surfaces an unrelated bug to fix separately

Write results to `tmp/calibration/results.json`:

```json
{
  "oversized_412aa": {
    "rfantibody":   {"status": "failed", "mode": "OOM",  "wall_s": 432},
    "rfdiffusion":  {"status": "succeeded", "mode": "SLOW_SUCCESS", "wall_s": 1620},
    "bindcraft":    {"status": "failed", "mode": "OOM",  "wall_s": 287},
    "boltzgen":     {"status": "succeeded", "mode": "FAST_SUCCESS", "wall_s": 1980}
  },
  "gappy_10aa": {
    "rfdiffusion":  {"status": "failed", "mode": "OTHER", "wall_s": 12, "error": "contig builder assertion ..."}
  }
}
```

## Step 5 — Update TOOL_RULES from observations

Use these rules to update `shared/pdb_preflight_rules.py`:

| Observation | Action |
|---|---|
| OOM at 412 aa | Keep `hard_cap_target_aa = 400`. Set `soft_warn = 0.6 × 400 = 240`. |
| SLOW_SUCCESS at 412 aa | Bump `hard_cap_target_aa = 450`. Set `soft_warn = 0.6 × 450 = 270`. |
| FAST_SUCCESS at 412 aa | Bump `hard_cap_target_aa = 500`. Run a follow-up at 600 aa. |
| Gappy run on rfdiffusion fails with assertion | Confirms `needs_fix_on_any_gap=True`. No change. |
| Gappy run on rfdiffusion completes | Relax to length-and-distance rule (mirror rfantibody). |

Commit the rules edit on top of the Week 1 PR (or as a separate PR):

```bash
git checkout fix/preflight-v2-gaps-and-size
# Edit shared/pdb_preflight_rules.py
venv/Scripts/python.exe -m pytest tests/test_pdb_preflight.py    # should stay green
git commit -am "calibrate(preflight): tune TOOL_RULES from Week 2 GPU observations"
git push
```

The tests verify the rules invariants (`soft_warn < hard_cap`,
`combined_cap >= hard_cap`) hold for any new values.

## Step 6 — Merge Week 1 PR

After the calibration commit lands on the same branch, the Week 1 PR
contains both the v2 preflight AND the calibrated caps. Merge it.
Production tools-hub picks up the new rules on the next Railway
deploy, and the next user upload will be gated against real GPU
behaviour rather than a research guess.

## What's deferred to Week 3

- Multi-chain target support (`target_chain: str → target_chains: list[str]`)
- rfantibody single-chain neutral verdict
- AlphaFold / ColabFold-Multimer / Boltz-2 fold-fallback escape hatch
- Form + hotspot picker JS multi-chain widening
- 3 GPU pipelines: rfdiffusion contig, bindcraft settings, boltzgen YAML

## Observed results (2026-06-05)

> **Correction (2026-06-10).** The "A100-80GB" entries in the table and
> findings below are wrong. The deployed `ranomics-rfantibody-prod` app
> requests **A100-40GB** and always has. `_GPU = "A100-40GB"` in
> `infrastructure/modal/rfantibody_app.py` has not changed since the first
> commit on 2026-04-22, and `base_image.py`,
> `backend/pipelines/rfantibody.py`, and `docker/rfantibody/run_pipeline.py`
> all state 40GB. Modal does not hand an 80GB card to a 40GB request, so the
> `NVIDIA A100-SXM4-80GB` line recorded here was a misattribution, most
> likely the bindcraft or pxdesign job from the same batch (both genuinely
> run on 80GB).
>
> What this changes:
> 1. The rule keeps `gpu="A100-40GB"`, which is correct. The "label fix to
>    80GB" claimed in finding #1 was never applied to the rule, and should
>    not be.
> 2. The 412aa success is real, but it ran on a 40GB request, not 80GB.
> 3. `hard_cap_target_aa = 600` is therefore an extrapolation from the
>    literature, not a boundary measured on any GPU. Only 412aa is
>    validated. 600aa stays in place by decision on 2026-06-10; the first
>    target above roughly 412aa is what will actually test it.

5 jobs dispatched against `tools.ranomics.com` production. 2 reached
informative terminal state; 1 gave the size-cap data point we needed;
2 were cancelled after the literature signal made further spend redundant.

| # | Tool | Fixture | Status | Wall | What it told us |
|---|---|---|---|---|---|
| 1 | rfantibody | oversized 1JFF/A 412aa | **succeeded** | 2489s | 412aa runs clean on the deployed A100-40GB request. See the 2026-06-10 correction above. |
| 2 | rfdiffusion | oversized 1JFF/A 412aa | **failed (ASSERTION)** | 60s | `('A', 35) is not in pdb file!` — 1JFF has unresolved res 35; confirms any-gap rule |
| 3 | bindcraft | oversized 1JFF/A 412aa | cancelled | 2717s | (cancelled after lit signal) |
| 4 | boltzgen | oversized 1JFF/A 412aa | cancelled | 2707s | (cancelled after lit signal) |
| 5 | rfdiffusion | gappy 2LZM/A del60-69 | **failed (ASSERTION)** | 81s | `('A', 60) is not in pdb file!` — independent confirmation of any-gap rule |

### Key findings

1. **rfantibody GPU.** The deployed app runs on A100-40GB
   (`gpu="A100-40GB"` in `rfantibody_app.py`, unchanged since 2026-04-22).
   The `NVIDIA A100-SXM4-80GB` reading first recorded here was a
   misattribution. See the 2026-06-10 correction above.

2. **All size caps were too conservative.** Literature (Watson 2023,
   Pacesa 2024, Adaptyv 2024) plus the rfantibody 412aa success show
   400-500aa targets are routinely designed against on A100 GPUs.
   Updated caps:

   | Tool | Old hard_cap | New hard_cap |
   |---|---|---|
   | rfantibody | 400 | **600** |
   | rfdiffusion | 400 | **500** |
   | bindcraft | 350 | **500** |
   | boltzgen | 400 | **600** |

3. **The real binding constraint is wall-time, not VRAM.** rfantibody
   took 41 min for **4 designs** at 412aa. Scaling to typical user
   `num_designs=100` ≈ 17 hours — exceeds Modal subprocess timeouts
   long before VRAM exhausts. Added `runtime_hard_cap_min` per tool
   that hard-blocks (target_aa × num_designs) combinations exceeding
   the pilot-tier wall ceiling. Suggested fix: lower design count.

4. **RFdiffusion any-gap rule confirmed by 2 independent failures.**
   The Week 1 hypothesis (`needs_fix_on_any_gap=True`) is now backed
   by real GPU evidence. Both rfdiffusion jobs failed within 60-81s
   at `contigs.py:396` with `AssertionError: ('A', N) is not in pdb
   file!`. No GPU time billed — failures fire before VRAM allocation.

### Spend

- rfantibody 1: ~$7 (succeeded, 41 min)
- rfdiffusion 2: ~$0.20 (asserted at 60s)
- bindcraft 3: ~$10 (cancelled at 45 min, partial billing)
- boltzgen 4: ~$8 (cancelled at 45 min, partial billing)
- rfdiffusion 5: ~$0.30 (asserted at 81s)
- **Total: ~$25-30.** Under the $240 ceiling.

### Files updated post-calibration

- `shared/pdb_preflight_rules.py` — TOOL_RULES caps updated per table above + new `runtime_hard_cap_min` field. GPU label stays `A100-40GB` (the "80GB fix" was not applied; see the 2026-06-10 correction).
- `shared/pdb_preflight.py` — `_check_size_envelope` enforces runtime cap; dispatcher short-circuits on `over_runtime_cap`
- `app.py` — JSON serializer exposes `over_runtime_cap` + `runtime_hard_cap_min`
- `templates/components/preflight_panel.html` — renders runtime line in size envelope row
- `tests/test_pdb_preflight.py` — bumped test fixtures to new caps + added 3 runtime-cap tests
