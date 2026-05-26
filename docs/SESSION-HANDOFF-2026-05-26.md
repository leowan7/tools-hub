# Session Handoff — Launch Punch List, 2026-05-26

Picks up after [HANDOFF-CREDITS-CUTOVER.md](HANDOFF-CREDITS-CUTOVER.md) and the wallet-pivot Session 15 (yesterday). Wallet is the sole money path on prod, full signup→topup→MPNN→settle flow validated 2026-05-25. Today closed the legal + cron items that gate opening signups to real users.

---

## Status at session end

- **Code shipped:** commit `dcf5e68` on `main` — `feat(legal): ToS + Privacy pages and required signup consent`. Pushed; Railway auto-deploys triggered for all three services.
- **Railway prod state:** three services
  - `web` (tools.ranomics.com) — building from `dcf5e68`
  - `tools-hub-digest` (existing daily cron, 16:00 UTC) — also rebuilding
  - `tools-hub-sweep-stuck` (NEW today, hourly cron) — first build in flight; first scheduled run at top of next UTC hour after build completes
- **Phase 1 hard blockers:** materially closed (4/5 done, 1 deferred by decision — see below).

---

## What shipped today

### Commit `dcf5e68`
- **New:** `templates/legal/terms.html`, `templates/legal/privacy.html` — boilerplate-placeholder copy covering no-refund, 90-day artifact retention, sub-processor list (Supabase, Stripe, Modal, Cloudflare R2, Resend, Railway). Marked as placeholder at the top of each file; Leo to replace with custom-drafted policies.
- **Modified `app.py`:**
  - Added `@flask_app.route("/terms")` and `@flask_app.route("/privacy")` after the `/pricing` route (~line 1188).
  - `signup()` POST handler now reads `terms_accepted = request.form.get("terms_accepted") == "on"` and rejects with `"You must accept the Terms of Service and Privacy Policy to create an account."` before reaching `register_user`. New `signup_terms` template variable threads through all four `render_template("login.html", mode="signup", …)` call sites.
- **Modified `templates/_footer.html`:** added a fourth "Legal" column with `{{ url_for('terms') }}` and `{{ url_for('privacy') }}`.
- **Modified `templates/login.html`:** required consent checkbox in the signup panel above the submit button; state persists across server-side validation errors via `signup_terms`.
- **Verified locally** on `127.0.0.1:5055`: all four routes return 200 with correct content; POST without ticking the checkbox returns the gate error.

### Railway dashboard
- Duplicated `tools-hub-digest` → renamed to `tools-hub-sweep-stuck`.
- Custom start command: `flask --app app jobs:sweep-stuck` (CLI defined in `cron/sweep_stuck_jobs.py`).
- Cron schedule: `0 * * * *` (Railway labelled this as "Hourly", UTC).
- Inherited 8 env vars + 13 settings + GitHub branch tracking (auto-deploy on push to `main`) from the digest service. Spot-check Variables tab on first cron run.

---

## Phase 1 status

| # | Item | Status |
|---|---|---|
| 1 | ToS + Privacy pages, signup checkbox | ✅ shipped `dcf5e68` |
| 2 | PXDesign + BindCraft verification | ⏸ deferred (see below) |
| 3 | `FLAG_TOOL_*` audit on Railway prod | ✅ all 9 ON, accepted as ground truth |
| 4 | Schedule `sweep-stuck` cron on Railway | ✅ live |
| 5 | Fresh-user smoke test | ✅ done 2026-05-25 (per Leo) — full signup→topup→MPNN→settle |

---

## Open TODO for next session

### 1. Post-deploy spot-check on prod (~3 min)

Once Railway finishes the rebuild of `web` (watch the Deployments tab):

```bash
# Both should return 200 with text/html (NOT the SPA fallback —
# see reference_cf_pages_deploy_verification memory entry)
curl -I https://tools.ranomics.com/terms
curl -I https://tools.ranomics.com/privacy
```

Then open `https://tools.ranomics.com/signup` in a browser:
- Confirm consent checkbox renders with both Terms + Privacy links.
- Submit without ticking → expect "You must accept the Terms of Service and Privacy Policy to create an account."
- (Do not exercise full signup again — that path is covered by yesterday's smoke.)

### 2. Verify the new cron's first run

After the next top-of-hour UTC:
- Railway dashboard → tools-hub-sweep-stuck → Cron Runs tab → confirm a successful execution.
- If it failed, check Variables tab — some env-var references may need to be re-pointed (the Railway duplicate-service flow generally preserves references, but worth verifying once).

### 3. PXDesign pilot-tier validation (Phase 1 item 2, deferred)

This is the only remaining Phase 1 item. The flag state is inconsistent today:

- `FLAG_TOOL_PXDESIGN=on` on Railway prod.
- `docs/VALIDATION-LOG.md` PXDesign section shows status 🟡 **SPLIT** as of 2026-04-29 — smoke tier 3× PASS, mini_pilot tier hidden in `tools/pxdesign/__init__.py`, pilot-tier web-UI campaign run still owed.

Resolve via one PXDesign pilot job on a caller-supplied PDB (PD-L1 is the standard fixture). Confirm:
- Caller-PDB upload via presigned URL works.
- `upload_urls_endpoint` callback delivers result PDBs.
- Hotspots + post_filter populated; `num_designs > 1`.
- Email notification fires on completion.
- Score table populates with real ipTM / pLDDT / i_pAE — not "0 candidates", not stub values.

Append a new row to the PXDesign section of `docs/VALIDATION-LOG.md` with the outcome. If GREEN, the prod flag state is justified; if RED, flip `FLAG_TOOL_PXDESIGN=off` until fixed.

BindCraft `_METRIC_MAP` expansion is also flagged unverified in `project_demo_run_verification_pending` memory, but VALIDATION-LOG marks BindCraft SHIP GATE READY. Only re-run if a customer reports a missing metric column.

---

## Phase 2 — first-week-live watch-list (reactive)

Items below trigger on real-user activity; do not need active work today.

- **Item 6:** Confirm `STAFF_NOTIFY_EMAIL` env var on Railway prod is set + Stripe webhooks route auto-reload failures and disputed charges to Leo's inbox.
- **Item 7:** Signup quality gate. Bot-spray mitigations from `d748c1d` (2026-05-14) still in place. Revisit if signup volume warrants hCaptcha (deferred — would require template patches to keep `verify_login` working).
- **Item 8:** Failed-job page UX. Completion email now has the "wallet was not charged for failures" copy (commit `4a79fcb`, 2026-05-25); the in-page detail view is still terse. Improve once a real failure surfaces.
- **Item 9:** `tool_jobs_p90` view (created by migration 0020) — empty for now, populates as live jobs accumulate. Spot-check `SELECT * FROM tool_jobs_p90 LIMIT 5` weekly.

---

## Phase 3 — opportunistic

- **Item 10:** Workspace product decision. `shared/credits.py` + `shared/workspaces.py:charge_for_job` still active for internal margin accounting on the Wave 2 Workspace product. Keep / retire / merge into wallet.
- **Item 11:** Drop `Preset.credits_cost` dataclass field — cascades through `tools/base.py`, 9 `tools/*/__init__.py`, ToolJob serdes, smoke tests. Harmless dead metadata for now.
- **Item 12:** Scope-C test gaps — E2E topup→submit→settle integration test, heartbeat→mid-run-monitor integration test, daily-cap burst concurrency test, auto-reload retry-after-failure path test.
- **Item 13:** Pricing transparency. Site has no published pricing per locked decision; revisit once real conversion data lands.

---

## Key facts / gotchas

- **Topup minimum is $20** — `MIN_TOPUP_USD = Decimal("20.00")` at `shared/wallet.py:76`. Overridable via `WALLET_MIN_TOPUP_USD` env var (currently unset on prod). `$5` attempts rejected at both form layer (HTML5 `min="20"`) and server (`billing/checkout.py:319` → "below the minimum of 20 USD").
- **Signup grant is $5**, lazy on first wallet access — `get_or_create_wallet` → `_create_wallet_with_signup_credit` → `record_signup_credit` (`shared/wallet.py:198-328`). Idempotent via synthetic event ID. Email via Resend is best-effort (try/except).
- **Smoke-tier MPNN is free** (0-credit baked target) — first-time users can run it with the $5 grant alone, no topup needed.
- **Legal pages are placeholders** — both files carry an explicit "Boilerplate placeholder — to be replaced" note at the top. Effective date hardcoded to 2026-05-26 in both. Update both date + body when custom-drafted copy is ready.
- **PXDesign flag inconsistency** — see TODO #3.
- **Railway duplicate-service caveat** — `tools-hub-sweep-stuck` inherited env var references, not copies. If you re-point shared vars on `web` later, sweep-stuck should pick up automatically — but verify on first cron run that env reads resolve.
- **Wallet ledger shape** — 2-row hold + partial release, not 3-row. `gpu_seconds` stamped on the release row, charge implicit in (hold − release). `wallet_transactions.job_id` (bigint) is NOT `tool_jobs.id` (uuid); join via `tool_jobs.inputs._wallet.hold_tx_id`. See `reference_tools_hub_wallet_ledger_shape` memory.

---

## Reference

- Today's plan: `C:\Users\lab\.claude\plans\in-my-tools-hub-cached-volcano.md`
- Wallet pivot final state: [HANDOFF-CREDITS-CUTOVER.md](HANDOFF-CREDITS-CUTOVER.md), [HANDOFF-WALLET-PIVOT-SESSION-14.md](HANDOFF-WALLET-PIVOT-SESSION-14.md), memory `project_tools_hub_wallet_pivot.md`.
- Validation log: [VALIDATION-LOG.md](VALIDATION-LOG.md) — append-only audit trail; PXDesign pilot row is the next entry needed.
- Cron sweeper source: [cron/sweep_stuck_jobs.py](../cron/sweep_stuck_jobs.py) — sweeps `pending > 30 min` and `running > 6 hr` via `shared.jobs.timeout_stuck_job`.
- Topup floor + signup grant constants: `shared/wallet.py:76` and `shared/wallet.py:85`.
- Signup gate code path: `app.py::signup()` ~line 780, especially the `terms_accepted` block at ~line 846.
