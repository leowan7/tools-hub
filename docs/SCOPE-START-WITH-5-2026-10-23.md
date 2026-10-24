# Scope: remove the signup credit, start with a $5 top-up (2026-10-23)

Leo, 2026-10-23: "we are removing free credits, start with as little as 5
dollars, make sure all backend and frontend aligns. scope it out carefully first."

Base: `ee1b8c17` (= `origin/main` at time of writing). Phase 1 only, no code.
Every file:line below comes from the grep output in the appendix, run on that
commit. Nothing here is built yet.

## 0. Where things stand today (verified)

| Fact | Evidence |
|---|---|
| Signup credit is **$5**, not $20 (PR #373 cut it, merged 2026-09-28) | `shared/wallet.py:161` `SIGNUP_CREDIT_USD = Decimal("5.00")`; `gh pr view 373` → MERGED |
| Credit expires after 30 days | `shared/wallet.py:166`; migration `0043` column default |
| Minimum top-up is **$20** | `shared/wallet.py:77` `MIN_TOPUP_USD = Decimal("20.00")` |
| Checkout floor can be overridden by env var `WALLET_MIN_TOPUP_USD` | `billing/checkout.py:150-171` |
| The page and the gate read the constant, the checkout reads the env var | `blueprints/wallet.py:857`, `shared/wallet_guard.py:280` vs `billing/checkout.py:337` |
| Tool-form gate link is `/account/wallet/topup?tool=<slug>` (no `need=` any more) | `templates/wallet/_partials.html` gate macro |
| Stripe Checkout uses inline `price_data` on one product; no Price object holds an amount | `billing/checkout.py` `line_items[0].price_data.unit_amount` |

So today a new user gets $5 free and must then top up $20. After the change: $0
free, first top-up can be $5.

## 1. The signup credit: every touchpoint

### 1a. Grant (the one switch)
- `shared/wallet.py:347` `get_or_create_wallet` → `:374` `_create_wallet_with_signup_credit` → `:402` `record_signup_credit(user_id)`.
- `shared/wallet.py:406-458` `record_signup_credit` credits `SIGNUP_CREDIT_USD` and sends the welcome email (`:444-445`).
- The wallet is created lazily on first wallet read, not at signup (`blueprints/auth.py:340-342` comment).
- SQL: migrations `0017`/`0018` backfilled $5 once in the past; nothing in SQL grants on new rows. No migration needed to stop the grant.

**Proposal:** `_create_wallet_with_signup_credit` stops calling `record_signup_credit`. Delete `record_signup_credit` and `SIGNUP_CREDIT_USD` (tests that call it are rewritten or deleted).

### 1b. Expiry machinery (#365, migration 0043)
- Cron: `cron/signup_credit.py` (CLI `app.py:1068-1075`), `shared/wallet.py:465-598` (`unspent_signup_credit`, `signup_credit_status`, `expire_signup_credit`), SQL `expire_signup_credit` (live version `0045_live_run_debits.sql:259`).
- A new wallet with **no** grant still gets `signup_credit_expires_at = now()+30d` (0043 column default). After 30 days the cron calls the SQL with amount 0, which writes no ledger row but stamps `signup_credit_expired_at` (`0045_live_run_debits.sql:339`). Harmless to money; it does inflate the admin "Credit expired (no email)" count (`blueprints/admin.py:1225-1226`, `templates/admin/users_list.html:77,100`).

**Proposal:** keep all of it for now so existing holders still expire and get their reminder. Retire the cron, the expiring email, the admin counters and the 0043 columns in a cleanup PR after the last grant has lapsed (deploy date + 30 days). No migration in this PR.

### 1c. Emails
- Welcome + credit email: `shared/email.py:2248-2285`, `templates/email/send_signup_credit.html`. **This is the only welcome email.** Stopping the grant stops it.
- Expiring reminder: `shared/email.py:2288-2322`, `templates/email/send_signup_credit_expiring.html`. Keep (existing holders).
- Re-engagement (`cron/reengagement.py:91-98`) only mails wallets with balance > 0. A never-topped-up new user has $0 and is never mailed. No code change; behaviour changes.
- Low-balance email: crossing test `shared/wallet.py:1804` `balance < LOW_BALANCE_EMAIL_THRESHOLD <= balance + actual_cost`, threshold `$5.00` (`:169`). A $5.00 wallet gets it once, on the run that takes it below $5. **Unchanged; must stay `<=`.** Pinned by `tests/test_wallet.py:1824` `test_low_balance_email_fires_once_on_crossing`; `:1839` `..._sent_once_to_a_fresh_signup_wallet` depends on the $5 grant and must be rewritten to top up $5 first.
- Header "Wallet below $5" pill: `templates/_header.html:47`. Unchanged.

**Leo decides (Q3):** no welcome email at all, or a plain welcome (same send point, no credit line).

### 1d. Admin + digest
- Admin user detail: `templates/admin/user_detail.html:17-18` (shows expiry dates). Keep until cleanup.
- Admin users list counters: see 1b.
- Daily digest "Spent on runs ... signup credit included": `templates/email/daily_digest.html:93`. After the last grant lapses that is just "spent on runs". Change wording in the cleanup PR, or drop "signup credit included" now (existing grants still being spent → it stays true for ~30 days). **Proposal:** leave now, cleanup PR.

### 1e. User-facing copy that promises the credit (all must change in this PR)
Everything that renders `signup_credit()` or names the credit:
- `templates/index.html:351, 358, 673-674` (homepage hero CTA "Start with $5 in your wallet ..." and pricing teaser)
- `templates/pricing.html:5, 18, 25, 47-50, 146-147, 195-196, 234-235`
- `templates/showcase.html:104, 109, 154`
- `templates/login.html:46, 80`
- `templates/components/_signin_cta.html:28`
- `templates/help/faq.html:33, 43, 136-137, 197` (HTML **and** FAQPage JSON-LD)
- `templates/help/getting_started.html:108-110`
- `templates/legal/terms.html:33-35` (**Terms of Service** — Leo should be aware this is a legal-text change)
- `templates/wallet/topup.html:113-116` ("the free signup credit expires ...")
- `templates/wallet/transactions.html:32, 217, 231-235` (ledger legend; keep the two kind rows because old ledgers contain them, but drop the "$5 ... on first account creation" promise)
- `templates/jobs_list.html:168` ("Add funds once your starting balance runs low")
- `blueprints/auth.py:380-381` flash after signup: "Account created with $5 of compute credit, usable for 30 days."
- SEO copy reaching JSON-LD: `tools/af2/meta.py:22-60`, `tools/mpnn/meta.py:22-49`, `tools/rfdiffusion/meta.py:16-55`
- Jinja globals `signup_credit` / `signup_credit_expiry_days`: `app.py:662-678`. Delete `signup_credit`; keep `signup_credit_expiry_days` only if a kept template still needs it (wallet overview panel + expiring email).

Kept as is (they only render for someone who still has credit): `templates/wallet/overview.html:94-99`, `blueprints/wallet.py:815`.

"Create a free account" / "Sign up free" (`pricing.html:45,174`, `showcase.html:201`, `_signin_cta.html:25`) stays true: the account is free and Epitope/Developability Scout cost nothing. **Proposal:** keep.

## 2. The $20 minimum top-up: every place

| Where | What | Change |
|---|---|---|
| `shared/wallet.py:77` | `MIN_TOPUP_USD = 20.00` | → `5.00` |
| `billing/checkout.py:150-171` | env override `WALLET_MIN_TOPUP_USD`, hardcoded fallback `20.00` at `:171` | **Proposal:** delete the env override, read the constant (one source, as was done for `WALLET_SIGNUP_CREDIT_USD`). Leo must check Railway: if `WALLET_MIN_TOPUP_USD` is set there today it silently wins over any code change. |
| `billing/checkout.py:337-343` | server validation "below the minimum" | follows the floor |
| `blueprints/wallet.py:857`, `shared/wallet_guard.py:280` | page/gate pass `min_topup_usd` | follows the constant |
| `templates/wallet/topup.html:100-116, 211` | `min=` attribute and "Minimum top up is $X" (both read the passed value; fallback `20` at `:101`) | fallback → constant |
| `templates/wallet/topup.html:103-107` | preset chips $20/$50/$200/$500/$2,500 | **Leo (Q4):** prepend $5 |
| `blueprints/wallet.py:1063-1068`, `templates/wallet/topup.html:298` | auto-reload amount floor = `MIN_TOPUP_USD` | **Leo (Q5):** follow to $5, or keep a $20 floor for off-session charges |
| `shared/wallet.py:251-265` `_round_up_topup_amount` | ceil to $5, floor `MIN_TOPUP_USD`; feeds `/api/wallet/estimate` `rounded_topup_usd` which no page reads | follows constant |
| Copy printing "$20" | `templates/help/faq.html:25, 99`; `templates/help/troubleshooting.html:81`; `templates/jobs_list.html:168, 172`; `templates/pricing.html:56, 193`; `templates/wallet/overview.html:30`; `templates/wallet/_partials.html:17, 32, 141` | render from the constant, not a literal |
| API errors | the checkout error string prints the floor from the value, no literal | none |

Not top-up minimums, leave alone: `templates/targets/launch.html:255,261` and `templates/tools/proteina_form.html:226,231` (`min="20"` is binder length in residues).

## 3. Per-tool start figure vs a $5 balance

Computed on `ee1b8c17` with the repo's own functions (`scratch: holds.py`, same method as `tests/test_signup_credit_covers_smallest_run.py::_worst_hold`: live tools → `live_start_usd`; others → worst 1-unit cushioned hold across both p90 regimes):

```
af2                hold       1.5000  (scaling_param=n_designs_total)
alphafold2         hold       1.5000  (scaling_param=None)
bindcraft          live-start 8.00
boltz2             live-start 0
boltzgen           hold       9.1035  (scaling_param=num_designs)
colabfold          hold       0.2186  (scaling_param=n_designs_total)
esmfold            hold       0.2186  (scaling_param=n_designs_total)
esmfold2-design    live-start 15.00
iggm               live-start 3.00
mpnn               hold       0.0750  (scaling_param=num_seq_per_target)
opendde            hold       15.0000  (scaling_param=None)
proteina           live-start 1.00
pxdesign           live-start 2.00
rfantibody         live-start 2.00
rfdiffusion        live-start 1.1371
```

At a $5.00 balance: **runs** af2, colabfold, esmfold, mpnn, boltz2, proteina, pxdesign, rfantibody, rfdiffusion, iggm. **Refused** bindcraft ($8.00), boltzgen ($9.10), esmfold2-design ($15.00), opendde ($15.00). This is exactly today's `NEEDS_TOPUP` set (`tests/test_signup_credit_covers_smallest_run.py:47-52`), because the credit and the new minimum are both $5.

Note boltz2's 0 start means any positive balance starts it; a $0 balance is refused as `wallet_empty` (`shared/wallet_guard.py:438, 449`).

**Options (Leo picks, Q1):**
- **A. Keep the minimums, honest copy without figures.** "$5 is enough to start structure prediction, sequence design and most binder design tools; BindCraft, BoltzGen, ESMFold2 design and OpenDDE need a larger balance to start." The gate on those tools already says "Your balance does not cover this run" (`templates/wallet/topup.html:147-155`). No published numbers. *Recommended — no billing change.*
- **B. Same, but print each refused tool's start minimum** ("BindCraft needs $8 to start"). These are thresholds, not run-price estimates, but the 10-01 rule ("no pre-run numbers") is yours to interpret; it would be the first $ figure on a tool page.
- **C. Lower bindcraft to $5.** It became $8 (its base cap) by your 10-16 call because a shorter balance can be stopped at $0 before its first design. Lowering it re-opens that. esmfold2-design / opendde / boltzgen cannot reasonably reach $5 (opendde has no smaller run).

## 4. Existing accounts

Existing grants and balances are untouched by any proposal here: expiry reads the `signup_credit` ledger row, not the constant (`shared/wallet.py:468-490`). **Recommendation (Q2):** honour unexpired credit until it expires; grant none from deploy on. One wrinkle: a user who signed up before deploy but whose wallet row is first created after deploy (wallet is created lazily) gets nothing. Count to check before deploy (Leo runs, read-only):

```sql
select count(*) filter (where w.signup_credit_expired_at is null and w.signup_credit_expires_at > now()) as live_grants
from public.user_wallets w
where exists (select 1 from public.wallet_transactions t where t.user_id = w.user_id and t.kind = 'signup_credit');
```

## 5. Stripe at $5

- Stripe's minimum USD charge is $0.50, so $5 is allowed. (Stripe docs, not verified in this repo.)
- Fees (unverified, standard card pricing; check your account's rate): about 2.9% + $0.30 → ≈ $0.45 on $5 (≈ 9%) vs ≈ $0.88 on $20 (≈ 4.4%). Stripe Tax is on for every top-up (`automatic_tax.enabled: True`, `billing/checkout.py`), which adds its own per-transaction fee. With the 1.70× compute markup (`shared/wallet.py:74`) a $5 top-up is still margin-positive, but thinner.
- No Price object or dashboard amount is involved: `price_data.unit_amount` is built per session. **No dashboard change is needed**, unless Railway has `WALLET_MIN_TOPUP_USD` set (see §2). Check Stripe Radar rules for any amount-based rule (dashboard, Leo).

## 6. Tests and docs that will go stale

Tests referencing the credit (counts are `git grep -c`, appendix):
`tests/test_signup_credit_expiry.py` (68), `tests/test_signup_credit_single_source.py` (24), `tests/test_wallet.py` (17), `tests/test_email_unsubscribe.py` (11), `tests/sql/test_0017_wallet.sql` (11), `tests/test_live_charging.py` (10), `tests/test_email_real.py` (10), `tests/test_signup_credit_covers_smallest_run.py` (9), `tests/test_wallet_templates.py` (8), `tests/test_help_tool_guides.py` (7), `tests/test_admin_users_paging.py` (6), plus 1–2 each in daily_digest, money_display_surfaces, no_prerun_numbers, orphan_jobs, wallet_invariants, wallet_small_balance.

Tests pinning $20 / `MIN_TOPUP_USD`: `tests/test_wallet.py:1861-1867` (asserts `== 20.00`), `tests/test_checkout.py:155, 476-483`, `tests/test_money_display_surfaces.py:157, 237-249` (whole-dollar check; $5 passes), `tests/test_no_prerun_numbers.py:48-53` (allow-list reads the constant), `tests/test_wallet_templates.py:190, 210`, and others in the appendix count list.

**Plan for the key ones:**
- `test_signup_credit_covers_smallest_run.py` → becomes "the minimum top-up starts every tool outside NEEDS_TOPUP" (same set, measured against `MIN_TOPUP_USD`; the $1 headroom rule is kept).
- `test_signup_credit_single_source.py` → becomes a guard that no template or meta prints the credit, and that "$20"/the minimum is never a literal in copy.
- Expiry tests stay (machinery kept).
- New: a fresh wallet has balance 0 and no `signup_credit` ledger row; checkout accepts $5.00 and refuses $4.99.

Living docs to update: `README.md:105`, `docs/WALLET-ENV-VARS.md:48, 106, 154`. Dated handoffs/QA/audits are records and stay as written.

Code comments that will become false: `shared/wallet.py:76-161` (credit history block), `shared/wallet_guard.py:136-150,163` (`SIGNUP_CREDIT_USD` references in the `live_start_usd` docstring), `shared/wallet_estimates.py:622-626`, `tools/esmfold2_design/modal_app.py:118`, `billing/checkout.py:7, 59, 153`.

## 7. Questions for Leo

1. **Tools above $5** (§3): A (keep, honest copy, no figures — recommended), B (print start minimums), or C (lower bindcraft)?
2. **Existing credit** (§4): honour until expiry, grant none new — OK?
3. **Welcome email** (§1c): none, or plain welcome without credit?
4. **Top-up chips**: prepend $5 to $20/$50/$200/$500/$2,500?
5. **Auto-reload floor**: follow to $5, or keep $20?
6. **Env var**: OK to delete the `WALLET_MIN_TOPUP_USD` override? Either way, please check whether Railway sets it.

## By hand (Leo), whatever the answers
- Railway: check / unset `WALLET_MIN_TOPUP_USD`.
- Stripe: nothing required; optionally look at Radar amount rules.
- No migration in this PR.
- Terms of Service text changes (§1e) — your call whether that needs a notice.

---

## Appendix: raw grep output (on `ee1b8c17`)

Each block is the verbatim output of the command in its heading.

### A1. `git grep -n "signup_credit\|SIGNUP_CREDIT" -- "*.py" ":!tests" ":!scripts"`
```
app.py:666:    #   signup_credit()      -> "5" for prose ("start with $5 in your wallet")
app.py:667:    #   signup_credit(True)  -> "5.00" where an exact figure reads better
app.py:668:    def _signup_credit(exact: bool = False) -> str:
app.py:669:        from shared.wallet import SIGNUP_CREDIT_USD  # noqa: PLC0415
app.py:672:            return f"{SIGNUP_CREDIT_USD:.2f}"
app.py:673:        return f"{SIGNUP_CREDIT_USD:.0f}"
app.py:675:    flask_app.jinja_env.globals["signup_credit"] = _signup_credit
app.py:676:    from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS  # noqa: PLC0415
app.py:678:    flask_app.jinja_env.globals["signup_credit_expiry_days"] = SIGNUP_CREDIT_EXPIRY_DAYS
app.py:1075:        from cron.signup_credit import run  # noqa: PLC0415
blueprints/admin.py:1138:                .select("user_id,balance_usd,signup_credit_reminder_sent_at,"
blueprints/admin.py:1139:                        "signup_credit_expired_at")
blueprints/admin.py:1223:                [r["signup_credit_reminder_sent_at"] for r in wallet_rows
blueprints/admin.py:1224:                 if r.get("signup_credit_reminder_sent_at")],
blueprints/admin.py:1225:                [r["signup_credit_expired_at"] for r in wallet_rows
blueprints/admin.py:1226:                 if r.get("signup_credit_expired_at")],
blueprints/admin.py:1304:            .select("balance_usd,signup_credit_expires_at,"
blueprints/admin.py:1305:                    "signup_credit_reminder_sent_at,signup_credit_expired_at")
blueprints/admin.py:1312:        target["credit_reminder_sent_at"] = data.get("signup_credit_reminder_sent_at") or ""
blueprints/admin.py:1313:        target["credit_expires_at"] = data.get("signup_credit_expires_at") or ""
blueprints/admin.py:1314:        target["credit_expired_at"] = data.get("signup_credit_expired_at") or ""
blueprints/auth.py:32:from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD
blueprints/auth.py:342:    # (shared.wallet._create_wallet_with_signup_credit). No legacy
blueprints/auth.py:380:            f"Account created with ${SIGNUP_CREDIT_USD:.0f} of compute "
blueprints/auth.py:381:            f"credit, usable for {SIGNUP_CREDIT_EXPIRY_DAYS} days. Sign in "
blueprints/auth.py:787:    The crons in ``cron.reengagement`` and ``cron.signup_credit`` read this key.
blueprints/wallet.py:34:    signup_credit_status,
blueprints/wallet.py:815:        signup_credit_status=signup_credit_status(ctx.user_id, wallet=wallet),
blueprints/wallet.py:930:    query param optionally filters by ledger kind (signup_credit,
cron/signup_credit.py:7:    claim ``user_wallets.signup_credit_reminder_sent_at`` and send the
cron/signup_credit.py:9:  * expiry date passed: :func:`shared.wallet.expire_signup_credit`.
cron/signup_credit.py:16:``tests/test_signup_credit_expiry.py`` for the double-run cases.
cron/signup_credit.py:36:            .select("user_id,signup_credit_expires_at,signup_credit_expired_at,"
cron/signup_credit.py:37:                    "signup_credit_reminder_sent_at")
cron/signup_credit.py:38:            .is_("signup_credit_expired_at", "null")
cron/signup_credit.py:39:            .lte("signup_credit_expires_at", until.isoformat())
cron/signup_credit.py:54:        .update({"signup_credit_reminder_sent_at": now_iso})
cron/signup_credit.py:56:        .is_("signup_credit_reminder_sent_at", "null")
cron/signup_credit.py:65:        .update({"signup_credit_reminder_sent_at": None})
cron/signup_credit.py:67:        .eq("signup_credit_reminder_sent_at", now_iso)
cron/signup_credit.py:76:    from shared.email import send_signup_credit_expiring_email  # noqa: PLC0415
cron/signup_credit.py:84:        logger.warning("signup_credit: no service client; nothing done")
cron/signup_credit.py:90:        logger.error("signup_credit: wallet query failed", exc_info=True)
cron/signup_credit.py:97:        expires_at = wallet._parse_ts(w.get("signup_credit_expires_at"))
cron/signup_credit.py:101:            outcome = wallet.expire_signup_credit(uid)
cron/signup_credit.py:107:                logger.info("signup_credit: expiry for %s deferred: %s", uid, outcome)
cron/signup_credit.py:110:        if w.get("signup_credit_reminder_sent_at"):
cron/signup_credit.py:114:            status = wallet.signup_credit_status(uid, wallet=w)
cron/signup_credit.py:121:                    logger.error("signup_credit: auth user listing failed; no reminders this run", exc_info=True)
cron/signup_credit.py:135:            if send_signup_credit_expiring_email(
cron/signup_credit.py:144:            logger.error("signup_credit: reminder failed for %s", uid, exc_info=True)
cron/signup_credit.py:150:                logger.warning("signup_credit: claim release failed for %s", uid, exc_info=True)
shared/email.py:45:from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD
shared/email.py:2248:def send_signup_credit_email(
shared/email.py:2260:            "send_signup_credit_email: no email for user %s; skipping", user_id
shared/email.py:2264:    # This used to read a separate WALLET_SIGNUP_CREDIT_USD env var, which
shared/email.py:2269:    credit_usd = _money(SIGNUP_CREDIT_USD, "nearest")
shared/email.py:2275:        "send_signup_credit.html",
shared/email.py:2278:        expiry_days=SIGNUP_CREDIT_EXPIRY_DAYS,
shared/email.py:2284:        log_tag=f"signup_credit user={user_id}",
shared/email.py:2288:def send_signup_credit_expiring_email(
shared/email.py:2297:    Trigger: ``cron.signup_credit.run``, which claims the send first.
shared/email.py:2302:            "send_signup_credit_expiring_email: no email for user %s", user_id
shared/email.py:2311:        "send_signup_credit_expiring.html",
shared/email.py:2321:        log_tag=f"signup_credit_expiring user={user_id}",
shared/wallet.py:12:    record_signup_credit()                  # one-time SIGNUP_CREDIT_USD grant
shared/wallet.py:151:# ``tests/test_signup_credit_covers_smallest_run.py`` pins which tools fall on
shared/wallet.py:154:# signup_credit ledger row, not this constant.
shared/wallet.py:157:# templates read it through the ``signup_credit`` jinja global and Python
shared/wallet.py:159:# override -- WALLET_SIGNUP_CREDIT_USD was removed 2026-08-18 because it
shared/wallet.py:161:SIGNUP_CREDIT_USD = Decimal("5.00")
shared/wallet.py:164:# 30 is the column default in supabase/migrations/0043_signup_credit_expiry.sql;
shared/wallet.py:165:# tests/test_signup_credit_expiry.py fails if the two differ.
shared/wallet.py:166:SIGNUP_CREDIT_EXPIRY_DAYS = 30
shared/wallet.py:331:    calling :func:`record_signup_credit`.
shared/wallet.py:347:        return _create_wallet_with_signup_credit(client, user_id)
shared/wallet.py:374:def _create_wallet_with_signup_credit(client, user_id: str) -> Optional[dict]:
shared/wallet.py:394:            logger.error("_create_wallet_with_signup_credit: empty insert response.")
shared/wallet.py:402:    record_signup_credit(user_id)
shared/wallet.py:406:def record_signup_credit(user_id: str) -> bool:
shared/wallet.py:409:    Inserts a ``signup_credit`` ledger row with a synthetic unique key
shared/wallet.py:415:    synthetic_event_id = f"signup_credit:{user_id}"
shared/wallet.py:425:            logger.info("record_signup_credit: idempotent skip for %s", user_id)
shared/wallet.py:429:            "record_signup_credit: dup check failed for %s", user_id, exc_info=True
shared/wallet.py:437:                "p_amount_usd": float(SIGNUP_CREDIT_USD),
shared/wallet.py:438:                "p_kind": "signup_credit",
shared/wallet.py:444:            from shared.email import send_signup_credit_email  # noqa: PLC0415
shared/wallet.py:445:            send_signup_credit_email(user_id=user_id)
shared/wallet.py:448:                "record_signup_credit: email dispatch failed for %s",
shared/wallet.py:454:            "record_signup_credit: credit_wallet failed for %s",
shared/wallet.py:464:_CREDIT_INFLOW_KINDS = frozenset({"signup_credit", "topup", "auto_reload", "promo"})
shared/wallet.py:468:def unspent_signup_credit(rows: list[Mapping], balance: Decimal) -> Decimal:
shared/wallet.py:482:        if r.get("kind") == "signup_credit_expiry":
shared/wallet.py:484:        if r.get("kind") == "signup_credit" and grant_id is None:
shared/wallet.py:537:def signup_credit_status(user_id: str, wallet: Optional[Mapping] = None) -> Optional[dict]:
shared/wallet.py:548:        if not wallet or wallet.get("signup_credit_expired_at"):
shared/wallet.py:550:        expires_at = _parse_ts(wallet.get("signup_credit_expires_at"))
shared/wallet.py:555:        remaining = unspent_signup_credit(rows, balance)
shared/wallet.py:557:        logger.warning("signup_credit_status failed for %s", user_id, exc_info=True)
shared/wallet.py:561:    grant = next(Decimal(str(r.get("amount_usd"))) for r in rows if r.get("kind") == "signup_credit")
shared/wallet.py:565:def expire_signup_credit(user_id: str) -> str:
shared/wallet.py:568:    Returns the outcome string of the ``expire_signup_credit`` SQL function
shared/wallet.py:574:    lock (supabase/migrations/0043_signup_credit_expiry.sql), which is what
shared/wallet.py:585:        amount = unspent_signup_credit(rows, balance).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
shared/wallet.py:588:            "expire_signup_credit",
shared/wallet.py:597:        logger.warning("expire_signup_credit failed for %s", user_id, exc_info=True)
shared/wallet_estimates.py:622:        # $14.79 -> $15.00. $15.00 was also shared/wallet.SIGNUP_CREDIT_USD
shared/wallet_estimates.py:626:        # this tool needs a top-up (tests/test_signup_credit_covers_smallest_run.py
shared/wallet_guard.py:138:    tests/test_signup_credit_covers_smallest_run.py::NEEDS_TOPUP pins that.
shared/wallet_guard.py:144:    it is below ``SIGNUP_CREDIT_USD``, which therefore still admits an iggm run;
shared/wallet_guard.py:145:    ::test_the_signup_credit_still_admits_an_iggm_run pins that.
shared/wallet_guard.py:150:    ``SIGNUP_CREDIT_USD``; ::test_proteina_starts_at_one_dollar pins both.
shared/wallet_guard.py:163:    ``SIGNUP_CREDIT_USD``; ::test_pxdesign_starts_at_two_dollars pins both.
tools/af2/meta.py:22:from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD
tools/af2/meta.py:27:_SIGNUP_CREDIT: str = f"${SIGNUP_CREDIT_USD:.0f}"
tools/af2/meta.py:60:            f"{_SIGNUP_CREDIT} of credit, usable for {SIGNUP_CREDIT_EXPIRY_DAYS} "
tools/esmfold2_design/modal_app.py:117:#     shared/wallet.SIGNUP_CREDIT_USD, taking headroom against the free credit
tools/esmfold2_design/modal_app.py:118:#     to zero. SIGNUP_CREDIT_USD was raised to $20.00 in the same change to
tools/mpnn/meta.py:22:from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD
tools/mpnn/meta.py:27:_SIGNUP_CREDIT: str = f"${SIGNUP_CREDIT_USD:.0f}"
tools/mpnn/meta.py:48:            f"{_SIGNUP_CREDIT} wallet balance, usable for "
tools/mpnn/meta.py:49:            f"{SIGNUP_CREDIT_EXPIRY_DAYS} days."
tools/rfdiffusion/meta.py:16:from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD
tools/rfdiffusion/meta.py:21:_SIGNUP_CREDIT: str = f"${SIGNUP_CREDIT_USD:.0f}"
tools/rfdiffusion/meta.py:55:            f"{_SIGNUP_CREDIT} balance, usable for {SIGNUP_CREDIT_EXPIRY_DAYS} "
```

### A2. `git grep -n "signup_credit\|SIGNUP_CREDIT\|signup credit\|starting balance" -- templates`
```
templates/admin/user_detail.html:17:    {% if target.credit_expired_at %}· signup credit expired {{ target.credit_expired_at[:10] }}
templates/admin/user_detail.html:18:    {% elif target.credit_expires_at %}· signup credit expires {{ target.credit_expires_at[:10] }}{% endif %}
templates/components/_signin_cta.html:28:    New accounts start with a ${{ signup_credit() }} wallet balance, usable for {{ signup_credit_expiry_days }} days. Pay by the second of
templates/email/daily_digest.html:93:            ({{ p.spend_users }} user{{ '' if p.spend_users == 1 else 's' }}, signup credit included)
templates/email/send_signup_credit.html:1:{# Welcome plus signup credit confirmation (amount from shared.wallet.SIGNUP_CREDIT_USD).
templates/email/send_signup_credit.html:2:   Sender: shared.email.send_signup_credit_email
templates/email/send_signup_credit.html:6:     credit_usd     dollar amount of the signup credit (string, e.g. "5")
templates/email/send_signup_credit.html:7:     expiry_days    days until unspent credit expires (SIGNUP_CREDIT_EXPIRY_DAYS)
templates/email/send_signup_credit_expiring.html:1:{# Reminder that unspent signup credit expires soon.
templates/email/send_signup_credit_expiring.html:2:   Sender: shared.email.send_signup_credit_expiring_email
templates/email/send_signup_credit_expiring.html:3:   Triggered once per user by cron.signup_credit.run.
templates/email/send_signup_credit_expiring.html:7:     credit_usd     unspent signup credit, formatted (e.g. "12.50")
templates/help/faq.html:33:   "a": "No. There are no subscriptions, seats, tiers, or credits, and no minimum monthly spend. You pay only for the compute your jobs use. Top ups are final and money you add does not expire; the free signup credit expires " ~ signup_credit_expiry_days ~ " days after signup."},
templates/help/faq.html:43:   "a": "Your inputs are validated before any compute runs, so a bad chain ID or a malformed PDB is caught up front rather than after you have paid. Your $" ~ signup_credit() ~ " signup balance, usable for " ~ signup_credit_expiry_days ~ " days after sign-up, covers a first structure prediction or sequence run on your target. Most binder design runs cost more than that and need a top-up."},
templates/help/faq.html:137:          signup credit expires {{ signup_credit_expiry_days }} days after signup.
templates/help/faq.html:197:          paid. Your ${{ signup_credit() }} signup balance, usable for {{ signup_credit_expiry_days }} days after sign-up,
templates/help/getting_started.html:108:          ${{ signup_credit() }} already in it, enough for many structure
templates/help/getting_started.html:110:          than that and need a top-up. Any of it left unspent expires {{ signup_credit_expiry_days }} days after sign-up. A hold is placed against
templates/index.html:351:        ${{ signup_credit() }} in their wallet, usable for {{ signup_credit_expiry_days }} days. No tiers, no subscriptions.</strong>
templates/index.html:358:          <a href="{{ url_for('auth.signup') }}" class="btn-primary-lg">Start with ${{ signup_credit() }} in your wallet, usable for {{ signup_credit_expiry_days }} days</a>
templates/index.html:673:          delivered. New accounts start with ${{ signup_credit() }} in their wallet on signup,
templates/index.html:674:          usable for {{ signup_credit_expiry_days }} days.
templates/jobs_list.html:168:                <span style="color: var(--text-secondary); font-size: 0.9rem;">Add funds once your starting balance runs low. Minimum top-up is $20.</span>
templates/legal/terms.html:33:    starting balance of ${{ signup_credit(True) }} USD as a courtesy; this credit has no cash value
templates/legal/terms.html:35:    starting credit that has not been spent {{ signup_credit_expiry_days }} days after it is granted
templates/login.html:46:          Sign in to your wallet. New accounts start with ${{ signup_credit() }} of balance, usable for {{ signup_credit_expiry_days }} days.
templates/login.html:80:          Start with ${{ signup_credit() }} in your wallet, usable for {{ signup_credit_expiry_days }} days. Your balance
templates/pricing.html:5:{% block description %}Fund a USD wallet and it becomes your spend cap. Design as many candidates as you want, pay only for the compute delivered. No tiers, no subscriptions. New accounts start with a ${{ signup_credit() }} wallet balance, usable for {{ signup_credit_expiry_days }} days.{% endblock %}
templates/pricing.html:18:      start with a ${{ signup_credit() }} wallet balance, usable for {{ signup_credit_expiry_days }} days.
templates/pricing.html:25:        <a href="{{ url_for('auth.signup') }}" class="btn-primary-lg">Start with ${{ signup_credit() }} in your wallet, usable for {{ signup_credit_expiry_days }} days</a>
templates/pricing.html:47:          Create an account at tools.ranomics.com. Get ${{ signup_credit() }} in your wallet
templates/pricing.html:50:          than that and need a top-up. Unspent signup credit expires {{ signup_credit_expiry_days }} days after sign-up.
templates/pricing.html:147:          credit expires {{ signup_credit_expiry_days }} days after sign-up. Start with a
templates/pricing.html:195:          consumes. New accounts start with ${{ signup_credit() }} in their wallet, usable for
templates/pricing.html:196:          {{ signup_credit_expiry_days }} days. There is no
templates/pricing.html:234:          wallet and does not expire; the free signup credit expires
templates/pricing.html:235:          {{ signup_credit_expiry_days }} days after sign-up. Start with a small top up if you want
templates/showcase.html:104:          <a href="{{ url_for('auth.signup') }}" class="btn-primary-lg">Start free with ${{ signup_credit() }}, usable for {{ signup_credit_expiry_days }} days</a>
templates/showcase.html:109:        New accounts start with a ${{ signup_credit() }} wallet, usable for {{ signup_credit_expiry_days }} days. Pay by the second, no subscription.
templates/showcase.html:154:                <span class="showcase-runit-chip">New accounts start with ${{ signup_credit() }} for {{ signup_credit_expiry_days }} days</span>
templates/wallet/overview.html:16:     signup_credit_status  None, or {remaining_usd, expires_at} while unspent
templates/wallet/overview.html:17:                           signup credit remains (shared.wallet.signup_credit_status)
templates/wallet/overview.html:94:        {% if signup_credit_status %}
templates/wallet/overview.html:95:        {% set _exp = signup_credit_status.expires_at %}
templates/wallet/overview.html:98:          <span class="wallet-stat-value">${{ display_balance_usd(signup_credit_status.remaining_usd) }}</span>
templates/wallet/overview.html:99:          <span class="wallet-stat-foot">of ${{ display_balance_usd(signup_credit_status.grant_usd) }} unspent, expires {{ _exp.strftime('%B') }} {{ _exp.day }}, {{ _exp.year }} (UTC)</span>
templates/wallet/topup.html:115:      expire; the free signup credit expires {{ signup_credit_expiry_days }}
templates/wallet/transactions.html:32:      Every ledger entry: signup credit, top ups, holds, charges, releases.
templates/wallet/transactions.html:76:          <option value="signup_credit"     {% if filter_kind == 'signup_credit' %}selected{% endif %}>Signup credit</option>
templates/wallet/transactions.html:77:          <option value="signup_credit_expiry" {% if filter_kind == 'signup_credit_expiry' %}selected{% endif %}>Signup credit expiry</option>
templates/wallet/transactions.html:217:          signup credit entry and the rest of the ledger fills as you run
templates/wallet/transactions.html:231:        <dt><span class="wallet-tx-kind wallet-tx-kind-signup_credit">signup credit</span></dt>
templates/wallet/transactions.html:232:        <dd>One time ${{ signup_credit() }} signup balance on first account creation, usable for {{ signup_credit_expiry_days }} days.</dd>
templates/wallet/transactions.html:234:        <dt><span class="wallet-tx-kind wallet-tx-kind-signup_credit_expiry">signup credit expiry</span></dt>
templates/wallet/transactions.html:235:        <dd>The part of the signup balance still unspent {{ signup_credit_expiry_days }} days after it was granted, removed. Money you added is never touched.</dd>
```

### A3. `git grep -n "MIN_TOPUP" -- ":!tests" ":!docs"`
```
README.md:105:- Minimum top-up: `$20` (`MIN_TOPUP_USD` in `shared/wallet.py`). Smaller
app.py:66:    MIN_TOPUP_USD,
billing/checkout.py:59:    WALLET_MIN_TOPUP_USD             defaults to ``20.00`` per
billing/checkout.py:60:                                     ``shared.wallet.MIN_TOPUP_USD``
billing/checkout.py:153:    Falls back to ``shared.wallet.MIN_TOPUP_USD`` (currently $20) if the
billing/checkout.py:156:    raw = os.environ.get("WALLET_MIN_TOPUP_USD", "").strip()
billing/checkout.py:162:                "WALLET_MIN_TOPUP_USD=%r is not a valid decimal; "
billing/checkout.py:167:        from shared.wallet import MIN_TOPUP_USD  # noqa: PLC0415
billing/checkout.py:169:        return MIN_TOPUP_USD
blueprints/wallet.py:30:    MIN_TOPUP_USD,
blueprints/wallet.py:857:        min_topup_usd=MIN_TOPUP_USD,
blueprints/wallet.py:1067:    if amount < MIN_TOPUP_USD:
blueprints/wallet.py:1068:        amount = MIN_TOPUP_USD
shared/wallet.py:77:MIN_TOPUP_USD = Decimal("20.00")
shared/wallet.py:252:    """Round the deficit up to the nearest $5, with a floor of MIN_TOPUP_USD.
shared/wallet.py:262:        return MIN_TOPUP_USD
shared/wallet.py:265:    return max(bumped, MIN_TOPUP_USD)
shared/wallet_guard.py:29:    MIN_TOPUP_USD,
shared/wallet_guard.py:280:        min_topup_usd=MIN_TOPUP_USD,
```

### A4. `git grep -n "\$20\b" -- templates "*.py" ":!tests"`
```
billing/checkout.py:7:with the user supplied amount, so the same product covers $20 and
billing/checkout.py:150:def _default_min_topup_usd() -> Decimal:
billing/checkout.py:153:    Falls back to ``shared.wallet.MIN_TOPUP_USD`` (currently $20) if the
billing/checkout.py:171:        return Decimal("20.00")
billing/checkout.py:297:    min_topup_usd: Optional[Decimal] = None,
billing/checkout.py:313:        ``min_topup_usd`` and ``max_topup_usd``.
billing/checkout.py:319:    product_id, base_url, min_topup_usd, max_topup_usd
billing/checkout.py:337:    floor = min_topup_usd if min_topup_usd is not None else _default_min_topup_usd()
blueprints/wallet.py:857:        min_topup_usd=MIN_TOPUP_USD,
scripts/deploy/pass7_baseline.py:1:"""Pass 7 baseline snapshot — test user wallet state before live $20 topup."""
scripts/deploy/pass7_preflight_live_stripe.py:179:    print("OVERALL: GREEN — safe to run the $20 live topup")
scripts/deploy/pass7_watch.py:7:$20 topup on https://tools.ranomics.com.
shared/wallet.py:77:MIN_TOPUP_USD = Decimal("20.00")
shared/wallet.py:145:# Cut 20.00 -> 5.00 (2026-09-28, Leo). At $20 nobody had ever run out of free
shared/wallet.py:255:    ``ceil((estimate - balance) / 5) * 5`` with a $20 minimum.
shared/wallet_estimates.py:624:        # spent a cent of the free credit; that constant was raised to $20.00
shared/wallet_guard.py:280:        min_topup_usd=MIN_TOPUP_USD,
templates/jobs_list.html:168:                <span style="color: var(--text-secondary); font-size: 0.9rem;">Add funds once your starting balance runs low. Minimum top-up is $20.</span>
templates/jobs_list.html:172:                <span style="color: var(--text-secondary); font-size: 0.9rem;">Top up your USD wallet. The minimum top-up is $20.</span>
templates/pricing.html:56:          Top up in any amount from $20. That balance is your cap. Your
templates/wallet/_partials.html:17:   prints no figure but the balance and the $20 minimum; nothing in this file
templates/wallet/_partials.html:32:         "rounded_topup_usd":       "20.00",        # ceil(deficit/5)*5, min 20
templates/wallet/_partials.html:141:        here and run the job once your balance updates. Minimum top up $20;
templates/wallet/overview.html:30:      Top up in any amount from $20 and design without limits. No tiers, no
templates/wallet/topup.html:9:     min_topup_usd           usually Decimal("20.00")
templates/wallet/topup.html:98:  {# Defensive: not every route passes min_topup_usd. Treat undefined and
templates/wallet/topup.html:100:  {% set _min_raw = min_topup_usd|default(none, true) %}
templates/wallet/topup.html:103:    {'value': 20,   'label': '$20'},
tools/esmfold2_design/modal_app.py:118:#     to zero. SIGNUP_CREDIT_USD was raised to $20.00 in the same change to
```

### A5. `git grep -c "signup_credit\|SIGNUP_CREDIT" -- tests`
```
tests/sql/test_0017_wallet.sql:10
tests/test_admin_users_paging.py:5
tests/test_daily_digest.py:1
tests/test_email_real.py:10
tests/test_email_unsubscribe.py:11
tests/test_help_tool_guides.py:7
tests/test_live_charging.py:9
tests/test_no_prerun_numbers.py:1
tests/test_signup_credit_covers_smallest_run.py:7
tests/test_signup_credit_expiry.py:66
tests/test_signup_credit_single_source.py:19
tests/test_wallet.py:14
tests/test_wallet_invariants.py:2
tests/test_wallet_templates.py:7
```

### A6. `git grep -c "MIN_TOPUP\|20\.00\|\$20" -- tests`
```
tests/test_checkout.py:6
tests/test_compute_campaigns.py:1
tests/test_daily_digest.py:1
tests/test_email_real.py:2
tests/test_help_tool_guides.py:1
tests/test_money_display_surfaces.py:4
tests/test_no_prerun_numbers.py:3
tests/test_no_prerun_numbers_logged_in.py:1
tests/test_signup_credit_covers_smallest_run.py:2
tests/test_stripe_webhooks.py:2
tests/test_wallet.py:23
tests/test_wallet_templates.py:2
```

### A7. `git grep -n "MIN_TOPUP\|\$20\|signup credit" -- README.md docs/WALLET-ENV-VARS.md`
```
README.md:105:- Minimum top-up: `$20` (`MIN_TOPUP_USD` in `shared/wallet.py`). Smaller
docs/WALLET-ENV-VARS.md:48:| `WALLET_MIN_TOPUP_USD` | `20` | Floor on a single top up |
docs/WALLET-ENV-VARS.md:65:| `WALLET_MIN_TOPUP_USD` | none | `billing/checkout.py` | `20.00` (from `shared.wallet.MIN_TOPUP_USD`) | Falls back to the constant |
docs/WALLET-ENV-VARS.md:106:| `MIN_TOPUP_USD` | `shared/wallet.py:76` | `Decimal("20.00")` | Default for the optional `WALLET_MIN_TOPUP_USD` override |
docs/WALLET-ENV-VARS.md:107:| `SIGNUP_CREDIT_USD` | `shared/wallet.py` | `Decimal("20.00")` | The wallet credit applied at signup, and the only home for that number. There is no env override: `WALLET_SIGNUP_CREDIT_USD` was removed 2026-08-18 because it changed only the welcome email, never the grant, so its sole effect was to let the email disagree with the balance. It did — preview kept `=5` after the grant went to $15. Delete it from any environment that still sets it. Raised $15 -> $20 on 2026-09-10: the credit is sized against the largest CUSHIONED HOLD of any tool's smallest real run, not the largest displayed price -- pricing on the price is what let proteina and opendde sit at $15.00 holds against a $15.00 credit, refusing any new user who had spent a cent. `tests/test_signup_credit_covers_smallest_run.py` enforces the rule, but note its own docstring is explicit that at one unit the binding clamp is `base_hard_cap_usd`, so it catches a cap or credit change and NOT a change to the cushion, markup, rate card or worst-case floor. |
docs/WALLET-ENV-VARS.md:154:WALLET_MIN_TOPUP_USD=
```

### A8. Anchor lines
```
shared/wallet.py:77: MIN_TOPUP_USD = Decimal("20.00")
shared/wallet.py:161: SIGNUP_CREDIT_USD = Decimal("5.00")
shared/wallet.py:166: SIGNUP_CREDIT_EXPIRY_DAYS = 30
shared/wallet.py:169: LOW_BALANCE_EMAIL_THRESHOLD = Decimal("5.00")
shared/wallet.py:1804:     if balance < LOW_BALANCE_EMAIL_THRESHOLD <= balance + actual_cost:
billing/checkout.py:171:         return Decimal("20.00")
blueprints/wallet.py:857:         min_topup_usd=MIN_TOPUP_USD,
shared/wallet_guard.py:280:         min_topup_usd=MIN_TOPUP_USD,
shared/wallet_guard.py:438:                 if 0 < pre.balance_usd < live_start_usd(resolved_slug, params):
blueprints/wallet.py:1067:     if amount < MIN_TOPUP_USD:
blueprints/wallet.py:1068:         amount = MIN_TOPUP_USD
blueprints/auth.py:380:             f"Account created with ${SIGNUP_CREDIT_USD:.0f} of compute "
blueprints/auth.py:381:             f"credit, usable for {SIGNUP_CREDIT_EXPIRY_DAYS} days. Sign in "
```

## Decisions (Leo, 2026-10-23, relayed by the hub lead session)

Answers to the open questions, verbatim: "1. keep their minimums. 2. let free
credits exprei 3. plain welcome - show me your draft 4. yes. 5. stay at 20 min."

1. Tools keep their start minimums. Copy names BindCraft, BoltzGen, ESMFold2
   design and OpenDDE as needing a larger balance, with no figures.
2. Credit granted before 2026-10-23 runs to its expiry date. No new grants.
3. A plain welcome email on wallet creation. Leo edited the draft and approved
   this text (the template adds a full stop after "at a small scale"):
   "Your account is ready. A $5 balance is enough to start structure
   prediction, ProteinMPNN sequence design and most binder design tools at a
   small scale. Epitope Scout and Developability Scout are free and do not
   touch your balance, so you can triage a target before you pay for
   anything. [Browse the catalog] Top up when you are ready:
   tools.ranomics.com/account/wallet/topup. Reply to this email if you get
   stuck. The Ranomics team". Subject: "Welcome to Ranomics tools".
4. Add a $5 chip on the top-up page.
5. Auto reload keeps a $20 minimum (MIN_AUTO_RELOAD_USD).
