# Wave 2 cross diff review

Reviewer: Wave 3 cross diff agent
Range reviewed: `1735c79..56e4e25` (8 commits, 28 files, +7856/-732 lines)
Authoritative plan: `C:\Users\lab\.claude\plans\i-am-in-the-moonlit-quill.md`
Dispatch handoff: `docs/HANDOFF-WALLET-PIVOT-SESSION-6.md`

---

## 1. Summary verdict

**YELLOW: small fixes needed before push.**

All five agent surfaces compile, type check at the import level, carry their own tests, and pass the dash rule on prose they newly wrote. Stripe Checkout, the webhook handler, the wallet decorator, the settle hook, and the email senders all wire to the canonical `shared.wallet` primitives correctly, and `wallet_funnel._call_handler` already trips the kwargs first vs positional fallback so Agent G's keyword only senders work with the funnel module untouched.

The yellows are concentrated on the Agent E to Agent H seam:

1. `templates/wallet/_partials.html` expects a richer JSON payload from `/api/wallet/estimate` than the route currently returns (six keys missing, see section 4). Without a contract fix the live estimate panel never lights up the soft warning, hard block, ceiling block, or gate.
2. `templates/wallet/topup.html` renders the same template for three distinct routes (manual top up, gate, topup confirmation) but the template never branches on `topup_success` / `topup_error` / `return_tool`, so the post Stripe confirmation page shows a fresh top up form, not a success message.
3. `templates/wallet/topup.html`, `templates/wallet/overview.html`, and `templates/wallet/transactions.html` reference routes `/account/wallet`, `/account/wallet/topup`, `/account/wallet/transactions`, `/account/wallet/checkout`, and `/account/wallet/auto-reload` that no agent registered in `app.py`. The wallet UI cannot be visited or its forms submitted until those routes ship.
4. `shared/jobs.py:924,947` call `send_job_capped_email(user_email=..., kind=..., job_id=...)` but Agent G's signature is `send_job_capped_email(*, user_id, tool_slug, attempted_usd, cap_usd, **_extra)`. The call is wrapped in try / except so jobs do not crash, but the email is silently lost on every overrun warning and overrun kill.
5. Agent H's three wallet templates use em dashes inside `{% block title %}` (rendered to the browser tab) and inside Jinja comments (not rendered, ignore).

None of the yellows block local push. They do block the live cutover. Sections 4 and 7 enumerate the fixes.

---

## 2. Scope compliance

| Agent | Intended scope | Files actually touched | Outside scope? | Severity |
|---|---|---|---|---|
| C | `billing/checkout.py`, `tests/test_checkout.py` | `billing/checkout.py`, `tests/test_checkout.py` | No | OK |
| D | `webhooks/stripe.py`, `tests/test_stripe_webhooks.py` | `webhooks/stripe.py`, `tests/test_stripe_webhooks.py` | No | OK |
| E | `app.py`, `shared/jobs.py`, `tests/test_wallet_api.py`, `tests/test_jobs.py` (extend) | `app.py`, `shared/jobs.py`, `tests/test_wallet_api.py`, `tests/test_jobs.py` (created fresh, not extended) | Borderline | LOW. Agent E created a brand new `tests/test_jobs.py` (568 lines) rather than extending one. The repo already has `tests/test_jobs_phase4.py`. No collision, no harm, but the plan and handoff said "extend tests/test_jobs.py". |
| G | `shared/email.py`, `templates/email/send_*.html`, `tests/test_email_real.py` | Same | No | OK |
| H | `templates/wallet/*.html`, `templates/pricing.html`, `templates/base.html` (1 line), `static/wallet.css` (new) | Same plus `static/wallet.css` | YES, `static/wallet.css` was not in the dispatched file list | LOW. `static/wallet.css` is a logical companion to the wallet templates and the 1 line in `templates/base.html` links it. Acceptable minor expansion. |

`templates/base.html` 1 line check: confirmed exactly one `+` line at the diff (`<link rel="stylesheet" href="{{ url_for('static', filename='wallet.css') }}">`). No prior content touched.

---

## 3. Dash audit

Search ran against every file touched in the range, using the rule "prose, log strings, error messages, email subjects + bodies, template rendered user facing strings". Identifiers, file paths, URL slugs, env var names, and Jinja comments may keep hyphens.

### New violations (introduced or carried into shipped surface)

| File | Line | Token | Surface | Severity |
|---|---|---|---|---|
| `templates/wallet/topup.html` | 22 | em dash inside `{% block title %}Top up wallet — Ranomics Tools{% endblock %}` | Rendered as the browser tab title | MEDIUM |
| `templates/wallet/overview.html` | 18 | em dash inside `{% block title %}Wallet — Ranomics Tools{% endblock %}` | Browser tab title | MEDIUM |
| `templates/wallet/transactions.html` | 23 | em dash inside `{% block title %}Transaction history — Ranomics Tools{% endblock %}` | Browser tab title | MEDIUM |

These three are real prose violations: `<title>` text is user visible.

### Not violations (verified safe)

- `templates/wallet/_partials.html:39,70,77,86,100,254`: em dashes inside `{# ... #}` Jinja comments, never rendered.
- `templates/wallet/transactions.html:38,135`: em dashes inside `{# ... #}` comments.
- `templates/wallet/overview.html:80,121,152`: em dashes inside `{# ... #}` comments.
- `templates/pricing.html:9,29,78,150,196,260,289`: `─` is the box drawing character, not an em or en dash. Also inside `{# ... #}` comments. Safe.
- `shared/email.py:5-712`: every em dash is in code Agent G did NOT touch (the legacy `send_job_complete_email`, `send_campaign_*`, `send_workspace_*`, daily digest senders). Wave 2's prose deliverables (the `send_*` senders Agent G wrote starting at line 725) are dash clean. The legacy senders belong to the workspace surface that the wallet pivot is decommissioning anyway. Out of scope for this review.
- `tests/test_email_real.py:37-38`: `EM_DASH = "—"` and `EN_DASH = "–"` are sentinels used by Agent G's own dash assertion tests. Not prose.
- `webhooks/stripe.py`, `billing/checkout.py`, `app.py` newly added lines, `shared/jobs.py` newly added lines: zero em or en dashes (`git diff 1735c79..HEAD -- ... | grep -E '^\+.*[—–]'` returns empty for all five files).
- `templates/email/send_*.html` (11 new templates from Agent G): zero em or en dashes.
- `templates/pricing.html` prose body: zero em or en dashes.

### Total

Three prose violations across three template titles, all in Agent H's surface. Fix is a one character edit per file (replace `—` with `:` or restructure).

---

## 4. Inter agent contract consistency

### 4.1 Decorator gate template vs Agent H's `wallet/topup.html`

**Findings**

- `app.py:461-474` (the `_render_topup_gate` helper inside `requires_wallet`) renders `wallet/topup.html` with context keys: `wallet`, `deficit_usd`, `estimate_usd`, `balance_usd`, `hard_cap_usd`, `suggested_amount`, `min_topup_usd`, `next_url`, `gate_reason`, `tool_slug`, `self_serve_ceiling_usd`.
- `templates/wallet/topup.html:7-19` documents its expected context as: `wallet`, `suggested_amounts` (plural), `min_topup_usd`, `next_url`, `deficit_usd`, `topup_action_url`.

**Mismatches**

| Context key | Sent by app.py | Consumed by template | Net effect |
|---|---|---|---|
| `suggested_amount` (singular) | yes | no (template reads `suggested_amounts` plural with inline fallback) | safe, template falls back |
| `suggested_amounts` (plural) | no | yes | template uses inline fallback list, OK |
| `topup_action_url` | no | yes (`{{ topup_action_url or '/account/wallet/checkout' }}`) | template falls back to `/account/wallet/checkout`, **which has no route in app.py** |
| `estimate_usd` | yes | not read by template | dead value, OK |
| `hard_cap_usd` | yes | not read by template | dead value, OK |
| `gate_reason` | yes | not read by template | dead value, OK |
| `tool_slug` | yes | not read by template | dead value, OK |
| `self_serve_ceiling_usd` | yes | not read by template | dead value, OK |
| `min_topup_usd`, `deficit_usd`, `next_url`, `wallet` | yes | yes | match |

**Severity:** MEDIUM. The template renders, but the submit button posts to `/account/wallet/checkout`, an endpoint nobody registered. The gate flow visually works but the click does nothing.

### 4.2 `/api/wallet/estimate` JSON shape vs `templates/wallet/_partials.html` expectations

**Findings**

- `app.py:1532-1542` returns: `{ok, tool_slug, estimate_usd, hard_cap_usd, balance_usd, balance_after_usd, self_serve_ceiling_usd, exceeds_hard_cap, exceeds_self_serve_ceiling}`.
- `templates/wallet/_partials.html:22-34` documents the contract it consumes: `estimate_usd, balance_usd, balance_after_usd, deficit_usd, rounded_topup_usd, scaled_hard_cap_usd, soft_block, hard_block, self_serve_block, confirm_band, wallet_frozen`.
- `templates/wallet/_partials.html:198,208-216` actually reads `data.deficit_usd`, `data.rounded_topup_usd`, `data.scaled_hard_cap_usd`, `data.soft_block`, `data.hard_block`, `data.self_serve_block`. None of these exist in the route response.

**Net effect**

The JS treats missing fields as `0` / `false` / `undefined`. The estimate value, balance value, and balance after value all paint correctly. But the gate visibility, the soft warn band, the hard block panel, and the ceiling block panel are all driven by the missing keys and so will never appear. The decorator side of the gate still works on POST submit, but the live in form preview never warns the user.

**Severity:** MEDIUM. Either Agent E extends the JSON response to include the six missing keys, or Agent H's partial reads the existing keys (`exceeds_hard_cap`, `exceeds_self_serve_ceiling`, derive `deficit_usd` from `balance_after_usd`, derive `rounded_topup_usd` from `deficit`). Either side fix is small.

### 4.3 `/account/topup-complete` template branching

**Findings**

- `app.py:1567-1622` calls `render_template("wallet/topup.html", ...)` with one of three intent flags: `topup_error="..."`, `topup_success=True`, or both unset (legitimate gate render). All paths pass `wallet`, optionally `return_tool` and `return_tool_url`.
- `templates/wallet/topup.html` does NOT branch on `topup_success` or `topup_error` or `return_tool` anywhere in the file. The template treats every render the same way.

**Net effect**

After Stripe Checkout returns, the user lands on `/account/topup-complete?session_id=cs_...`, the route looks up the session, the webhook credits in the background, and then the user sees the same top up form they just paid through. No confirmation, no "your balance is $X now", no return-to-tool link.

**Severity:** MEDIUM. Either app.py picks a different template for the confirmation page, or Agent H adds an `{% if topup_success %}` branch to topup.html.

### 4.4 `top_up_wallet` / `freeze_wallet_on_dispute` signatures (Agent D)

Verified against `shared/wallet.py:336` and `shared/wallet.py:767`:

- `top_up_wallet(user_id, amount_usd, *, stripe_payment_intent_id, stripe_event_id, kind)`: Agent D calls this at `webhooks/stripe.py:339-345` and `webhooks/stripe.py:398-404` with all five kwargs supplied. Match.
- `freeze_wallet_on_dispute(user_id, dispute_id)`: Agent D calls at `webhooks/stripe.py:521`. Match.

### 4.5 Stripe `success_url` placeholder and Agent E session_id read

- `billing/checkout.py:275`: `success_url = base_url + "/account/topup-complete?session_id={CHECKOUT_SESSION_ID}"`. Literal placeholder. Match.
- `app.py:1562`: `session_id = (request.args.get("session_id") or "").strip()`. Match.
- `tests/test_checkout.py:282-291` exercises this contract.

### 4.6 Agent G email sender signatures vs Agents C / D / E imports

`grep "from shared.email import"` and `getattr(email_module, name)` across the diff:

| Caller | Sender used | Agent G signature | Match? |
|---|---|---|---|
| `webhooks/stripe.py:350,408,466,521,...` (via `_send_email_safe(getattr, **kwargs)`) | `send_topup_confirmation_email(user_id=..., amount_usd=...)`, `send_auto_reload_charged_email(user_id=..., amount_usd=...)`, `send_auto_reload_failed_email(user_id=..., reason=...)` | matches all three | yes |
| `shared/wallet.py:780,795` (via `_send_email_safe`) | `send_wallet_frozen_email(user_id=..., dispute_id=...)`, `alert_ops_slack(event=..., user_id=..., dispute_id=...)` | matches both | yes |
| `shared/wallet_funnel.py:96` (via `_resolve_handler` and kwargs first) | `send_pilot_intro_email(user_id=..., spent_30d_usd=...)`, `alert_sales_slack(user_id=..., spent_30d_usd=...)`, `alert_sales_slack_high(user_id=..., spent_30d_usd=...)` | matches all three | yes |
| `shared/jobs.py:924-931` (direct call, NOT through `_send_email_safe`) | `send_job_capped_email(user_email=..., tool_slug=..., attempted_usd=..., cap_usd=..., kind="overrun_warning", job_id=...)` | Agent G signature is `(*, user_id, tool_slug, attempted_usd, cap_usd, **_extra)`. `user_email` is swallowed by `**_extra`, `user_id` is missing as a required kwarg, call raises `TypeError`. | NO |
| `shared/jobs.py:947-954` (direct call) | same as above with `kind="overrun_kill"` | same TypeError | NO |

`shared/jobs.py:922,945` wraps each direct call in `try / except Exception` so the TypeError is caught and logged. Result: every mid-run overrun warning and every safety-kill notice silently drops the email. Settle path is unaffected.

The plan listed `send_overrun_warning_email` and `send_overrun_kill_email` as separate senders at plan lines 1641-1642. Agent G did not implement them; Agent E reused `send_job_capped_email` with a `kind=` switch. The reuse is reasonable but the kwarg name mismatch breaks the path.

**Severity:** MEDIUM. Two line fix: rename `user_email=` to `user_id=job.user_id` and resolve email inside the email sender, or split into `send_overrun_warning_email` and `send_overrun_kill_email`.

### 4.7 Env var unification

Every env var referenced by an agent in this wave:

| Env var | Used in | Notes |
|---|---|---|
| `STRIPE_SECRET_KEY` | billing/checkout.py, webhooks/stripe.py | single name, fine |
| `STRIPE_WEBHOOK_SECRET` | webhooks/stripe.py | single name, fine |
| `STRIPE_WALLET_TOPUP_PRODUCT_ID` (plan name) and `STRIPE_TOPUP_PRODUCT_ID` (alias) | billing/checkout.py:152-154 | Agent C accepts either, plan name first |
| `WALLET_MIN_TOPUP_USD` | billing/checkout.py | fine, with fallback to `shared.wallet.MIN_TOPUP_USD` |
| `WALLET_MAX_TOPUP_USD` | billing/checkout.py | fine, default $5000 (matches Agent C self-report) |
| `WALLET_SIGNUP_CREDIT_USD` | shared/email.py:979 | fine |
| `WALLET_DEFAULT_DAILY_CAP_USD` | shared/email.py:1266 | fine |
| `APP_BASE_URL` and `APP_URL` alias | billing/checkout.py:139-140 | Agent C uses these |
| `PUBLIC_BASE_URL` | shared/email.py:865, app.py:857,1032, cron/daily_digest.py:87 | every other surface uses this name |
| `EMAIL_FROM` | shared/email.py:855 | legacy default |
| `RESEND_FROM_TRANSACTIONAL` | shared/email.py:852 | Agent G unified, falls back to `EMAIL_FROM` |
| `RESEND_API_KEY` | shared/email.py | single name, fine |
| `SUPPORT_EMAIL` | shared/email.py:859 | new, Agent G; reasonable default |
| `SLACK_SALES_WEBHOOK_URL` (dispatch name) and `WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL` (plan name) | shared/email.py:1401-1402 | Agent G accepts either |
| `SLACK_OPS_WEBHOOK_URL` | shared/email.py:1408 | Agent G unilateral; plan does not name it |

**Divergence to flag**

- **Base URL alias divergence**: Agent C reads `APP_BASE_URL` then `APP_URL`. Agent G and `cron/daily_digest.py` and pre-existing app.py code read `PUBLIC_BASE_URL`. These are the same logical variable. Pick one canonical name in the Railway config (recommend `PUBLIC_BASE_URL` because it has more incumbents) and either add it as an alias to Agent C's `_base_url`, or rename Agent G to read `APP_BASE_URL`.
- **Slack ops webhook name**: Agent G coined `SLACK_OPS_WEBHOOK_URL`. The plan only specifies the var for sales. Reasonable invention, but should be locked into a written env table before Railway config.

**Severity:** MEDIUM for the base URL alias divergence (silently swappable in prod misconfig). LOW for the ops slack name (greenfield).

---

## 5. Missing tests

Plan testing strategy is at plan lines 1480-1582. Coverage gaps relative to that strategy:

| Surface | Plan layer | Status |
|---|---|---|
| `templates/wallet/topup.html`, `overview.html`, `transactions.html`, `_partials.html` jinja render smoke | Layer 1 (unit) | MISSING. `tests/test_wallet_api.py:299,506` asserts `render_template` was called with the right name but never actually renders the template. Recommended: a single `test_wallet_templates_render` test that instantiates the Flask app, calls `flask_app.test_request_context().push()`, and `render_template("wallet/topup.html", **min_ctx)` for each template with realistic context, asserting no Jinja exception. Catches 4.1 mismatches at CI time. |
| `templates/pricing.html` jinja render | Layer 1 | MISSING. Same recipe. The template uses `session.get('user_email')` so smoke test needs an active session. |
| Tool form integration test (Moment 1 estimate panel rendered + estimate endpoint hit) | Layer 2 (integration) | BLOCKED on H follow up: the partial is not integrated into `templates/tools/*.html` yet. |
| `/account/wallet`, `/account/wallet/topup`, `/account/wallet/transactions`, `/account/wallet/checkout`, `/account/wallet/auto-reload` route gating + render | Layer 2 | MISSING. Routes do not exist yet. |
| `_settle_wallet_hold_for_completed_job` for `cancelled` status with non zero gpu_seconds | Layer 1 | PARTIAL. `tests/test_jobs.py` covers succeeded, failed plus gpu, failed plus no gpu, timeout plus no gpu, cancelled plus no gpu. Cancelled with gpu is undefined by the plan but is a real edge case (Modal cancel hits after work started). |
| `mid_run_monitor_check` does not warn twice or kill twice | Layer 1 | COVERED at `tests/test_jobs.py` (per Agent E self report on idempotency on `overrun_warned`). |
| Webhook race: `checkout.session.completed` arrives before user's session expires | Layer 1 | PARTIAL. `tests/test_stripe_webhooks.py` covers idempotent replay, but not "session metadata user_id is stale" path. Low priority. |
| Auto-reload monthly cap and 24h count enforcement | Layer 1 | COVERED in `tests/test_email_real.py` and (per Wave 1) `tests/test_wallet.py`. |
| Negative path: `top_up_wallet` returns None inside webhook handler | Layer 1 | COVERED at `tests/test_stripe_webhooks.py` (per Agent D self-report). |

**Total missing**: 5 test areas. The first two (jinja render smoke) are the cheap, high value additions and should land before the live cutover.

---

## 6. Plan compliance (Critical files at a glance, plan line 1592)

Each plan listed file checked against the working tree.

| Plan reference | Status | Notes |
|---|---|---|
| `billing/tiers.py` strip Workspace SKU, add wallet config | not checked here, Wave 1 territory | out of Wave 2 scope |
| `billing/checkout.py` variable-amount Checkout + optional SetupIntent | YES, file exists with `create_topup_session`. Naming divergence: plan said `create_topup_checkout_session`, Agent C used `create_topup_session`. Functional content matches plan code block. | LOW: rename for plan alignment, otherwise OK |
| `webhooks/stripe.py` wallet credit handler + auto-reload PI handler | YES, file rewritten by Agent D. Covers all 4 event types. | OK |
| `shared/workspaces.py` → `shared/wallet.py` rewrite | not touched in Wave 2 (Wave 1 territory) | n/a here |
| `shared/jobs.py` swap charge function | YES via `_settle_wallet_hold_for_completed_job` hook added at `complete_job`. Note: the original `_charge_workspace_for_completed_job` was NOT removed; both run sequentially. Intentional per the plan gotcha #4 "do not delete workspaces.py yet". | OK |
| `shared/wallet_funnel.py` | not touched in Wave 2 (Wave 1) | n/a |
| `shared/email.py` add 8 new senders | YES, Agent G added 11 user senders + 3 internal Slack senders | plan said 8, Agent G shipped 11. Plan-listed `send_overrun_warning_email` and `send_overrun_kill_email` are absent (Agent E reuses `send_job_capped_email` with kind switch instead, see 4.6). |
| `app.py` `requires_wallet` decorator | YES at `app.py:477`, plus `/api/wallet/estimate` at `app.py:1445`, plus `/account/topup-complete` at `app.py:1544` | OK |
| `supabase/migrations/0017_wallet.sql` | YES, Wave 1, Gate A closed | n/a |
| `templates/wallet/*` balance widget, top-up form, transaction history, auto-reload settings | YES, Agent H. Balance widget = overview.html top panel. Top-up form = topup.html. Transaction history = transactions.html. Auto-reload settings = topup.html bottom panel. | OK |
| `templates/pricing.html` rewrite | YES, Agent H | OK |
| `tests/test_wallet.py`, `tests/test_wallet_route_gating.py`, `tests/test_wallet_completion.py` | Agent E shipped `tests/test_wallet_api.py` + extended `tests/test_jobs.py` (created fresh) instead | LOW: different naming, equivalent coverage |
| `send_overrun_warning_email` (sender #12) | MISSING. Plan line 1641. | MEDIUM (see 4.6) |
| `send_overrun_kill_email` (sender #13) | MISSING. Plan line 1642. | MEDIUM (see 4.6) |
| `alert_sales_slack` | PRESENT at shared/email.py:1411 | OK |
| `_alert_ops_slack` (plan named it private) | PRESENT at shared/email.py:1457 as `alert_ops_slack` (public, underscore dropped) | LOW: naming nit, no breakage |

---

## 7. Open follow ups

### HIGH

- [HIGH] Integrate Moment 1 partial and the "Top up and run" gate into `templates/tools/*.html` submit forms. Agent H reported this skipped. Without it the inline cost preview and the gate are unreachable from any tool form. (Carried from dispatch.)
- [HIGH] Wire up `/account/wallet`, `/account/wallet/topup`, `/account/wallet/transactions`, `/account/wallet/checkout`, `/account/wallet/auto-reload` Flask routes in `app.py`. Wallet UI and all linked nav are broken until these ship. The templates already exist; the routes do not.
- [HIGH] Fix the `/api/wallet/estimate` JSON shape OR `wallet/_partials.html` consumer to align on `deficit_usd`, `rounded_topup_usd`, `scaled_hard_cap_usd`, `soft_block`, `hard_block`, `self_serve_block`, `confirm_band`, `wallet_frozen`. Without alignment the inline gate banner never appears (see 4.2).

### MEDIUM

- [MEDIUM] Fix `shared/jobs.py:924,947` overrun email calls. `send_job_capped_email` requires `user_id=`, not `user_email=`. Currently every overrun warning email and overrun kill email is silently swallowed by the try / except. Either rename the kwarg in the caller, or split `send_overrun_warning_email` and `send_overrun_kill_email` per plan lines 1641-1642.
- [MEDIUM] Resolve the `APP_BASE_URL` vs `PUBLIC_BASE_URL` divergence (see 4.7). Recommend `PUBLIC_BASE_URL` because it is the incumbent across `shared/email.py`, `app.py`, and `cron/daily_digest.py`. Add it as an alias inside `billing/checkout.py:_base_url` between `APP_BASE_URL` and `APP_URL` so a single Railway value lights up every surface.
- [MEDIUM] Strip em dashes from the three `{% block title %}` lines in Agent H's templates (`templates/wallet/topup.html:22`, `overview.html:18`, `transactions.html:23`). One character per file.
- [MEDIUM] Add a `topup_success` / `topup_error` / `return_tool` branch to `templates/wallet/topup.html` so `/account/topup-complete` shows a real confirmation page after Stripe Checkout returns. Currently the user lands on the top up form again.
- [MEDIUM] Normalize env var aliases across agents (carried from dispatch). Write a single env table in `docs/` so the Railway side has one source of truth.
- [MEDIUM] Add jinja render smoke tests for `templates/wallet/{overview,topup,transactions,_partials}.html` and `templates/pricing.html`. Without these, the contract mismatches in 4.1, 4.2, and 4.3 stay invisible to CI.

### LOW

- [LOW] Resolve `create_topup_session` vs `create_topup_checkout_session` naming. Agent C used the shorter name. The plan code block at plan line 1606 said the longer one. Pick one and align the docs (carried from dispatch).
- [LOW] Decide on `SLACK_OPS_WEBHOOK_URL` as the canonical env name for the ops channel. Agent G coined it. Write into the docs/env table.
- [LOW] Agent E created `tests/test_jobs.py` rather than extending it. Repo also has `tests/test_jobs_phase4.py`. No actual collision. Consider folding existing legacy jobs coverage and the new wallet jobs coverage into one file in a future cleanup.
- [LOW] Plan named the ops Slack alerter `_alert_ops_slack` (leading underscore = private). Agent G shipped it as public `alert_ops_slack`. No breakage, but rename for plan fidelity if the public surface is not desired.
- [LOW] Consider adding `cancelled` with non zero `gpu_seconds_used` to `tests/test_jobs.py`. Modal cancel can race work that already burned some seconds.

---

## Outcome

- 8 commits compile and self test.
- Dispatch scopes were honoured (one minor expansion: Agent H added `static/wallet.css`, acceptable).
- 3 prose dash violations in template titles, 3 missing routes, 2 contract mismatches (estimate JSON, topup-complete branching), 1 silent email path failure (overrun emails).
- Fixes are concentrated in app.py and the wallet templates; no agent surface needs a full rewrite.

Recommend pushing the 8 commits to `origin/main`, then opening Wave 3 fix commits for the HIGH items before any Railway preview deploy.
