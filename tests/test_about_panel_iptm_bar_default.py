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
which is what the template consults. Whether the two AGREE where both
exist is a separate file's job: tests/test_guide_bar_matches_legend.py
pins that half, and pxdesign's guide stated ipTM >= 0.70 against a
declared 0.75 until it landed.

THE GUIDE PAGE IS NOW PINNED HERE TOO, and it was not when the tests
above this point were written. Declaring a bar and PRINTING one are
different facts about a tool, and the pointer promises the second while
the template can only consult the first. af2, bindcraft and colabfold
declare 0.6, 0.75 and 0.6, and no guide of the three printed a bar of
its own -- each prints other figures, and the shared band sits in the
ipTM entry itself, but bindcraft's nearest thing to its OWN bar was
"above the BindCraft default thresholds" in its inputs list, which names
no number. So both surfaces of all three sent a reader to look up a
figure that was not there. The ``output_summary`` in each of their
meta.py now states it, and
test_the_pointer_is_only_made_where_the_guide_states_a_number reads the
number back off the RENDERED page rather than off the constant.

THE FIGURES COME FROM THE LEGEND, THE PHRASING DOES NOT. Each summary
takes its ``good`` and ``excellent`` from that tool's legend in
shared/score_legends.py, but states them INCLUSIVELY ("0.75 or more")
where the legend's own ``explanation`` says "Above 0.75". The bar is
inclusive: shared/score_legends.py::judge gates on ``seen >= good``,
shared/metric_glossary.py publishes that to readers ("a value exactly ON
a bar counts as meeting it") and tests/test_derived_verdicts.py pins it
in test_a_value_exactly_on_the_bar_meets_it. score_legends.py has ruled
on this wording once already, in the comment above its
("esmfold2-design", "ipTM") entry: "0.75 OR MORE", not "above 0.75". It
matters most on bindcraft, whose own worked example carries ipTM 0.75
exactly and narrates it as sitting ON the bar rather than above it --
with "above" in the summary that page contradicted itself within one
scroll. The legend ``explanation`` strings still say "Above"; correcting
them changes the results-table tooltips and is not this file's business.

THREE FURTHER LIMITS. Every check reads the ipTM ``<dd>`` only, so a
pointer placed elsewhere on the page is invisible to it; the clause is
matched by phrase, so a reworded one is too; and a printed bar is
matched by SHAPE -- an ipTM token, a comparison, a figure -- so a bar
given in words alone does not count as one, which is the call that makes
bindcraft's old wording a miss.
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
# (shared/wallet_estimates.py::_historical_p90_seconds). Counted on this
# branch by wrapping those functions: 10 of the 28 pages an _entries call
# renders reach the SELECT -- the 14 guide pages build no such context at
# all, and af2, colabfold, esmfold and opendde stop short of it. _entries
# memoises on the module-scoped app, so its 28 renders happen once for the
# module:
# 10 live SELECTs total, not 10 on each of the call sites.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

#: ANY pointer wording. Used where PRESENCE is the question: a tool that
#: declares no bar must not be sent looking for one in either surface's
#: words. Not redundant with POINTER below: a bare "states its one"
#: fragment on a legend-less guide page passes every other test in this
#: file if the defect test matches only that surface's full clause, and
#: fails on ANY_POINTER.
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
#: tests/test_public_tool_pages.py::test_every_general_legend_reads_the_glossary
#: asserts this string is on /tools/mpnn, and mpnn has no ipTM legend.
BAND = GLOSSARY["ipTM"]["good_range"]


def _iptm_legend(slug: str) -> dict | None:
    """This tool's ipTM legend under whichever case it spells the column.

    Spelling-agnostic, and deliberately NOT ``get_legend``: the
    expectation this file checks against must not be computed by the code
    under test. Delegating here would make every PAGE test in this file
    agree with a lookup that stopped folding case and pass vacuously; the two
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
    # still slipped a phantom pointer past every test here. This is not
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


#: The two literals that bracket the guide's results summary -- the
#: paragraph tool_guide.html:149 renders from ``about.output_summary``,
#: and the one thing the guide-surface pointer names ("this guide's own
#: results summary above"). SUMMARY_OPENS is the section's ``<h2>``
#: (tool_guide.html:145); SUMMARY_CLOSES is the ``<p>`` that introduces
#: the shared glossary below it (:152), not a heading. Slicing between
#: them is what makes the check read the PAGE; asking
#: tools/<slug>/meta.py for the same string would pass whether or not the
#: guide renders it.
SUMMARY_OPENS = "How to read the results"
SUMMARY_CLOSES = "Where a tool reports them, the scores mean:"

#: A per-tool bar as a guide that states one actually writes it: the ipTM
#: token, then the figure, in one of the two shapes the 14 summaries use
#: -- an operator ("ipTM &ge; 0.75") or the inclusive English form ("an
#: ipTM of 0.75 or more"). ``&ge;`` and ``&gt;`` reach this unescaped by
#: _visible.
#:
#: THE FIGURE MUST BELONG TO ipTM. Only whitespace and at most one
#: connective word may stand between the ipTM token and the COMPARISON
#: -- or, in the operator-less branch, the figure itself. That covers
#: every stating summary: measured across all 14
#: ``about["output_summary"]`` values, the four operator forms (boltz2,
#: esmfold2-design, pxdesign, rfdiffusion) put a single space ahead of
#: their ``&gt;`` or ``&ge;``, and the three inclusive ones (af2,
#: bindcraft, colabfold) read " of " straight to the figure.
#: A wider window lets a NEIGHBOURING metric's bar stand in for
#: this one: under the ``[^.;]{0,40}`` window this pattern used to carry,
#: "binders with ipTM, pLDDT above 80" matched. Executed: that string
#: matches the old pattern and not this one, and standing as bindcraft's
#: whole summary -- its own ipTM sentence deleted, a pLDDT bar left
#: beside the token -- it fails this test, where the old pattern would
#: have reported bindcraft as stating a bar.
#:
#: SHAPE, not agreement -- no figure is captured and none is compared to
#: ``good``. An equality check would be a different and stricter test
#: than the pointer's promise -- tests/test_guide_bar_matches_legend.py
#: is where that half lives. "ipTM of 0.95 or more" satisfies this.
#:
#: ``\biptm\b``: EITHER boundary alone keeps
#: ``cdr_distogram_iptm_proxy >= 0.5`` in esmfold2-design's summary from
#: standing in for a bar on the ipTM column -- ``_`` is a word character,
#: so neither side is a boundary. Both are kept because either could be
#: edited away.
#:
#: The cost of a tight pattern is that it dictates phrasing. Executed
#: misses: "ipTM greater than 0.75", "ipTM of 0.75 and above", "ipTM
#: must be above 0.6", "ipTM, above 0.75" (comma -- deliberately, since
#: admitting one reopens the pLDDT hole above), "ipTM (multimer only)
#: above 0.6", "ipTM of 75% or more", and "0.75 or more on ipTM" with
#: the figure first. All fail LOUDLY, which is the safe direction -- a
#: false pass lets the defect back in silently.
#:
#: U+2265 via chr() rather than typed or escaped into the pattern: every
#: other byte in this file is ASCII.
GE = chr(0x2265)
#: Whitespace, optionally around one connective word. Deliberately admits
#: nothing that could carry another metric's name.
_NEAR = r"(?:\s+(?:of|is|at|score|value))?\s*"
#: The figure must be FRACTIONAL. ipTM runs 0 to 1, so every bar these
#: guides state is written with a decimal point -- read off all 14
#: summaries: 0.6, 0.65, 0.7, 0.75. A bare integer next to the
#: token is a COUNT, and the "or more" branch below carries no operator
#: to tell the two apart: while this was ``\d+(?:\.\d+)?``, "ipTM of 3 or
#: more designs" and "ipTM at least 1 in 5" both read as stated bars.
#: Executed both ways. Requiring the point also accepts a bare ".7".
_BAR = r"\d*\.\d+"
STATED_BAR = re.compile(
    r"\biptm\b" + _NEAR + r"(?:"
    + r"(?:>=|" + GE + r"|>|at or above|at least|above|over)\s*" + _BAR
    + r"|" + _BAR + r"\s+or\s+(?:more|better|higher)"
    + r")",
    re.I,
)


def _results_summary(body: str, path: str) -> str:
    """The visible text of one guide's results summary."""
    for anchor in (SUMMARY_OPENS, SUMMARY_CLOSES):
        assert body.count(anchor) == 1, (
            f"{path} carries {body.count(anchor)} copies of {anchor!r}; the "
            "slice below takes the first of each and would read the wrong "
            "span of the page"
        )
    start = body.index(SUMMARY_OPENS) + len(SUMMARY_OPENS)
    end = body.index(SUMMARY_CLOSES)
    assert start < end, (
        f"{path}: {SUMMARY_CLOSES!r} precedes {SUMMARY_OPENS!r}, so the "
        "summary is not between them any more"
    )
    text = _visible(body[start:end])
    # A slice that ran past SUMMARY_CLOSES would be reading the shared
    # glossary instead of this tool's summary, so `states` would stop
    # meaning what the offenders loop below takes it to mean. It would
    # not, on today's copy, flip any tool INTO `states`: ran STATED_BAR
    # over the glossary region reconstructed from tool_guide.html with
    # all three branches concatenated, and it matches nothing -- the band
    # ("0.65 to 0.75 depending on the tool") carries no comparison token
    # and no ipTM token sits within reach of one. So this fires on a
    # moved anchor, not
    # on a wrong verdict -- which is why the two bounds below are the
    # ones that guard the verdict itself.
    assert DEFINITION not in text, (
        f"{path}: the slice reached past the summary into the shared "
        f"glossary: {text}"
    )
    return text


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
    # assertion in every test that reads _entries.
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

    Exactly two tests below filter the 28 pages on the same predicate
    these groups use -- the defect test takes the tools without ``good``,
    the pointer test those with it -- so an empty group would make its
    test pass by having nothing to check. No other test here can be
    emptied that way: test_only_a_legend_without_a_bar_drops_the_general_band
    and test_the_no_shared_scale_wording_stays_with_the_tool_it_describes
    branch on the narrower "legend present but carrying no ``good``"
    (boltzgen alone) and assert on every page either way,
    test_the_pointer_pattern_matches_something uses neither,
    test_the_legend_lookup_folds_case_at_the_source takes no fixture, and
    test_the_pointer_is_only_made_where_the_guide_states_a_number splits
    the tools on their RENDERED summary rather than on these groups --
    and carries its own two non-vacuity bounds for that split.

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
    tests/test_public_tool_pages.py::test_every_general_legend_reads_the_glossary,
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


def test_the_pointer_is_only_made_where_the_guide_states_a_number(
    all_tools_app,
):
    """The pointer's promise, checked against the page it names.

    Every test above that consults a legend at all turns on whether the
    tool DECLARES a bar in shared/score_legends.py, because that is all
    the template can see; test_the_pointer_pattern_matches_something is
    the exception, reading rendered text only. The sentence promises
    something else: that the guide PRINTS one. The two sets were not the
    same. af2, bindcraft and colabfold declared 0.6, 0.75 and 0.6 and
    their guides printed no ipTM bar of their own anywhere -- they print
    plenty of other figures, runtimes and input ranges among them, and
    the shared 0.65-to-0.75 band sits in the same entry as the pointer,
    but none of those is the tool's own bar. So the reader who followed
    the pointer from either surface arrived at a page that did not answer
    it -- six of the 28.

    Read off the rendered summary rather than tools/<slug>/meta.py: the
    promise is about what the guide SHOWS, and a meta-module read would
    hold even if tool_guide.html stopped rendering the field.

    The direction is one-way here: a guide may state a bar with no
    pointer beside it and pass. NO TEST IN THIS FILE OWNS THAT CASE.
    test_a_tool_that_declares_a_bar_still_gets_the_pointer is the nearest
    and is not it -- it keys on whether the tool DECLARES a bar, so it
    covers a declaring tool that loses its pointer, not a legend-less
    tool whose summary prints a figure. That tool is required by
    test_no_tool_is_pointed_at_a_pass_bar_it_does_not_declare to carry no
    pointer, which is the honest page; the stray figure goes unremarked.
    """
    flask_app, slugs = all_tools_app
    client = flask_app.test_client()
    summaries = {}
    for slug in slugs:
        path = f"/help/tools/{slug}"
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        summaries[slug] = _results_summary(resp.get_data(as_text=True), path)

    states = {s for s, text in summaries.items() if STATED_BAR.search(text)}
    # Both bounds, because either one alone leaves a broken reader
    # looking like a clean result. An extractor returning "" for every
    # guide empties `states` and the offenders loop would then flag all
    # 14; a pattern loose enough to match something every summary says
    # fills it and the loop would flag none. (An over-running SLICE is a
    # different failure and lands on neither bound -- see the assert in
    # _results_summary.) Re-measured 2026-09-24 by reading all 14
    # ``about["output_summary"]`` values: 7 state a bar, 7 do not.
    assert states, (
        "no guide states an ipTM bar; either STATED_BAR stopped matching "
        f"or the summaries came back empty: {summaries}"
    )
    assert set(slugs) - states, (
        "every guide reads as stating an ipTM bar, including the tools "
        "with no ipTM legend at all -- STATED_BAR is matching something "
        f"the page says about every tool: {sorted(states)}"
    )

    offenders = {}
    for (slug, surface), text in _entries(all_tools_app).items():
        if ANY_POINTER.search(text) and slug not in states:
            offenders[surface.format(slug=slug)] = summaries[slug]
    assert not offenders, (
        "these pages send the reader to the tool's guide for its own ipTM "
        "pass bar, but that guide's results summary states no figure "
        f"(summary shown): {offenders}"
    )
