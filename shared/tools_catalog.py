"""Unified tool catalog for the homepage tile grid and the /tools page.

Extracted verbatim from ``app.py`` (blueprint refactor, Commit 0). Consumed by
``index`` (homepage) and ``tools_comparison`` (``/tools``), which land in
different blueprints, so the catalog lives in a shared leaf both can import.
"""

from flask import url_for

from shared.feature_flags import tool_enabled
from tools import base as tool_base

# Static taglines for the hardcoded (non-adapter) tools. These three tools
# are not part of the GPU tool_base registry, so they are added to the
# catalog directly.
#
# NOTE (blueprint refactor): the "developability" and "library_planner"
# endpoint values become "tools.developability" / "tools.library_planner"
# when those standalone routes move into the tools blueprint (Commit 7).
# "scout.index" is already blueprint-qualified.
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
        "category": "Scope the target",
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
        "category": "Check developability",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "runtime_band": "<5 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "developability",
        "external": False,
        "status": "live",
    },
    {
        "slug": "library-planner",
        "name": "Yeast Display Library Planner",
        "tagline": (
            "Plan yeast display libraries with realistic diversity and "
            "screen-size estimates for your scaffold and Kd target."
        ),
        "comparison_one_liner": (
            "Pick the Library Planner when you have a binder design "
            "shortlist and need to scope library size, diversification "
            "scheme, and screening throughput before ordering DNA."
        ),
        "category": "Scope the target",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "runtime_band": "<5 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "library_planner",
        "external": False,
        "status": "live",
    },
)


# Maps each GPU tool slug to a workflow-stage category. The buckets
# describe what each tool actually designs so a scientist scanning the
# catalog can find the right scaffold class at a glance. The earlier
# single "Design binders" bucket lumped six tools doing different jobs
# (de novo minibinders vs antibody scaffolds vs sequence-on-backbone)
# and forced readers to open each card to disambiguate.
_TOOL_CATEGORIES: dict[str, str] = {
    "rfdiffusion": "De novo minibinders",
    "bindcraft": "De novo minibinders",
    "pxdesign": "De novo minibinders",
    "rfantibody": "Antibodies (VHH)",
    "esmfold2-design": "Antibodies (scFv) + minibinders",
    "boltzgen": "Dual capabilities (minibinder + antibody scaffolds)",
    "mpnn": "Sequence on a backbone",
    "af2": "Structure prediction",
    "colabfold": "Structure prediction",
    "esmfold": "Structure prediction",
    "boltz2": "Structure prediction",
}


def _build_tools_catalog() -> list[dict]:
    """Return the unified tool catalog used by the homepage tile grid
    and the ``/tools`` discovery page.

    Each entry includes display name, tagline, category, route, and
    runtime bands so a single template can render the tile layout, the
    comparison matrix, and the homepage cards from one source of truth. Hardcoded tools (Epitope Scout, Developability,
    Library Planner) are included regardless of feature flags;
    GPU adapters are filtered through ``tool_enabled`` so a flag-off
    tool stays invisible to the catalog.
    """
    import importlib  # noqa: PLC0415

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

        meta = None
        try:
            meta = importlib.import_module(f"tools.{adapter.slug}.meta")
        except ImportError:
            pass

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
            route = url_for("tool_form", tool=adapter.slug)
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
