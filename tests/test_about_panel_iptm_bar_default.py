"""No tool may be pointed at a per-tool ipTM pass bar it does not declare.

The "What good looks like" panel renders on all 14 tool forms. The 14
guides do not import it -- templates/help/tool_guide.html carries its own
copy of the same ipTM entry under "How to read the results" -- and both
ended a sentence by sending the reader off to find the tool's own bar.
On the form:

    Individual tools set their own pass bar a little either side of that
    -- the guide for the tool you ran states its one.

The guide words the pointer differently ("this guide's own results summary
above states this tool's"); ANY_POINTER below matches either.

That clause was reached through an INVERTED default. The branch guarding
the honest alternative read::

    {%- if _ipl and 'good' not in _ipl %}

so it fired only when a legend EXISTED and omitted ``good`` -- boltzgen,
the one tool the branch was written for. A tool with no ipTM legend at
all fell through to ``{%- else %}`` and got the confident sentence, which
is the case that most needs the caveat. Six tools shipped it: esmfold,
iggm, mpnn, opendde, proteina and rfantibody. A bench biologist on the
opendde form followed that sentence to nothing -- tools/opendde/meta.py
declares no per-tool bars and shared/score_legends.py has no opendde key.

THE SAME CONDITION HAD A SECOND, OPPOSITE FAILURE, and it is why
``get_legend`` now folds case. It was an exact-case dict lookup and both
templates hardcoded ``"ipTM"``, but af2 and colabfold spell their column
``"iptm"`` (shared/score_legends.py). Both therefore resolved to None and
rendered the confident sentence for the same wrong reason as opendde --
true by accident, since both DO declare a bar of 0.6. Flipping the
condition without fixing the lookup would have moved two correct
sentences to "there is no cross-tool band to compare it to", which for
af2 and colabfold is false.

WHAT THIS FILE DOES NOT PIN. "Declares one" here means an ipTM legend in
shared/score_legends.py carrying ``good`` -- the machine-readable bar,
which is what the template consults. It is NOT a check that the tool's
guide PAGE prints that number, nor that the two agree where both exist
-- pxdesign's guide states ipTM >= 0.70 against a declared 0.75. Two
further limits: every check reads the ipTM ``<dd>`` only, so a pointer
placed elsewhere on the page is invisible to it, and the clause is
matched by phrase, so a reworded one is too. The sets differ: on
2026-09-10 only boltz2, esmfold2-design, pxdesign and rfdiffusion state
an ipTM bar in their guide's visible text, so the pointer still overshoots
for af2, bindcraft and colabfold. Reconciling the guide text with the
legend is a separate change; this file pins the defect that was reported.
"""
from __future__ import annotations

import html
import re

import pytest

from shared.metric_glossary import GLOSSARY
from shared.score_legends import SCORE_LEGENDS, get_legend

# Nothing here may consult the real database. A /tools/<slug> render runs
# _public_tool_context -> _build_public_tool_context -> _pilot_context ->
# estimated_cost_for_tool -> _historical_p90_seconds, an uncached Supabase
# SELECT on tool_jobs_p90
# (shared/wallet_estimates.py:672). Counted on this branch by wrapping
# those functions: 10 of the 28 pages an _entries call renders reach the
# SELECT -- the 14 guide pages build no such context at all, and af2,
# colabfold, esmfold and opendde stop short of it. _entries memoises on
# the module-scoped app, so its 28 renders happen once for the module:
# 10 live SELECTs total, not 10 on each of the five call sites.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

#: ANY pointer wording. Used where PRESENCE is the question: a tool that
#: declares no bar must not be sent looking for one in either surface's
#: words. Not redundant with POINTER below: a bare "states its one"
#: fragment on a legend-less guide page passes all seven tests if the
#: defect test matches only that surface's full clause, and fails on
#: ANY_POINTER.
ANY_POINTER = re.compile(r"states\s+(?:its\s+one|this\s+tool'?s)", re.I)

#: The clause each surface SHOULD carry. Used where PLACEMENT is the
#: question: the form sends the reader to the tool's guide, the guide to
#: its own results summary above. Matched separately because ANY_POINTER
#: alone passes the two being SWAPPED -- a form telling the reader to
#: consult "this guide's own results summary above", which a form does not
#: have, and a guide pointing at the page already open.
#:
#: The two per-page tests below split all 14 tools on whether the legend
#: carries ``good``, so a clause on a tool that declares no bar fails one
#: and a clause missing from a tool that does fails the other. Neither
#: reads the REST of the sentence: a wrong band, an invented threshold
#: printed beside the right one, or reversed advice is not caught here.
POINTER = {
    "/tools/{slug}": re.compile(
        r"the guide for the tool you ran states its one", re.I),
    "/help/tools/{slug}": re.compile(
        r"this guide's own results summary above states this tool's", re.I),
}

#: The honest branch for a tool whose legend exists but omits ``good``.
NO_SHARED_SCALE = "no cross-tool band to compare it to"

#: Rendered on both surfaces, and the reason the no-legend branch still
#: prints the general band rather than dropping the sentence:
#: test_public_tool_pages::test_every_general_legend_reads_the_glossary
#: asserts this string is on /tools/mpnn, and mpnn has no ipTM legend.
BAND = GLOSSARY["ipTM"]["good_range"]


def _iptm_legend(slug: str) -> dict | None:
    """This tool's ipTM legend under whichever case it spells the column.

    Spelling-agnostic, and deliberately NOT ``get_legend``: the
    expectation this file checks against must not be computed by the code
    under test. Delegating here would make the five PAGE tests agree
    with a lookup that stopped folding case and pass vacuously; the two
    that compare it against a case-sensitive expectation rather than
    against rendered text -- the group check and the lookup test -- would
    still fail.
    """
    for (tool, column), legend in SCORE_LEGENDS.items():
        if tool == slug and column.lower() == "iptm":
            return legend
    return None


def _visible(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    # Fold the common apostrophe lookalikes to ASCII before matching.
    # The patterns above are phrases, so a clause typed with a smart quote
    # falls out of ANY_POINTER's reach and the defect test stops seeing
    # it. Measured: with only U+2019 folded, each of the other five here
    # still slipped a phantom pointer past all seven tests. This is not
    # every lookalike Unicode has -- a fullwidth or Armenian apostrophe,
    # or a combining accent, still would -- it is the set a keyboard or an
    # editor's smart quotes actually produce.
    text = re.sub(r"[\u2018\u2019\u02bc\u00b4\u2032`]", "'", text)
    return re.sub(r"\s+", " ", text)


#: The definition sentence both surfaces open the entry with. All 28
#: pages carry exactly one ``<dt>ipTM</dt>`` today, so this filter
#: discriminates nothing yet. It is here for the page that grows a second
#: one: a tool naming an input "ipTM" would put an entry in the guide's
#: input glossary (tool_guide.html:108), which PRECEDES the results list,
#: so an unfiltered reader would take that one. Injected such an entry to
#: check: two dt/dd pairs, one match -- the filter picks the real entry
#: and the assert below stays quiet. It fires only if a second entry
#: carries this sentence too.
DEFINITION = "Predicted confidence in the contact between two chains"


def _iptm_entry(body: str, slug: str, path: str) -> str:
    """The visible text of the ipTM ``<dd>`` on one rendered page."""
    entries = [
        _visible(dd) for dd in re.findall(
            r"<dt[^>]*>\s*(?:<strong>)?\s*ipTM\s*(?:</strong>)?\s*</dt>"
            r"\s*(?:<!--.*?-->\s*)*<dd[^>]*>(.*?)</dd>",
            body, re.S,
        )
    ]
    named = [e for e in entries if DEFINITION in e]
    assert len(named) == 1, (
        f"{path} rendered {len(named)} ipTM entries carrying "
        f"{DEFINITION!r} (of {len(entries)} ipTM dt/dd pairs); the "
        f"assertions need exactly the one this PR changed: {named}"
    )
    return named[0]


#: Both surfaces that render the per-tool clause. help/faq.html states the
#: same band but names no tool and points nowhere, so it is correct as it
#: stands and is not listed here.
SURFACES = ("/tools/{slug}", "/help/tools/{slug}")


#: Keyed by the app the module-scoped fixture built, so the 28 renders
#: happen on the first call and every later call in the module reuses
#: them. A new module builds a new app, which misses and rebuilds.
#: Do not call this inside a patch that changes rendering: the first
#: call's HTML is what every later call in the module receives.
_ENTRIES: dict = {}


def _entries(all_tools_app) -> dict[tuple[str, str], str]:
    flask_app, slugs = all_tools_app
    cached = _ENTRIES.get(flask_app)
    if cached is not None:
        return cached
    client = flask_app.test_client()
    out = {}
    for slug in slugs:
        for surface in SURFACES:
            path = surface.format(slug=slug)
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"
            out[(slug, surface)] = _iptm_entry(
                resp.get_data(as_text=True), slug, path
            )
    # The first assert catches a surface dropped from SURFACES alone.
    # The second is a literal 2 rather than len(SURFACES) -- which would
    # read its own length back and never fail -- and catches one dropped
    # together with its pattern, which would otherwise halve every
    # assertion that reads _entries, five of the seven tests.
    assert set(POINTER) == set(SURFACES), (
        "POINTER is keyed by surface; a surface with no pattern would go "
        f"unchecked: {sorted(set(SURFACES) ^ set(POINTER))}"
    )
    assert len(SURFACES) == 2 and len(out) == len(slugs) * 2, (
        f"expected {len(slugs)} slugs x 2 surfaces = {len(slugs) * 2} "
        f"entries; SURFACES={SURFACES} produced {len(out)}"
    )
    _ENTRIES[flask_app] = out
    return out


def test_the_two_groups_are_both_populated(all_tools_app):
    """Non-vacuity. The two groups are complements by construction, so
    the thing worth asserting is that neither is EMPTY.

    Two of the six tests below filter the 28 pages on the same predicate
    these groups use -- the defect test takes the tools without ``good``,
    the pointer test those with it -- so an empty group would make its
    test pass by having nothing to check. The other four cannot be
    emptied that way: two branch on the narrower "legend present but
    carrying no ``good``" (boltzgen alone) and assert on every page either
    way, test_the_pointer_pattern_matches_something uses neither, and
    test_the_legend_lookup_folds_case_at_the_source takes no fixture.

    The groups derive from SCORE_LEGENDS, which no template can affect;
    but building `with_bar` needs a case-folding read, and an exact-case
    one silently moves af2 and colabfold into `without_bar`."""
    _flask_app, slugs = all_tools_app
    # Key presence, not truthiness, to match the templates' own
    # ``'good' not in _ipl``. A legend written good=None or good=0 would
    # otherwise read as bar-less here while the page still prints the
    # pointer: the pointer test would skip it and the defect test would
    # report it as an offender.
    with_bar = [s for s in slugs if "good" in (_iptm_legend(s) or {})]
    without_bar = [s for s in slugs if "good" not in (_iptm_legend(s) or {})]
    assert with_bar, "no tool declares an ipTM bar; the pointer test is vacuous"
    assert without_bar, "every tool declares a bar; the defect test is vacuous"
    # The case-folded read is the point: af2 and colabfold declare their
    # bar under "iptm", and an exact-case reader puts them in `without_bar`.
    assert {"af2", "colabfold"} <= set(with_bar), sorted(with_bar)
    # boltzgen's legend exists and states no bar, so it belongs with the
    # legend-less six. tests/test_boltzgen_iptm_has_no_cofold_bar.py is the
    # authoritative pin for that; this line is here because it is the only
    # assertion in THIS file that notices boltzgen gaining a bar -- every
    # page test still passes if it does.
    assert "boltzgen" in without_bar, sorted(without_bar)


def test_no_tool_is_pointed_at_a_pass_bar_it_does_not_declare(all_tools_app):
    """The reported defect, on both surfaces.

    A tool that declares no per-tool bar has none for the reader to go
    and find, so the sentence must not send them looking for one.

    "Declares no bar" is the absence of a ``good`` key, which covers two
    shapes: the six tools with no ipTM legend at all, and boltzgen, whose
    legend exists and states no bar. Filtering on ``legend is None``
    exempted boltzgen -- the one tool the honest branch was written for --
    and a pointer rendered on boltzgen alone passed every test in this
    file."""
    offenders = {}
    for (slug, surface), text in _entries(all_tools_app).items():
        if ("good" not in (_iptm_legend(slug) or {})
                and ANY_POINTER.search(text)):
            offenders[surface.format(slug=slug)] = text
    assert not offenders, (
        "these pages send the reader off to find a per-tool ipTM pass "
        "bar, but the tool declares no such bar in "
        f"shared/score_legends.py: {offenders}"
    )


def test_a_tool_that_declares_a_bar_still_gets_the_pointer(all_tools_app):
    """The opposite error, which the naive fix would have introduced.

    Defaulting a missing legend to the honest branch is only correct if
    the lookup actually finds the legends that exist. af2 and colabfold
    spell the column "iptm", so an exact-case get_legend reads them as
    bar-less while shared/score_legends.py sets both to good=0.6.
    Under a two-way flip of the original condition, that would have told
    their readers there is no cross-tool band to compare the number to,
    which is
    false of them; under the three branches that shipped, it instead drops
    a pointer they have earned. Either way these four pages fail here."""
    missing = {}
    for (slug, surface), text in _entries(all_tools_app).items():
        legend = _iptm_legend(slug) or {}
        if "good" in legend and not POINTER[surface].search(text):
            missing[surface.format(slug=slug)] = text
    assert not missing, (
        "these tools declare an ipTM bar in shared/score_legends.py but "
        f"their page no longer points the reader at it: {missing}"
    )


def test_the_pointer_pattern_matches_something(all_tools_app):
    """Guards the guards. Both patterns are phrase matches: a reword that
    ANY_POINTER stops matching leaves the defect test above blind, and one
    that POINTER[surface] stops matching loses the placement check. A
    clause added to a single tool is caught by the defect test only while
    ANY_POINTER still matches its wording -- a freshly phrased pointer
    ("see this tool's own guide for the bar it clears") is caught by
    nothing in this file."""
    entries = _entries(all_tools_app)
    stale, misplaced = [], []
    for surface in SURFACES:
        texts = [t for (_slug, s), t in entries.items() if s == surface]
        for label, pat in (("POINTER", POINTER[surface]),
                           ("ANY_POINTER", ANY_POINTER)):
            if not any(pat.search(t) for t in texts):
                stale.append((surface, label, pat.pattern))
        # The clause belonging to the OTHER surface must not appear here,
        # even alongside the right one: a form carrying "this guide's own
        # results summary above" names something a form does not have.
        for other in SURFACES:
            if other == surface:
                continue
            for (slug, s), text in entries.items():
                if s == surface and POINTER[other].search(text):
                    misplaced.append(surface.format(slug=slug))
    assert not (stale or misplaced), (
        f"patterns that matched nothing on the pages they guard: {stale}; "
        "pages carrying the OTHER surface's pointer clause, which names a "
        f"place they do not have: {sorted(set(misplaced))}"
    )


def test_only_a_legend_without_a_bar_drops_the_general_band(all_tools_app):
    """A tool with NO legend is not the same as boltzgen.

    boltzgen's number comes from its generator's own confidence head, so
    the shared band is the wrong ruler and is suppressed. A tool that
    simply declares nothing has not made that claim about itself, and the
    panel's own preamble already says each tool reports a subset of these
    metrics -- so it keeps the general band and only loses the pointer.
    Dropping the band there would also break
    test_public_tool_pages::test_every_general_legend_reads_the_glossary,
    which asserts the band renders on /tools/mpnn."""
    wrong = {}
    for (slug, surface), text in _entries(all_tools_app).items():
        legend = _iptm_legend(slug)
        declares_no_bar = legend is not None and "good" not in legend
        has_band = BAND in text
        if declares_no_bar and has_band:
            wrong[surface.format(slug=slug)] = f"band kept: {text}"
        if not declares_no_bar and not has_band:
            wrong[surface.format(slug=slug)] = f"band dropped: {text}"
    assert not wrong, (
        f"the general ipTM band {BAND!r} is on the wrong pages: {wrong}"
    )


def test_the_no_shared_scale_wording_stays_with_the_tool_it_describes(
    all_tools_app,
):
    """"...'s number is not on the shared scale" asserts the tool HAS a
    number and that it was measured differently. That is a claim about
    boltzgen, backed by tests/test_boltzgen_iptm_has_no_cofold_bar.py. A
    tool that declares no ipTM legend has not told us it reports an ipTM
    at all -- mpnn does sequence recovery -- so the sentence must not be
    reused for it."""
    wrong = {}
    for (slug, surface), text in _entries(all_tools_app).items():
        legend = _iptm_legend(slug)
        expected = legend is not None and "good" not in legend
        if (NO_SHARED_SCALE in text) is not expected:
            wrong[surface.format(slug=slug)] = text
    assert not wrong, (
        f"{NO_SHARED_SCALE!r} claims the tool reports an ipTM measured off "
        f"the shared scale; it is on the wrong pages: {wrong}"
    )


def test_the_legend_lookup_folds_case_at_the_source():
    """The other half of the fix, where it now lives.

    af2 and colabfold spell the column "iptm", so a caller naming the
    display column reads them as bar-less while shared/score_legends.py
    declares a bar for each. Folding case in get_legend answers that for
    its production callers -- both templates here,
    candidate_table.html's multi-tool Score cell and its single-tool
    column header, and _join_bar and judge in score_legends.py. That
    header went through
    ``score_legends_for``, a separate exact-case reader, until that line
    was pointed at this function; the three pLDDT legends case alone could
    not reach were re-keyed to the spelling their pages pass
    (tests/test_score_legend_lookup.py).

    Case, and nothing more. ``_COLUMN_ALIASES`` folds complex_pLDDT into
    pLDDT for reading a result RECORD; a legend is per (tool, column) and
    must not be answered out of a different column's, which is why that
    map is not reused here.
    """
    for slug in ("af2", "colabfold"):
        stored = _iptm_legend(slug)
        assert stored is not None, f"{slug} declares no ipTM legend in any case"
        # Identity, not the value of the bar: retuning ``good`` is someone
        # else's change and must not land on this test.
        assert get_legend(slug, "iptm") is stored, slug
        assert get_legend(slug, "ipTM") is stored, slug

    # A tool that spells it the other way round is unaffected. Asserted
    # non-None first, or this degrades to ``None is None``.
    rfd = _iptm_legend("rfdiffusion")
    assert rfd is not None, "rfdiffusion lost its ipTM legend"
    assert get_legend("rfdiffusion", "ipTM") is rfd

    # A hashable non-string column is a miss, not a crash. A truthy
    # unhashable one raises at the dict lookup, here and before the fold
    # alike; a falsy one returns None on the guard above it.
    assert get_legend("af2", 1) is None

    # Not a general alias, and not a way to invent a legend.
    assert get_legend("af2", "complex_pLDDT") is None
    assert get_legend("af2", "no_such_column") is None
    assert get_legend("", "ipTM") is None
