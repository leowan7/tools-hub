"""Every GPU adapter must carry a real workflow-stage category.

Proteina-Complexa and OpenDDE both shipped without an entry in
``_TOOL_CATEGORIES``, so they silently fell into the homepage's "Other"
bucket. This guards the next adapter from doing the same, and locks the
two things that break silently alongside a band rename: the glyph map is
keyed by the band label, and the homepage catalog cards used to route
anonymous visitors into ``/login?next=``.
"""

# ``tools.base._REGISTRY`` is populated only by the ``import tools.<slug>``
# side effects in app.py, so importing app is what makes the adapters
# visible here. Without it this test iterates an empty registry and
# passes vacuously -- green while the bug ships. The explicit count
# assertion below keeps that failure mode from coming back silently.
import app  # noqa: F401
import pytest
from tools import base as tool_base

from shared.category_glyphs import category_glyph_slug, inline_category_glyph
from shared.tools_catalog import (
    _HARDCODED_TOOLS,
    _TOOL_CATEGORIES,
    CATEGORY_ORDER,
    _build_tools_catalog,
    group_catalog,
)


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    # Tool flags fail closed, so an unflagged environment would build a
    # catalog of the two hardcoded tools only and every assertion below
    # would pass over nothing.
    for slug in _TOOL_CATEGORIES:
        monkeypatch.setenv(
            "FLAG_TOOL_" + slug.upper().replace("-", "_"), "on"
        )
    from app import create_app
    application = create_app()
    application.config["TESTING"] = True
    return application


def test_every_adapter_has_a_category():
    adapters = tool_base.all_adapters()
    assert adapters, (
        "no tool adapters registered -- this test cannot prove anything; "
        "app must be imported so tools.base._REGISTRY is populated"
    )
    missing = sorted(a.slug for a in adapters if a.slug not in _TOOL_CATEGORIES)
    assert not missing, (
        f"adapters with no _TOOL_CATEGORIES entry (they render under "
        f'"Other" on the homepage): {missing}'
    )


def test_no_tool_lands_in_other(flask_app):
    """The "Other" fallback must stay empty in a fully flagged build."""
    with flask_app.test_request_context("/"):
        catalog = _build_tools_catalog()

    assert len(catalog) == len(_HARDCODED_TOOLS) + len(_TOOL_CATEGORIES), (
        "catalog size does not match hardcoded + categorised adapters -- "
        "a flag is off or an adapter is unregistered, so the assertions "
        "below would not cover the whole catalog"
    )
    orphans = sorted(
        t["slug"] for t in catalog if t.get("category") not in CATEGORY_ORDER[:-1]
    )
    assert not orphans, f'tools falling through to "Other": {orphans}'


def test_every_rendered_band_has_a_glyph(flask_app):
    with flask_app.test_request_context("/"):
        grouped = group_catalog(_build_tools_catalog())

    assert len(grouped) == len(CATEGORY_ORDER) - 1, (
        f"expected all five bands to render, got "
        f"{[band for band, _ in grouped]}"
    )
    for band, _members in grouped:
        assert category_glyph_slug(band), (
            f'band "{band}" resolves no glyph slug -- _CATEGORY_GLYPHS in '
            f"shared/category_glyphs.py is keyed by the band label and was "
            f"not renamed alongside _TOOL_CATEGORIES"
        )
        assert str(inline_category_glyph(band)).lstrip().startswith("<svg"), (
            f'band "{band}" has a glyph slug but no SVG file behind it'
        )


def test_anonymous_homepage_has_no_login_wall_and_lists_every_tool(flask_app):
    """An anonymous visitor gets real tool links, not /login?next=."""
    client = flask_app.test_client()
    with flask_app.test_request_context("/"):
        catalog = _build_tools_catalog()

    body = client.get("/").get_data(as_text=True)

    assert "auth.login" not in body
    assert "/login?next=" not in body and "/login%3Fnext" not in body
    assert "See how it works" in body
    assert "Sign in to run" not in body

    for band in CATEGORY_ORDER[:-1]:
        assert band in body, f'band "{band}" missing from the homepage'
    for tool in catalog:
        assert f'href="{tool["route"]}"' in body, (
            f'{tool["slug"]} has no card link on the anonymous homepage'
        )

    # Phase 4b chooser, above the catalog and JS-free.
    assert 'id="start-here"' in body
    assert body.index('id="start-here"') < body.index('id="tools"')
    assert "What do you have, and what do you want?" in body
    for row_label in (
        "A target protein structure",
        "A target and a rough epitope",
        "A backbone from somewhere else",
        "A designed binder sequence",
        "An antibody or nanobody sequence",
    ):
        assert row_label in body
