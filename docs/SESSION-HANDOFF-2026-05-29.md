# tools-hub - handoff 2026-05-29 (wallet per-tier estimates + rfantibody num_designs, shipped)

Picked up from the 2026-05-28 handoff (all 5 composite GPU tools validated + live). This session closed three of that handoff's follow-up items and shipped them to production. The only friction was an unrelated Railway build-infra regression that briefly blocked the deploy; it is worked around and the deploy is green. All three commits are on `origin/main` and live (deploy `a78eefe9`, `tools.ranomics.com` HTTP 200).

## Done this session
- **Per-tier hold estimates** (`325ec3a`, `shared/wallet_estimates.py` +3 tests). Added `ToolSpec.tier_gpu_seconds`, a per-tier GPU-second override consulted *before* the tool-wide historical p90 (the p90 view is not tier-aware and is dominated by heavy pilot runs). Populated mini_pilot bootstraps for boltzgen (450s), rfantibody (350s), rfdiffusion (450s) from observed validation GPU-s. BoltzGen mini_pilot now estimates ~$0.79 instead of inheriting the $8.74 pilot reservation; pilot unchanged. Full suite green (649 passed, 6 skipped).
- **rfantibody num_designs** (`5d60285`, `tools/rfantibody/__init__.py` + `templates/tools/rfantibody_form.html`). Exposed `num_designs` (1-5, default 2) in the pilot form, mirroring the rfdiffusion pattern; replaced the hardcoded `2` in the pilot payload. Closes the 2026-04-29 follow-up TODO about weak n=2 distribution analysis. Smoke/mini_pilot unchanged.
- **PXDesign doc hygiene** (`fdcaa1e`, `docs/VALIDATION-LOG.md`). The pilot ship-gate summary still read 🟡 SPLIT / "pilot pending" even though the pilot PASS row (job `816fc4a9`, 2026-05-27) already satisfied it. Updated the status block + Wave 4 gate to GREEN; mini_pilot stays BLOCKED + hidden.
- **Deploy unblocked.** Push triggered a Railway build that FAILED at `mise install` ("No GitHub artifact attestations found for python@3.13.0"). Root cause: Railway bumped its Railpack builder image's mise version overnight (`mise-2026.3.17` -> `mise-2026.5.16`); the newer mise enforces GitHub attestations the python-build-standalone 3.13.0 artifact lacks. Not our code. Fixed by setting `MISE_PYTHON_GITHUB_ATTESTATIONS=false` on the `web`/`production` service (the exact remedy mise's error suggests); redeploy `a78eefe9` went SUCCESS.

## Next steps
- **Monitor first organic pilots.** Estimates remain bootstrap values until each tool logs >=20 runs (`MIN_HISTORICAL_RUNS`), at which point per-tool p90 auto-supersedes for the pilot tier. Preview (mini_pilot) tiers stay on the fixed per-tier bootstrap by design.
- **PXDesign mini_pilot re-validation still owed** (Leo-gated). The pilot tier carries launch; mini_pilot is hidden in `tools/pxdesign/__init__.py` pending its own 2x re-validation (paid GPU runs — Leo pulls the trigger).
- **bindcraft mini_pilot** has no `tier_gpu_seconds` override yet (its mini_pilot GPU-s was never captured in VALIDATION-LOG; pxdesign mini_pilot is hidden). If bindcraft preview cost matters, capture a mini_pilot GPU-s figure and add the override the same way.
- **Remove the mise workaround** once Railway/mise resolve the attestation lookup upstream. The `MISE_PYTHON_GITHUB_ATTESTATIONS=false` var can then be deleted from the service.

## Notes (optional)
- The mise/attestation failure will hit **every** Railway Python service on its next rebuild, not just tools-hub (e.g. `kendrew-backend-prod`) — set the same service var if one starts failing at `mise install`. To pull build logs for a specific failed deploy, pass the FULL deployment UUID: `railway logs <uuid> --build --lines N` (a short prefix returns "Deployment not found").
- `expected_gpu_seconds` is now explicitly the *pilot-tier* default; `tier_gpu_seconds` overrides it per preview tier. Smoke still short-circuits to the fixed `SMOKE_TIER_ESTIMATE_USD` and never reads the override.
- Working tree carries one untracked-noise file, `supabase/.temp/cli-latest` (Supabase CLI version cache) — intentionally left uncommitted.
