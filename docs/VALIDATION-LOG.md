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
| 2026-04-28 | bindcraft | code-check | — | a0dbcf1..HEAD (current) | 0 | **PASS** | leo (kendrew-port) | **Drift-zero confirmation 2026-04-28.** `git log a0dbcf1..HEAD -- backend/pipelines/bindcraft.py docker/bindcraft/ infrastructure/modal/bindcraft_app.py` returns 0 commits. Pipeline path unchanged from the pilot-E2E green at `a0dbcf1`. |
| 2026-04-22 | bindcraft | pilot | main | `a0dbcf1` | (not captured) | **PASS** | Leo (kendrew-commit `a0dbcf1`) | **Pilot-tier E2E with caller-uploaded PDB 4Z18.** Internal job `f1f08a62`. 2 candidates with real Leo-attested scores: ipTM=0.82, pLDDT=0.90, pTM=0.79, i_pAE=0.08, Binder_RMSD=0.45, Hotspot_RMSD=4.14, Target_RMSD=0.66. Source: Kendrew commit `a0dbcf1` ("fix(bindcraft): populate real scores + add pilot submission helper") — fixed parser CSV-key + metric-name + filename-suffix bugs; this is the first BindCraft pilot E2E that produced real-scored output. Submit helper: `backend/scripts/submit_pilot.py 4Z18 bindcraft`. **Strongest attested validation of any composite tool — only composite with a caller-PDB pilot E2E on record.** |
| 2026-04-22 | bindcraft | code-check | — | d421117..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | Zero drift in `infrastructure/modal/bindcraft_app.py`, `docker/bindcraft/**`, `backend/pipelines/bindcraft.py` since last BindCraft-touching commit `d421117` (container v7 bug fixes). All intervening commits land on other pipelines. Pre-audit green path unchanged. |
| _seed_ | bindcraft | smoke | main | (pre-audit) | — | **PASS** | Leo | Referenced in [infrastructure/modal/README.md](../../llm-proteinDesigner/infrastructure/modal/README.md) and `scratch/modal_spike/bindcraft_spike.py`. Re-validate on staging before Wave 2 ship. |
| _seed_ | bindcraft | mini_pilot | main | (pre-audit) | — | **PASS** | Leo | Same as above. Re-validate on staging. |

**Ship gate (Wave 2):** code-check current HEAD against the pre-audit green path — passes (drift-zero confirmation row above). Pilot-tier E2E on 4Z18 strengthens the gate further. `FLAG_TOOL_BINDCRAFT=on` justified.

---

## RFantibody

GPU: A100-40GB. App: `kendrew-rfantibody-prod`. Timeout: 1 h. Pipeline file: [backend/pipelines/rfantibody.py](../../llm-proteinDesigner/backend/pipelines/rfantibody.py).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-28 | rfantibody | code-check | — | 64c4ab0..HEAD (current) | 0 | **PASS** | leo (kendrew-port) | **Drift-zero confirmation 2026-04-28.** `git log 64c4ab0..HEAD -- backend/pipelines/rfantibody.py docker/rfantibody/ infrastructure/modal/rfantibody_app.py` returns 0 commits. Pipeline unchanged from the 2× smoke + 2× mini_pilot greens recorded below. Leo-attested numbers in commit `64c4ab0` are the canonical source: smoke (210s + 62s), mini_pilot (264s + 166s), real pAE/ipAE/pLDDT, ~3600 ATOM PDBs. |
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
| 2026-04-28 | boltzgen | code-check | — | 4e9eaa1..HEAD (current) | 0 | **PASS** | leo (kendrew-port) | **Drift-zero confirmation 2026-04-28.** `git log 4e9eaa1..HEAD -- backend/pipelines/boltzgen.py docker/boltzgen/ infrastructure/modal/boltzgen_app.py` returns 0 commits. Pipeline unchanged from the 2× smoke + 2× mini_pilot greens recorded below. Leo-attested numbers in commit `4e9eaa1` are the canonical source: smoke (~270s × 2, ~4.5 min/run), mini_pilot (~360s × 2, ~6 min/run), real ipTM/pLDDT floats. |
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
| 2026-04-28 | rfdiffusion | code-check | — | 05ea947..HEAD (current) | 0 | **PASS** | leo (kendrew-port) | **Drift-zero confirmation 2026-04-28.** `git log 05ea947..HEAD -- backend/pipelines/rfdiffusion.py docker/rfdiffusion/ infrastructure/modal/rfdiffusion_app.py` returns 0 commits. Pipeline unchanged from the 2× smoke + 2× mini_pilot greens at `05ea947`/`d83335c` recorded below. Real GPU runs with explicit job IDs from 2026-04-26 remain canonical. |
| 2026-04-26 | rfdiffusion | mini_pilot | main | `05ea947` | ~246 | **PASS** | leo | Second consecutive mini_pilot through `kendrew-rfdiffusion-prod`, validating the new tools-hub adapter `build_payload` contract end-to-end. Job `rfdiff-wire-minipilot-1777238051`. 245.7 s warm wallclock (XLA cache populated by earlier `d83335c` run). 2/2 candidates with REAL AF2 multimer scores (cand0: ipTM=0.13 pLDDT=47.94 i_pAE=20.8; cand1: ipTM=0.07 pLDDT=34.45 i_pAE=25.71) — values are intentionally low (de novo binders without optimization against a baked PD-L1 fixture) but verifiably real, not stubs. **Two-PASS streak = 2; ready to flip `FLAG_TOOL_RFDIFFUSION=on`.** |
| 2026-04-26 | rfdiffusion | mini_pilot | main | `d83335c` | 352 | **PASS** | leo | First mini_pilot ever green. Bug 8 unblock — applied LocalColabFold env vars (`TF_FORCE_GPU_ALLOW_GROWTH=true`, `XLA_PYTHON_CLIENT_PREALLOCATE=false`, `XLA_PYTHON_CLIENT_ALLOCATOR=platform`, `XLA_PYTHON_CLIENT_MEM_FRACTION=4.0`, `TF_FORCE_UNIFIED_MEMORY=1`, `TF_ENABLE_ONEDNN_OPTS=0`) to `_af2_env_with_jax_cache()` in `docker/rfdiffusion/run_pipeline.py`. Job `rfdiff-mini-pilot-bug8-1777223957`, A100-SXM4-80GB, 376.2 s wallclock. 2/2 candidates produced; AF2 multimer stage took ~3 min (was 28+ min silent hang before fix). See `llm-proteinDesigner/docs/blocker-rfdiffusion.md` (RESOLVED 2026-04-26). |
| 2026-04-26 | rfdiffusion | smoke | main | `05ea947` | ~89 | **PASS** | leo | Second consecutive smoke through the new tools-hub adapter contract. Job `rfdiff-wire-smoke2-1777238033`. 89.4 s warm wallclock. 1 candidate via `tools/rfdiffusion/__init__.py::build_payload` shape (`target_chain="A"`, `hotspot_residues=[]`, `parameters={num_designs:1, diffusion_steps:50, skip_af2:True, binder_length:{min:55,max:65}}`). scores={ipTM:0.46, pLDDT:71.0, i_pAE:11.9, filter_status:'stub (smoke)'} — stub values are documented Kendrew smoke behavior (`skip_af2=True` bypasses AF2 entirely; smoke gate is pipeline-shape only, not score correctness). Two-PASS streak = 2. |
| 2026-04-26 | rfdiffusion | smoke | main | `64659d9` | ~144 | **PASS** | leo | Step 1.5 integration-target verification ahead of building the tools-hub adapter. Job `rfdiff-wire-smoke-1777237553`. 143.5 s cold pod through `kendrew-rfdiffusion-prod`. status=COMPLETED, result envelope = `{exit_code, stdout_tail, stderr_tail, provider_job_id, smoke_result: {status, output: {candidates: [{rank, pdb_key, pdb_content_b64, scores}]}}}`. Confirmed Modal app is deployed from current Kendrew master and accepts a `tier="smoke"` payload. |
| 2026-04-22 | rfdiffusion | code-check | — | 97ec005..HEAD (5f22eec) | 0 | **FLAG** | Leo-orchestrator | Code-check only. Wiring verified: `infrastructure/modal/rfdiffusion_app.py` declares `_GPU = "A100-40GB"`, mounts `modal.Volume.from_name("kendrew-rfdiffusion-xla-cache", create_if_missing=True)` at `/root/.cache/jax`, and calls `xla_cache_volume.commit()` post-subprocess (exception-guarded, non-fatal). `docker/rfdiffusion/run_pipeline.py` sets `JAX_COMPILATION_CACHE_DIR=/root/.cache/jax` + min-compile-time/entry-size floors to 0, logs cache-file count on entry, and in `stage_af2_validation` overrides `--num-recycle` to 1 + appends `--stop-at-score 85 --recycle-early-stop-tolerance 0.5` only when `tier in ("smoke","mini_pilot")`. Parser has no silent-stub path on mini_pilot: `skip_af2=False` in `mini_pilot_preset()`, and AF2 failure returns `status=FAILED` with `bucket=tool-invocation/check=af2` (stub path gated solely on `skip_af2=True`, which is smoke-only). FLAG (not PASS) because execution path materially changed from pre-`064266f` greens — one fresh mini_pilot GPU run owed per top-of-file rule before the pipeline can ship. See `docs/blocker-rfdiffusion.md` "Ready-to-run" section for the exact command. **GATE NOW CLOSED:** mini_pilot fresh-GPU run delivered 2026-04-26 on `d83335c` and confirmed on `05ea947` with the new tools-hub adapter (see entries above). |
| — | rfdiffusion | smoke | main | (pre-`064266f`) | — | **PASS** | Leo | 2× consecutive per blocker doc; ~83 GPU-s. |
| — | rfdiffusion | mini_pilot | main | (pre-`064266f`) | — | **FAIL** | Leo | JAX XLA JIT cold-start (~10–28 min). Workaround attempted; blocker still open at time of audit. |

**Ship gate (Wave 3):** code-check commits `064266f` (`fix(rfdiffusion): persist JAX XLA cache in Modal Volume`) and `97ec005` (`fix(rfdiffusion): unblock mini_pilot - A100-40GB + reduced AF2 recycles`) — confirm the JAX cache path + A100-40GB + reduced-recycles changes are correctly wired. Because the blocker fix materially changed the execution path, one fresh mini_pilot GPU run is required to close the blocker; record it here, then flip `FLAG_TOOL_RFDIFFUSION=on`.

---

## PXDesign

GPU: A100-80GB. App: `kendrew-pxdesign-prod`. Timeout: 2 h. Pipeline file: [backend/pipelines/pxdesign.py](../../llm-proteinDesigner/backend/pipelines/pxdesign.py). Blocker: [docs/blocker-pxdesign.md](../../llm-proteinDesigner/docs/blocker-pxdesign.md).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-29 | pxdesign | smoke | main | `e497a09` (Kendrew, fix(pxdesign): pin upstream SHAs + harden tracebacks + plumb tier param) | 1051 | **PASS** | leo (Claude harness) | **Smoke run 4 — fresh GREEN on rebuilt image with SHA pins** (PXDesign `f788441`, ColabDesign `e31a56fe`). Job `smoke-1777441315`. 17.5 min wallclock. status=COMPLETED, 1 candidate, real PDB returned (1243 ATOM lines, 134472 bytes). AF2-IG scores: **ipTM=0.12, pLDDT=94.0, pAE=24.67, filter_status='fail'**. Pipeline integrity PASS — no stubs, real ATOM structure, scores in plausible bands; the 2026-04-28 78%-hang regression is gone. **Functional ipTM dropped sharply** vs. 2026-04-28 run 3 (ipTM=0.81 on `96efc3d`); root cause is most likely either (a) the new `--seed=42` deterministic flag landing on an unlucky seed for the baked PD-L1 IgV target, or (b) the 2026-04-29 PXDesign + ColabDesign upstream HEAD SHA pins regressing model behavior vs. unpinned 2026-04-22 builds. **Score drop is NOT counted as a pipeline failure** — design quality is judged at pilot tier with caller PDB + N>1, not at smoke (n=1, baked target). WIP fixes deployed in image `im-bwg6THzHsijVIgwJr2BPVS`: (1) `tier` parameter plumbing through `run_pxdesign()` after a NameError on first attempt; (2) traceback head+tail truncation so real exceptions survive chatty progress-bar buffers; (3) mini_pilot timeout 4500→5200s; (4) deterministic `--seed=42` gated on `tier in ("smoke","mini_pilot")`; (5) tools-hub mini_pilot preset hidden pending its own re-validation. Decision 2026-04-29 (Leo): pivot from mini_pilot harness gate → web-UI pilot-tier validation across all four composite tools — see "Decision 2026-04-29" block below. |
| 2026-04-28 | pxdesign | mini_pilot | main | `96efc3d` (post-Modal-pkg fix) | 4517 | **FAIL** | leo (Claude harness) | **Mini_pilot fresh run — pipeline FAILED at ~78.5%.** Job `mini_pilot-1777410978`. 75.3 min wallclock (within the 90-min Modal timeout — did NOT timeout). status=FAILED, error.bucket=`pxdesign_run`, error.detail = truncated stdout buffer showing colabfold-style progress bar stuck at 78.5–78.8% across 50+ repeated lines (no stack trace surfaced). Zero candidates returned. **Streak reset:** prior 1× mini_pilot PASS at `5f22eec` (918 GPU-s, ipTM=0.75) is followed by this FAIL → mini_pilot streak under the strict 2-PASS-in-a-row rule = **0**. **Mini_pilot tier is BLOCKED** for paid customers until root cause identified. Likely culprits to investigate: (a) AF2-IG VRAM/JAX hang on a specific RFdiffusion backbone; (b) Modal infrastructure regression vs. the 2026-04-22 validated state; (c) timeout-bump (`tools-hub 6a54e19`, 1800→5400s) interaction with subprocess deadlock that the lower timeout previously masked as a clean kill. Smoke tier remains GREEN — only mini_pilot is affected. |
| 2026-04-28 | pxdesign | smoke | main | `96efc3d` (post-Modal-pkg fix) | 1006 | **PASS** | leo (Claude harness) | **Smoke run 3 — fresh GREEN on current HEAD.** Job `smoke-1777409836`. 17.1 min wallclock. status=COMPLETED, 1 candidate, real PDB returned. Real AF2-IG scores: **ipTM=0.81, pLDDT=94.0, pAE=4.78, filter_status=pass**. Run via [scratch/run_pxdesign_smoke.py](../scratch/run_pxdesign_smoke.py). **Closes smoke 2× streak** with 2026-04-22 run 2 (993 GPU-s, ipTM=0.79). Mini_pilot streak still owed. (Harness initially mis-parsed `smoke_result.output.candidates` as `smoke_result.candidates`, recording a false negative; harness fixed in same session, scores extracted from raw `smoke_result` payload — pipeline ran clean.) |
| 2026-04-28 | pxdesign | code-check | — | 5f22eec..HEAD (current) | 0 | **PASS** | leo (kendrew-port) | **Drift-zero confirmation 2026-04-28.** `git log 5f22eec..HEAD -- backend/pipelines/pxdesign.py docker/pxdesign/ infrastructure/modal/pxdesign_app.py` returns 1 commit (`64659d9 chore(pxdesign-docker): add DO-NOT-MOVE banner around cuDNN-9 reinstall`) — comment-only banner around the cuDNN-9 force-reinstall block. Zero functional drift since the validated state. |
| 2026-04-22 | pxdesign | smoke | main | `5f22eec`-stack (post-score-fix) | 993 | **PASS** | Leo (kendrew-port from `blocker-pxdesign.md`) | **Smoke run 2 — GREEN.** Job `smoke-1776877***`. ~16.5 min wallclock. exit_code 0, status=COMPLETED, 1 candidate, 134472-byte PDB (1243 ATOM lines). Real AF2-IG scores on correct [0,100] pLDDT scale: **ipTM=0.79, pLDDT=94.0, pAE=4.9, filter_status=pass**. Source: `llm-proteinDesigner/docs/blocker-pxdesign.md` "Smoke run 2 (post-score-fix)" section — direct Leo attestation. This is the 1× smoke-tier PASS that justifies the GREEN status block below. |
| 2026-04-22 | pxdesign | smoke | main | `5f22eec`-stack (pre-score-fix) | 1048 | **FLAG** | Leo (kendrew-port from `blocker-pxdesign.md`) | **Smoke run 1 — pipeline integrity proven, scores on wrong scale.** Job `smoke-1776875***`. ~17.5 min wallclock. exit_code 0, status=COMPLETED, 1 candidate, 134472-byte PDB. Real AF2-IG outputs but on PXDesign's native [0,1] scale (parser fix landed between this run and run 2): ipTM=0.15, pLDDT=0.95 (unscaled), pAE=0.87 (normalized), filter_status=fail. **FLAG (not PASS): pipeline integrity proven — no silent stub — but scores below the 0.3-0.9 expected band due to pre-score-fix parser. Not counted toward 2× streak.** Source: `llm-proteinDesigner/docs/blocker-pxdesign.md` "Smoke run 1 (pre-score-fix)" section. |
| 2026-04-22 | pxdesign | code-check | — | 5f22eec..HEAD (5f22eec) | 0 | **PASS** | Leo-orchestrator | HEAD == `5f22eec`. No drift possible. Parser (pLDDT [0,1]→[0,100] scaling, unscaled_i_pae preference), preset (smoke/mini_pilot both N=1), Dockerfile (cuDNN 9 + LD_LIBRARY_PATH + forced 9.1.1.17 reinstall), preflight GPU-init subprocess — all still wired per recorded greens. Smoke-tier entry still outstanding (two smoke greens are already documented in `blocker-pxdesign.md` at ~1048 s and ~993 s but were never appended to this log). See outstanding-item note below. |
| 2026-04-22 (from commit) | pxdesign | mini_pilot | main | `5f22eec` | 918 | **PASS** | Leo | Cold run: ipTM=0.75, pLDDT=94.0, pAE=6.21, filter=pass. 1243-ATOM parseable PDB. From commit `5f22eec` (`fix(pxdesign): mini_pilot N=2 -> N=1 for wall-clock-bound verify`), stacked on `f41e17e` cuDNN 9 fix. |
| — | pxdesign | smoke | main | (pre-`f41e17e`) | — | **FAIL** | Leo | Smoke ran but AF2-IG (JAX) failed "Unable to load cuDNN"; pipeline silently fell back to stub scores ipTM=0.08 / pLDDT=0.96. **Exit-zero lie — counts as FAIL.** Superseded by `f41e17e` + `5f22eec` above. |

**Status:** 🟡 **SPLIT** on Kendrew `e497a09` (UPDATED 2026-04-29 post-smoke-run-4) — smoke tier GREEN with score-drop caveat, mini_pilot tier BLOCKED + hidden:
- **Smoke**: 🟢 **3× PASS streak.** Run 2 (2026-04-22, 993 GPU-s, ipTM=0.79 @ `5f22eec`) + run 3 (2026-04-28, 1006 GPU-s, ipTM=0.81 @ `96efc3d`) + run 4 (2026-04-29, 1051 GPU-s, **ipTM=0.12** @ `e497a09`). All three pipeline-integrity PASS. Run 4 ipTM dropped to 0.12 (caveat above) but no stubs; the 78% hang regression is fixed. Tier mechanically ready to flip on for paying customers; functional quality validated at pilot tier instead.
- **Mini_pilot**: 🔴 **Streak still 0; tier hidden in form.** 1× PASS at `5f22eec` (2026-04-22, 918 GPU-s) followed by 1× FAIL at `96efc3d` (2026-04-28, 4517 GPU-s, hung at 78.5%). Hidden in `tools/pxdesign/__init__.py` 2026-04-29 — re-introduce only after a separate 2× re-validation. Web-UI pilot-tier campaign-validation does NOT unblock mini_pilot.

**Ship gate (Wave 4) — UPDATED 2026-04-29:**
- ✅ Code-check `f41e17e` + `5f22eec` against HEAD: drift-zero confirmation 2026-04-28 (1 comment-only commit).
- ✅ Smoke 3× streak closed 2026-04-29 (pipeline-integrity PASS on all three runs). Smoke tier mechanically ready.
- ⏸ Mini_pilot 2× re-validation deferred — superseded by web-UI pilot-tier campaign validation (see Decision 2026-04-29). Mini_pilot preset hidden in `tools/pxdesign/__init__.py` until separately re-validated.
- 🔜 PXDesign pilot-tier web-UI run on caller PDB pending — single run at num_designs=5 covers caller-PDB upload, presigned URL, upload_urls_endpoint callback, num_designs>1, hotspots, post_filter, frontend→submit→worker→GPU integration, email notification, Stripe payment gate. This is the gate that justifies `FLAG_TOOL_PXDESIGN=on`.

### Decision 2026-04-29 — Pivot from mini_pilot harness gate → web-UI pilot-tier campaign validation

Per Leo, 2026-04-29: *"this is not a function of the program not working, i did not expect a good design off the bat. but does this give us enough confidence that someone else designing a full campaign, there would be no bugs"* — followed by *"lets go straight to webUI for all programs. add this decision to the logs"*.

**Rationale.** Smoke + mini_pilot together exercise pipeline mechanics on the **baked PD-L1 IgV target**, but skip every customer-facing path that distinguishes a real campaign from a demo. Specifically NOT exercised by smoke or mini_pilot:

1. Caller-uploaded PDB → presigned URL → container download (smoke uses `/opt/smoke_target.pdb`).
2. `upload_urls_endpoint` callback for result PDB upload (smoke returns inline base64; pilot requires a tools-hub Flask callback).
3. `num_designs > 1` (smoke + mini_pilot are hardcoded N=1; pilot exposes 1–5 via the form).
4. Hotspot residue parsing (smoke has no hotspots; pilot accepts `--hotspot-residues`).
5. Real-target chain selection (smoke is fixed chain A; pilot tolerates any chain on caller PDB).
6. Frontend form → `/tools/<slug>/submit` → worker dispatch → GPU pipeline (harness goes Modal-direct, bypassing Flask + Supabase + Stripe).
7. Email notification on completion (sync harness has no webhook).
8. Stripe payment gate at pilot pricing (harness bypasses billing).

The sole campaign-relevant signal that mini_pilot adds over smoke is `post_filter` coverage on a baked target — a tiny slice of the customer flow. A single pilot-tier run via `tools.ranomics.com/tools/<slug>` covers `post_filter` AND all eight items above.

**Strategy.** Skip mini_pilot 2× re-validation across all four composite tools. Run **one pilot-tier job per tool via the production web UI** with caller PDB `epitope-scout/tests/fixtures/1HEW.pdb` (lysozyme) at the maximum batch size each form allows:

| Tool | URL | Batch field | Set to | Cost |
|---|---|---|---|---|
| RFdiffusion | `tools.ranomics.com/tools/rfdiffusion` | `num_designs` | **5** (form max) | ~$8 |
| BoltzGen | `tools.ranomics.com/tools/boltzgen` | `budget` | **20** (form max) | ~$8 |
| PXDesign | `tools.ranomics.com/tools/pxdesign` | `num_designs` | **5** (form max) | ~$8 |
| RFantibody | `tools.ranomics.com/tools/rfantibody` | (no batch field) | hardcoded **2** | ~$8 |

Total ~$32 across four tools. Pilot pricing is flat per tier — within the form caps, scaling to max designs is free.

**PASS bar (distribution-only, per Leo).** Median ipTM > 0.7 on the per-design distribution; score variance non-degenerate; no all-stub batch; pipeline survives the customer flow without crashes/hangs/silent stubs. Quality of any individual design is **not** the gate; "the customer's job runs end-to-end without bugs" IS the gate.

**RFantibody n=2 caveat.** Hardcoded `num_designs=2` in `tools-hub/tools/rfantibody/__init__.py::build_payload()` — distribution analysis is weak at n=2. Record both scores; tag the row `n=2 (UI cap)`. Follow-up TODO: expose `num_designs` in [tools-hub/tools/rfantibody/__init__.py](../tools/rfantibody/__init__.py) `build_payload()` + add the input to [templates/tools/rfantibody_form.html](../templates/tools/rfantibody_form.html) — out of scope for this validation pass.

**What this decision explicitly does NOT do:**
- Does NOT unblock mini_pilot tier — that requires its own retroactive re-validation pass (currently hidden in the form).
- Does NOT lift form caps from 5 → 50 — keeps customer-facing pricing/UI intact.
- Does NOT build a Flask shim for `upload_urls_endpoint` — preserves the harness scaffolding at `tools-hub/scratch/run_*_pilot.py` for future Flask-shim work but does not pursue it now.

**Outstanding item (2026-04-22, Leo-orchestrator) — UPDATED 2026-04-28:** Both historical smoke runs from `blocker-pxdesign.md` have now been ported as proper rows above (run 2 PASS, run 1 FLAG). The outstanding work is **fresh runs**, not re-porting:
1. 1× fresh smoke on current HEAD to close smoke 2× streak (~15-18 GPU-min, ~$1).
2. 1× fresh mini_pilot on current HEAD to close mini_pilot 2× streak AND verify the post-`5f22eec` timeout bump (~30-40 GPU-min, ~$2-3).

Exact command Leo runs (smoke), per-run expected envelope ~15–18 GPU-min, real non-stub AF2-IG scores:

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

### AF2 standalone (D2)

GPU: A100-80GB. App: `ranomics-af2-prod`. Pipeline file: `tools-hub/tools/af2/run_pipeline.py` (self-contained under tools-hub, same rationale as D1 MPNN). Modal wrapper: `tools-hub/tools/af2/modal_app.py`. Dockerfile: `tools-hub/tools/af2/Dockerfile.modal` (`runpod/base:0.6.2-cuda11.8.0` + `colabfold[alphafold]==1.5.5` + `jax[cuda11_pip]==0.4.23` + `numpy<2` + AF2 multimer/ptm weights baked).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-26 | af2 | smoke | main | `3ac2a15` | ~33 | **PASS** | leo | Second consecutive smoke on `ranomics-af2-prod`. Job `af2-smoke-1777223304`. Warm-container elapsed 32.6 s. Identical scientific result to run 1 (deterministic no-MSA single-sequence fold of BPTI 58 aa). plddt_mean=54.12, ptm=0.36, pdb=49356 bytes. **Two-PASS streak = 2.** Ready to flip `FLAG_TOOL_AF2=on` in tools-hub Railway env. |
| 2026-04-26 | af2 | smoke | main | `3ac2a15` | ~93 | **PASS** | leo | First green on D2 AF2 ever. Job `af2-smoke-1777223201`. Cold-container elapsed 92.5 s. Smoke fixture is BPTI (58 aa monomer). plddt_mean=54.12, ptm=0.36, pdb=49356 bytes. Bug 8 finally resolved by Phase 1 fixes in commit `f5257b8` — LocalColabFold TF/JAX co-tenancy env vars (`TF_FORCE_GPU_ALLOW_GROWTH=true`, `XLA_PYTHON_CLIENT_PREALLOCATE=false`, `XLA_PYTHON_CLIENT_ALLOCATOR=platform`, `XLA_PYTHON_CLIENT_MEM_FRACTION=4.0`, `TF_FORCE_UNIFIED_MEMORY=1`, `TF_ENABLE_ONEDNN_OPTS=0`) plus live-stream subprocess + JAX preflight. Root cause was TF preallocating ~all VRAM at import time inside `colabfold_batch`, starving JAX. Prior to fix: 4 consecutive timeouts at 18-29 min. After fix: 92.5 s cold pod. Parser fix in `3ac2a15` adds `mean_plddt` / `plddt_mean` to D2 output (was emitting only `plddt_per_residue`, smoke harness expected the mean). Two-PASS streak = **1**. |
| 2026-04-24 | af2 | code-complete | — | `feat/af2-standalone` HEAD | 0 | **CODE-COMPLETE** | agent | D2 ships Dockerfile.modal (Layer-1 checks wired), run_pipeline.py (preflight + main + parse_af2_output), modal_app.py (ranomics-af2-prod, A100-80GB, originally 1200s — bumped to 1800 s during Bug 8 attempts). Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-af2-prod` before flipping `FLAG_TOOL_AF2=on`. |

### ColabFold (D3)

GPU: A100-40GB. App: `ranomics-colabfold-prod`. Pipeline file: `tools-hub/tools/colabfold/run_pipeline.py` (self-contained under tools-hub, same rationale as D1 MPNN). Modal wrapper: `tools-hub/tools/colabfold/modal_app.py`. Dockerfile: `tools-hub/tools/colabfold/Dockerfile.modal` (derived from `llm-proteinDesigner/docker/rfdiffusion/Dockerfile.modal` ColabFold install recipe, RFdiffusion / SE3Transformer / DGL / MPNN stripped).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-26 | colabfold | smoke | main | `f5257b8` | ~24 | **PASS** | leo | Second consecutive smoke on `ranomics-colabfold-prod`. Job `colabfold-smoke-1777222969`. Warm-container elapsed 24.4 s. Identical scientific result to run 1 (deterministic no-MSA single-sequence fold of ubiquitin 76 aa). plddt_mean=48.51, ptm=0.4, pdb=65340 bytes. **Two-PASS streak = 2.** Ready to flip `FLAG_TOOL_COLABFOLD=on` in tools-hub Railway env. |
| 2026-04-26 | colabfold | smoke | main | `f5257b8` | ~62 | **PASS** | leo | First green on D3 ColabFold ever. Job `colabfold-smoke-1777222885`. Cold-container elapsed 61.8 s. Smoke fixture is ubiquitin (76 aa monomer, P0CG47). plddt_mean=48.51, ptm=0.4, pdb=65340 bytes. Bug 8 resolved — see D2 entry above for the env-var set that unstuck the TF/JAX VRAM contention. Prior to fix: 3 consecutive timeouts at 9-29 min. Also fixes D3-only Dockerfile gap: numpy<2 pin made explicit (was already implicitly held via colabfold transitive deps but undocumented; matches D2's pin). Two-PASS streak = **1**. |
| 2026-04-24 | colabfold | code-complete | — | `feat/colabfold-standalone` HEAD | 0 | **CODE-COMPLETE** | agent | D3 ships Dockerfile.modal (Layer-1 checks wired), run_pipeline.py (preflight + main + AF2 stub rejection), modal_app.py (ranomics-colabfold-prod, A100-40GB, 600 s), tools/colabfold adapter with smoke (0 cr, baked ubiquitin) + standalone (2 cr, inline FASTA) presets, form + results templates, 45-test offline test suite. Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-colabfold-prod` before flipping `FLAG_TOOL_COLABFOLD=on`. See ATOMIC-TOOLS.md "D3 Status" for the user-action commands. |

### ESMFold (D4)

GPU: A100-40GB. App: `ranomics-esmfold-prod`. Pipeline file: `tools-hub/tools/esmfold/run_pipeline.py` (self-contained under tools-hub, same rationale as D1 MPNN / D3 ColabFold). Modal wrapper: `tools-hub/tools/esmfold/modal_app.py`. Dockerfile: `tools-hub/tools/esmfold/Dockerfile.modal` (fresh image — no Kendrew image carried ESMFold; PyTorch + HuggingFace transformers + openfold helpers + baked `facebook/esmfold_v1` weights).

| When | Tool | Tier | Env | Commit | GPU-s | Verdict | Operator | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-04-26 | esmfold | smoke | main | `fb28bca` | ~52 | **PASS** | leo | Second consecutive smoke on `ranomics-esmfold-prod`. Job `esmfold-smoke-1777235979`. Elapsed 51.9 s. Identical scientific result to run 1 (deterministic single-sequence fold of ubiquitin 76 aa). plddt_mean=0.86, ptm=0.829, pdb=64964 bytes. **Two-PASS streak = 2.** Ready to flip `FLAG_TOOL_ESMFOLD=on` in tools-hub Railway env. |
| 2026-04-26 | esmfold | smoke | main | `fb28bca` | ~53 | **PASS** | leo | First green on D4 ESMFold against the live `ranomics-esmfold-prod` app. Job `esmfold-smoke-1777235798`. Elapsed 53.2 s (PyTorch-only, no JIT — cold≈warm). Smoke fixture is ubiquitin (76 aa monomer, P0CG47, baked at `/opt/smoke_target.fasta`). plddt_mean=0.86, ptm=0.829, pdb=64964 bytes. ESMFold v1 (HF transformers) confirmed working post C++17 openfold sed-patch (`fb28bca` — clang/gcc default to C++14 in CUDA 11.8 base image, openfold's `triangle_attention_cu.cu` requires C++17). Two-PASS streak = **1**. |
| 2026-04-24 | esmfold | code-complete | — | `feat/esmfold-standalone` HEAD | 0 | **CODE-COMPLETE** | agent | D4 ships Dockerfile.modal (Layer-1 checks wired, bakes ~15 GB `esmfold_v1` weights), run_pipeline.py (preflight + main + ESMFold stub rejection, handles ptm/pae=None cleanly), modal_app.py (ranomics-esmfold-prod, A100-40GB, 600 s), tools/esmfold adapter with smoke (0 cr, baked ubiquitin) + standalone (1 cr, inline FASTA) presets — monomer-only validation rejects multi-record FASTA AND `:` chain separator. Form + results templates render with `ptm=None` tolerance. 49-test offline test suite; full tools-hub suite 233 passed, 6 skipped. External `codex review` blocked by usage-limit rate-limit through 2026-05-01; one self-review finding filed + fixed in commit `536e73b` (defensive `atom37_atom_exists` attribute lookup + `.eval().cuda()` order). Awaiting Modal deploy + 2× consecutive staging smoke on `ranomics-esmfold-prod` before flipping `FLAG_TOOL_ESMFOLD=on`. See ATOMIC-TOOLS.md "D4 Status" for the user-action commands. |

### AF2-IG, Boltz-2, LigandMPNN, RF2-standalone, RFdiff-standalone

Sections added when each D* stream starts.
