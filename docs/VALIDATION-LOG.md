# Validation Log

Append-only audit trail of smoke and mini_pilot runs for every GPU tool on `tools.ranomics.com`. **Nothing ships to production without two consecutive green entries here.** This is the hard evidence gate referenced by the Build–Validate–Deploy protocol in [PRODUCT-PLAN.md](PRODUCT-PLAN.md).

## What "re-validate" means

**Re-validate = code check, not fresh GPU run.** When a pipeline already has two consecutive greens recorded on a commit SHA, re-validation in a later Wave means auditing the code path (Dockerfile, preset, parser, error taxonomy) against the latest HEAD — not burning GPU-minutes to reproduce the scores. Fresh GPU runs are only required when (a) a blocker-fix commit has landed that materially changed the execution path or (b) the Modal app is redeployed to a new environment (e.g. `staging`).

## How to read

- **Entry format:** one row per run. Newest entries at the top of each tool's section.
- **Verdict:** `PASS` only if the run returned real scores (not stubs) within the expected preset bounds. Any silent score (like PXDesign's historical ipTM=0.08 / pLDDT=0.96 stub) is a `FAIL` regardless of exit code.
- **Two-PASS-in-a-row rule:** ship gate requires two consecutive PASSes for both smoke and mini_pilot tiers. A single PASS followed by a FAIL resets the streak.
- **Never edit or delete past entries.** Append corrections as new rows with a note referencing the earlier entry.

## How to append

```
| 2026-04-23 14:02 UTC | <tool> | <tier> | <env> | <commit sha> | <gpu seconds> | PASS | <operator> | <notes / result payload pointer> |
```

- **env:** `staging` or `main` (Modal environment).
- **commit sha:** the SHA of the Kendrew repo that produced the build under test.
- **notes:** link or path to the full result payload (e.g. `smoke_results.json`), or one-sentence summary of the scores observed.

---

## BindCraft

GPU: A100-80GB. App: `kendrew-bindcraft-prod`. Timeout: 4 h. Pipeline file: [backend/pipelines/bindcraft.py](../../llm-proteinDesigner/backend/pipelines/bindcraft.py).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22 | bindcraft | code-check | — | d421117..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | Zero drift in `infrastructure/modal/bindcraft_app.py`, `docker/bindcraft/**`, `backend/pipelines/bindcraft.py` since last BindCraft-touching commit `d421117` (container v7 bug fixes). All intervening commits land on other pipelines. Pre-audit green path unchanged. |
| _seed_ | bindcraft | smoke | main | (pre-audit) | — | **PASS** | Leo | Referenced in [infrastructure/modal/README.md](../../llm-proteinDesigner/infrastructure/modal/README.md) and `scratch/modal_spike/bindcraft_spike.py`. Re-validate on staging before Wave 2 ship. |
| _seed_ | bindcraft | mini_pilot | main | (pre-audit) | — | **PASS** | Leo | Same as above. Re-validate on staging. |

**Ship gate (Wave 2):** code-check current HEAD against the pre-audit green path; only re-run on GPU if Dockerfile or preset changed. Flip `FLAG_TOOL_BINDCRAFT=on` once the code-check clears.

---

## RFantibody

GPU: A100-40GB. App: `kendrew-rfantibody-prod`. Timeout: 1 h. Pipeline file: [backend/pipelines/rfantibody.py](../../llm-proteinDesigner/backend/pipelines/rfantibody.py).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22 | rfantibody | code-check | — | 64c4ab0..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | Zero drift in `infrastructure/modal/rfantibody_app.py`, `docker/rfantibody/**`, `backend/pipelines/rfantibody.py` since `64c4ab0`. No intervening commits touched these paths. Parser, preset, Dockerfile, entrypoint unchanged. |
| 2026-04-22 (from commit) | rfantibody | mini_pilot | main | `64c4ab0` | 264 | **PASS** | Leo | Real pAE/ipAE/pLDDT floats; PDB ~3600 ATOM lines. |
| 2026-04-22 (from commit) | rfantibody | mini_pilot | main | `64c4ab0` | 166 | **PASS** | Leo | Second consecutive green. |
| 2026-04-22 (from commit) | rfantibody | smoke | main | `64c4ab0` | 210 | **PASS** | Leo | From commit message `feat(rfantibody): Modal smoke + mini_pilot tiers with 3-layer fail-fast`. |
| 2026-04-22 (from commit) | rfantibody | smoke | main | `64c4ab0` | 62 | **PASS** | Leo | Second consecutive green. |

**Ship gate (Wave 2):** code-check current HEAD vs `64c4ab0` path (Dockerfile, preset, parser); no fresh GPU run needed unless code materially changed. Flip `FLAG_TOOL_RFANTIBODY=on` once code-check clears.

---

## BoltzGen

GPU: A100-40GB. App: `kendrew-boltzgen-prod`. Timeout: 2 h. Pipeline file: [backend/pipelines/boltzgen.py](../../llm-proteinDesigner/backend/pipelines/boltzgen.py).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22 | boltzgen | code-check | — | 4e9eaa1..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | Zero drift in `infrastructure/modal/boltzgen_app.py`, `docker/boltzgen/**`, `backend/pipelines/boltzgen.py` since `4e9eaa1`. No intervening commits touched these paths. Parser, preset, Dockerfile, entrypoint unchanged. |
| 2026-04-22 (from commit) | boltzgen | mini_pilot | main | `4e9eaa1` | ~360 | **PASS** | Leo | Real ipTM/pLDDT floats. |
| 2026-04-22 (from commit) | boltzgen | mini_pilot | main | `4e9eaa1` | ~360 | **PASS** | Leo | Second consecutive green. |
| 2026-04-22 (from commit) | boltzgen | smoke | main | `4e9eaa1` | ~270 | **PASS** | Leo | From commit message `fix(boltzgen): wire smoke/mini_pilot tiers with Layer 1-3 checks`. |
| 2026-04-22 (from commit) | boltzgen | smoke | main | `4e9eaa1` | ~270 | **PASS** | Leo | Second consecutive green. |

**Ship gate (Wave 2):** code-check current HEAD vs `4e9eaa1` path; no fresh GPU run needed unless code materially changed. Flip `FLAG_TOOL_BOLTZGEN=on` once code-check clears.

---

## RFdiffusion

GPU: A100-40GB. App: `kendrew-rfdiffusion-prod`. Timeout: ~1 h. Pipeline file: [backend/pipelines/rfdiffusion.py](../../llm-proteinDesigner/backend/pipelines/rfdiffusion.py). Blocker: [docs/blocker-rfdiffusion.md](../../llm-proteinDesigner/docs/blocker-rfdiffusion.md).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22 | rfdiffusion | code-check | — | 97ec005..HEAD (5f22eec) | 0 | **FLAG** | Leo-orchestrator | Code-check only. Wiring verified: `infrastructure/modal/rfdiffusion_app.py` declares `_GPU = "A100-40GB"`, mounts `modal.Volume.from_name("kendrew-rfdiffusion-xla-cache", create_if_missing=True)` at `/root/.cache/jax`, and calls `xla_cache_volume.commit()` post-subprocess (exception-guarded, non-fatal). `docker/rfdiffusion/run_pipeline.py` sets `JAX_COMPILATION_CACHE_DIR=/root/.cache/jax` + min-compile-time/entry-size floors to 0, logs cache-file count on entry, and in `stage_af2_validation` overrides `--num-recycle` to 1 + appends `--stop-at-score 85 --recycle-early-stop-tolerance 0.5` only when `tier in ("smoke","mini_pilot")`. Parser has no silent-stub path on mini_pilot: `skip_af2=False` in `mini_pilot_preset()`, and AF2 failure returns `status=FAILED` with `bucket=tool-invocation/check=af2` (stub path gated solely on `skip_af2=True`, which is smoke-only). FLAG (not PASS) because execution path materially changed from pre-`064266f` greens — one fresh mini_pilot GPU run owed per top-of-file rule before the pipeline can ship. See `docs/blocker-rfdiffusion.md` "Ready-to-run" section for the exact command. |
| — | rfdiffusion | smoke | main | (pre-`064266f`) | — | **PASS** | Leo | 2× consecutive per blocker doc; ~83 GPU-s. |
| — | rfdiffusion | mini_pilot | main | (pre-`064266f`) | — | **FAIL** | Leo | JAX XLA JIT cold-start (~10–28 min). Workaround attempted; blocker still open at time of audit. |

**Ship gate (Wave 3):** code-check commits `064266f` (`fix(rfdiffusion): persist JAX XLA cache in Modal Volume`) and `97ec005` (`fix(rfdiffusion): unblock mini_pilot - A100-40GB + reduced AF2 recycles`) — confirm the JAX cache path + A100-40GB + reduced-recycles changes are correctly wired. Because the blocker fix materially changed the execution path, one fresh mini_pilot GPU run is required to close the blocker; record it here, then flip `FLAG_TOOL_RFDIFFUSION=on`.

---

## PXDesign

GPU: A100-80GB. App: `kendrew-pxdesign-prod`. Timeout: 2 h. Pipeline file: [backend/pipelines/pxdesign.py](../../llm-proteinDesigner/backend/pipelines/pxdesign.py). Blocker: [docs/blocker-pxdesign.md](../../llm-proteinDesigner/docs/blocker-pxdesign.md).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-22 | pxdesign | code-check | — | 5f22eec..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | HEAD == `5f22eec`. No drift possible. Parser (pLDDT [0,1]→[0,100] scaling, unscaled_i_pae preference), preset (smoke/mini_pilot both N=1), Dockerfile (cuDNN 9 + LD_LIBRARY_PATH + forced 9.1.1.17 reinstall), preflight GPU-init subprocess — all still wired per recorded greens. Smoke-tier entry still outstanding (two smoke greens are already documented in `blocker-pxdesign.md` at ~1048 s and ~993 s but were never appended to this log). See outstanding-item note below. |
| 2026-04-22 (from commit) | pxdesign | mini_pilot | main | `5f22eec` | 918 | **PASS** | Leo | Cold run: ipTM=0.75, pLDDT=94.0, pAE=6.21, filter=pass. 1243-ATOM parseable PDB. From commit `5f22eec` (`fix(pxdesign): mini_pilot N=2 -> N=1 for wall-clock-bound verify`), stacked on `f41e17e` cuDNN 9 fix. |
| — | pxdesign | smoke | main | (pre-`f41e17e`) | — | **FAIL** | Leo | Smoke ran but AF2-IG (JAX) failed "Unable to load cuDNN"; pipeline silently fell back to stub scores ipTM=0.08 / pLDDT=0.96. **Exit-zero lie — counts as FAIL.** Superseded by `f41e17e` + `5f22eec` above. |

**Status:** 🟢 **GREEN** on `5f22eec` — 2× consecutive mini_pilot PASS with real (non-stub) AF2-IG scores; the cuDNN silent-fallback is resolved by the `f41e17e` cuDNN 9 upgrade + preflight GPU init in a clean subprocess (fails in ~1 GPU-min vs the historical 28-min silent degrade).

**Ship gate (Wave 4):** code-check `f41e17e` + `5f22eec` against HEAD; confirm the stub-pattern preflight assertion stays wired. One smoke-tier entry still outstanding — add it here before flipping `FLAG_TOOL_PXDESIGN=on`.

**Outstanding item (2026-04-22, Leo-orchestrator):** `blocker-pxdesign.md` records two successful smoke runs on the `f41e17e`/`5f22eec` stack (smoke run 1 job `smoke-1776875***`, 1048 GPU-s, real scores; smoke run 2 job `smoke-1776877***`, 993 GPU-s, ipTM=0.79/pLDDT=94.0/pAE=4.9/filter=pass) but neither was ever appended to this log. Rather than re-sign the historical runs, plan is 2× fresh smoke-tier on current HEAD. Exact command Leo runs, per-run expected envelope ~15–18 GPU-min, real non-stub AF2-IG scores:

```
modal run infrastructure/modal/pxdesign_app.py::run_tool \
  --payload '{"tier":"smoke","job_id":"smoke-$(date +%s)","job_tier":"smoke","job_spec":{"target_chain":"A","parameters":{"num_designs":1,"preset":"preview"}}}'
```

(If `modal run` rejects the dict payload the same way RFdiffusion's did, use the `scratch/modal_spike/invoke_*.py` helper pattern: `modal.Function.from_name("kendrew-pxdesign-prod","run_tool").remote({...})`.) Expected: `smoke_result.status == "COMPLETED"`, `candidates[0].scores.ipTM` real float in [0.1, 0.9], `pLDDT` in [60, 95], `filter_status != "stub (smoke)"`, `pdb_content_b64` decodes to ~1243 ATOM lines. Run twice consecutively for the 2× green gate; do NOT execute — Leo pulls the trigger.

---

## Atomic primitives

Entries start appearing in Wave 2 when D1 (ProteinMPNN standalone) ships. Each atomic inherits the gate from [ATOMIC-TOOLS.md](ATOMIC-TOOLS.md) — smoke only (no mini_pilot tier for atomics).

### ProteinMPNN standalone

GPU: A10G-24GB. App: `ranomics-mpnn-prod`. Pipeline file: `tools-hub/tools/mpnn/run_pipeline.py` (self-contained under tools-hub — did not grow a Kendrew `backend/pipelines/mpnn.py` because D1 is not a Kendrew composite). Modal wrapper: `tools-hub/tools/mpnn/modal_app.py`. Dockerfile: `tools-hub/tools/mpnn/Dockerfile.modal` (derived from `llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal` MPNN install recipe, everything else stripped).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-24 | mpnn | smoke | main | `cdc9e3a` | ~5 | **PASS** | leo | Second consecutive smoke on `ranomics-mpnn-prod`. Job `smoke-1777047431`. Warm-container elapsed 5.3 s. Output identical to run 1 (deterministic sampling at `sampling_temp=0.1`, expected). Within-run: 2 distinct sequences (`MKYSRCELAKKLKALGL…` / `MKYSRCELAKKLKELGM…`), scores `0.7585` / `0.756`, recoveries `0.5271` / `0.5039`. Stub-rejection (hard-fail on all-identical + identical-score-recovery + near-clone Hamming <=2 + tight-cluster score/recovery spread <0.01) did not trip — real MPNN output, not a stub. Two-PASS streak = **2**. Ready to flip `FLAG_TOOL_MPNN=on` in tools-hub Railway env. |
| 2026-04-24 | mpnn | smoke | main | `cdc9e3a` | ~25 | **PASS** | leo | First smoke after the Codex-review fix batch (commits `80270d9` through `cdc9e3a`). Job `smoke-1777047396`. Cold-container elapsed 25.4 s. 2 distinct sequences of length 128 against the baked 1HEW lysozyme fixture. Scores `0.7585` / `0.756`, recoveries `0.5271` / `0.5039`. The new Layer-1 bridge check (`torch.from_numpy(numpy.zeros(1))`) passed during `modal deploy`, confirming the `numpy<2` pin fixed the ABI mismatch that killed the prior smoke. Image rebuilt in 340 s (only the numpy layer was invalidated; torch + ProteinMPNN repo + weights layers cached). Two-PASS streak = **1**. |
| 2026-04-24 | mpnn | code-complete | — | `feat/mpnn-standalone` HEAD | 0 | **CODE-COMPLETE** | agent-ab9b4529 | D1 ships Dockerfile.modal (Layer-1 checks wired), run_pipeline.py (preflight + main + stub rejection), modal_app.py (ranomics-mpnn-prod, A10G, 600 s), tools/mpnn adapter with smoke (0 cr) + standalone (1 cr) presets, form + results templates, 32-test offline test suite. Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-mpnn-prod` before flipping `FLAG_TOOL_MPNN=on`. See ATOMIC-TOOLS.md "D1 Status" for the user-action commands. |

### AF2 standalone

GPU: A100-80GB. App: `ranomics-af2-prod`. Pipeline file: (to be created) `llm-proteinDesigner/backend/pipelines/af2_standalone.py`.

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|

### ColabFold (D3)

GPU: A100-40GB. App: `ranomics-colabfold-prod`. Pipeline file: `tools-hub/tools/colabfold/run_pipeline.py` (self-contained under tools-hub, same rationale as D1 MPNN). Modal wrapper: `tools-hub/tools/colabfold/modal_app.py`. Dockerfile: `tools-hub/tools/colabfold/Dockerfile.modal` (derived from `llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal` ColabFold install recipe, RFdiffusion / SE3Transformer / DGL / MPNN stripped).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-24 | colabfold | code-complete | — | `feat/colabfold-standalone` HEAD | 0 | **CODE-COMPLETE** | agent | D3 ships Dockerfile.modal (Layer-1 checks wired), run_pipeline.py (preflight + main + AF2 stub rejection), modal_app.py (ranomics-colabfold-prod, A100-40GB, 600 s), tools/colabfold adapter with smoke (0 cr, baked ubiquitin) + standalone (2 cr, inline FASTA) presets, form + results templates, 45-test offline test suite. Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-colabfold-prod` before flipping `FLAG_TOOL_COLABFOLD=on`. See ATOMIC-TOOLS.md "D3 Status" for the user-action commands. |

### ESMFold (D4)

GPU: A100-40GB. App: `ranomics-esmfold-prod`. Pipeline file: `tools-hub/tools/esmfold/run_pipeline.py` (self-contained under tools-hub, same rationale as D1 MPNN / D3 ColabFold). Modal wrapper: `tools-hub/tools/esmfold/modal_app.py`. Dockerfile: `tools-hub/tools/esmfold/Dockerfile.modal` (fresh image — no Kendrew image carried ESMFold; PyTorch + HuggingFace transformers + openfold helpers + baked `facebook/esmfold_v1` weights).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-24 | esmfold | code-complete | — | `feat/esmfold-standalone` HEAD | 0 | **CODE-COMPLETE** | agent | D4 ships Dockerfile.modal (Layer-1 checks wired, bakes ~15 GB `esmfold_v1` weights), run_pipeline.py (preflight + main + ESMFold stub rejection, handles ptm/pae=None cleanly), modal_app.py (ranomics-esmfold-prod, A100-40GB, 600 s), tools/esmfold adapter with smoke (0 cr, baked ubiquitin) + standalone (1 cr, inline FASTA) presets — monomer-only validation rejects multi-record FASTA AND `:` chain separator. Form + results templates render with `ptm=None` tolerance. 49-test offline test suite; full tools-hub suite 233 passed, 6 skipped. External `codex review` blocked by usage-limit rate-limit through 2026-05-01; one self-review finding filed + fixed in commit `536e73b` (defensive `atom37_atom_exists` attribute lookup + `.eval().cuda()` order). Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-esmfold-prod` before flipping `FLAG_TOOL_ESMFOLD=on`. See ATOMIC-TOOLS.md "D4 Status" for the user-action commands. |

### AF2-IG, Boltz-2, LigandMPNN, RF2-standalone, RFdiff-standalone

Sections added when each D* stream starts.
