# HANDOFF 2026-07-10: unlimited design caps (tools.ranomics.com)

## One-line status
Both PRs are **coded, tested, independently QC clean, committed, NOT pushed**. All remaining work is human-gated (Leo deploys and runs GPU canaries). Nothing here is live yet.

## UPDATE 2026-07-11 (pre-merge re-verify + one fix on PR-2)
Re-verified both branches a day later and ran a fresh 5-lens adversarial review. Findings:
- `origin/main` unchanged at `2c9ae03`, so **no rebase needed** (branches are current).
- Both suites reproduce green: PR-1 **1187 passed / 15 skipped**, PR-2 (pre-fix) **1198 passed**.
- **Fixed one confirmed HIGH bug on PR-2** (new commit `78b5738` on top of `c4ce0e9`): pxDesign's campaign wallet HOLD was priced per-design (~12x inflated) because `child_hold_usd` special-cased only boltzgen while its sibling `_estimate_chunk_cost` had already been fixed to treat pxDesign as a fixed-container tool. Effect: the campaign START gate (`first_wave_hold_usd`) demanded ~$157 for a 48-design pxDesign campaign that should gate at ~$13 (and ~$786 vs ~$50 for 240 designs), which would have **falsely blocked the very pxDesign canary in step 2 below**. Money-safe (reserve_hold/settle/ledger untouched; `balance == SUM(ledger)` preserved), but a real admission-gate defect. Root-cause fix: a single `_FIXED_CONTAINER_TOOLS` constant now feeds both functions so they cannot drift again.
- Closed a money-path **test-coverage gap** the review flagged: the D1 backstop's `view_early_return` hold-release branch (its money-safety guarantee) was previously exercised only via a $0-estimate path that skips the hold. Added a direct release-branch test.
- PR-2 tip is now `78b5738`; full suite **1201 passed / 15 skipped**. Independent non-author QC PASS (5/5 checks). Still NOT pushed.
- All other review findings were adversarially refuted (single_container_ceiling-on-error-rerender, MPNN split-deploy, thin JS coverage) as non-defects; the MPNN redeploy note in step 1 already covers the one real ops caveat.

## Why this work exists
On 2026-07-10 tools.ranomics.com shipped copy promising a wallet-cap / unlimited-design model (origin/main tip `2c9ae03`). The campaign path was already unlimited, but the single-job tool forms still capped design counts at single-container hardware limits, so the "unlimited" copy overpromised. This workstream lifts those caps so behavior matches the copy, without ever touching the money path.

Spec: `docs/WALLET-CAP-UNLIMITED-DESIGN-D-HANDOFF.md` (defines D1, D2a, D2b, D3, D5).

## Hard constraints (still in force)
- **Never touch the money path**: `reserve_hold` / `settle`, `balance == SUM(ledger)`. Routing, presets, copy only. Confirmed safe by two independent QC agents.
- **D2 is not done** until one small real pxDesign campaign AND one small real rfantibody campaign each run end-to-end on GPU and settle clean. Real spend, needs Leo. Ship separately from D1/D3/D5.
- Keep pytest green, add tests. No dashes, no emojis, minimal changes.
- Auto-commit only after independent QC via isolated worktree. Do NOT push (Leo deploys).
- Windows test invocation: `PYTHONIOENCODING=utf-8 PYTHONUTF8=1 venv/Scripts/python.exe -m pytest -q`

## Git state (verified 2026-07-10)
- `origin/main` tip: `2c9ae03` (unchanged, both PRs unmerged)
- Worktrees:
  - `C:\Users\lab\Documents\Claude_projects\tools-hub` = `b0c6dad` [feat/showcase-preview-parity] STALE showcase branch, do not build here
  - `C:\Users\lab\tools-hub-wt\pr1` = `7dee0a4` [feat/unlimited-design-caps]
  - `C:\Users\lab\tools-hub-wt\pr2` = `78b5738` [feat/pxdesign-rfantibody-campaigns] STACKED on pr1 (was `c4ce0e9`; +1 fix commit 2026-07-11)
- Both `7dee0a4` and `78b5738` have **zero remote refs** (unpushed).

## PR-1 `feat/unlimited-design-caps` (commit `7dee0a4`, off `2c9ae03`)
Worktree: `C:\Users\lab\tools-hub-wt\pr1`. Scope: D1 + D3 + D5. Two QC passes CLEAN. Full suite **1187 passed**.

- **D1** auto-route over-ceiling single-job submits into a campaign (rfdiffusion, bindcraft). Approach chosen by Leo: **client reroute + server backstop**.
  - `shared/compute_campaigns.py`: added `single_container_ceiling(tool)` accessor (= `_chunk_size_for`; rfdiffusion 12, bindcraft 16).
  - `app.py` tool_form GET: passes `single_container_ceiling` to the template for SUPPORTED_TOOLS.
  - `app.py` tool_submit: server backstop returns campaign-pointer error before `create_job` for over-ceiling counts. The `requires_wallet` decorator auto-releases the hold on early return via `g.wallet_hold_consumed`, so no hold is stranded.
  - `templates/tools/_campaign_reroute.html` (NEW macro): dedicated `type="button"` `#campaign-submit-btn` (the single-job wallet gate selector cannot disable it), re-points form action to the campaign create endpoint, hides single-job panels via `style.display`.
  - Wired into `rfdiffusion_form.html`, `bindcraft_form.html`. `boltzgen_form.html` got a hint link only (its single-job field maxes at exactly one chunk).
- **D3** MPNN cap reconciled and raised 20 to 1000. This fixes a **live bug**: prod (`run_pipeline.py`) silently clamped to 20 while the form promised 200. Touched `tools/mpnn/__init__.py`, `run_pipeline.py`, `mpnn_form.html`, `meta.py`. **Takes prod effect only after `ranomics-mpnn-prod` is redeployed.**
- **D5** shared campaign copy already shipped; pxDesign-specific copy finalized in PR-2 (only true once pxDesign is campaign-supported).
- Minor fix: `static/js/preflight.js` dispatches a synthetic change event after clearing the file input on AlphaFold fallback (prevents a stale campaign button).
- Tests: `tests/test_unlimited_design_caps.py` (NEW), `tests/test_mpnn_smoke.py` (cap-agnostic).

## PR-2 `feat/pxdesign-rfantibody-campaigns` (tip `78b5738` = `c4ce0e9` + 2026-07-11 fix, STACKED on PR-1)
Worktree: `C:\Users\lab\tools-hub-wt\pr2`. Scope: D2a + D2b (+ 2026-07-11 pxDesign hold fix, see the UPDATE block above). QC CLEAN. Full suite **1201 passed / 15 skipped**.

- **D2a pxDesign**: added to `SUPPORTED_TOOLS` + `_DESIGN_PARAM_KEY`. Chunk size override 24 (= validated single-job pilot). `_estimate_chunk_cost` prices pxDesign per-container like boltzgen (QC caught a 12x inflation: $4.37 not $52.44). Validator cap raised 24 to 1000 so over-ceiling passes to the reroute/backstop. pxDesign form + reroute wired; overflow copy reworded.
- **D2b rfantibody**: added to the chunker mirroring bindcraft. `_RFANTIBODY_CAMPAIGN_CONTAINER_S = 36000` (10h), chunk size 16, session budget `_total_budget_hours: 10`. rfantibody has a FLAG-gated single-job form, so its reroute is wired too (carries `cdr_lengths`).
  - **Two Modal numbers recovered from source** (D2b was never actually blocked on Leo's Modal access): `llm-proteinDesigner` `origin/master` `infrastructure/modal/rfantibody_app.py` has `_MAX_SESSION_S = 82800` (23h timeout) and payload key `total_budget_hours`. `modal app list` confirms `ranomics-rfantibody-prod` deployed. The 10h chunk sits well under the 23h hard timeout.
- Tests: extended `tests/test_compute_campaigns.py`, `test_unlimited_design_caps.py`, `test_compute_campaign_routes.py`.

## Known UX gaps (noted, NOT fixed in PR-2; do not expand scope without Leo)
- Generic campaign form collects `binder_length_min/max`, but pxDesign reads a single `binder_length` (defaults 80). Campaigns from the generic form use 80. `validate` still passes.
- Generic campaign form has no `cdr_lengths` field, so rfantibody campaigns from it use the adapter default `H1:8,H2:7,H3:10-16`. `validate` still passes.

## RESUME HERE: what is left (all Leo / human-gated)
1. **Merge PR-1** (`7dee0a4` into main; Railway auto-deploys) and **redeploy `ranomics-mpnn-prod`** so D3 takes prod effect. Verify a >20-seq MPNN run returns >20 sequences after redeploy.
2. **Two GPU canaries before merging PR-2** (real spend, `balance == SUM(ledger)` throughout):
   - one small pxDesign campaign (~48 designs, 2 sub-jobs)
   - one small rfantibody campaign (~32 designs, 2 sub-jobs), confirm no container timeout
   - Drive with `PUBLIC_BASE_URL=https://tools.ranomics.com` (localhost strands the Modal webhook; see the 2026-07-06 canary note).
3. After merges: `git -C tools-hub worktree remove ../tools-hub-wt/pr1` and `../tools-hub-wt/pr2`.

## Where this is also recorded
- Memory: `project_pending_landings.md` (canonical, git-verified status).
- Plan file: `C:\Users\lab\.claude\plans\make-the-remaining-tools-ranomics-com-greedy-kettle.md` (full plan with line refs).
