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


def test_guideless_copy_does_not_promise_anonymous_access(app):
    """``/scout/`` and ``/developability`` can redirect to login.

    The first draft of this paragraph told the reader to "open one and the
    page explains itself", which is false while either route is
    ``@login_required``. Epitope Scout is being opened to anonymous
    visitors on a separate branch, so the copy has to be true either way —
    it may not claim these open without an account.
    """
    client = app.test_client()
    body = client.get("/help").get_data(as_text=True)

    with app.test_request_context():
        guideless = [
            e for e in _build_tools_catalog() if tool_base.get(e["slug"]) is None
        ]
    routes = [e["route"] for e in guideless]

    # The one paragraph that carries every guideless link.
    para = next(
        frag
        for frag in body.split("<p ")
        if all(f'href="{r}"' in frag for r in routes)
    )
    assert "sign in" in para.lower(), (
        "the guideless paragraph must flag that some of these need an "
        f"account; routes currently gated: "
        f"{[r for r in routes if client.get(r).status_code in (301, 302)]}"
    )


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
