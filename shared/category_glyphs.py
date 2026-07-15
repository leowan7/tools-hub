"""Map workflow-stage category labels to inline SVG glyph filenames.

The homepage tile grid and ``/tools`` discovery page group tools by
workflow-stage category. The glyph helper renders a small SVG next to
each category section title so a scientist scanning the catalog can
identify the stage at a glance without reading the label.

Scope and Developability share one glyph because they bookend the same
workflow stage. "Other" returns no glyph (template falls back to a
text-only header).

Files live at ``static/img/categories/<slug>.svg``. The Flask layer
returns just the slug; the template composes the full ``url_for``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

# Category display label -> glyph slug (no extension, no path).
_CATEGORY_GLYPHS: dict[str, str] = {
    "Scope the target": "target-scoping",
    "Check developability": "target-scoping",
    "Design binders": "de-novo-minibinders",
    "Sequence on a backbone": "sequence-on-backbone",
    "Structure prediction": "structure-prediction",
}


def category_glyph_slug(category: str | None) -> str | None:
    """Return the glyph slug for a workflow-stage category, or ``None``.

    Templates call this via the ``category_glyph`` Jinja global and
    pass the returned slug into ``url_for('static', filename=...)``.
    ``None`` means no glyph is available for this category (e.g. the
    "Other" catchall bucket); the template should fall back to a
    text-only header.
    """
    if not category:
        return None
    return _CATEGORY_GLYPHS.get(category)


_GLYPH_DIR = Path(__file__).resolve().parent.parent / "static" / "img" / "categories"


@lru_cache(maxsize=16)
def _read_glyph(slug: str) -> str:
    """Return SVG markup for ``<slug>.svg``; empty string if missing."""
    path = _GLYPH_DIR / f"{slug}.svg"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def inline_category_glyph(category: str | None) -> Markup:
    """Inline-render the SVG glyph for a category, or return empty.

    Returned as ``Markup`` so Jinja's autoescape leaves the SVG intact.
    Inlining (vs ``<img src=>``) keeps ``stroke="currentColor"`` honoring
    the surrounding text color, so the glyph adapts to themed surfaces
    without per-template overrides. Missing slugs and unknown
    categories both return empty markup so the template caller can
    safely emit it inside a span without conditional rendering.
    """
    slug = category_glyph_slug(category)
    if not slug:
        return Markup("")
    markup = _read_glyph(slug)
    return Markup(markup) if markup else Markup("")
