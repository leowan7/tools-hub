"""Every per-tool guide linked from ``/help`` resolves to a real page.

The guide grid on ``/help`` used to be a hardcoded list of nine
``(slug, label, blurb)`` tuples in ``templates/help/index.html``. Five
GPU tools shipped after it was written — boltz2, iggm, opendde,
proteina, esmfold2-design — and none of them ever appeared, because
nothing failed when the list went stale. It is now derived from
``_build_tools_catalog()``.

Deriving it introduces the opposite hazard: the catalog also carries two
non-adapter entries (Epitope Scout, the Developability Scout) whose
slugs make ``public.help_tool_guide`` render 404, so a naive loop over
the catalog would emit two dead links. ``help_index`` splits on exactly
the condition that route uses.

Both hazards are checked here by following the links the page actually
renders, not by asserting against a list this test also owns.

The catalog is flag-gated (``shared/feature_flags.py`` is fail-closed),
so with no ``FLAG_TOOL_*`` set the grid renders EMPTY and a "every link
resolves" assertion passes over zero links. The count assertions below
are what stop that vacuous pass.
"""

from __future__ import annotations

import re

import pytest

# ``tools.base._REGISTRY`` is populated as a side effect of importing the
# app. Without this, ``all_adapters()`` returns [] and every assertion
# below iterates over nothing.
import app as _app  # noqa: F401
from shared.tools_catalog import _build_tools_catalog
from tools import base as tool_base

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# Slugs the old hardcoded list missed. Pinned by name because "the list
# went stale" is the actual bug, and a count alone would not notice a
# newly added tool silently taking a dropped one's place.
PREVIOUSLY_MISSING = ("boltz2", "iggm", "opendde", "proteina", "esmfold2-design")

_GUIDE_HREF = re.compile(r'href="(/help/tools/[^"]+)"')


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    # Every GPU tool on: production state, and the only state in which
    # the catalog is non-empty. Fail-closed flags would otherwise make
    # this whole module assert nothing.
    for adapter in tool_base.all_adapters():
        monkeypatch.setenv(
            "FLAG_TOOL_" + adapter.slug.upper().replace("-", "_"), "on"
        )
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _guide_links(client) -> list[str]:
    resp = client.get("/help")
    assert resp.status_code == 200
    return _GUIDE_HREF.findall(resp.get_data(as_text=True))


def test_registry_is_populated():
    """Guard: an empty registry would make every assertion below vacuous."""
    assert len(tool_base.all_adapters()) >= 14


def test_every_guide_link_resolves(app):
    """Follow every /help/tools/... link the page renders. None may 404."""
    client = app.test_client()
    links = _guide_links(client)
    assert links, "/help rendered no per-tool guide links at all"
    for href in links:
        assert client.get(href).status_code == 200, f"{href} did not return 200"


def test_one_guide_per_registered_tool(app):
    """The grid covers the live registry — no stale list, no dead entries."""
    client = app.test_client()
    linked = {href.rsplit("/", 1)[-1] for href in _guide_links(client)}
    assert linked == {a.slug for a in tool_base.all_adapters()}


def test_the_five_tools_the_hardcoded_list_missed_are_linked(app):
    """The regression that motivated deriving the list."""
    client = app.test_client()
    linked = {href.rsplit("/", 1)[-1] for href in _guide_links(client)}
    assert set(PREVIOUSLY_MISSING) <= linked


def test_non_adapter_tools_get_no_guide_link(app):
    """Epitope Scout / Developability have no guide page; linking one 404s."""
    client = app.test_client()
    for slug in ("epitope-scout", "developability"):
        assert client.get(f"/help/tools/{slug}").status_code == 404
        assert f"/help/tools/{slug}" not in _guide_links(client)


def test_guideless_tools_are_still_reachable_from_help(app):
    """The guideless paragraph renders, and links every non-adapter tool.

    Without this, deleting the whole ``guideless_tools`` block leaves the
    suite green: the guide-grid tests above only assert about the tools
    that DO have a guide, so the two that do not would silently vanish
    from ``/help`` with nothing to catch it.

    Routes are read back out of the catalog rather than hardcoded, so a
    tool moving URL does not turn this into a false failure.
    """
    client = app.test_client()
    body = client.get("/help").get_data(as_text=True)

    with app.test_request_context():
        guideless = [
            e for e in _build_tools_catalog() if tool_base.get(e["slug"]) is None
        ]
    assert guideless, "catalog carries no non-adapter tools; test asserts nothing"

    for entry in guideless:
        assert f'href="{entry["route"]}"' in body, (
            f"/help no longer links {entry['slug']} ({entry['route']})"
        )
        assert entry["name"] in body


def test_guideless_copy_matches_the_real_gate_on_those_routes(app):
    """The paragraph's access claim has to track the routes it links.

    This assertion has been inverted once already. When it was written,
    ``/scout/`` and ``/developability`` were both ``@login_required`` and
    the copy was required to say "sign in". #148 and #149 then opened
    both to anonymous visitors, which made the warning the false claim.

    So it is no longer pinned to either wording: it reads the actual
    anonymous status of every guideless route and requires the paragraph
    to match. If one of them is ever gated again, the copy must say so
    and this fails until it does.
    """
    client = app.test_client()
    body = client.get("/help").get_data(as_text=True)

    with app.test_request_context():
        guideless = [
            e for e in _build_tools_catalog() if tool_base.get(e["slug"]) is None
        ]
    assert guideless, "catalog carries no non-adapter tools; test asserts nothing"
    routes = [e["route"] for e in guideless]
    gated = [r for r in routes if client.get(r).status_code in (301, 302)]

    # The one paragraph that carries every guideless link.
    para = next(
        frag
        for frag in body.split("<p ")
        if all(f'href="{r}"' in frag for r in routes)
    )
    if gated:
        assert "sign in" in para.lower(), (
            "the guideless paragraph must flag that some of these need an "
            f"account; routes currently gated: {gated}"
        )
    else:
        assert "without an account" in para.lower(), (
            "every guideless route opens anonymously, so the paragraph "
            "should say so rather than warn about signing in"
        )
        assert "sign in" not in para.lower(), (
            "the paragraph warns about signing in, but none of these "
            f"routes is gated: {routes}"
        )


def test_no_guide_falls_into_the_other_bucket(app):
    """``_TOOL_CATEGORIES`` has no entry for a slug -> band is "Other".

    That fallback is silent and has shipped twice: Proteina and OpenDDE
    both landed with no band and rendered under a meaningless heading on
    the homepage. The guide grid inherits the same field, so it inherits
    the same failure mode.
    """
    client = app.test_client()
    with app.test_request_context():
        catalog = _build_tools_catalog()
    assert len(catalog) >= 16, "catalog too small; this test asserts nothing"

    stray = [e["slug"] for e in catalog if e.get("category") == "Other"]
    assert not stray, f"no workflow band for {stray}; add them to _TOOL_CATEGORIES"

    body = client.get("/help").get_data(as_text=True)
    assert ">Other<" not in body, "/help rendered an 'Other' band heading"


def test_every_rendered_band_resolves_a_glyph(app):
    """Band strings are also ``_CATEGORY_GLYPHS`` keys; a rename drops one."""
    from shared.category_glyphs import _CATEGORY_GLYPHS  # noqa: PLC0415

    with app.test_request_context():
        bands = {e["category"] for e in _build_tools_catalog()}
    assert len(bands) >= 5, "too few bands; this test asserts nothing"
    missing = sorted(b for b in bands if b not in _CATEGORY_GLYPHS)
    assert not missing, f"catalog bands with no glyph: {missing}"


def test_guide_grid_is_grouped_by_catalog_category(app):
    """Design tools sit together, not scattered by registry order.

    ``all_adapters()`` order is roughly alphabetical by slug, which put
    ProteinMPNN between IgGM and OpenDDE. Band names are read from the
    catalog, never spelled out here, so the Phase 4a rename cannot break
    this test.
    """
    client = app.test_client()
    body = client.get("/help").get_data(as_text=True)

    with app.test_request_context():
        by_slug = {e["slug"]: e for e in _build_tools_catalog()}

    # Position of each tool's guide link in the rendered page, in render order.
    ordered = [href.rsplit("/", 1)[-1] for href in _guide_links(client)]
    bands = [by_slug[slug]["category"] for slug in ordered]
    assert len(set(bands)) > 1, "only one band present; grouping asserts nothing"

    # A band may not reappear after another band has started.
    seen: list[str] = []
    for band in bands:
        if not seen or seen[-1] != band:
            assert band not in seen, f"band {band!r} is split across the grid"
            seen.append(band)

    for band in seen:
        assert band in body, f"band heading {band!r} is not rendered"


def test_empty_guide_grid_renders_an_empty_state(monkeypatch):
    """All flags off empties the catalog; the section may not be blank."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    for adapter in tool_base.all_adapters():
        monkeypatch.delenv(
            "FLAG_TOOL_" + adapter.slug.upper().replace("-", "_"), raising=False
        )
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()

    body = client.get("/help").get_data(as_text=True)
    assert not _GUIDE_HREF.findall(body), "expected an empty guide grid"
    assert "No tool guides are available right now" in body


# ---------------------------------------------------------------------
# Getting-started copy. Every claim below was FALSE when this page was
# first written and became true only once #144 (anonymous tool pages),
# #145 (task-first catalog bands) and #147 (guided pilot recipes) landed.
# Nothing else in the suite would notice them going false again.
# ---------------------------------------------------------------------

def test_getting_started_claim_you_can_look_without_an_account(app):
    """Step 2 says "You do not need an account to look"."""
    client = app.test_client()
    body = client.get("/help/getting-started").get_data(as_text=True)
    assert "do not need an account to look" in body

    slugs = [a.slug for a in tool_base.all_adapters()]
    assert len(slugs) >= 14, "registry empty; this test asserts nothing"
    gated = [s for s in slugs if client.get(f"/tools/{s}").status_code != 200]
    assert not gated, f"tool pages that do not open anonymously: {gated}"


def test_getting_started_claim_the_home_page_groups_by_task(app):
    """Step 1 says the home page groups the tools by the user's question."""
    client = app.test_client()
    home = client.get("/")
    assert home.status_code == 200, "home page does not open anonymously"
    home_body = home.get_data(as_text=True)

    with app.test_request_context():
        bands = {e["category"] for e in _build_tools_catalog()}
    assert len(bands) >= 5, "too few bands; this test asserts nothing"
    for band in bands:
        assert band in home_body, f"home page does not render the {band!r} band"


def test_getting_started_names_the_pilot_card_cta_that_exists(app):
    """Step 3 tells the reader to press "Load these settings".

    That string is the ``pilot_card.html`` CTA. If the card is reworded
    the instruction becomes a wild goose chase, and nothing else fails.
    """
    client = app.test_client()
    body = client.get("/help/getting-started").get_data(as_text=True)
    assert "Load these settings" in body

    with_card = [
        a.slug
        for a in tool_base.all_adapters()
        if "Load these settings"
        in client.get(f"/tools/{a.slug}").get_data(as_text=True)
    ]
    # Ten of fourteen carry a pilot; the page says "most", not "every".
    assert len(with_card) >= 10, f"only {len(with_card)} tools render a pilot card"
    assert len(with_card) < len(tool_base.all_adapters()), (
        "every tool now has a pilot card — step 3's carve-out for the fast "
        "predictors is stale and should be dropped"
    )


def test_help_index_step_count_matches_the_guide(app):
    """/help summarises the guide as N steps; the guide has N <li>."""
    client = app.test_client()
    guide = client.get("/help/getting-started").get_data(as_text=True)
    steps = guide.count('<h2 class="help-step-title">')
    assert steps > 1, "no steps parsed; this test asserts nothing"

    words = {
        5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
    }
    index_body = client.get("/help").get_data(as_text=True)
    assert f"{words[steps]} steps from the protein" in index_body, (
        f"the guide has {steps} steps; /help says otherwise"
    )


def test_signup_credit_actually_covers_every_pilot(app):
    """Step 4 claims the signup credit "covers every pilot on the site".

    The credit is $15 and the dearest pilot (proteina) is $12.59 — true,
    but only by $2.41, and both numbers move independently: #151/#153
    changed the credit, and every pilot price is derived from live GPU
    rates over the recipe's params. Neither side knows about this
    sentence.
    """
    import re as _re  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    from shared.wallet import SIGNUP_CREDIT_USD  # noqa: PLC0415

    client = app.test_client()
    assert "signup_credit()" not in client.get(
        "/help/getting-started"
    ).get_data(as_text=True), "signup_credit() did not render"

    prices = {}
    for adapter in tool_base.all_adapters():
        body = client.get(f"/tools/{adapter.slug}").get_data(as_text=True)
        hit = _re.search(r"About <strong>\$([\d,.]+)</strong>", body)
        if hit:
            prices[adapter.slug] = Decimal(hit.group(1).replace(",", ""))
    assert len(prices) >= 10, f"only {len(prices)} pilot prices found"

    over = {s: p for s, p in prices.items() if p > SIGNUP_CREDIT_USD}
    assert not over, (
        f"the ${SIGNUP_CREDIT_USD} signup credit no longer covers {over}; "
        "step 4 of /help/getting-started says it covers every pilot"
    )
