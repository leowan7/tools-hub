# Tools-Hub Wallet Pivot, Session 12 Handoff

**Date:** 2026-05-21
**Supersedes:** `HANDOFF-WALLET-PIVOT-SESSION-11.md`
**Authoritative plan:** `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
**Pass 6 results doc:** `docs/PASS-6-SANDBOX-RESULTS.md`

---

## TL;DR

Session 12 audited Finding 2 (`daily_spend_cap_usd`), found it was **not working**, and
fixed it.

Session 11's carry-over note ("column exists but no enforcement path found") was
half-right. There IS an enforcement path (`wallet_preflight` -> `_spent_today_usd`), but
it summed the wrong ledger rows, so the daily cap never engaged for normal usage. Fixed
by summing `hold` rows instead of `charge`/`absorbed_variance`. The same bug was fixed in
the wallet-overview "Spent today" / "Spent 30d" figures.

**The fix is UNCOMMITTED.** HEAD on `main` is still `84b79f0` (Session 11's Finding 1).
Three files modified, 87/88 wallet tests green (1 pre-existing failure). The next session
must decide whether to commit.

**Pass 7 is still blocked** on the same 2-minute Stripe webhook edit from Session 11 — no
change there.

---

## What Session 12 did

1. **Audited Finding 2 and corrected a wrong first verdict.** The initial read was "wired,
   not race-safe." Tracing the full ledger lifecycle (`try_hold_for_job` in `0019` ->
   `settle_hold` in `0017`) exposed the real defect: the cap was inert.

2. **Root cause.** `_spent_today_usd` (`shared/wallet.py`) summed `wallet_transactions`
   rows of kind `charge` + `absorbed_variance`. But `settle_hold` writes those kinds
   **only on cost overruns** (actual > estimate). A normal job — actual <= estimate, the
   designed case — produces a `hold` row (the debit) plus a `hold_release` row (surplus
   refund), and no `charge` row. So `_spent_today_usd` returned ~$0 for normal usage and
   `wallet_preflight`'s `spent_today + estimate > daily_cap` check never tripped.

3. **Same bug in two more places.** The `app.py` wallet-overview handler computes "Spent
   today" via `_spent_today_usd` and "Spent 30d" via an inline copy of the same
   `charge`-summing query. Both showed ~$0 for any user running normal jobs.

4. **Why the tests missed it.** Both daily-cap tests seeded synthetic `charge` rows
   directly — exercising the sum logic but never the real ledger shape a job produces.

5. **The fix.** `_spent_today_usd` and the `app.py` 30d query now sum `hold` rows — where
   job spend actually lands in the ledger. Holds-based "spent" runs slightly conservative
   (an under-estimate job's surplus refund is not netted out; a cancelled-before-run hold
   still counts) — the safe direction for a spend cap.

6. **Docstring correction.** `reserve_hold`'s docstring claimed "All blocking checks run
   again inside the SQL function so this is safe under concurrent submission." False —
   `try_hold_for_job` (`0019`) re-checks only wallet-frozen state, the hard cap, and
   balance. The daily cap and self-serve ceiling are Python-only. The docstring now says
   so accurately.

7. **Tests.** Reseeded the two daily-cap tests with `hold` rows. Added
   `test_spent_today_counts_holds_not_charges` — seeds holds plus a decoy `charge` row and
   asserts only the holds count. Running `test_wallet.py` + `test_wallet_api.py` +
   `test_wallet_templates.py`: 87 passed, 1 failed. The failure is
   `test_auto_reload_triggers_when_eligible` (`RuntimeError: Stripe is not configured`) —
   the pre-existing failure Session 11 documented; unrelated.

---

## Files modified (uncommitted)

```
 app.py               |  6 +++---
 shared/wallet.py     | 23 ++++++++++++++++++-----
 tests/test_wallet.py | 48 +++++++++++++++++++++++++++++++++++++++++++-----
 3 files changed, 64 insertions(+), 13 deletions(-)
```

- `shared/wallet.py` — `_spent_today_usd` now sums `hold` rows (+ rewritten docstring);
  `reserve_hold` docstring corrected.
- `app.py` — 30d spend query now sums `hold` rows (+ comment).
- `tests/test_wallet.py` — two daily-cap tests reseeded with `hold` rows; new
  `test_spent_today_counts_holds_not_charges`.

Suggested commit, if the next session approves:
`fix(wallet): count hold rows for daily spend cap and spend display`.

---

## Ledger model reference (the crux of the bug)

A job produces exactly **2 rows**:

1. `hold` at submit (`try_hold_for_job`, `0019`) — `amount_usd = -estimate`. The debit of
   record.
2. One settle row at completion (`settle_hold`, `0017`), depending on actual vs estimate:
   - actual < estimate -> `hold_release`, `amount = +(estimate - actual)` surplus. **No
     `charge` row.**
   - actual > estimate, wallet covers -> `charge`, `amount = (estimate - actual)`
     (negative) — the overrun true-up only.
   - actual > estimate, wallet cannot cover -> `absorbed_variance` (Ranomics eats it).
   - actual == estimate -> zero-amount `charge`.

Net wallet effect is `-actual` in all cases. The job's real cost lives in the `hold`;
`charge` is an overrun correction, not the job cost. That is why summing `charge` to
measure spend was wrong.

---

## Pass 7 — still blocked (unchanged from Session 11)

Pass 7 (live $20 Stripe topup) is blocked on one 2-minute dashboard edit by Leo. Nothing
in Session 12 touched this.

**The webhook gap:**
- Endpoint `we_1TPPD4HK3YN42tFlJK8mQ6LS` at `https://tools.ranomics.com/webhooks/stripe`,
  API version `2026-03-25.dahlia` (leave pinned).
- Subscribed to `checkout.session.completed` only (of the wallet-required events).
- **Missing:** `payment_intent.succeeded`, `payment_intent.payment_failed`,
  `charge.dispute.created`.

**Fix:** dashboard.stripe.com/webhooks in Live mode -> that endpoint -> Edit -> add the 3
events -> Save. Confirm the signing secret still matches Railway's
`STRIPE_WEBHOOK_SECRET` (value in `HANDOFF-WALLET-PIVOT-SESSION-11.md`); if it rotated,
update the Railway var and redeploy. Full steps in the Session 11 handoff.

Re-run pre-flight after:
```
cd C:/Users/lab/Documents/Claude_projects/tools-hub
railway run --service web --environment production -- "C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe" scripts/deploy/pass7_preflight_live_stripe.py
```

Pre-flight items 1-3 are GREEN; item 4 flips GREEN once the 3 events are added.

---

## Test user baseline (carry into Pass 7)

- **user_id:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (leowan7@gmail.com)
- **balance_usd:** `$89.9895`
- **wallet_frozen:** False
- **stripe_customer_id:** `cus_UWR3IFRvQ2R2GW` (SANDBOX — overwritten on first live checkout)
- **stripe_payment_method_id:** `pm_1TXNqcHK3YN42tFlNElV3diy` (SANDBOX)
- **auto_reload_enabled:** True (threshold $80, amount $25, monthly cap $1000)
- **daily_spend_cap_usd:** $200 (default) — now actually enforced after the Session 12 fix.

---

## What's left

### Immediate next-session priorities

1. **Decide on the Session 12 commit.** 3 files, tested, self-contained. If approved,
   commit it — and also commit the uncommitted `HANDOFF-WALLET-PIVOT-SESSION-11.md` and
   this doc (see Known mess).
2. **Pass 7.** Leo fixes the webhook -> re-run pre-flight -> live $20 topup with
   `scripts/deploy/pass7_watch.py` polling.
3. **Optional Pass 7 extension:** submit an MPNN job on prod to exercise the full
   hold -> settle path with a live wallet.

### Finding 2 follow-ups (all opt-in — judged NOT urgent)

1. **Precise spend display.** Holds-based "spent" is conservative (ignores surplus refunds
   and cancelled holds). To show actual spend on the overview, net each hold against its
   settle row(s). ~20 lines across 2 query sites (`_spent_today_usd` + the `app.py` 30d
   block). Worth it only if the conservative UI number is a problem.
2. **Race-safe daily cap.** The daily-cap check is Python-only — a TOCTOU window
   (preflight read -> `try_hold_for_job` insert, milliseconds) lets concurrent submits
   slip past. To close it, add a daily-cap check inside `try_hold_for_job` under the
   existing `FOR UPDATE` lock. New migration `0020`. Low practical risk; defer unless the
   cap is raised or starts to matter more.
3. **`spent_usd_30d` SQL view bug.** `0017_wallet.sql:153` — the reporting view's
   `SUM(amount_usd) FILTER (WHERE kind = 'charge')` has the identical bug. Admin-facing,
   not load-bearing. Needs a migration to fix.

### Observed, not investigated

`settle_hold`'s `absorbed_variance` branch (`0017_wallet.sql`, ~line 333) inserts a row
with a nonzero `amount_usd` but sets `balance_after_usd` to the unchanged balance and does
not UPDATE `user_wallets.balance_usd`. That looks like it would break the documented
`SUM(amount_usd) = balance_usd` invariant whenever Ranomics absorbs an overrun. Could be
intentional (absorbed_variance excluded from the invariant by convention) or a real bug.
Not chased — needs its own look.

### Deferred (long-standing, from Session 11)

- `wallet_transactions.job_id` bigint vs `tool_jobs.id` uuid mismatch.
- `tool_jobs_p90` view missing (referenced in `wallet_estimates.py`).
- Credit-era copy cleanup ("1 credits" on `/jobs/<id>`, MPNN form estimate text).
- Sub-test 16b live (absorbed_variance branch) — code-review verified only.

---

## Known mess to clean up

**Uncommitted handoff docs:** `docs/HANDOFF-WALLET-PIVOT-SESSION-11.md` is untracked
(Session 10's was committed as `db7c008`). This doc will be too. Commit both — alongside
the wallet fix or as a separate `docs(wallet):` commit.

**Untracked-but-intentional:** `.deploy-logs/` (gitignored) holds the Pass 7 scripts
(`pass7_preflight_live_stripe.py`, `pass7_watch.py`, `pass7_baseline.py`,
`pass7_webhook_deliveries.py`). Safe to leave.

**Memory:** `project_tools_hub_wallet_pivot.md` is not yet updated for Session 12. Do this
after the commit decision, not before — the fix is uncommitted and the 3 follow-ups are
undecided.

**Branches:** `main` is clean apart from the 3 uncommitted Session 12 files.

---

## Quick reference

- **HEAD on `main`:** `84b79f0` (Session 11). Session 12 work is uncommitted.
- **Test user:** `03e51184-4d04-4acd-ab22-0cbd7fa08c77` (`leowan7@gmail.com`)
- **Live Stripe account:** `acct_17ntxDHK3YN42tFl` / Ranomics Inc.
- **Live webhook endpoint:** `we_1TPPD4HK3YN42tFlJK8mQ6LS` at
  `https://tools.ranomics.com/webhooks/stripe`
- **Production URL:** `https://tools.ranomics.com` (git-connected to `main`)
- **Production env query pattern:**
  `railway run --service web --environment production -- <abs-python-path> <script>`
- **Wallet test suites:** `tests/test_wallet.py`, `tests/test_wallet_api.py`,
  `tests/test_wallet_templates.py` — run with `./venv/Scripts/python.exe -m pytest`.
  Baseline: 87 pass / 1 pre-existing fail (`test_auto_reload_triggers_when_eligible`,
  Stripe-not-configured).

---

## To start the next session

Read this handoff and `HANDOFF-WALLET-PIVOT-SESSION-11.md`. Then:

1. **First:** decide whether to commit the Session 12 wallet fix (3 modified files). It is
   tested and self-contained.
2. **If Leo says "webhook fixed":** re-run the Pass 7 pre-flight, confirm 4/4 GREEN, run
   the live $20 topup with the watch script.
3. **If Leo wants the Finding 2 follow-ups:** pick from the 3 opt-in items above (precise
   display / race-safe cap / view fix).
4. **Otherwise:** the deferred long-standing list.
