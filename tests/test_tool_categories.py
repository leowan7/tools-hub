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
import re

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

# ``CATEGORY_ORDER`` ends with the "Other" catchall, which is a fallback
# bucket rather than a band anyone should land in. Name it instead of
# slicing it off with ``[:-1]`` -- a slice silently changes meaning if
# the tuple is ever reordered, and every assertion below is about "the
# real bands", not "all but the last entry".
OTHER_BAND = "Other"
REAL_BANDS = tuple(band for band in CATEGORY_ORDER if band != OTHER_BAND)

# Catalog card links that an anonymous visitor is currently still
# bounced off. Both are being opened up separately; when that lands
# these move out of the exception list and are asserted 200 like the
# rest. Listed explicitly so the hole is visible rather than silent.
KNOWN_LOGIN_WALLED_ROUTES = {"/scout/", "/developability"}

# Matches the href of a tool-catalog card on either page. The homepage
# uses ``home-catalog-card``, /tools uses ``catalog-card``; both put the
# class before the href on the anchor.
_CARD_HREF = re.compile(r'class="(?:home-)?catalog-card"[^>]*?href="([^"]+)"')


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


def test_every_adapter_resolves_its_meta_in_the_catalog(monkeypatch):
    """The catalog must not silently render a tool with no metadata.

    ``_build_tools_catalog`` built the meta module path by interpolating
    the adapter slug raw. Package directories use underscores and
    ``esmfold2-design`` does not, so that import raised ImportError, the
    ``except ImportError: pass`` swallowed it, and the tool rendered on
    the homepage with no runtime band, no positioning line and no
    citation — which looks exactly like a tool that simply has none.
    Four other call sites had the same bug and were moved to
    ``shared.tool_meta.meta_for``; this was the fifth.

    Asserting on the OUTPUT rather than on the import mechanism, so it
    keeps holding if the loading changes again.
    """
    from app import create_app
    from shared.feature_flags import flag_name
    from shared.tools_catalog import _build_tools_catalog

    slugs = {a.slug for a in tool_base.all_adapters()}
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    # monkeypatch, not bare os.environ: the sibling ``flask_app`` fixture
    # above already sets these through monkeypatch, and this test used to
    # set fourteen FLAG_TOOL_* vars and SESSION_SECRET_KEY with no restore,
    # leaking them into whatever ran next.
    for slug in slugs:
        monkeypatch.setenv(flag_name(slug), "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    flask_app = create_app()
    with flask_app.test_request_context("/"):
        catalog = _build_tools_catalog()

    listed = {e["slug"] for e in catalog}
    assert slugs <= listed, f"missing from the catalog: {sorted(slugs - listed)}"

    blank = sorted(
        e["slug"] for e in catalog
        if e["slug"] in slugs
        and "—" in (e["runtime_band"], e["comparison_one_liner"],
                    e["paper_citation"])
    )
    assert not blank, (
        f"catalog entries with no metadata resolved (hyphen-vs-underscore "
        f"slug, most likely): {blank}"
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
        t["slug"] for t in catalog if t.get("category") not in REAL_BANDS
    )
    assert not orphans, f'tools falling through to "Other": {orphans}'


def test_every_rendered_band_has_a_glyph(flask_app):
    with flask_app.test_request_context("/"):
        grouped = group_catalog(_build_tools_catalog())

    assert len(grouped) == len(REAL_BANDS), (
        f"expected all {len(REAL_BANDS)} real bands to render, got "
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

    for band in REAL_BANDS:
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


def test_anonymous_tools_page_has_no_login_wall(flask_app):
    """/tools is the destination of the hero CTA and the nav link.

    Its card block was a verbatim copy of the homepage's login-wall
    branch, so fixing the homepage alone left a first-time visitor's
    most likely click landing on a fully walled page.
    """
    client = flask_app.test_client()
    with flask_app.test_request_context("/tools"):
        catalog = _build_tools_catalog()

    body = client.get("/tools").get_data(as_text=True)

    assert "/login?next=" not in body and "/login%3Fnext" not in body
    assert "See how it works" in body
    assert "Sign in to run" not in body
    for tool in catalog:
        assert f'href="{tool["route"]}"' in body, (
            f'{tool["slug"]} has no card link on the anonymous /tools page'
        )


@pytest.mark.parametrize("page", ["/", "/tools"])
def test_anonymous_catalog_links_actually_resolve(flask_app, page):
    """Follow every catalog link, do not just read its href.

    Asserting the emitted href is what let a login-walled route survive
    review: the href pointed at the tool, and the tool then bounced the
    visitor to /login anyway. This requests each link as an anonymous
    visitor and asserts the status it really returns.
    """
    client = flask_app.test_client()
    body = client.get(page).get_data(as_text=True)
    hrefs = sorted(set(_CARD_HREF.findall(body)))

    assert len(hrefs) >= 14, (
        f"{page} emitted only {len(hrefs)} catalog card links -- the card "
        f"markup changed and this test is no longer following anything"
    )

    walled = []
    for href in hrefs:
        status = client.get(href).status_code
        if href in KNOWN_LOGIN_WALLED_ROUTES:
            # Expected to become 200 once these two are opened up; until
            # then assert they behave as the known exception, so this
            # list cannot quietly grow.
            assert status in (200, 302), f"{href} returned {status}"
            continue
        if status != 200:
            walled.append(f"{href} -> {status}")

    assert not walled, (
        f"catalog links on {page} that an anonymous visitor cannot open: "
        f"{walled}"
    )
