# tools-hub — Comprehensive Audit (2026-06-17)

Master index for the gstack-driven audit of tools-hub (tools.ranomics.com), a live production Flask app: 12 protein-design tools, Supabase, Modal GPU, Stripe USD wallet. Read-only — no source changed by the audit.

## Sub-reports
| Dimension | Report | Tool | Status |
|---|---|---|---|
| Security | [`cso.md`](cso.md) | `/cso --comprehensive` (4 parallel domain scans + verification) | ✅ done |
| Correctness | [`REVIEW.md`](REVIEW.md) | fan-out review, every bug adversarially verified | ✅ done |
| Code health | [`health.md`](health.md) | fan-out code-quality scan | ✅ done |
| Functional / UX fit | `qa.md` | `/qa-only` + `/browse` on staging | ⏳ blocked on staging Supabase |

## Posture in one line
The money, access-control, and webhook trust boundaries are genuinely well-built (no SQLi/cmdi/template-injection, wallet ledger row-locked + idempotent + Stripe-signature-verified, owner-scoped main-app routes, strong outbound-webhook SSRF guard). The real exposure is concentrated in **(a)** a few unauthenticated/under-validated edges and **(b)** robustness bugs that turn malformed input or odd job states into 500s and stuck jobs.

## Findings tally
- **Security (cso.md):** 1 HIGH · 3 MEDIUM · 5 LOW · 3 tentative · 1 info
- **Correctness (REVIEW.md):** 1 CRITICAL · 8 HIGH · 10 MEDIUM · 1 LOW (20 confirmed; 14 candidates rejected as FPs)
- **Health (health.md):** 1 CRITICAL · 17 HIGH · 41 MEDIUM · 16 LOW (maintainability backlog, unverified)

## Top priorities (cross-dimension)
1. **[CRITICAL · correctness]** Unhandled error status from `modal_client.poll` leaves a job **stuck forever** → user's wallet hold never settles. `app.py:4879-4897`. (REVIEW.md #1)
2. **[HIGH · security]** Unauthenticated `/webhooks/heartbeat` bills on client-supplied GPU seconds → cross-user job-cancel + forced charge. `webhooks/modal.py:96,355`. *(queued — parallel fix session 1)*
3. **[HIGH · correctness]** Malformed input → 500s: campaign-submit `float()/int()` with no try/except (`app.py:5661,5664`); `export_csv`/`export_fasta` crash on non-dict candidates (`app.py:5324,5360`); `export_zip` invalid base64 (`app.py:5611`).
4. **[HIGH · correctness/billing]** `gpu_seconds` falsy-coalescing treats `0` as missing (`gpu/modal_client.py:465,472`); webhook backpressure path misses the attempts increment (`shared/webhooks.py:599`).
5. **[MEDIUM · security]** Scout cross-user IDOR + traversal (`scout/routes.py:530`) *(fix session 2)*; cookie/CSRF coupled to `ENABLE_PLATFORM_API` *(session 3)*; `.gitignore` env gap *(session 4)*.
6. **[MEDIUM · correctness]** Wallet-transactions pagination always "no next page" (`app.py:2861`); off-session PaymentIntent amount precision loss (`billing/checkout.py:540`); `wallet_preflight` wrong `hard_cap_usd` (`shared/wallet.py:451`); Scout `cleanup_old_jobs` can't delete nested dirs (`scout/jobs.py:83`).

## Staging-critical note
`app.py:949-951` sets `SECRET_KEY` to **bytes** (`os.urandom(32)`) when `SESSION_SECRET_KEY` is unset. The planned `.env.staging` **must set `SESSION_SECRET_KEY`** (a string), or staging sessions break. This is REVIEW.md MEDIUM #11 and also why the M3 `.gitignore` fix matters before staging.

## Fixes in flight
4 parallel sessions (isolated git worktrees, no push to main) addressing the security findings: H1 heartbeat, Scout IDOR (M1), cookie hardening (M2), `.gitignore` (M3). The correctness bugs above (esp. the CRITICAL stuck-job and the financial-adjacent ones) are the recommended next fix batch after triage.

## Remaining audit work
- **Functional/UX (`qa.md`)** — dogfood the full de-novo design journey across all 12 tools on the staging instance once it exists. Blocked on the staging Supabase handoff.
- **HTML knowledge map** — this doc renders to `AUDIT-FINDINGS.html`; a `/graphify` pass maps findings↔modules into `graphify-out/graph.html` once the audit is complete.
