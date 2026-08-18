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
