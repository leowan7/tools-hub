"""Load ``tools/<slug>/meta.py`` by ADAPTER SLUG, not by package name.

Package directories use underscores; two adapter slugs do not
(``esmfold2-design``, and any future hyphenated tool). Four call sites
built the module path by interpolating the slug raw, got an
ImportError for those, swallowed it, and rendered the tool's page with
NO metadata at all — no FAQ, no positioning line, no references, no
runtime band — which looks exactly like a tool that simply has none.
Found because a PILOT card silently did not render on esmfold2-design.

Lives in ``shared/`` rather than ``tools/base.py`` on purpose. It is
web-tier plumbing that never reaches a GPU container, and anything
under ``tools/`` outside the ``meta.py`` / ``example/`` negations in
.github/workflows/deploy-modal.yml redeploys all nine Modal images on
merge.
"""

from __future__ import annotations

import importlib
from types import ModuleType


def meta_for(slug: str) -> ModuleType | None:
    """Return ``tools.<slug>.meta`` or None if the tool ships none."""
    try:
        return importlib.import_module(f"tools.{slug.replace('-', '_')}.meta")
    except ImportError:
        return None


def _runtime_entry(meta, preset_slug: str) -> tuple[str, object] | None:
    """(display text, ``minutes``) for one preset, or None.

    Two sources because two generations of metadata are live:
    ``PRESET_RUNTIME[slug]["typical_minutes"]`` (a bare number or range,
    so the unit is appended here) and the older ``preset_runtime_rows``
    (already carries "min"), which rfdiffusion and pxdesign still use.
    Either may carry ``minutes``: a ``(low, high)`` pair of numbers.
    """
    if meta is None:
        return None
    entry = (getattr(meta, "PRESET_RUNTIME", None) or {}).get(preset_slug) or {}
    if entry.get("typical_minutes"):
        return f"{entry['typical_minutes']} min", entry.get("minutes")
    for row in getattr(meta, "preset_runtime_rows", None) or ():
        if row.get("slug") == preset_slug and row.get("runtime"):
            return row["runtime"], row.get("minutes")
    return None


def preset_runtime_text(meta, preset_slug: str) -> str | None:
    """The typical runtime text for ONE preset, or None."""
    entry = _runtime_entry(meta, preset_slug)
    return entry[0] if entry else None


def _minutes_span(value) -> tuple[float, float] | None:
    """``value`` as a (low, high) pair of positive numbers, else None."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    low, high = value
    for x in (low, high):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return None
    if not 0 < low <= high:
        return None
    return float(low), float(high)


def runtime_band(meta, preset_slugs) -> str:
    """One runtime band for a tool, formatted from each preset's ``minutes``.

    The band runs from the lowest ``low`` to the highest ``high``, with a
    leading "~" when any contributing preset's text does. A preset with
    no usable ``minutes`` (or no runtime entry at all) is left out and the
    band says "(some presets vary)". When no preset has usable ``minutes``,
    the band is the first preset's own text, never a join of several.
    """
    preset_slugs = list(preset_slugs)
    entries = [e for e in (_runtime_entry(meta, s) for s in preset_slugs) if e]
    if not entries:
        return "—"
    numeric = [(text, span) for text, m in entries if (span := _minutes_span(m))]
    if not numeric:
        return entries[0][0]
    low = min(span[0] for _, span in numeric)
    high = max(span[1] for _, span in numeric)
    band = f"{low:g} min" if low == high else f"{low:g} to {high:g} min"
    if any(text.lstrip().startswith("~") for text, _ in numeric):
        band = "~" + band
    if len(numeric) < len(preset_slugs):
        band += " (some presets vary)"
    return band
