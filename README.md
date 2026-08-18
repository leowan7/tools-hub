# Ranomics Tools Hub

Flask app that hosts Ranomics' scientific tools under `tools.ranomics.com`.
The hub itself stays lightweight: auth, a landing page, a USD wallet, and
per-tool routes. Each tool lives in its own package under `tools/` and
exposes a small stable API that `app.py` imports lazily.

## Tools

Fourteen GPU tools live in `tools/<slug>/` and register through
`tools.base`; each one is gated on `FLAG_TOOL_<SLUG>` (see
`shared/feature_flags.py`, fail-closed) and appears in the catalog only
when its flag is on. Category is the workflow band from
`shared/tools_catalog.py::_TOOL_CATEGORIES`, which drives the homepage
grid and `/tools`.

| Tool | Slug | Category | Route |
|------|------|----------|-------|
| RFdiffusion | `rfdiffusion` | Make new binders for my target | `/tools/rfdiffusion` |
| BindCraft | `bindcraft` | Make new binders for my target | `/tools/bindcraft` |
| PXDesign | `pxdesign` | Make new binders for my target | `/tools/pxdesign` |
| RFantibody | `rfantibody` | Make new binders for my target | `/tools/rfantibody` |
| BoltzGen | `boltzgen` | Make new binders for my target | `/tools/boltzgen` |
| IgGM | `iggm` | Make new binders for my target | `/tools/iggm` |
| Proteina-Complexa | `proteina` | Make new binders for my target | `/tools/proteina` |
| ESMFold2 design | `esmfold2-design` | Make new binders for my target | `/tools/esmfold2-design` |
| ProteinMPNN | `mpnn` | Choose sequences for a structure I already have | `/tools/mpnn` |
| AlphaFold2 | `af2` | Predict or check a 3D structure | `/tools/af2` |
| ColabFold | `colabfold` | Predict or check a 3D structure | `/tools/colabfold` |
| ESMFold | `esmfold` | Predict or check a 3D structure | `/tools/esmfold` |
| Boltz-2 | `boltz2` | Predict or check a 3D structure | `/tools/boltz2` |
| OpenDDE co-folding | `opendde` | Predict or check a 3D structure | `/tools/opendde` |

Two catalog entries are not GPU adapters and are hardcoded in
`_HARDCODED_TOOLS`. They are not flag-gated, and `/help/tools/<slug>`
does not resolve for them because `tools.base.get()` returns `None`:

| Tool | Category | Route |
|------|----------|-------|
| Epitope Scout | Check if my target is a good one to bind | `scout.index` (`https://scout.ranomics.com` in production) |
| Binder Developability Scout | See if a binder will hold up in the lab | `/developability` |

The **Yeast Display Library Planner** (`/library-planner`) was
deliberately delisted from the catalog on 2026-08-17 — its
`_HARDCODED_TOOLS` entry was dropped, so no tile appears on the homepage
or `/tools`. The route, its templates, and the `tools/library_planner`
package are intentionally still live so existing job links and job
history keep resolving instead of 404ing. Delisted, not removed; do not
"clean up" the route.

## Architecture

The hub is the orchestrator. Atomic and composite GPU pipelines run on
Modal, but the Modal app code (image build, Function definitions, weight
provisioning) lives in the sibling `llm-proteinDesigner` repo under
`infrastructure/modal/`. That repo's CI deploys the Modal apps under the
`ranomics-*-prod` namespace (for example `ranomics-mpnn-prod`,
`ranomics-bindcraft-prod`, `ranomics-pxdesign-prod`).

tools-hub never invokes `modal deploy`. It calls already-deployed Modal
Functions over the RPC contract defined in `contracts/` and consumed via
`shared/modal_client.py`. Outputs land in the `tool-outputs` Supabase
storage bucket and are served back through `app.py` resolver endpoints.

See `docs/ATOMIC-TOOLS.md` for the current tool catalog and
`docs/PRODUCT-PLAN.md` for tiering and pricing context.

## Contracts (shared boundary)

`contracts/` holds the pydantic v2 models that define the request and
response payloads exchanged with Modal. It is vendored byte-identical
into the sibling `llm-proteinDesigner` repo, which mounts the directory
into each composite Modal image at `/opt/contracts` via
`modal.Image.add_local_dir(...)`.

Two rules:

1. Any edit to `rpc.py` or `upload_urls.py` must land in BOTH repos in
   the same release. Bump `CONTRACT_VERSION` in `contracts/__init__.py`
   and log the change in `docs/ORCH-LOG.md`.
2. Refresh `contracts/CONTRACTS_SHA256.lock` in BOTH repos. The CI guard
   at `.github/workflows/contracts-drift.yml` runs `sha256sum -c` against
   that lockfile on every PR touching `contracts/` and fails if the
   hashes drift. `__init__.py` is excluded from the lockfile because
   each repo carries a per-repo sync-source comment in that file.

## Wallet (USD wallet is the sole money path)

Wallet code lives in `shared/wallet.py` (ledger), `shared/wallet_estimates.py`
(cost projections), and `billing/checkout.py` (Stripe). Settlement is a
two-row hold + partial-release pattern on `wallet_transactions`; see
the `docs/HANDOFF-WALLET-PIVOT-SESSION-*.md` series for the ledger shape
and migration history.

- Minimum top-up: `$20` (`MIN_TOPUP_USD` in `shared/wallet.py`). Smaller
  amounts are rejected at the form and the server.
- Stripe is the payment provider; live keys are read from environment.
- Backing tables live in the shared Supabase project: `wallets`,
  `wallet_transactions`, `tool_jobs` (whose `inputs._wallet.hold_tx_id`
  pivots back to the wallet ledger).
- Credits are fully retired. There is no longer a credits balance or a
  per-tool credit price; every job debits in USD.

Env vars for wallet/billing live in `docs/WALLET-ENV-VARS.md`.

## Auth

Shared Supabase project with Epitope Scout. One account signs users into
every tool linked from the hub. See `shared/auth.py` for the helpers and
`shared/supabase_client.py` for the client factory.

## Local development

```powershell
# From the tools-hub/ directory, Windows PowerShell.
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

# Create a .env next to app.py with the values from .env.example. The
# app reads SUPABASE_URL, SUPABASE_KEY, and SESSION_SECRET_KEY. Without
# Supabase configured, the login route returns "Authentication service
# is not configured."

# Load env vars into the shell, then run Flask:
venv\Scripts\python.exe app.py
```

Open <http://127.0.0.1:5000/>. The landing page, `/tools`, every
`/tools/<slug>` form, `/scout/`, `/developability`, and `/help` all render
for anonymous visitors — only submitting a job (and anything under
`/jobs` or `/account`, which is where the wallet lives) redirects to
`/login`.

### One-line dev command

```powershell
venv\Scripts\python.exe app.py
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPABASE_URL` | yes | Supabase project URL (shared with Scout) |
| `SUPABASE_KEY` | yes | Supabase publishable/anon key (`SUPABASE_ANON_KEY` is also accepted for compatibility) |
| `SESSION_SECRET_KEY` | yes | Flask session signing secret (any long random string) |
| `PORT` | no | Port for local dev; defaults to 5000. Platform-provided in production. |

Wallet/Stripe/Modal variables are documented in `docs/WALLET-ENV-VARS.md`
and `.env.example`.

## Deployment

Designed for Railway via Nixpacks. The build spec is in
`nixpacks.toml`; the start command is in `Procfile` (also duplicated in
`nixpacks.toml`).

Health check: `/health` returns `{"status": "ok"}` unauthenticated, so
the port scanner can verify the app without credentials.

Python version pinned in `runtime.txt` (currently 3.13.0) matches Epitope
Scout for consistency.

Modal apps are deployed by the sibling `llm-proteinDesigner` repo's CI,
not by this repo.

## Adding a tool

1. Create `tools/<name>/` with an `__init__.py` that exports a stable
   public API (e.g. a `score(...)` or `analyze(...)` function).
2. Import lazily inside the Flask route so a broken tool does not take
   down the hub. Pattern:

   ```python
   @flask_app.route("/mytool", methods=["POST"])
   @login_required
   def mytool():
       from tools.mytool import score  # noqa: PLC0415
       return jsonify(score(request.json))
   ```

3. Give it a workflow band in `shared/tools_catalog.py::_TOOL_CATEGORIES`
   and a glyph in `shared/category_glyphs.py`. A slug with no band falls
   into the `"Other"` bucket, which is how Proteina and OpenDDE once
   disappeared from the homepage. There is no hand-maintained list in
   `index()` any more — the catalog, `/tools`, and the `/help` guide grid
   are all derived from the registry.

If the tool runs on Modal, define the Modal app in the sibling
`llm-proteinDesigner` repo and call it through `shared/modal_client.py`
using the `contracts/` payload models.

## Project layout

```
tools-hub/
  app.py                 Flask application
  contracts/             Vendored RPC contract (shared with llm-proteinDesigner)
  billing/               Stripe checkout + webhooks
  shared/                Auth, wallet, jobs, storage, Modal client
  tools/                 Per-tool packages
  templates/             Jinja2 templates
  static/                Logo + CSS
  docs/                  Architecture, handoffs, validation log, ORCH-LOG
  .github/workflows/     CI (contracts drift guard, etc.)
  requirements.txt       Pinned deps
  Procfile               Gunicorn start command
  gunicorn.conf.py       Gunicorn config (preload, logging)
  nixpacks.toml          Railway build config
  runtime.txt            Python version pin
  .env.example           Documented env vars
  .gitignore
```
