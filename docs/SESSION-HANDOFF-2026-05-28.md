# tools-hub - handoff 2026-05-28 (GPU tools validated + live)

All four repo-separation GPU tools (RFantibody, RFdiffusion, BindCraft, BoltzGen) are now validated end-to-end in production and live to users. After the llm-proteinDesigner job_id/ToolPayload fix deployed, each tool was re-run as a real pilot through the public web flow against an uploaded target (4ZQK / PD-L1); all four passed, returned real non-stub scores, delivered result files via both transport paths, and billed customers the correct metered amount. The earlier RFantibody failure (job faa607c1) is closed, the feature flags were already on, and the full validation evidence is committed and pushed to main. The only runtime change shipped this session was a recalibration of BoltzGen's pre-authorization estimate, which had been set far too low.

## Done this session
- Re-ran all 4 GPU tools as live production pilots; every one PASSED end-to-end (rfantibody e29a462d, rfdiffusion 5e5109ee, bindcraft 1c4d5803, boltzgen 758c45e5).
- Confirmed the job_id/ToolPayload regression is fixed across the whole batch (Modal v2, commit 3fa0b77); closed the faa607c1 failure with a GREEN evidence row.
- Verified customer billing is correct on every run, including BoltzGen's overrun - the "overcharge" first flagged was a check-script misread, not a real bug (net charge always equals metered actual; balance == sum invariant holds).
- Recalibrated BoltzGen's hold estimate (expected_gpu_seconds 1800 -> 5000) and deployed it; pre-auth now ~$8.74 to match the real ~$8.64 pilot cost instead of under-reserving 2.7x.
- Fixed the local check-pilot.py reporting script (now sums `charge` rows into net_charge; stops the false "byte-loss" label on non-succeeded jobs).
- Logged all 4 ship gates GREEN in docs/VALIDATION-LOG.md and pushed the full evidence trail to origin/main (commits 731041b, c49e27e, d6c6dee, 81fd263, 26bbda2, 4569510, 2ee33b5).
- Confirmed all 9 FLAG_TOOL_* feature flags already `on` in Railway prod and all 4 tool pages live (HTTP 200 with pilot form).

## Next steps
- Confirm the Railway deploy of commit 4569510 (BoltzGen estimate) went green in prod - it is the only runtime-affecting change this session.
- Optionally make hold estimates per-tier so BoltzGen's cheap mini_pilot stops over-reserving ~$8.74 then releasing the surplus (only the pilot tier needed the higher estimate).
- Monitor the first organic customer pilots now that all 4 tools are live, and let historical p90 take over the cost estimates once each tool has >=20 runs.

## Notes (optional)
- Billing model: a job places a `hold` for the pre-auth estimate; on settle the surplus comes back as `hold_release` (actual < estimate) or an overrun is debited as a `charge` of the *delta* (actual > estimate). Net = metered actual either way; the per-tool $ hard cap clamps the worst case.
- BoltzGen is the heaviest tool by far (~82 min, ~4944 GPU-s, ~$8.64/run, $10 hard cap). The other three run 4-20 min and cost under $2.10.
- Feature flags live in Railway env vars (FLAG_TOOL_<NAME>), not code; tool_enabled() fails closed. All 9 tools currently on.
- Evidence detail lives in docs/VALIDATION-LOG.md under the RFantibody / RFdiffusion / BindCraft / BoltzGen sections (each ship gate now 🟢 GREEN).
- Local-only helper scripts scratch/check-pilot.py (poller) and scratch/wallet-audit.py (ledger + balance==sum check) are gitignored; handy for future pilot verification.
