"""The results table asks for a column by the name the PAGE gives it.

af2 and colabfold register ``iptm``/``ptm`` while their results pages
display ``ipTM``/``pTM``; all three of af2, colabfold and
esmfold used to register a pLDDT legend as ``plddt`` while their pages display
``mean_pLDDT``. The pages build each candidate's ``scores`` dict under the
displayed spelling and pass those as the table's ``columns``, and
``candidate_table.html`` resolved the header legend through
``score_legends_for``, an exact-case dict reader, so seven (tool, column)
pairs resolved to nothing on the page that shows the numbers those legends
judge.

#258 (c500bc0) folded CASE into ``get_legend`` and closed this for the about
panel and the tool guide. It left the results table open, and said so: the
table went through the other reader, and ``mean_pLDDT`` differs from ``plddt``
by more than case.

THE FIX IS TWO EDITS, AND NEITHER IS A WIDER FOLD.

  * The header now resolves through ``score_legend_for`` (``get_legend``),
    which folds case, instead of the exact-case reader. That alone reaches the
    four ``ipTM``/``pTM`` pairs.
  * The three pLDDT legends are re-keyed from ``plddt`` to ``mean_pLDDT``,
    which is what their pages pass. That reaches the other three, and it
    brings THAT key into line with the invariant stated above
    ``SCORE_LEGENDS`` -- that a legend's column_key matches the key in
    ``candidate.scores[...]``. af2 and colabfold still register
    ``iptm``/``ptm`` against a displayed ``ipTM``/``pTM``, so they are not
    brought into line with it; the case fold is what covers those.

A ``mean_``-stripping fold was written first and removed: the re-key is a
data change that reaches the page AND, by #258's existing case fold, the
``mean_plddt`` the containers store. Both are pinned below.

The re-key does cost one spelling, and the fold would not have. ``pLDDT``
-- the key ``_COLUMN_ALIASES`` is keyed on -- folds to ``plddt``, not to
``mean_plddt``, so it no longer resolves for these three. No results page
passes it to them today, from any of the column sources the check below
reads, and that check fails if one starts to; the note above the af2
legend records it where the keys are.

The four misses that must stay misses -- ``complex_pLDDT``, an unknown column,
an empty slug, a non-string key -- are pinned by
tests/test_about_panel_iptm_bar_default.py::test_the_legend_lookup_folds_case_at_the_source
and are not repeated here. The case-only pairs ARE re-asserted below. The
render test earns all four: it asserts on the rendered results table, which
that file never asserts on. In the pure-lookup
test the two ``ipTM`` rows duplicate that file's assertions, but
the two ``pTM`` rows do not -- that file carries no pTM assertion at all,
so they are the only pure-lookup cover the pTM fold has. Do not trim them
as redundant.

The two kinds of miss had different symptoms. ``ipTM`` and ``pTM`` still drew
the global ``metric_glossary`` band, so the header offered a cross-tool range
where the tool's own bar belonged; ``mean_pLDDT`` has no ``metric_glossary``
entry at all, so with neither legend nor glossary entry that header rendered
no tooltip whatsoever.
"""

from __future__ import annotations

import pathlib
import re
from html.parser import HTMLParser

import pytest
from jinja2 import Environment, FileSystemLoader

from shared import metric_glossary, pdb_bfactors, ranking, refold, score_legends
from shared.result_columns import columns_for, primary_metric_for
from shared.score_legends import SCORE_LEGENDS, get_legend

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "templates"

# (tool, the column its results page displays, the column its legend is
# registered under). Every row is a pair the table used to resolve to nothing.
# The displayed spellings are re-read off the templates themselves by
# ``test_those_are_the_columns_those_pages_actually_pass``, so this table
# cannot quietly drift away from the pages it claims to describe.
BROKEN_PAIRS = [
    ("af2", "mean_pLDDT", "mean_pLDDT"),
    ("af2", "ipTM", "iptm"),
    ("af2", "pTM", "ptm"),
    ("colabfold", "mean_pLDDT", "mean_pLDDT"),
    ("colabfold", "ipTM", "iptm"),
    ("colabfold", "pTM", "ptm"),
    ("esmfold", "mean_pLDDT", "mean_pLDDT"),
]

_IDS = [f"{t}-{s}" for t, s, _r in BROKEN_PAIRS]

_RESULTS_TEMPLATES = {
    "af2": "af2_results.html",
    "colabfold": "colabfold_results.html",
    "esmfold": "esmfold_results.html",
}

# What the containers persist for that pLDDT mean, which is neither the
# displayed spelling nor the old registered one. Named without line numbers,
# which drift: tools/<slug>/run_pipeline.py.
_STORED_PLDDT_KEY = "mean_plddt"


@pytest.mark.parametrize("tool,shown,registered", BROKEN_PAIRS, ids=_IDS)
def test_the_displayed_column_resolves_to_that_tools_own_legend(
    tool, shown, registered,
):
    """Identity, not truthiness: the page must reach the SAME legend object
    the tool registered, not merely some legend."""
    assert get_legend(tool, shown) is SCORE_LEGENDS[(tool, registered)]


@pytest.mark.parametrize("tool", sorted(_RESULTS_TEMPLATES))
def test_the_pLDDT_legend_is_registered_as_the_page_spells_it(tool):
    """The re-key itself, and the invariant it restores. These three
    registered ``plddt``, which their pages do not pass; the key must now be
    the displayed one."""
    assert (tool, "mean_pLDDT") in SCORE_LEGENDS
    assert (tool, "plddt") not in SCORE_LEGENDS


@pytest.mark.parametrize("tool", sorted(_RESULTS_TEMPLATES))
def test_the_stored_spelling_still_reaches_the_same_legend(tool):
    """The re-key must not strand the spelling the CONTAINER writes.

    It differs from the registered key only by case, so #258's fold
    carries it. This pins the FOLD, not the container: that the pipelines
    still write ``mean_plddt`` is asserted by nothing here, so a rename
    upstream would leave this green.
    """
    assert get_legend(tool, _STORED_PLDDT_KEY) is SCORE_LEGENDS[
        (tool, "mean_pLDDT")
    ]


def _columns_passed_by(tool: str) -> list[str]:
    """The column list ``tool``'s results template hands the macro.

    Exactly one ``set columns`` per template is required rather than assumed:
    a page that grows a second one builds its columns in branches, and no
    single list is then THE list. Failing loudly beats picking the first
    silently or unioning them into a set no render produces.
    """
    template = _RESULTS_TEMPLATES[tool]
    text = (_TEMPLATES / "tools" / template).read_text(encoding="utf-8")
    lists = re.findall(r"\{%\s*set\s+columns\s*=\s*\[([^\]]*)\]", text)
    assert len(lists) == 1, (
        f"{template}: expected one `set columns = [...]`, found {len(lists)}. "
        "If the page now builds columns in branches, this helper and the "
        "tests that use it need to say which branch they mean."
    )
    return re.findall(r"['\"]([^'\"]+)['\"]", lists[0])


def test_those_are_the_columns_those_pages_actually_pass():
    """The pairs above are only worth anything if those spellings are the ones
    the templates hand the macro. Re-read them rather than trust the list.

    It also guards the narrowing the re-key cost, at the column sources this
    test can reach: the per-tool template's own list; ``columns_for``, which
    feeds the campaign page and the single-cohort target table (pooled mode
    is handed [] instead, see shared/target_results.py); and
    ``primary_metric_for``, which names the pooled Score cell's metric.

    ONLY THE FIRST OF THE THREE CARRIES ANYTHING for these tools today:
    ``columns_for`` returns [] and ``primary_metric_for`` returns
    ``(None, 'desc')`` for all three. Those two legs are forward tripwires,
    and narrow ones: they fire only if a wiring passes a ``plddt``-cased
    name. Wired to the displayed spelling they stay quiet, correctly.

    A GATE COLUMN IS THE OTHER WAY a metric name reaches ``get_legend`` on
    this page, and this test does not read it. That path has its own guard:
    tests/test_derived_verdicts.py::test_every_gate_column_has_a_legend.
    """
    for tool in sorted(_RESULTS_TEMPLATES):
        passed = _columns_passed_by(tool)
        expected = [s for t, s, _r in BROKEN_PAIRS if t == tool]
        missing = [c for c in expected if c not in passed]
        assert not missing, (
            f"{_RESULTS_TEMPLATES[tool]} passes {passed}; this file claims it "
            f"displays {missing}, so the pairs it pins are not the real ones"
        )
        # These legends are keyed ``mean_pLDDT``, so ANY casing of
        # ``plddt`` -- which is what ``pLDDT`` folds to -- reaches no legend
        # for these three. The header would then show the global
        # metric_glossary band ALONE, which is the fallback this change
        # replaced for ipTM and pTM (the band still follows the legend
        # there; what went away is it standing on its own). For a casing
        # the glossary does not carry either, there is no tooltip at all.
        primary, _direction = primary_metric_for(tool)
        for source, cols in (("its template", passed),
                             ("result_columns", columns_for(tool)),
                             ("primary_metric_for", [primary] if primary else [])):
            stray = [c for c in cols if c.lower() == "plddt"]
            assert not stray, (
                f"{tool}: {source} now passes {stray}, which resolves to no "
                "legend for this tool -- re-key the legend to that spelling, "
                "or register a second SCORE_LEGENDS entry under it. An "
                "_COLUMN_ALIASES entry will NOT do it: get_legend never "
                "reads that map."
            )


def test_every_registered_legend_answers_to_its_own_key():
    """Broader than it looks: it runs every legend through ``get_legend``.
    Several other tests iterate the whole dict -- the case-variant test
    below is one -- but they read the mapping directly, so a defect in the
    LOOKUP is invisible to them. No count here: that set moves.

    It was deleted once as tautological, on the grounds that ``get_legend``
    tries the exact key first. That holds for the body as written and not
    for the function: prepend one per-column rewrite to it -- mapping
    ``RMSD`` to ``refolding_rmsd``, say -- and rfdiffusion's and
    bindcraft's RMSD legends stop answering to their own name while every
    other test in this file and in
    tests/test_about_panel_iptm_bar_default.py stays green.
    """
    for (tool, column), legend in SCORE_LEGENDS.items():
        assert get_legend(tool, column) is legend, (tool, column)


def test_no_tool_declares_two_case_variants_of_one_column():
    """``get_legend`` resolves to the FIRST case-insensitive match in
    SCORE_LEGENDS order, so a tool declaring two spellings of one column would
    hand one of them the other's bar, silently and by insertion order. This is
    the claim that function's docstring cites."""
    seen: dict[tuple[str, str], str] = {}
    clashes = []
    for tool, column in SCORE_LEGENDS:
        key = (tool, column.lower())
        if key in seen and seen[key] != column:
            clashes.append((tool, seen[key], column))
        seen[key] = column
    assert not clashes, f"one tool, two spellings of one column: {clashes}"



# ── the surface the defect was on ────────────────────────────────────


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["pdb_b64_on_100"] = pdb_bfactors.bfactors_on_100_b64
    env.globals["metric_glossary"] = metric_glossary.GLOSSARY
    env.globals["format_metric_value"] = metric_glossary.format_value
    env.globals["score_legend_for"] = score_legends.get_legend
    env.globals["raw_metric"] = score_legends.raw_metric
    env.globals["judge_design"] = score_legends.judge
    env.globals["verdict_text"] = score_legends.verdict_text
    env.globals["plddt_on_100"] = metric_glossary.plddt_on_100
    # Registered by app.py too. Without it the macro's ``col in PLDDT_COLUMNS``
    # test reads an Undefined as False and silently skips the 0-100 rescale,
    # so the rendered cell would differ from production for exactly the column
    # this file is about.
    env.globals["PLDDT_COLUMNS"] = metric_glossary.PLDDT_COLUMNS
    env.globals["gate_bar_text"] = score_legends.gate_bar_text
    env.globals["tool_has_bar"] = score_legends.tool_has_bar
    env.globals["shortfall_bar_text"] = score_legends.shortfall_bar_text
    env.globals["legend_text"] = score_legends.legend_text
    env.globals["score_era_caveat"] = score_legends.score_era_caveat
    env.globals["ordinal"] = ranking.ordinal
    env.globals["is_refold_source"] = refold.SOURCE_TOOLS.__contains__
    env.globals["csrf_input"] = lambda: ""
    env.globals["url_for"] = lambda _e, **kw: "/static/" + kw.get("filename", "")
    return env


class _Headers(HTMLParser):
    """``th[data-col]`` attributes, plus the tooltip span inside each.

    The span's ``data-tooltip`` is collected under ``_tooltip``. It and
    ``title`` are gated differently in the macro -- ``title`` on the legend,
    the span on the legend OR a metric_glossary entry -- so they do not
    always carry the same text, and a column can have one without the
    other (esmfold's ``pTM`` has the span and no title). For the columns
    this file asserts on, dropping either one fails a row below.
    """

    def __init__(self):
        super().__init__()
        self.by_col: dict[str, dict] = {}
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrib = dict(attrs)
        if tag == "th":
            self._current = attrib.get("data-col")
            if self._current:
                self.by_col[self._current] = attrib
        elif tag == "span" and self._current and "data-tooltip" in attrib:
            self.by_col[self._current]["_tooltip"] = attrib["data-tooltip"]

    def handle_endtag(self, tag):
        if tag == "th":
            self._current = None


def _render(tool: str, columns: list[str]) -> str:
    tmpl = _env().from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug) }}"
    )
    return tmpl.render(
        candidates=[{
            "scores": {c: 0.8 for c in columns},
            "pdb_key": "designs/d.pdb",
            "_source_index": 0,
        }],
        columns=columns,
        job_id="job-1",
        tool_slug=tool,
    )


@pytest.mark.parametrize("tool,shown,registered", BROKEN_PAIRS, ids=_IDS)
def test_the_results_table_heads_the_column_with_that_tools_own_legend(
    tool, shown, registered,
):
    """The page, not the lookup. Rendered with the columns that tool's own
    results template passes -- harvested from the template, not from the pair
    list, so the table is rendered the width the page renders it -- each
    header must carry its tool's legend as the title. af2's ``ipTM`` header
    did not (it offered the global band instead) and its ``mean_pLDDT`` header
    could not (no legend and no glossary entry means no tooltip at all)."""
    parser = _Headers()
    parser.feed(_render(tool, _columns_passed_by(tool)))
    header = parser.by_col.get(shown)
    assert header is not None, (
        f"{tool}: no th[data-col={shown}] rendered at all; "
        f"got {sorted(parser.by_col)}"
    )
    expected = score_legends.legend_text(SCORE_LEGENDS[(tool, registered)])
    assert header.get("title") == expected
    # Pin the styled tooltip too, not the title alone: the two are gated
    # separately, so dropping the span leaves the assertion above green.
    # Where the column has a metric_glossary
    # entry the legend leads and the glossary follows it; where it has
    # none -- ``mean_pLDDT`` -- the tooltip IS the legend. startswith
    # covers both. (That the span renders AT ALL is also asserted by
    # tests/test_candidate_table_js_contract.py; what is pinned here is
    # that this column's legend is what leads it.)
    tooltip = header.get("_tooltip")
    assert tooltip is not None, (
        f"{tool}/{shown}: the header carries the legend as its title but "
        "renders no .mtt tooltip span beside it"
    )
    assert tooltip.startswith(expected), (tool, shown, tooltip[:120])
