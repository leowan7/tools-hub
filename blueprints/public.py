"""Public marketing / content / probe routes (blueprint refactor, Commit 2).

GET-only marketing, legal, help, showcase, sitemap, and health / readiness
probes, plus the anonymous analytics beacon. Lifted verbatim from
``create_app()``; only ``@flask_app.route`` -> ``@public_bp.route`` and the
factory-local ``flask_app`` references rewired to ``current_app``. Endpoint
names are unchanged apart from the ``public.`` blueprint prefix.
"""

from __future__ import annotations

import logging
import os

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from shared.credits import load_user_context
from shared.feature_flags import tool_enabled
from shared.jobs import list_jobs_for_user
from shared.tools_catalog import _build_tools_catalog, _short_name_for_label
from tools import base as tool_base

logger = logging.getLogger(__name__)

public_bp = Blueprint("public", __name__)


@public_bp.route("/health", methods=["GET"])
def health():
    """Unauthenticated health check for Railway port scanner."""
    return jsonify({"status": "ok"}), 200

@public_bp.route("/readyz", methods=["GET"])
def readyz():
    """Deep readiness probe (catches incident 2026-06-10 Mode B).

    /health is static and DB-free, so it stays green even when the
    Supabase client fails to build and the entire authenticated surface
    (login, wallet, credits, Platform API) is down. /readyz does ONE
    cheap, bounded Supabase read so an external uptime monitor catches
    that mode directly. Bounded by SUPABASE_CLIENT_TIMEOUT_S (30s) and
    >1 gunicorn worker, so the probe itself can never wedge the site.

    Uses the service-role client because user_events is service-role-only
    under RLS. A None client (construction failed) or any read error
    returns 503 so the monitor's keyword check ("ready") and status code
    both fail. Unauthenticated by design: an external prober cannot log
    in, and it is placed above the login_required routes for that reason.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    try:
        client = get_service_client()
        if client is None:
            return (
                jsonify({"status": "degraded", "reason": "no_client"}),
                503,
            )
        client.table("user_events").select("id").limit(1).execute()
        return jsonify({"status": "ready"}), 200
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        logger.warning("readyz degraded: %s", exc)
        return jsonify({"status": "degraded", "reason": "db_error"}), 503


@public_bp.route("/", methods=["GET"])
def index():
    """Landing page.

    For anonymous visitors: marketing hero + tool catalog tiles
    with sign-in CTAs, so first-time visitors can see what runs on
    the platform without signing up.

    For authenticated users: a "Recent runs" dashboard strip on top
    (top 3 jobs with clone shortcuts), then the tool catalog tiles
    for new runs.
    """
    catalog = _build_tools_catalog()

    # Match the grouped layout used by /tools — same categories,
    # same order, just rendered as wide tile sections instead of a
    # comparison matrix. Ordering walks the iteration loop:
    # scope → design (4 scaffold-class buckets) → predict → QC.
    category_order = (
        "Scope the target",
        "De novo minibinders",
        "Antibodies (VHH)",
        "Dual capabilities (minibinder + antibody scaffolds)",
        "Sequence on a backbone",
        "Structure prediction",
        "Check developability",
        "Other",
    )
    grouped: list[tuple[str, list[dict]]] = []
    for category in category_order:
        members = [t for t in catalog if t.get("category") == category]
        if members:
            grouped.append((category, members))

    recent_jobs: list = []
    if session.get("user_email"):
        ctx = load_user_context()
        if ctx is not None:
            try:
                recent_jobs = list(
                    list_jobs_for_user(ctx.user_id, limit=3)
                )
            except Exception:  # noqa: BLE001 — never block the homepage
                logger.exception("Failed to load recent jobs for homepage")
                recent_jobs = []

    return render_template(
        "index.html",
        tools=catalog,
        grouped=grouped,
        recent_jobs=recent_jobs,
        authenticated=bool(session.get("user_email")),
    )

@public_bp.route("/pricing", methods=["GET"])
def pricing():
    """Public pricing page — logged-out visitors can reach it."""
    return render_template("pricing.html")

@public_bp.route("/terms", methods=["GET"])
def terms():
    return render_template("legal/terms.html")

@public_bp.route("/privacy", methods=["GET"])
def privacy():
    return render_template("legal/privacy.html")

@public_bp.route("/robots.txt", methods=["GET"])
def robots_txt():
    """Serve the static robots.txt from /static/ at the URL root.

    Search engines fetch /robots.txt, not /static/robots.txt, so we
    need an explicit route that maps one to the other.
    """
    return send_from_directory(
        current_app.static_folder, "robots.txt", mimetype="text/plain"
    )


@public_bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Emit a sitemap listing every public, crawlable URL.

    Sources of truth:
      * Static URLs are enumerated below in ``_static_paths``.
      * Per-tool help pages are pulled from ``tool_base.all_adapters()``
        so newly enabled tools appear automatically.
    Tool run forms (``/tools/<slug>``) are NOT listed because they
    currently require login and serve a redirect to crawlers.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    base = request.url_root.rstrip("/")
    today = datetime.now(timezone.utc).date().isoformat()

    _static_paths = [
        "/",
        "/tools",
        "/pricing",
        "/help",
        "/help/getting-started",
        "/help/faq",
        "/help/troubleshooting",
        "/scout",
        "/showcase",
        "/terms",
        "/privacy",
    ]

    urls: list[tuple[str, str, str]] = []
    # (loc, changefreq, priority)
    for path in _static_paths:
        priority = "1.0" if path == "/" else "0.7"
        urls.append((f"{base}{path}", "weekly", priority))

    # Per-tool help guides + public preview pages (B2). The preview
    # page at /tools/<slug> serves logged-out crawlers a real HTML
    # response; the run form (same URL, logged-in) is not crawled.
    try:
        for adapter in tool_base.all_adapters():
            if not tool_enabled(adapter.slug):
                continue
            urls.append(
                (f"{base}/help/tools/{adapter.slug}", "monthly", "0.6")
            )
            urls.append(
                (f"{base}/tools/{adapter.slug}", "weekly", "0.7")
            )
    except Exception:
        logger.warning("sitemap: failed to enumerate tool adapters", exc_info=True)

    # Render manually — Flask's jsonify + render_template are overkill
    # for a fixed XML shape and the templating cost isn't worth it.
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, freq, priority in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        lines.append(f"    <lastmod>{today}</lastmod>")
        lines.append(f"    <changefreq>{freq}</changefreq>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")

    return Response("\n".join(lines), mimetype="application/xml")


@public_bp.route("/api/track", methods=["POST"])
def api_track():
    """Append a behavioural event to ``public.user_events``.

    Body: JSON ``{event_type, path?, props?, session_id?}``.
    Returns 204 always (best-effort).
    """
    from shared.events import log_event  # noqa: PLC0415

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}
    event_type = str(payload.get("event_type") or "").strip()[:64]
    if not event_type:
        return ("", 204)
    path = payload.get("path")
    props_raw = payload.get("props") or {}
    props = props_raw if isinstance(props_raw, dict) else {}
    session_id = (payload.get("session_id") or "").strip() or None
    if session_id:
        session["anon_session_id"] = session_id[:64]
    elif session.get("anon_session_id"):
        session_id = session["anon_session_id"]

    log_event(
        event_type=event_type,
        user_id=session.get("user_id"),
        session_id=session_id,
        path=path if isinstance(path, str) else None,
        props=props,
        ip=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr),
        user_agent=request.headers.get("User-Agent"),
    )
    return ("", 204)


# ------------------------------------------------------------------
# B7 — showcase loader. Reads content/showcase/*.md, parses a simple
# ``---``-delimited frontmatter block, and returns a list of
# ``{meta, body, slug, tool_url, guide_url}`` dicts for the template.
# ------------------------------------------------------------------

_SHOWCASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content", "showcase"
)

def _parse_showcase_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a minimal YAML-ish frontmatter block from ``text``.

    Accepts ``key: value`` lines between two ``---`` separators.
    ``true``/``false`` (case-insensitive) coerce to bool; bare
    numbers coerce to int or float. Everything else stays a string.
    Returns ``(meta, body)``. If the frontmatter block is missing,
    ``meta`` is empty and the whole input is the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value.lower() in ("true", "false"):
                meta[key] = value.lower() == "true"
            else:
                try:
                    if "." in value:
                        meta[key] = float(value)
                    else:
                        meta[key] = int(value)
                except ValueError:
                    meta[key] = value
        i += 1
    body = "\n".join(lines[i + 1:]).strip("\n")
    return meta, body

def _showcase_body_blocks(body: str) -> list[dict]:
    """Split a showcase body into render blocks for the template.

    Returns ``{"type": ...}`` dicts: ``subhead`` (a lead-in line ending
    in a colon), ``list`` (with a ``bullets`` list), or ``para`` (joined
    text). Wrapped source lines are joined so the template renders real
    paragraphs and bullet lists instead of a raw ``<pre>`` dump.
    """
    blocks: list[dict] = []
    for chunk in (body or "").strip().split("\n\n"):
        stripped = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
        if not stripped:
            continue
        if any(ln.startswith("* ") for ln in stripped):
            items: list[str] = []
            for ln in stripped:
                if ln.startswith("* "):
                    items.append(ln[2:].strip())
                elif items:
                    items[-1] += " " + ln
                else:
                    items.append(ln)
            blocks.append({"type": "list", "bullets": items})
        elif len(stripped) == 1 and stripped[0].endswith(":"):
            blocks.append({"type": "subhead", "text": stripped[0]})
        else:
            blocks.append({"type": "para", "text": " ".join(stripped)})
    return blocks

def _load_showcase_entries() -> list[dict]:
    """Read every .md file under content/showcase/, sorted by filename.

    Filename order is the curated display order (entries are named
    ``01-...``, ``02-...`` etc). Each entry's ``tool`` frontmatter
    is matched to a registered tool adapter so the entry can link
    into the matching /tools/<slug> preview page from B2. The
    hardcoded Epitope Scout slug ``scout`` resolves to the Scout
    index route instead.
    """
    if not os.path.isdir(_SHOWCASE_DIR):
        return []
    entries: list[dict] = []
    for filename in sorted(os.listdir(_SHOWCASE_DIR)):
        if not filename.endswith(".md"):
            continue
        full = os.path.join(_SHOWCASE_DIR, filename)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            logger.warning("showcase: failed to read %s", filename, exc_info=True)
            continue
        meta, body = _parse_showcase_frontmatter(raw)
        meta.setdefault("internal_benchmark", True)
        stats: list[dict] = []
        for item in str(meta.get("stats") or "").split("|"):
            item = item.strip()
            if not item:
                continue
            value, _, label = item.partition("=")
            stats.append({"value": value.strip(), "label": label.strip()})
        glyph = str(meta.get("glyph") or "").strip()
        slug = filename[:-3]
        render_rel = "img/showcase/" + slug + ".png"
        render_url = None
        if os.path.exists(os.path.join(current_app.static_folder or "", render_rel)):
            try:
                render_url = url_for("static", filename=render_rel)
            except Exception:
                render_url = "/static/" + render_rel
        tool_slug = (meta.get("tool") or "").strip()
        tool_url: str | None = None
        guide_url: str | None = None
        if tool_slug == "scout":
            try:
                tool_url = url_for("scout.index")
            except Exception:
                tool_url = "/scout"
        elif tool_slug:
            adapter = tool_base.get(tool_slug)
            if adapter is not None and tool_enabled(tool_slug):
                try:
                    tool_url = url_for("tool_form", tool=tool_slug)
                except Exception:
                    tool_url = f"/tools/{tool_slug}"
                try:
                    guide_url = url_for(
                        "public.help_tool_guide", tool=tool_slug
                    )
                except Exception:
                    guide_url = f"/help/tools/{tool_slug}"
        entries.append({
            "meta": meta,
            "body": body,
            "blocks": _showcase_body_blocks(body),
            "stats": stats,
            "glyph": glyph,
            "render_url": render_url,
            "slug": slug,
            "tool_url": tool_url,
            "guide_url": guide_url,
        })
    return entries

# ------------------------------------------------------------------
# B7 — public /showcase: curated anonymized runs with deep links into
# the matching /tools/<slug> preview pages from B2. Indexable.
# ------------------------------------------------------------------

@public_bp.route("/showcase", methods=["GET"])
def showcase():
    """Render the curated showcase index.

    Loads every ``.md`` file under ``content/showcase/``, parses a
    simple YAML-ish frontmatter block, and renders the body as
    plaintext inside the template's ``<pre>`` block. Per-entry
    Dataset JSON-LD is emitted from the template so each entry is
    indexable as its own dataset.

    Frontmatter shape:
        ---
        title: str
        tool: <slug matching tools.<slug>>
        target_kind: str
        top_score: number
        date: YYYY-MM-DD
        internal_benchmark: bool (default True)
        glyph: <category label for inline_category_glyph, optional>
        stats: value=label | value=label | ...  (optional stat chips)
        ---
    """
    entries = _load_showcase_entries()
    breadcrumbs = [
        {"name": "Home", "url": url_for("public.index", _external=True)},
        {"name": "Showcase", "url": url_for("public.showcase", _external=True)},
    ]
    return render_template(
        "showcase.html", entries=entries, breadcrumbs=breadcrumbs
    )

# ------------------------------------------------------------------
# Help / docs hub — public (no login required).
# ------------------------------------------------------------------

@public_bp.route("/help", methods=["GET"])
def help_index():
    """Docs hub: getting started, per-tool guides, FAQ, troubleshooting."""
    breadcrumbs = [
        {"name": "Home", "url": url_for("public.index", _external=True)},
        {"name": "Help", "url": url_for("public.help_index", _external=True)},
    ]
    return render_template(
        "help/index.html",
        adapters=tool_base.all_adapters(),
        breadcrumbs=breadcrumbs,
    )

@public_bp.route("/help/getting-started", methods=["GET"])
def help_getting_started():
    return render_template("help/getting_started.html")

@public_bp.route("/help/tools/<tool>", methods=["GET"])
def help_tool_guide(tool: str):
    adapter = tool_base.get(tool)
    if adapter is None:
        return render_template("404.html"), 404
    import importlib  # noqa: PLC0415
    try:
        meta = importlib.import_module(f"tools.{tool}.meta")
    except ImportError:
        meta = None
    short_name = _short_name_for_label(adapter.label)
    breadcrumbs = [
        {"name": "Home", "url": url_for("public.index", _external=True)},
        {"name": "Help", "url": url_for("public.help_index", _external=True)},
        {"name": "Tools", "url": url_for(
            "tools_comparison", _external=True
        )},
        {"name": short_name, "url": url_for(
            "public.help_tool_guide", tool=tool, _external=True
        )},
    ]
    return render_template(
        "help/tool_guide.html",
        tool=tool,
        adapter=adapter,
        meta=meta,
        short_name=short_name,
        breadcrumbs=breadcrumbs,
    )

@public_bp.route("/help/faq", methods=["GET"])
def help_faq():
    return render_template("help/faq.html")

@public_bp.route("/help/troubleshooting", methods=["GET"])
def help_troubleshooting():
    return render_template("help/troubleshooting.html")
