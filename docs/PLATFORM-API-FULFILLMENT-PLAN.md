# Platform API — Operator Fulfillment Pipeline (build plan)

**Status:** spec / handoff for a fresh build session. Written 2026-06-10.
**Scope:** the *receiving end* of the MCP/REST Platform API — what the operator
(Leo) does to take an inbound experiment from submission → quote → fulfillment →
results delivery. The customer-facing API surface already exists; the **operator
tooling to populate quotes and results does not**. This doc maps what's live and
specifies the build.

---

## 0. Context you need first

- **App:** `tools-hub` Flask app, deployed on Railway (auto-deploy on push to
  `main`), Supabase backend (project ref `wjlhbxfnihboqebdvnns`). Service-role DB
  access via `shared.credits.get_service_client()`.
- **Just hardened:** this repo went through a production HTTP/2 incident on
  2026-06-10. Before deploying, skim `ALERTING.md`, `docs/SESSION-HANDOFF-2026-06-10*.md`,
  and the memory note `project_tools_hub_http2_incident.md`. Deploys are sensitive:
  verify locally, then watch `/health` + `/readyz` + `scripts/smoke_platform_api.py`
  after each push.
- **Conventions:** no auto-commits (Leo reviews every commit). No em-dashes in any
  user-facing copy. Migrations live in `supabase/migrations/00NN_*.sql`.
- **Pricing note:** the marketing site publishes no pricing, but **per-customer
  quotes via the API are the intended mechanism** (the `/quote` endpoint exists for
  exactly this). Building operator quote entry is consistent with that decision.
- **Staff gating:** admin routes are gated to `shared.auth.STAFF_EMAILS`.
  `leowan7@gmail.com` is NOT staff — the admin account is a separate login. Keep
  that in mind when testing `/admin/*`.

---

## 1. The lifecycle that EXISTS today

A customer's AI agent (via the MCP server `ranomics-mcp/`, which proxies the REST
API ~1:1) drives this:

| Stage | Customer/agent call | Operator action |
|---|---|---|
| Estimate | `POST /api/v1/experiments/cost-estimate` → USD band (catalog) or `requires_human_quote` (custom) | — |
| List targets | `GET /api/v1/targets` | — |
| **Submit** | `POST /api/v1/experiments` → **201, status `WaitingForConfirmation`**, fires `experiment.waiting_for_confirmation` webhook | gets operator-alert email (`notify_operator_new_submission`) |
| Review | `GET /api/v1/experiments/{id}` (poll) | opens `/admin/campaigns/<id>`, sees sequences/target/assay/library/status-log |
| **Quote** | `GET /api/v1/experiments/{id}/quote` (once `QuoteSent`) | moves status → `QuoteSent` via the admin dropdown |
| Confirm | `POST /api/v1/quotes/{id}/confirm` → status `WaitingForMaterials`, fires `experiment.confirmed` webhook | — |
| Lab run | poll `GET /experiments/{id}` | advances `WaitingForMaterials → LibraryConstruction → Sorting → NGS → DataAnalysis → InReview → Done` via dropdown |
| **Results** | `GET /api/v1/experiments/{id}/results` (once `results_status != none`) | — (no path to set this today) |

**Status FSM (API rows):** `Draft → WaitingForConfirmation → QuoteSent →
WaitingForMaterials → LibraryConstruction → Sorting → NGS → DataAnalysis →
InReview → Done`; `Cancelled` reachable from any pre-terminal state. Forward-only.
Source: `shared/campaigns.py:48-61`; atomic transitions via
`transition_api_status` (`shared/campaigns.py:~513-580`) → RPC
`transition_lab_campaign_api` (migration 0026).

**Webhook asymmetry (important):** webhooks fire on **API-initiated** transitions
(submit, confirm-quote) via `_fire_webhook` (`tools/platform_api/routes.py:~877-909`,
signed HMAC, retried 5× over ~7h, persisted in `webhook_deliveries`). By product
decision, **operator/admin status changes do NOT fire a webhook or email** — the
agent sees them on its next poll (`app.py:~5691-5696`).

**Admin review page** (`templates/admin/campaign_detail.html`, route
`app.py:~5608-5664`): just rebuilt (2026-06-10, commit `763b044`) to support API
rows — shows experiment name, target, sequences, library design, status-log
timeline, webhook URL; status form uses `API_STATUSES`; transitions via
`transition_api_status` + `set_campaign_admin_fields` for contact/notes. **No
webhook/email on admin change** (intentional).

---

## 2. The gaps (what blocks order → fulfillment → delivery)

### CRITICAL

**G1 — Quote is a stub.** `GET /experiments/{id}/quote`
(`tools/platform_api/routes.py:645-679`) returns `total_usd: null`,
`line_items: []`, `valid_until: null`. There is **no operator path** to enter a
price, and **no DB column** for it (`Campaign` dataclass has no quote fields,
`shared/campaigns.py:67-132`). So `QuoteSent` is just a marker; the agent gets an
empty quote and cannot cost-gate. The OpenAPI `Quote` schema already documents the
fields as optional (`tools/platform_api/openapi_spec.py:~667-692`), so the contract
is ready — only the data path is missing.

**G2 — Results have no operator upload path.** `GET /experiments/{id}/results`
(`tools/platform_api/routes.py:731-768`) reads from `library_design['results']`, a
JSONB blob **nothing writes to** — a code comment at `:748-752` says the admin
tooling "is not yet wired." Also `results_status` (`none→partial→all`) is **read-only
in the admin UI**; it can only be set via the RPC/DB. So even with data in hand the
customer can't fetch results. Results envelope shape (Adaptyv YDS) is documented at
`openapi_spec.py:~735-748`.

### SECONDARY (lower priority)

- **N1** — Admin transitions are silent to the customer (no webhook/email). Fine for
  routine steps; you likely *want* to notify on `QuoteSent`, `Done`, `Cancelled`.
  There is no `notes_customer` field to attach a reason.
- **N2** — No results-ready webhook even on the API path.
- **N3** — No `list_my_experiments` endpoint (agent must persist `experiment_id`).
- **N4** — No quote expiry/rejection; no materials tracking for `WaitingForMaterials`;
  no admin audit log of note/contact edits; no `notes_customer`/decline-reason.

---

## 3. Build plan (phased)

Build in this order; each phase is independently shippable.

### Phase 1 — Quote entry (HIGHEST leverage; the gate to paid work)

1. **Schema** — new migration `supabase/migrations/00NN_campaign_quote.sql`:
   add `quote_total_usd numeric`, `quote_currency text default 'USD'`,
   `quote_line_items jsonb default '[]'`, `quote_valid_until timestamptz`,
   `quote_notes text` to `public.lab_campaigns`. (Alternative: nest under the
   existing `library_design` JSONB to avoid a migration, but dedicated columns are
   cleaner and queryable — prefer the migration.)
2. **Model** — add the fields to `Campaign` + `Campaign.from_row`
   (`shared/campaigns.py:67-132`); add a `set_campaign_quote(...)` helper near
   `set_campaign_admin_fields`.
3. **Admin UI** — add a "Quote" section to `templates/admin/campaign_detail.html`
   (API rows only): `total_usd`, `valid_until`, repeatable line-items (label +
   amount), `quote_notes`. Posts to a new route `POST /admin/campaigns/<id>/quote`
   (or fold into the status form). Saving the quote should also be able to move the
   row to `QuoteSent`.
4. **Wire the API** — `GET /experiments/{id}/quote`
   (`tools/platform_api/routes.py:645-679`) returns the persisted quote instead of
   the stub. Keep the response shape matching `openapi_spec.py` (fields are already
   defined; just populate them).
5. **Verify** — extend `scripts/smoke_platform_api.py` to set a quote (admin path or
   service client) and assert `GET /quote` returns real numbers.

### Phase 2 — Results delivery (the other end)

1. **Admin UI** — a results-attach form on `campaign_detail.html`: upload file(s)
   (enrichment CSV / hits FASTA / raw FASTQ) to Supabase Storage
   (`lab-campaigns/{id}/results/`) and/or paste a results JSON in the Adaptyv YDS
   envelope; write into `library_design['results']` (or a dedicated `results` JSONB
   column). Add a `results_status` picker (`none/partial/all`).
2. **Signed URLs** — generate signed Supabase Storage download URLs for the uploaded
   artifacts and embed them in the results envelope (`downloads.*`).
3. **Wire the API** — `GET /experiments/{id}/results`
   (`tools/platform_api/routes.py:731-768`) already reads `library_design['results']`;
   confirm it returns the populated envelope once `results_status != none`.
4. **(Optional N2)** — fire a results-ready webhook via `_fire_webhook` when
   `results_status` flips, so agents are notified, not just on poll.

### Phase 3 — Customer notification + comms (polish)

1. Add a `notes_customer` field + a "notify customer" checkbox to the admin status
   form. When checked, for API rows fire `_fire_webhook` (and/or
   `send_campaign_status_email`) on the admin transition — overriding the default
   no-notify behavior for the transitions you choose (`QuoteSent`, `Done`,
   `Cancelled`).
2. Surface `notes_customer` in the customer's `GET /experiments/{id}` view and/or the
   webhook payload.

---

## 4. Key file map

| Area | File:lines |
|---|---|
| Platform API routes | `tools/platform_api/routes.py` — targets `236-251`, create `259-470`, cost-estimate `478-572`, get `580-586`, delete `602-637`, **quote `645-679`**, confirm `687-723`, **results `731-768`**, `_fire_webhook ~877-909`, openapi `776-833` |
| Campaign model + FSM + helpers | `shared/campaigns.py` — `Campaign` `67-132`, `API_STATUSES` `48-61`, `transition_api_status ~513-580`, `create_api_campaign 350-446`, views `588-624`, `set_campaign_admin_fields` (added 2026-06-10) |
| Admin routes | `app.py` — list `~5590`, **detail `~5608-5664`**, **status `~5667-5749`** |
| Admin templates | `templates/admin/campaign_detail.html` (rebuilt for API), `templates/admin/campaigns_list.html` |
| Customer email + operator alert | `shared/email.py` — `notify_operator_new_submission`, `send_campaign_status_email` |
| Webhook dispatch/signing/retry | `shared/webhooks.py` |
| OpenAPI schemas (Quote/Results) | `tools/platform_api/openapi_spec.py` — Quote `~667-692`, Results `~735-748` |
| Migrations | `supabase/migrations/` — `0023` lab_campaigns, `0026` transition RPC |
| MCP server (proxy, 7 tools) | `ranomics-mcp/src/tools.ts`, `ranomics-mcp/src/api-client.ts` |
| E2E smoke test | `scripts/smoke_platform_api.py` |

---

## 5. Constraints / gotchas

- Keep `/quote` and `/results` **response shapes Adaptyv-compatible** and matching
  `openapi_spec.py`. Filling optional fields doesn't break the contract; changing
  shapes would (the contract is also referenced by `ranomics-mcp/`).
- All `lab_campaigns` mutations require the **service-role client** (RLS blocks
  authenticated users); go through `shared.credits.get_service_client()` or the
  existing `shared/campaigns.py` helpers (they apply the bounded-timeout + HTTP/1.1
  patched client).
- Windows env: Node at `/c/Program Files/nodejs` (prepend to PATH); use
  `venv\Scripts\python.exe`; `modal deploy` needs `PYTHONIOENCODING=utf-8`.
- A second session may be concurrently editing this repo (alerting follow-ups). Check
  `git status` / `git log` before committing; stage explicit files.
