# Blueprint Refactor Plan — app.py monolith → Flask blueprints

**Status:** DEFERRED. Explicitly out of scope for the current hardening sweep. This is the
follow-up plan, to be executed as a standalone effort after the sweep lands.

**Goal:** Decompose the 6494-line `create_app()` factory in `app.py` into focused Flask
blueprints, relocate the platform API surface out of `tools/`, and fix module topology so the
~119 in-function lazy imports can become top-level imports.

**Non-goals / invariants (do not touch in this refactor):**
- No behavior change. Every route keeps its **exact URL path and HTTP methods**.
- Do **not** alter validated GPU pipeline logic, the wallet RPC SQL (`supabase/migrations`
  `0018`/`0020`), or RLS.
- No new dependencies. Blueprints are stdlib Flask, already in use (`scout_bp`,
  `platform_api_bp` are the existing precedent — see `app.py:387,1124,1169-1171`).

---

## 0. Current state (ground truth)

- `create_app()` registers **74** `@flask_app.route` handlers inline (plus **4** more
  `/account/api-keys*` handlers gated behind `ENABLE_PLATFORM_API`, defined inside the flag
  block ~`app.py:1355-1478`).
- Additional routes are mounted by helper registrars, **already factored out of the inline
  body** and out of scope for this plan: `register_stripe_webhook`, `register_metrics`,
  `register_modal_webhooks`, `register_upload_urls` (`app.py:1106-1118`), plus the existing
  `scout_bp` blueprint (`/scout/*`).
- Two blueprints already register cleanly today: `scout_bp` (`url_for('scout.index')` is live
  in templates) and `platform_api_bp`. **This proves the blueprint + `url_for('bp.endpoint')`
  pattern works in this codebase** — the refactor generalizes it.
- Cross-cutting app hooks live inline: `@flask_app.context_processor inject_workspace_context`
  (`app.py:1012`), `@flask_app.errorhandler(404/500)` (`app.py:6376,6381`).
- Decorators: `login_required` is already imported from `shared/auth.py:696`; `idempotent`
  from `shared/idempotency.py` (`app.py:81`); **`requires_wallet` is defined inline inside the
  factory at `app.py:736`** — this is the one decorator that must be relocated.
- **291** `# noqa: PLC0415` (in-function lazy imports) repo-wide; **119** of them in `app.py`.
  These exist to dodge import cycles created by the monolith and are the primary topology debt
  this refactor pays down.

---

## (a) Route → blueprint mapping

Seven blueprints. Each blueprint is a module under a new `blueprints/` package
(`blueprints/auth.py`, etc.), exporting `<name>_bp = Blueprint("<name>", __name__)`. Routes
move verbatim; only the decorator changes from `@flask_app.route(...)` to `@bp.route(...)`.

Endpoint names are preserved as written today so the `url_for` rename is mechanical and
greppable (see Risks). Blueprint endpoints become `"<bp>.<func>"`, e.g. `auth.login`.

### `auth_bp` — session, signup, password, API keys
Prefix: none (paths are top-level by historical URL). Owns:
- `/login`, `/signup`, `/logout`, `/forgot-password`, `/reset-password`
  (`login`, `signup`, `logout`, `forgot_password`, `reset_password_update`)
- `/account`, `/account/api-keys`, `/account/api-keys/create`,
  `/account/api-keys/<key_id>/revoke`, `.../rotate-webhook-secret`
  (`account`, `account_api_keys`, `account_api_keys_create`, `account_api_keys_revoke`,
  `account_api_keys_rotate_webhook_secret`)
- `/api/track` (`api_track`)
- `/.well-known/ai-plugin.json` (`ai_plugin_manifest`)

  Note: the API-key + ai-plugin routes stay **gated behind `ENABLE_PLATFORM_API`** — the
  blueprint registers always, but those four `add_url_rule`s are wrapped in the same flag
  check, or split into a sub-registration `register_api_key_routes(auth_bp)` called only when
  the flag is on (preferred: keeps the gate explicit).

### `wallet_bp` — money path (USD wallet, top-up, billing)
Prefix: none (mixed `/account/wallet/*`, `/api/wallet/*`, `/billing/*` paths). Owns:
- `/api/wallet/estimate`, `/api/wallet/balance`
  (`api_wallet_estimate`, `api_wallet_balance`)
- `/account/wallet`, `/account/wallet/topup`, `/account/wallet/checkout`,
  `/account/wallet/transactions`, `/account/wallet/auto-reload`, `/account/topup-complete`
  (`wallet_overview`, `wallet_topup`, `wallet_checkout`, `wallet_transactions`,
  `wallet_auto_reload`, `topup_complete`)
- `/billing/checkout`, `/billing/portal` (`billing_checkout`, `billing_portal`)
- `/workspaces`, `/workspaces/new` (GET+POST), `/workspaces/<workspace_id>`
  (`workspaces_list`, `workspaces_new`, `workspaces_new_submit`, `workspace_detail`)

  Rationale: workspaces are the wallet/seat container, not a tool; grouping with wallet keeps
  the billing surface in one file. **This blueprint owns `requires_wallet`'s relocated home**
  (see (c) and Risks) since it is the wallet domain.

### `tools_bp` — tool catalog, forms, submit, in-app CPU tools
Prefix: none (`/tools/*` plus the two CPU tools at top level). Owns:
- `/tools` (catalog), `/tools/<tool>` (form), `/tools/<tool>/preflight`,
  `/tools/<tool>/submit`
  (`tools_comparison`, `tool_form`, `tool_preflight`, `tool_submit`)
- `/developability`, `/developability/score` (`developability`, `developability_score`)
- `/library-planner`, `/library-planner/plan` (`library_planner`, `library_planner_plan`)

  Note: `tool_submit` carries `@login_required @idempotent() @requires_wallet` — the decorator
  stack ordering must be preserved exactly (see (c)/(e)).

### `jobs_bp` — job lifecycle, results, exports, downloads
Prefix: none (`/jobs/*`, `/api/jobs/*`). Owns:
- `/jobs`, `/jobs/compare`, `/jobs/<job_id>`, `/jobs/<job_id>/status.json`
  (`jobs_list`, `jobs_compare`, `job_detail`, `job_status`)
- `/jobs/<job_id>/refold`, `/cancel`, `/share`
  (`job_refold`, `job_cancel`, `job_share`)
- `/jobs/<job_id>/export.csv`, `/export.fasta`, `/export.zip`, `/af2.pdb`, `/af2_pae.npy`
  (`export_csv`, `export_fasta`, `export_zip`, `af2_download_pdb`, `af2_download_pae`)
- `/api/jobs/<job_id>/pdb/<path:filename>` (`job_candidate_pdb`)

  `job_refold` and `job_cancel` carry `@idempotent()`.

### `campaigns_bp` — customer-facing campaign (custom-scope) intake
Prefix: `/campaigns`. Owns:
- `/campaigns/submit`, `/campaigns`, `/campaigns/<campaign_id>`, `/campaigns/new`
  (`campaigns_submit`, `campaigns_dashboard`, `campaign_detail`, `campaigns_new_stub`)

### `admin_bp` — staff console
Prefix: `/admin`. Owns:
- `/admin/campaigns`, `/admin/campaigns/<id>`, `/admin/campaigns/<id>/status`,
  `/admin/campaigns/<id>/quote`
  (`admin_campaigns_list`, `admin_campaign_detail`, `admin_campaign_update_status`,
  `admin_campaign_save_quote`)
- `/admin/users`, `/admin/users/<user_id>`, `/admin/signups/rejected`
  (`admin_users_list`, `admin_user_detail`, `admin_signups_rejected`)

  Staff gating today is the `is_staff`/`STAFF_EMAILS` check inside each handler (no
  `@requires_admin` decorator exists). Leave that in place; do not invent a decorator in this
  refactor.

### `public_bp` — marketing, SEO, help, health, misc unauthenticated
Prefix: none. Owns:
- `/` (`index`), `/pricing`, `/terms`, `/privacy`, `/showcase`
- `/robots.txt`, `/sitemap.xml`, `/<indexnow_key>.txt`
  (`robots_txt`, `sitemap_xml`, `indexnow_key_file`)
- `/help`, `/help/getting-started`, `/help/tools/<tool>`, `/help/faq`, `/help/troubleshooting`
  (`help_index`, `help_getting_started`, `help_tool_guide`, `help_faq`, `help_troubleshooting`)
- `/health`, `/readyz` (`health`, `readyz`)

  Note: `indexnow_key_file` is registered conditionally (dynamic key in path). Preserve the
  conditional `add_url_rule` — register it on `public_bp` inside the same `if _indexnow_key`
  guard.

**What stays in `app.py` / `create_app()` (not a blueprint):**
- App + extension wiring (`Compress`, `ProxyFix` ~`app.py:937-950`), config, secret key.
- `inject_workspace_context` context processor (app-level — see Risks).
- `errorhandler(404/500)` (app-level).
- The four `register_*` webhook/metric registrars and `scout_bp`/`platform_api_bp`/new-blueprint
  `register_blueprint` calls.
- Jinja globals (`tool_about`, `platform_api_enabled`, etc.).

---

## (b) Relocate `tools/platform_api/` → top-level `api/` package

`tools/platform_api/` (~2014 lines: `routes.py` 973, `openapi_spec.py` 823,
`calibrated_targets.py` 210, `__init__.py` 8) is **an external API surface, not a tool
adapter.** It does not implement the tool-adapter contract (`tools/base.py`) that every real
tool under `tools/<slug>/` follows; it only lives there for historical reasons, and its
presence pollutes `tools/` adapter discovery/iteration.

Move:
```
tools/platform_api/  →  api/
  __init__.py            (exports platform_api_bp)
  routes.py
  openapi_spec.py
  calibrated_targets.py
```
- Update the single import site in `app.py:1169`
  (`from tools.platform_api import platform_api_bp` → `from api import platform_api_bp`).
- Grep for any other `tools.platform_api` / `tools/platform_api` references (tests, contracts,
  `openapi_spec` consumers) and update. `routes.py` already carries only 3 lazy imports, so
  topology risk is low.
- Blueprint endpoint name is unchanged (it's set by `Blueprint(...)` inside the module, not by
  the import path), so `url_for` for the platform API is unaffected.
- Keep this as its **own commit, separate from the blueprint extraction**, so a `git log
  --follow api/routes.py` cleanly shows the rename.

---

## (c) Lazy-import (`# noqa: PLC0415`) cleanup

119 in-function imports in `app.py` exist because the monolith creates cycles: handlers need
`shared.*` / `gpu.*` symbols that, if imported at module top, would re-enter `app` during its
own import (the factory and the helpers reference each other). Splitting into blueprint modules
breaks the cycle structurally:

- Each blueprint module imports `shared.*` / `gpu.*` / `tools.*` at **module top level**. None
  of those packages import `blueprints/*`, so no cycle exists → the lazy imports become normal
  top imports. Examples already proven safe at app top level today: `shared.credits`,
  `shared.wallet`, `shared.jobs`, `gpu.modal_client`, `shared.idempotency`
  (`app.py:57-125`).
- The `inject_workspace_context` processor's four lazy imports (`datetime`, `shared.auth`,
  `shared.credits`, `shared.workspaces` — `app.py:1014-1017`) move to the top of the (still
  app-level) processor's module.
- **`requires_wallet` relocation is the keystone:** it is defined inline in the factory
  (`app.py:736`) and closes over factory-local helpers/loggers. Extract it to
  `shared/wallet_guard.py` (new module) so both `tools_bp` (the `tool_submit` path) and
  `wallet_bp` can import it at top level. Its body's lazy imports collapse once it lives in a
  leaf module.
- **Method:** do NOT bulk-delete the noqa comments. Per blueprint, after the routes compile and
  the suite is green, run `ruff check --select PLC0415 blueprints/<name>.py` and promote each
  flagged import to the top **only if** it does not reintroduce a cycle (verified by
  `python -c "import blueprints.<name>"` booting clean and the full suite staying green). Any
  import that genuinely must stay lazy keeps its noqa with a one-line reason. Target: drive the
  119 toward ~0 in moved code; treat residual lazy imports as explicit, documented exceptions.

---

## (d) Per-blueprint commit sequence

Each commit is independently green: **full `pytest` suite passes** (baseline **948 passed, 6
skipped**; the 6 skips are Modal smoke tests that degrade offline) **+ a manual smoke of that
blueprint's routes** against a locally booted app. No commit lands red. (Reminder: commits are
made by the user — this plan does not auto-commit.)

Test invocation (Windows):
```
set PYTHONIOENCODING=utf-8 && set PYTHONUTF8=1 && venv\Scripts\python.exe -m pytest -q
```

**Commit 0 — scaffolding (no routes moved yet).**
Create `blueprints/__init__.py` and empty blueprint modules; create `shared/wallet_guard.py`
with `requires_wallet` moved out of the factory (factory imports it back, behavior identical);
register the (empty) blueprints in `create_app()`. Smoke: app boots, every existing route still
resolves (nothing moved yet). Gate: full suite green.

**Commit 1 — `public_bp`** (lowest risk: mostly static/unauthenticated, few `url_for` inbound
refs except `index`, `pricing`, `help_*`). Move routes, update `url_for('index')` →
`url_for('public.index')` etc. across templates + `app.py` redirects. Smoke: `/`, `/pricing`,
`/help`, `/health`, `/readyz`, `/sitemap.xml`, `/robots.txt`.

**Commit 2 — `auth_bp`.** Move login/signup/logout/password/account/api-keys. Update
`url_for('login'|'signup'|'logout'|'forgot_password'|'account'|'account_api_keys*')`. Smoke:
login, logout redirect, signup gate (signups-OFF path), forgot-password, `/account`; with
`ENABLE_PLATFORM_API=1` also smoke `/account/api-keys` create/reveal/revoke.

**Commit 3 — `wallet_bp`.** Move wallet/billing/workspaces. Update
`url_for('wallet_overview'|'wallet_topup'|'wallet_transactions'|'billing_portal'|'workspaces_*')`.
Smoke (no real charge): `/account/wallet` renders balance chip, `/account/wallet/topup` form,
`/api/wallet/balance` + `/api/wallet/estimate` JSON, `/workspaces`. **Extra care: this is the
money path** — confirm `requires_wallet` (now in `shared/wallet_guard.py`) still places/releases
holds (covered by existing wallet tests; verify they're green in THIS commit).

**Commit 4 — `tools_bp`.** Move catalog/form/preflight/submit + the two CPU tools. Update
`url_for('tool_form'|'tool_submit'|'tools_comparison'|'developability*'|'library_planner*')`
(`tool_form` has 23 template refs, `tools_comparison` 14, `tool_submit` 11 — highest inbound
fan-in). Smoke: `/tools` catalog, one `/tools/<slug>` form, a preflight POST, and a **full
submit through `@login_required @idempotent() @requires_wallet`** on a cheap tool (verify
decorator stack order unchanged and a hold is placed).

**Commit 5 — `jobs_bp`.** Move job lifecycle + exports + downloads. Update
`url_for('jobs_list'|'job_detail'|'job_refold'|'export_*'|'af2_*')` (`jobs_list` 18 template
refs, `job_detail` 5+8). Smoke: `/jobs` list, a `/jobs/<id>` detail page, `status.json`, an
`export.csv` + `export.zip`, a refold POST (idempotent).

**Commit 6 — `campaigns_bp`.** Move `/campaigns/*`. Update `url_for('campaigns_dashboard'|
'campaign_detail')`. Smoke: `/campaigns`, submit, detail.

**Commit 7 — `admin_bp`.** Move `/admin/*`. Update `url_for('admin_campaign_detail')` (6 refs in
`app.py`). Smoke as staff: `/admin/users`, `/admin/campaigns`, one campaign detail + a
status/quote POST.

**Commit 8 — topology cleanup.** Promote lazy imports to top-level in the moved modules per (c);
remove the now-stale `# noqa: PLC0415` where safe. Gate: full suite + `ruff check` clean. (The
`tools/platform_api` → `api/` move from (b) is its own separate commit, sequenced **before
Commit 0** or after Commit 8 — independent of the blueprint work; recommend doing it first so
`tools/` is clean before extraction begins.)

After every commit: re-run the full suite and the targeted smoke. If any `url_for` was missed,
Flask raises `BuildError` at render time — the per-blueprint smoke is specifically there to
catch that before the commit lands.

---

## (e) RISKS

**R1 — `url_for` endpoint-name changes (highest blast radius).**
Moving a route into a blueprint changes its endpoint from `login` to `auth.login`. Every
`url_for('login')` in templates and Python redirects breaks with a `BuildError` until updated.
Measured inbound references today (grep): templates use bare endpoints heavily —
`tool_form` ×23, `jobs_list` ×18, `tools_comparison` ×14, `tool_submit` ×11, `signup`/`login`
×8 each, `pricing`/`index` ×7 each, plus ~30 singletons; `app.py` redirects use `login` ×35,
`job_detail` ×8, `admin_campaign_detail` ×6, `tool_form`/`jobs_list` ×6 each.
Mitigation:
- The `scout.index` precedent already in templates proves the `bp.endpoint` form renders fine.
- Update `url_for` refs **in the same commit** that moves the routes, per blueprint, so each
  commit is internally consistent and the smoke catches any miss.
- Greppable, mechanical edit: `url_for('login'` → `url_for('auth.login'`. Search BOTH
  `templates/**` and `app.py` (and any `*.py` that builds URLs).
- Optional safety net during transition: register a tiny set of `app.add_url_rule(..., endpoint=
  "<old>")` aliases — **rejected** here because it hides misses and defeats the cleanup; rely on
  the smoke + `BuildError` instead.

**R2 — `inject_workspace_context` scoping (app-level vs blueprint-level).**
This `@context_processor` injects `nav_wallet_usd`, `active_workspaces_count`,
`show_onboarding_ribbon`, analytics keys, `support_email`, `now`, `canonical_url` into **every**
template (the shared header/base renders them on all pages, including public + auth + jobs). It
MUST stay an **app-level** `@flask_app.context_processor`, not be attached to any one blueprint —
a blueprint-scoped processor only fires for that blueprint's views, which would blank the navbar
wallet chip / workspace badge on every other blueprint's pages. Keep it defined in `app.py`
(its four lazy imports hoist to module top there). Same rule for the `404/500` errorhandlers:
keep them app-level so they cover all blueprints. `before_request`: there is no inline
`before_request` in the factory today (auth is enforced per-route via `@login_required`); do
**not** introduce a blueprint-level `before_request` for auth during this refactor — keep the
explicit per-route decorator so the security posture is unchanged and auditable.

**R3 — `requires_wallet` + `idempotent` decorator placement and ordering.**
`requires_wallet` is currently a **closure defined inside `create_app()`** (`app.py:736`),
which is why it can't be imported by a blueprint module today. It must move to a leaf module
(`shared/wallet_guard.py`) so `tools_bp` can import it at top level — this is the keystone
extraction. Risks:
- **Ordering is load-bearing.** On `tool_submit` the stack is, top-to-bottom,
  `@login_required` → `@idempotent()` → `@requires_wallet` (`app.py:3958-3960`). The hold is
  placed by `requires_wallet` *after* idempotency replay protection and auth, and the handler
  reads the hold id the decorator stashed (`app.py:4015,4300,4357`). Preserve this exact order
  on every wallet-bearing route. A reorder (e.g. wallet before idempotent) would place/replay a
  financial hold incorrectly — **do not reorder**.
- `idempotent` already imports cleanly from `shared/idempotency.py`; no change needed beyond
  moving the decorated route.
- `requires_wallet` closes over factory-local logger / config today; when extracted, pass those
  via module-level logger + reads from `current_app.config` / `os.environ` (it already reads
  env in places) rather than capturing factory locals. Behavior must stay byte-identical — gate
  on the existing wallet test suite (hold place/release, estimate, settle) staying green in the
  Commit-0 extraction before any route moves.
- `requires_wallet` supports both bare (`@requires_wallet`) and factory
  (`@requires_wallet(tool_slug='mpnn')`) forms (`app.py:917-920`). The extracted version must
  keep both call shapes — covered by existing tests; verify.

**R4 — platform_api move missing a reference.**
`tools/platform_api` may be referenced by tests, the contracts lock, or OpenAPI consumers, not
just `app.py:1169`. Grep `tools.platform_api` and `tools/platform_api` repo-wide before moving;
update all. Keep the move in its own commit for a clean `git log --follow`.

**R5 — blueprint registration order / gated routes.**
The `ENABLE_PLATFORM_API` flag block sets `SESSION_COOKIE_*` and registers the API-key routes +
`platform_api_bp` only when on (`app.py:1134-1177`). Preserve the flag semantics: `auth_bp`
registers unconditionally, but its `/account/api-keys*` + `/.well-known/ai-plugin.json` rules
stay inside the flag guard (via a `register_api_key_routes(auth_bp)` helper called only when the
flag is set). Verify both flag states in smoke: with flag off those paths must still 404.
