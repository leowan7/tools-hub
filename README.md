# Ranomics Tools Hub

Flask app that hosts Ranomics' free scientific tools as lead magnets under
`tools.ranomics.com`. The hub itself is lightweight: auth, a landing page,
and per-tool routes. Each tool lives in its own package under `tools/` and
exposes a small stable API that `app.py` imports lazily.

## Tools

| Tool | Status | Route |
|------|--------|-------|
| Epitope Scout | Live | external link to `https://scout.ranomics.com` |
| Binder Developability Scout | Coming soon | `/developability` |
| Yeast Display Library Planner | Coming soon | `/library-planner` |

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

Open <http://127.0.0.1:5000/> and you should be redirected to `/login`.

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

## Deployment

Designed for Render or Railway via Nixpacks. The build spec is in
`nixpacks.toml`; the start command is in `Procfile` (also duplicated in
`nixpacks.toml` because Render sometimes ignores Procfile).

Health check: `/health` returns `{"status": "ok"}` unauthenticated, so
the port scanner can verify the app without credentials.

Python version pinned in `runtime.txt` (currently 3.13.0) matches Epitope
Scout for consistency.

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

3. Add the tool to the `tools` list in the `index()` route so it shows
   up on the hub landing page.

## Project layout

```
tools-hub/
  app.py                 Flask application
  shared/                Auth + Supabase client
  tools/                 Per-tool packages
    developability/      Placeholder for Binder Developability Scout
    library_planner/     Placeholder for Yeast Display Library Planner
  templates/             Jinja2 templates (base, index, auth, account, coming_soon)
  static/                Logo + CSS
  requirements.txt       Pinned deps
  Procfile               Gunicorn start command
  gunicorn.conf.py       Gunicorn config (preload, logging)
  nixpacks.toml          Render/Railway build config
  runtime.txt            Python version pin
  .env.example           Documented env vars
  .gitignore
```
