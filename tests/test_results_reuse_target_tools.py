"""The "Reuse this target in" list, and the cross-sell block under it.

Both are from Leo's 2026-11-11 report on the first paid rfd4 run: the reuse
list offered every enabled tool, including five that consume a finished design
rather than a target, and the cross-sell block under it led with a line about
lab validation being optional.

The reuse list is asserted through ``designs_against_a_target`` over the LIVE
adapter registry rather than against a hand-written slug list, because that is
the shape the regression took: a tool is registered and silently joins the
offer. A frozen list here would keep passing while the page gained a tool.
"""

from __future__ import annotations

import re

import pytest

from shared.tools_catalog import (
    CATEGORY_ORDER,
    TARGET_DESIGN_CATEGORY,
    _TOOL_CATEGORIES,
    designs_against_a_target,
)
from tests.test_worked_examples import tools_app  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")


# The five Leo named as not belonging in the reuse list. They take a finished
# design, not a target, and three of them already sit in the second-opinion
# fold panel directly above it (shared/refold.py::DESTINATION_TOOLS).
_NOT_TARGET_DESIGNERS = ("colabfold", "esmfold", "boltz2", "opendde", "mpnn")

# The view's second filter: slugs whose FORM takes no PDB, so the handoff's
# target structure, chain and hotspots have nowhere to land. Only
# esmfold2-design needs it -- af2 is in "Predict or check a 3D structure"
# (shared/tools_catalog.py:104), so the band filter already drops it and its
# membership here is belt-and-braces, predating that filter.
#
# esmfold2-design joined 2026-11-13 on Leo's ruling: its target is a built-in
# preset name or a pasted sequence (templates/tools/esmfold2_design_form.html,
# the target_mode radio pair over target_name/target_sequence;
# tools/esmfold2_design/__init__.py:133-154).
_NON_PDB_INPUT_TOOLS = ("af2", "esmfold2-design")


def test_the_target_design_band_is_a_real_category():
    """``TARGET_DESIGN_CATEGORY`` is one string used in three places.

    It is the value ``_TOOL_CATEGORIES`` stamps, a member of
    ``CATEGORY_ORDER`` (so the band renders on /tools and the homepage), and
    the constant ``designs_against_a_target`` compares against. A typo in any
    one of them makes the predicate silently answer False for every tool,
    which EMPTIES the reuse list rather than filtering it -- a failure that
    looks like "the feature works" from the blueprint's side.
    """
    assert TARGET_DESIGN_CATEGORY in CATEGORY_ORDER, CATEGORY_ORDER
    in_band = [s for s, c in _TOOL_CATEGORIES.items()
               if c == TARGET_DESIGN_CATEGORY]
    assert in_band, (
        "no tool carries TARGET_DESIGN_CATEGORY -- the spelling drifted from "
        "_TOOL_CATEGORIES and the predicate now answers False for everything"
    )
    assert all(designs_against_a_target(s) for s in in_band), in_band


def test_the_five_tools_leo_named_are_not_offered_a_target():
    for slug in _NOT_TARGET_DESIGNERS:
        assert slug in _TOOL_CATEGORIES, f"{slug} left the catalog"
        assert not designs_against_a_target(slug), slug


def test_rfd4_is_offered_a_target():
    """The run that produced the report. Named explicitly because the list
    would still be non-empty if rfd4 alone fell out of the band."""
    assert designs_against_a_target("rfd4")


def test_an_unknown_slug_is_not_offered_a_target():
    """``_build_tools_catalog`` falls back to "Other" for an uncategorised
    slug, and "Other" is not this band. So a tool registered without a
    category is left out of the offer rather than quietly added to it."""
    assert not designs_against_a_target("no-such-tool")
    assert not designs_against_a_target("")


def test_every_registered_adapter_is_decided_one_way_or_the_other():
    """No adapter may be missing from ``_TOOL_CATEGORIES``.

    The predicate's fallback for an unlisted slug is False, which is the safe
    direction for the reuse list but the wrong one for a genuine binder
    designer: it would simply stop being offered, with nothing failing. This
    is the guard that notices.
    """
    import app  # noqa: F401, PLC0415  (populates tools.base._REGISTRY)

    from tools import base as tool_base

    uncategorised = [a.slug for a in tool_base.all_adapters()
                     if a.slug not in _TOOL_CATEGORIES]
    assert not uncategorised, (
        "these adapters carry no catalog band, so designs_against_a_target "
        f"answers False for them by default: {uncategorised}"
    )


def test_the_reuse_list_for_an_rfd4_run_is_the_other_designers():
    """The list the blueprint builds, reproduced from its own four filters.

    ``blueprints/jobs.py::job_detail`` drops the running tool itself, the
    non-PDB-input tools (``NON_PDB_INPUT_TOOLS``), anything outside this
    band, and anything flag-off. Reproduced here over the live registry
    rather than asserted as a frozen list of slugs: the point is that the
    five design-consuming tools are gone and the designers are not.
    """
    import app  # noqa: F401, PLC0415

    from tools import base as tool_base

    # The view's fourth filter, ``tool_enabled``, is deliberately NOT
    # reproduced. It is an orthogonal deployment gate -- a slug's FLAG_TOOL_*
    # env var, defaulting off (shared/feature_flags.py) -- and whether it is
    # set here depends on which other module's app fixture ran first
    # (tests/conftest.py's module-scoped fixture turns them all on). Including
    # it can make this test's set empty, and every assertion below passes on
    # nothing when it is.
    # The three filters reproduced are the applicability ones, which is what
    # this test is about.
    offered = {a.slug for a in tool_base.all_adapters()
               if a.slug != "rfd4"
               and a.slug not in _NON_PDB_INPUT_TOOLS
               and designs_against_a_target(a.slug)}

    # Non-vacuity: every assertion below passes on an empty set, so an
    # over-wide filter would read as a clean run.
    assert offered, "the reuse list is empty -- nothing below can fail"
    assert not (offered & set(_NOT_TARGET_DESIGNERS)), offered
    assert "rfd4" not in offered
    assert not (offered & set(_NON_PDB_INPUT_TOOLS)), offered


def test_esmfold2_design_is_still_a_binder_designer():
    """Why the second filter is needed for it at all.

    esmfold2-design is in ``TARGET_DESIGN_CATEGORY``, so the band predicate
    KEEPS it and only ``NON_PDB_INPUT_TOOLS`` takes it out. If it were ever
    moved to another band this test fails, and its entry in that set becomes
    dead weight to delete rather than a silent duplicate filter.

    af2, the set's other member, is NOT in that band
    (shared/tools_catalog.py:104), so the band filter already drops it --
    asserted here so the two members are not read as one case.
    """
    for slug in _NON_PDB_INPUT_TOOLS:
        assert slug in _TOOL_CATEGORIES, f"{slug} left the catalog"
    assert designs_against_a_target("esmfold2-design"), (
        "esmfold2-design is a binder designer; it is excluded from the reuse "
        "list for its FORM's inputs, not for what the tool does"
    )
    assert not designs_against_a_target("af2")


def test_job_detail_excludes_esmfold2_design_by_slug():
    """A SOURCE assertion, for the same reason as the predicate one below.

    The set is a local inside ``job_detail``, so nothing importable carries
    it; test_the_reuse_list_for_an_rfd4_run_is_the_other_designers reproduces it
    from ``_NON_PDB_INPUT_TOOLS``, which
    would keep passing if the view's own set lost the slug. Scoped to the one
    function via ``inspect.getsource``.
    """
    import inspect

    from blueprints import jobs as jobs_bp

    src = inspect.getsource(jobs_bp.job_detail)
    assert "NON_PDB_INPUT_TOOLS = frozenset({\"af2\", \"esmfold2-design\"})" in src, (
        "job_detail's NON_PDB_INPUT_TOOLS no longer holds both slugs"
    )


def test_the_blueprint_imports_the_predicate():
    """Guards the import, which is the whole-page failure mode.

    ``designs_against_a_target`` is called in ``job_detail``; called without
    being imported it raises NameError on EVERY /jobs/<id> page, not only on
    the filtered branch. This asserts the module-level binding exists and is
    the same function tested above; it says nothing about whether the view
    calls it, which is the next test's job.
    """
    from blueprints import jobs as jobs_bp

    assert jobs_bp.designs_against_a_target is designs_against_a_target


def test_job_detail_applies_the_predicate_to_the_reuse_list():
    """A SOURCE assertion, deliberately, and the only one in this file.

    Every other test here reaches the predicate directly, so deleting the
    ``if not designs_against_a_target(...)`` line from the view's loop would
    leave all of them green and put the five design-consuming tools back on
    the page. Rendering the real route instead would need a logged-in session
    and a stored job, which is a far larger fixture than this one guard is
    worth. Scoped to the one function via ``inspect.getsource`` rather than
    grepping the file, so an unrelated mention elsewhere in the blueprint
    cannot satisfy it.
    """
    import inspect

    from blueprints import jobs as jobs_bp

    src = inspect.getsource(jobs_bp.job_detail)
    assert "designs_against_a_target(" in src, (
        "job_detail no longer filters the reuse list by catalog band"
    )


# ---------------------------------------------------------------------------
# The cross-sell block, both branches
# ---------------------------------------------------------------------------

_HANDOFF = [{"slug": "bindcraft", "label": "BindCraft",
             "url": "/tools/bindcraft?from_job=job-1"}]


def _panel(flask_app, *, is_example: bool, handoff=None) -> str:
    """``results_panel`` rendered on its own, in each of its two branches.

    Rendered through the real template rather than read as source: the block
    under test sits inside an ``{% if is_example %}`` and a source-substring
    assertion cannot tell which branch it matched. The branch is selected the
    way the macro selects it -- by the ``job_id`` sentinel, which is what
    ``is_example`` is derived from inside results_shell.html.
    """
    tmpl = flask_app.jinja_env.from_string(
        '{% from "components/results_shell.html" import results_panel %}'
        "{{ results_panel(candidates, columns, tool_slug, job_id,"
        "                 send_target_tools=send_target_tools) }}"
    )
    return tmpl.render(
        candidates=[{"rank": 0, "name": "d0", "pdb_key": "d0.cif"}],
        columns=[], tool_slug="rfd4",
        job_id="example" if is_example else "job-1",
        send_target_tools=_HANDOFF if handoff is None else handoff,
    )


@pytest.mark.parametrize("is_example", [True, False])
def test_the_cross_sell_leads_with_what_ranomics_does(tools_app, is_example):
    """Leo's words were that leading on validation being optional is a bad
    cross-sell. The heading now names the service; the "yours to download"
    trust line stays in both branches."""
    flask_app, _ = tools_app
    with flask_app.test_request_context("/jobs/job-1"):
        html = _panel(flask_app, is_example=is_example)

    assert "Ranomics makes and measures designs like these" in html
    assert "Validating in the lab is optional" not in html
    assert "yours to download and take anywhere" in html


@pytest.mark.parametrize("is_example", [True, False])
def test_the_handoff_block_says_design_new_binders(tools_app, is_example):
    """The label above the reuse list. "Reuse this target in" described the
    mechanism; this describes what the reader gets."""
    flask_app, _ = tools_app
    with flask_app.test_request_context("/jobs/job-1"):
        html = _panel(flask_app, is_example=is_example)

    assert "Design new binders for this target:" in html
    assert "Reuse this target in" not in html


def test_the_second_opinion_sentence_is_not_duplicated(tools_app):
    """``results_shell.html`` gates one sentence on ``is_refold_source``.

    rfd4 is a refold source, so that sentence can render; the reworded
    cross-sell must not say the same thing a second time on the same page.
    """
    flask_app, _ = tools_app
    with flask_app.test_request_context("/jobs/job-1"):
        html = _panel(flask_app, is_example=False)

    assert len(re.findall(r"second-opinion fold", html)) <= 1
