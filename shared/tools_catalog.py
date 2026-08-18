"""Unified tool catalog for the homepage tile grid and the /tools page.

Extracted verbatim from ``app.py`` (blueprint refactor, Commit 0). Consumed by
``index`` (homepage) and ``tools_comparison`` (``/tools``), which land in
different blueprints, so the catalog lives in a shared leaf both can import.
"""

from flask import url_for

from shared.feature_flags import tool_enabled
from shared.tool_meta import meta_for
from tools import base as tool_base

# Static taglines for the hardcoded (non-adapter) tools. These two tools
# are not part of the GPU tool_base registry, so they are added to the
# catalog directly.
#
# NOTE (blueprint refactor): the "developability" endpoint value becomes
# "tools.developability" when that standalone route moves into the tools
# blueprint (Commit 7). "scout.index" is already blueprint-qualified.
#
# The Yeast Display Library Planner was delisted 2026-08-17 at Leo's
# request: dropping its entry here removes the tile from both the
# homepage and /tools. Its route (tools.library_planner), templates, and
# tools/library_planner package are deliberately left in place so
# existing job links and job history keep resolving instead of 404ing.
_HARDCODED_TOOLS: tuple[dict, ...] = (
    {
        "slug": "epitope-scout",
        "name": "Epitope Scout",
        "tagline": (
            "Score a target's surface for binder-design feasibility "
            "before committing GPU time."
        ),
        "comparison_one_liner": (
            "Pick Epitope Scout first to identify candidate epitopes "
            "and per-dimension feasibility for any target."
        ),
        "category": "Check if my target is a good one to bind",
        "smoke_runtime": "~30 s",
        "pilot_runtime": "—",
        "runtime_band": "~30 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "scout.index",
        "external": False,
        "status": "live",
    },
    {
        "slug": "developability",
        "name": "Binder Developability Scout",
        "tagline": (
            "Flag developability liabilities in antibody and nanobody "
            "sequences before you order them."
        ),
        "comparison_one_liner": (
            "Pick Developability Scout when you have a sequence and "
            "need a quick liability scan (CDR length, hydrophobic "
            "patches, charge balance, isoelectric point)."
        ),
        "category": "See if a binder will hold up in the lab",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "runtime_band": "<5 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "tools.developability",
        "external": False,
        "status": "live",
    },
)


# Maps each GPU tool slug to a workflow-stage category. All binder and
# antibody scaffold designers share one design bucket so the catalog
# reads as scope -> design -> sequence -> predict -> QC. (An earlier
# revision split the designers into per-scaffold subsections; that was
# reverted back to the single bucket.)
#
# The band labels are written as the task a bench biologist has, not as
# the pipeline stage a computational scientist would name. They are the
# literal section headings on the homepage and /tools, and they are also
# the keys of ``shared.category_glyphs._CATEGORY_GLYPHS`` -- renaming one
# without the other silently drops the glyph, so tests/test_tool_categories.py
# asserts every band still resolves one.
_TOOL_CATEGORIES: dict[str, str] = {
    "rfdiffusion": "Make new binders for my target",
    "bindcraft": "Make new binders for my target",
    "pxdesign": "Make new binders for my target",
    "rfantibody": "Make new binders for my target",
    "esmfold2-design": "Make new binders for my target",
    "boltzgen": "Make new binders for my target",
    "iggm": "Make new binders for my target",
    "proteina": "Make new binders for my target",
    "mpnn": "Choose sequences for a structure I already have",
    "af2": "Predict or check a 3D structure",
    "colabfold": "Predict or check a 3D structure",
    "esmfold": "Predict or check a 3D structure",
    "boltz2": "Predict or check a 3D structure",
    "opendde": "Predict or check a 3D structure",
}


# Display order for the catalog bands, shared by the homepage and
# /tools so the two can never drift apart. "Other" is the
# ``_build_tools_catalog`` fallback for a slug with no category; it stays
# last so a miscategorised tool is still visible rather than dropped.
CATEGORY_ORDER: tuple[str, ...] = (
    "Check if my target is a good one to bind",
    "Make new binders for my target",
    "Choose sequences for a structure I already have",
    "Predict or check a 3D structure",
    "See if a binder will hold up in the lab",
    "Other",
)


def group_catalog(catalog: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group a catalog into ordered ``(band, tools)`` pairs.

    Bands with no members are dropped, so an all-flags-off deployment
    renders nothing rather than a run of empty headings.
    """
    grouped: list[tuple[str, list[dict]]] = []
    for category in CATEGORY_ORDER:
        members = [t for t in catalog if t.get("category") == category]
        if members:
            grouped.append((category, members))
    return grouped


def _build_tools_catalog() -> list[dict]:
    """Return the unified tool catalog used by the homepage tile grid
    and the ``/tools`` discovery page.

    Each entry includes display name, tagline, category, route, and
    runtime bands so a single template can render the tile layout, the
    comparison matrix, and the homepage cards from one source of truth.
    Hardcoded tools (Epitope Scout, Developability) are included
    regardless of feature flags; GPU adapters are filtered through
    ``tool_enabled`` so a flag-off tool stays invisible to the catalog.
    """
    catalog: list[dict] = []

    # Hardcoded tools (not part of tool_base registry). External Scout
    # link is resolved at request time so the URL adapts to the current
    # host (covers local dev vs production).
    for entry in _HARDCODED_TOOLS:
        item = dict(entry)
        endpoint = item.pop("endpoint", None)
        try:
            item["route"] = url_for(endpoint) if endpoint else "#"
        except Exception:  # noqa: BLE001 — outside request context
            item["route"] = "#"
        catalog.append(item)

    # GPU adapters (flag-gated).
    for adapter in tool_base.all_adapters():
        if not tool_enabled(adapter.slug):
            continue

        # meta_for(), not a raw-slug import path: package dirs use
        # underscores and ``esmfold2-design`` does not, so interpolating
        # the slug here raised ImportError and silently gave that tool no
        # runtime band on the homepage catalog. Fifth and last call site.
        meta = meta_for(adapter.slug)

        # Build the runtime band from whatever presets the adapter exposes
        # (smoke + mini_pilot tiers were removed 2026-05-29; atomic tools
        # now have a single standalone preset, composites have pilot
        # only). Show the fastest preset's runtime through the slowest
        # as a band.
        runtime_band = "—"
        if meta is not None:
            runtime_map = getattr(meta, "PRESET_RUNTIME", None) or {}
            legacy_rows = getattr(meta, "preset_runtime_rows", None) or ()
            legacy_by_slug = {
                r.get("slug"): r.get("runtime")
                for r in legacy_rows
                if r.get("slug") and r.get("runtime")
            }
            runtimes: list[str] = []
            for preset in adapter.presets:
                entry = runtime_map.get(preset.slug) or {}
                if entry.get("typical_minutes"):
                    rt = f"{entry['typical_minutes']} min"
                else:
                    rt = legacy_by_slug.get(preset.slug)
                if rt and rt not in runtimes:
                    runtimes.append(rt)
            if len(runtimes) >= 2:
                runtime_band = f"{runtimes[0]} to {runtimes[-1]}"
            elif len(runtimes) == 1:
                runtime_band = runtimes[0]

        display_name = adapter.label.split("—")[0].strip() or adapter.label
        try:
            route = url_for("tools.tool_form", tool=adapter.slug)
        except Exception:  # noqa: BLE001
            route = f"/tools/{adapter.slug}"

        catalog.append(
            {
                "slug": adapter.slug,
                "name": display_name,
                "tagline": adapter.blurb,
                "comparison_one_liner": getattr(
                    meta, "comparison_one_liner", "—"
                ) if meta is not None else "—",
                "category": _TOOL_CATEGORIES.get(adapter.slug, "Other"),
                "runtime_band": runtime_band,
                "paper_citation": getattr(
                    meta, "paper_citation", "—"
                ) if meta is not None else "—",
                "paper_url": getattr(
                    meta, "paper_url", ""
                ) if meta is not None else "",
                "github_url": getattr(
                    meta, "github_url", ""
                ) if meta is not None else "",
                "route": route,
                "external": False,
                "status": "live",
            }
        )

    return catalog


def _short_name_for_label(label: str) -> str:
    """Return the algorithm name only — strip any 'X — descriptor' tail.

    Accepts em-dash ('—'), double-dash ('--'), or bare label. Used
    in SEO titles and h1s so the page leads with the searchable
    algorithm name rather than the full marketing label.
    """
    for sep in (" — ", " -- ", " – "):
        if sep in label:
            return label.split(sep, 1)[0].strip()
    return label.strip()
