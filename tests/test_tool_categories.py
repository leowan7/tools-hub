"""Every GPU adapter must carry a real workflow-stage category.

Proteina-Complexa and OpenDDE both shipped without an entry in
``_TOOL_CATEGORIES``, so they silently fell into the homepage's "Other"
bucket. This guards the next adapter from doing the same.
"""

# ``tools.base._REGISTRY`` is populated only by the ``import tools.<slug>``
# side effects in app.py, so importing app is what makes the adapters
# visible here. Without it this test iterates an empty registry and
# passes vacuously -- green while the bug ships. The explicit count
# assertion below keeps that failure mode from coming back silently.
import app  # noqa: F401
from tools import base as tool_base

from shared.tools_catalog import _TOOL_CATEGORIES


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


def test_every_adapter_resolves_its_meta_in_the_catalog():
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
    import os

    from app import create_app
    from shared.feature_flags import flag_name
    from shared.tools_catalog import _build_tools_catalog

    slugs = {a.slug for a in tool_base.all_adapters()}
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    for slug in slugs:
        os.environ[flag_name(slug)] = "on"
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")

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
