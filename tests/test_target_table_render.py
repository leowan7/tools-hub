"""The candidate_table macro in multi-tool mode, rendered and parsed.

Every assertion here reads the RENDERED HTML rather than the template source.
A source-substring assertion cannot tell which branch it matched, and this
repo has previously shipped a test named for one property while asserting
another. So the table is rendered through Jinja, parsed with html.parser, and
the resulting cells are read.

Rows are produced by ``shared.ranking.rank_candidates`` rather than hand-built,
so the annotations under test are the ones the real pipeline stamps. A test
built on hand-written ``_rank_percentile`` keys would keep passing if the
ranking layer renamed them.

Rendered through a bare Jinja environment, not ``create_app()``: the macro
needs four globals and nothing else, and booting the app would drag in the
production credentials this suite must never reach.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from shared import metric_glossary, ranking, score_legends
from shared import pdb_bfactors

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["pdb_b64_on_100"] = pdb_bfactors.bfactors_on_100_b64
    env.globals["metric_glossary"] = metric_glossary.GLOSSARY
    env.globals["score_legends_for"] = score_legends.score_legends_for
    env.globals["format_metric_value"] = metric_glossary.format_value
    env.globals["score_legend_for"] = score_legends.get_legend
    env.globals["judge_design"] = score_legends.judge
    env.globals["verdict_text"] = score_legends.verdict_text
    env.globals["plddt_on_100"] = metric_glossary.plddt_on_100
    env.globals["gate_bar_text"] = score_legends.gate_bar_text
    env.globals["shortfall_bar_text"] = score_legends.shortfall_bar_text
    env.globals["tool_has_bar"] = score_legends.tool_has_bar
    env.globals["raw_metric"] = score_legends.raw_metric
    env.globals["legend_text"] = score_legends.legend_text
    env.globals["score_era_caveat"] = score_legends.score_era_caveat
    # The REAL function, not a stub. A stub here would let the percentile cell
    # be tested against this file's idea of an ordinal rather than production's.
    env.globals["ordinal"] = ranking.ordinal
    env.globals["csrf_input"] = lambda: ""
    # The macro closes with two <script src="{{ url_for('static', ...) }}">
    # tags. A bare environment has no Flask app to resolve them, and the tests
    # here care about the table, not the asset paths.
    env.globals["url_for"] = lambda _endpoint, **kw: "/static/" + kw.get("filename", "")
    return env


def _render(**kwargs) -> str:
    params = dict(
        candidates=[], columns=[], job_id="", tool_slug="",
        clone_url="", campaign_id="", target_id="", multi_tool=False,
        sort_mode="", split_tools=(), per_tool={},
    )
    params.update(kwargs)
    tmpl = _env().from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "                   campaign_id, target_id, multi_tool, sort_mode,"
        "                   split_tools, per_tool) }}"
    )
    return tmpl.render(**params)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class _Table(HTMLParser):
    """Collects the first table's header cells and its CANDIDATE rows.

    ``rows`` holds only candidate rows: a row's cells are the text of each
    ``td``. Two other row kinds are pulled out rather than counted, because
    every positional assertion below indexes ``rows`` against the list of
    candidate dicts that produced it, and a non-candidate row in that list
    silently shifts the mapping:

    ``viewer_colspans``
        the colspan of every viewer row's single cell, which is what the
        header-count assertion compares against.
    ``group_rows``
        the text of every ``cand-group-row`` header, in render order. These
        appear only under ``sort_mode='tool'`` (register item A82).
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.header_cells: list[dict] = []
        self.header_text: list[str] = []
        self.rows: list[list[str]] = []
        self.viewer_colspans: list[int] = []
        self.group_rows: list[str] = []
        self.group_colspans: list[int] = []
        self._in_thead = False
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._row_is_viewer = False
        self._row_is_group = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row = []
            self._row_is_viewer = "viewer-row" in (a.get("class") or "")
            self._row_is_group = "cand-group-row" in (a.get("class") or "")
        elif tag in ("th", "td"):
            self._cell = []
            if tag == "th" and self._in_thead:
                self.header_cells.append(a)
            if tag == "td" and self._row_is_viewer and a.get("colspan"):
                self.viewer_colspans.append(int(a["colspan"]))
            if tag == "td" and self._row_is_group and a.get("colspan"):
                self.group_colspans.append(int(a["colspan"]))

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_thead = False
        elif tag in ("th", "td") and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            if tag == "th" and self._in_thead:
                self.header_text.append(text)
            elif self._row is not None:
                self._row.append(text)
            self._cell = None
        elif tag == "tr":
            if self._row and self._row_is_group:
                self.group_rows.append(" ".join(self._row))
            elif self._row and not self._row_is_viewer:
                self.rows.append(self._row)
            self._row = None
            self._row_is_viewer = False
            self._row_is_group = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _parse(html: str) -> _Table:
    p = _Table()
    p.feed(html)
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# An independent, LITERAL enumeration of correct English ordinals over the
# reachable percentile domain (rank_statistics clamps to 0..99).
#
# Deliberately not built from ranking.ordinal_suffix. The first version of the
# two percentile tests below derived their expectation from the function under
# test, so mutating that function moved the expectation with it and both stayed
# green while the helper was broken. Membership sets rather than a modulo rule,
# so this shares no logic with the implementation it checks.
_ST = {1, 21, 31, 41, 51, 61, 71, 81, 91}
_ND = {2, 22, 32, 42, 52, 62, 72, 82, 92}
_RD = {3, 23, 33, 43, 53, 63, 73, 83, 93}


def _english_ordinal(n) -> str:
    n = int(n)
    if n in _ST:
        return f"{n}st"
    if n in _ND:
        return f"{n}nd"
    if n in _RD:
        return f"{n}rd"
    return f"{n}th"


def _row(tool, metric, value, *, job, index, preset="pilot", pdb=True):
    row = {
        "_source_tool": tool, "_source_preset": preset,
        "_source_job_id": job, "_source_index": index,
        "_source_campaign_id": "c-" + tool,
        "scores": {metric: value},
    }
    if pdb:
        row["pdb_key"] = f"designs/design_{index}.pdb"
    return row


def _two_tool_rows(n=25):
    """Two tools whose metrics are on incompatible scales and directions.

    bindcraft ipTM is higher-is-better on 0..1; rfantibody ipAE is
    lower-is-better in Angstrom. Cohorts are >= MIN_PERCENTILE_COHORT so the
    percentile is not suppressed.
    """
    bc = [_row("bindcraft", "ipTM", 0.91 - 0.002 * i, job="job-bc01", index=i)
          for i in range(n)]
    ra = [_row("rfantibody", "ipAE", 3.7 + 0.05 * i, job="job-ra01", index=i)
          for i in range(n)]
    return bc + ra


def _multi_tool_table(limit=None):
    ranked = ranking.rank_candidates(_two_tool_rows(), limit=limit)
    return _render(
        candidates=ranked["rows"], columns=[], job_id="", tool_slug="",
        target_id="t-abc12345", multi_tool=True,
    )


# ---------------------------------------------------------------------------
# The JS sorter must not bind in multi-tool mode
# ---------------------------------------------------------------------------

def test_no_multi_tool_header_carries_data_col():
    """This is what actually disables the client-side sorter.

    static/js/candidate_table.js:180 binds its click handler to `th[data-col]`,
    NOT to `.sortable-col` (which is only hover CSS). Asserting on the class
    would pass while the handler still bound and a user could still sort ipTM
    0.91 against ipAE 3.70 in the browser, which is the one comparison this
    table exists to prevent. So the assertion is on the attribute.
    """
    table = _parse(_multi_tool_table())

    assert table.header_cells, "no header cells parsed"
    with_data_col = [c for c in table.header_cells if "data-col" in c]
    assert with_data_col == [], f"sortable headers survived: {with_data_col}"


def test_single_tool_headers_keep_data_col_so_sorting_still_works():
    """The pair to the above. Without it, deleting data-col everywhere would
    satisfy the multi-tool assertion while silently breaking every existing
    single-tool results page."""
    rows = [{"scores": {"ipTM": 0.9, "pLDDT": 88.0}, "pdb_key": "d.pdb"}]
    table = _parse(_render(
        candidates=rows, columns=["ipTM", "pLDDT"],
        job_id="job-1", tool_slug="bindcraft",
    ))

    cols = [c["data-col"] for c in table.header_cells if "data-col" in c]
    assert cols == ["ipTM", "pLDDT"]


# ---------------------------------------------------------------------------
# Columns and layout
# ---------------------------------------------------------------------------

def test_multi_tool_headers_are_the_seven_fixed_columns():
    table = _parse(_multi_tool_table())
    assert table.header_text == ["#", "★", "Tool", "Score", "Pctile", "3D", "PDB"]


def test_single_tool_renders_its_own_metric_columns():
    rows = [{"scores": {"ipTM": 0.9, "pLDDT": 88.0}, "pdb_key": "d.pdb"}]
    table = _parse(_render(
        candidates=rows, columns=["ipTM", "pLDDT"],
        job_id="job-1", tool_slug="bindcraft",
    ))
    assert "Tool" not in table.header_text
    assert "Pctile" not in table.header_text
    assert table.header_text[:2] == ["#", "★"]


def test_viewer_row_spans_exactly_the_header_count():
    """Parsed, never hard-coded to 7: a hard-coded number would agree with a
    wrong colspan if both were wrong in the same way."""
    table = _parse(_multi_tool_table())

    assert table.viewer_colspans, "no viewer rows rendered"
    assert set(table.viewer_colspans) == {len(table.header_cells)}


# ---------------------------------------------------------------------------
# The Score cell
# ---------------------------------------------------------------------------

def test_each_row_shows_its_own_tools_metric_with_that_metrics_name():
    """The heart of the design: two rows, two different metrics, in one table,
    each labelled so neither reads as comparable with the other."""
    table = _parse(_multi_tool_table())
    scores = {r[0 + 2] for r in table.rows}   # column index 2 is Tool... see below

    # Column order is #, star, Tool, Score, Pctile, 3D, PDB.
    by_tool = {}
    for cells in table.rows:
        by_tool.setdefault(cells[2], []).append(cells[3])

    assert set(by_tool) == {"bindcraft", "rfantibody"}
    assert any(s.startswith("ipTM 0.9") for s in by_tool["bindcraft"]), by_tool["bindcraft"][:3]
    assert any(s.startswith("ipAE (Å) 3.7") for s in by_tool["rfantibody"]), by_tool["rfantibody"][:3]
    # Neither tool's label leaks onto the other's rows.
    assert not any("ipAE" in s for s in by_tool["bindcraft"])
    assert not any("ipTM" in s for s in by_tool["rfantibody"])
    assert scores  # silence the unused binding without weakening anything above


def test_the_score_tooltip_comes_from_the_rows_own_tool():
    """Two tools, the SAME metric name, different pass bars.

    shared/score_legends.py puts bindcraft's ipTM good bar at 0.75 and
    rfdiffusion's at 0.65. A header-level legend (which is what the single-tool
    branch uses) would state one of those over the other tool's numbers. This is
    why multi-tool resolves the legend per cell instead, and this test is the
    only thing standing between that and a plausible-looking regression.
    """
    bar = {t: score_legends.get_legend(t, "ipTM")["good"]
           for t in ("bindcraft", "rfdiffusion")}
    assert bar["bindcraft"] != bar["rfdiffusion"], (
        "premise gone: the two tools now share an ipTM bar, so this test can no "
        "longer detect a header-level legend"
    )

    rows = ([_row("bindcraft", "ipTM", 0.90 - 0.002 * i, job="j-bc", index=i)
             for i in range(25)]
            + [_row("rfdiffusion", "ipTM", 0.80 - 0.002 * i, job="j-rd", index=i)
               for i in range(25)])
    ranked = ranking.rank_candidates(rows, limit=None)
    html = _render(candidates=ranked["rows"], columns=[], job_id="",
                   tool_slug="", target_id="t-1", multi_tool=True)

    for tool in ("bindcraft", "rfdiffusion"):
        explanation = score_legends.get_legend(tool, "ipTM")["explanation"]
        assert explanation in html, f"{tool}'s own ipTM legend is missing"


class _Titles(HTMLParser):
    """Every ``title`` and ``data-tooltip`` value on the page.

    Read through the parser rather than by substring, because HTMLParser
    unescapes attribute values — so the assertions see the string the user's
    browser shows, apostrophes and all, rather than ``&#39;``.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("title", "data-tooltip") and value:
                self.values.append(value)


def _tooltips(html: str) -> list[str]:
    parser = _Titles()
    parser.feed(html)
    return parser.values


def test_a_legend_caveat_reaches_both_of_this_macros_tooltip_surfaces():
    """The macro shows a legend in two places, and they are easy to split.

    A legend may carry an optional ``caveat`` — the half that is about what an
    OLD STORED result holds rather than about the metric — and this table is
    the only surface in the app that renders old stored results. It renders
    them two ways: the single-tool column header, and the multi-tool per-row
    Score cell, which resolves the legend from THAT ROW'S tool.

    The reason this is a test and not a comment: the caveat used to live
    inside ``explanation``, which meant every consumer carried it including
    one it was false in — the job-completion email, see
    tests/test_job_complete_email_caption.py. Splitting it out fixed the email
    and created exactly one new way to be wrong: rendering it on one of these
    two surfaces and not the other. Both go through a single ``legend_text``
    global, and both are asserted here, so they cannot drift apart.
    """
    caveat = score_legends.get_legend("boltzgen", "ipTM").get("caveat")
    assert caveat, (
        "boltzgen's ipTM legend no longer carries a caveat, so this test "
        "guards nothing; re-point it rather than leave it passing"
    )

    single = _render(
        candidates=[{"pdb_key": "designs/design_0.pdb", "sequence": "MKTAY",
                     "scores": {"ipTM": 0.91}}],
        columns=["ipTM"], job_id="j-1", tool_slug="boltzgen",
    )
    assert any(caveat in t for t in _tooltips(single)), (
        f"the single-tool column header shows the boltzgen ipTM legend "
        f"without its caveat: {_tooltips(single)!r}"
    )

    rows = ranking.rank_candidates(
        [_row("boltzgen", "ipTM", 0.90 - 0.002 * i, job="j-bg", index=i)
         for i in range(25)],
        limit=None,
    )["rows"]
    multi = _render(candidates=rows, columns=[], job_id="", tool_slug="",
                    target_id="t-1", multi_tool=True)
    assert any(caveat in t for t in _tooltips(multi)), (
        "the multi-tool Score cell shows the boltzgen ipTM legend without "
        "its caveat — this is the pooled target page, where a pre-deploy "
        "boltzgen run gets no banner and the tooltip is the only warning left"
    )


def test_percentile_column_renders_a_percentile():
    """This test used to read ``all(p.endswith("th"))``, and it was green.

    It was green because the template hardcoded "th", so the assertion pinned
    the defect rather than the property: 27 of the 100 reachable percentiles
    need st/nd/rd, and "93th" was on screen. Asserting a shape the code happens
    to have is not the same as asserting the shape it should have.
    """
    table = _parse(_multi_tool_table())
    pctiles = [cells[4] for cells in table.rows]
    assert pctiles, "no rows parsed"
    for p in pctiles:
        number = p.rstrip("stndrdth")
        assert p == _english_ordinal(number), p


def test_a_small_cohort_shows_a_rank_instead_of_a_percentile():
    """Under MIN_PERCENTILE_COHORT the cell must not print a number implying
    precision the sample does not support."""
    rows = [_row("bindcraft", "ipTM", 0.9 - 0.01 * i, job="j", index=i)
            for i in range(3)]
    ranked = ranking.rank_candidates(rows, limit=None)
    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1", multi_tool=True))

    pctiles = [cells[4] for cells in table.rows]
    assert pctiles == ["1 of 3", "2 of 3", "3 of 3"], pctiles


# ---------------------------------------------------------------------------
# Rank column
# ---------------------------------------------------------------------------

def test_rank_column_is_the_global_index_not_the_tool_local_rank():
    """Every tool emits a rank 1. Rendering cand.rank would give 1,1,2,2,...
    down a table whose entire claim is that it is ONE ranking."""
    rows = _two_tool_rows()
    for r in rows:                      # every row claims to be its tool's best
        r["rank"] = 1
    ranked = ranking.rank_candidates(rows, limit=None)
    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1", multi_tool=True))

    ranks = [cells[0].split()[0] for cells in table.rows]
    assert ranks[:5] == ["1", "2", "3", "4", "5"], ranks[:5]
    assert ranks == [str(i + 1) for i in range(len(ranks))]


def test_the_subjob_chip_names_its_tool():
    """Chunk 0 exists in every campaign, so a bare "#0" appears once per tool
    meaning something different each time."""
    rows = [_row("bindcraft", "ipTM", 0.9, job="job-bc01", index=0),
            _row("boltzgen", "ipTM", 0.8, job="job-bz01", index=0)]
    for r in rows:
        r["_source_chunk"] = 0
    ranked = ranking.rank_candidates(rows, limit=None)
    html = _render(candidates=ranked["rows"], columns=[], job_id="",
                   tool_slug="", target_id="t-1", multi_tool=True)

    assert "bindcraft #0" in html
    assert "boltzgen #0" in html


# ---------------------------------------------------------------------------
# The shortlist bar
# ---------------------------------------------------------------------------

def test_target_mode_shows_the_shortlist_button():
    """Phase 5.3. The bar was hidden here because the modal's hidden inputs
    branched on campaign_id and fell back to source_job_id, and in target mode
    both were empty, so the POST would have named no parent. Migration 0040 and
    the source_target_id branch give it one, so the button ships."""
    html = _multi_tool_table()
    assert "Send shortlist to Ranomics lab" in html


def test_campaign_mode_still_shows_the_shortlist_button():
    """The pair. Without it, hiding the bar unconditionally would pass the
    assertion above while removing a working feature from every run page."""
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb", "_source_job_id": "j1"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", campaign_id="c-1")
    assert "Send shortlist to Ranomics lab" in html


def test_stars_survive_in_target_mode():
    """Only the submit button goes. The selection mechanic stays, because its
    refs are already globally unique and Phase 5.3 needs them."""
    html = _multi_tool_table()
    assert "star-btn" in html
    assert "data-ref-idx" in html


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def test_target_mode_points_exports_at_the_target():
    html = _multi_tool_table()
    for fmt in ("csv", "fasta", "zip"):
        assert f"/targets/t-abc12345/export.{fmt}" in html
    assert "/campaigns/" not in html


def test_campaign_mode_export_base_is_unchanged():
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", campaign_id="c-9")
    assert "/campaigns/c-9/export.csv" in html
    assert "/targets/" not in html


class _Forms(HTMLParser):
    """Every ``<form>``'s action plus its hidden inputs as ``{name: value}``.

    Parsed, not grepped: the assertions below are about which parent field a
    given form carries, and a substring search over the whole page cannot tell
    the lab-submit form from the starred-export form sitting beside it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, dict]] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._current = {}
            self.forms.append((a.get("action") or "", self._current))
        elif tag == "input" and self._current is not None:
            if a.get("name"):
                self._current[a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._current = None


def _form_for(html: str, action_contains: str) -> dict:
    p = _Forms()
    p.feed(html)
    for action, fields in p.forms:
        if action_contains in action:
            return fields
    raise AssertionError(
        f"no form whose action contains {action_contains!r}; "
        f"saw {[a for a, _ in p.forms]}"
    )


def test_target_mode_posts_a_source_target_id_and_no_source_job_id():
    """THE Phase 5.3 wiring, and the reason the bar could not ship before it.

    ``campaigns_submit`` dispatches on which parent field is present. A target
    page that posted ``source_job_id=""`` fell through to the legacy single-job
    branch, which reads ``candidate_indices`` (empty here) and would have
    created nothing while looking like it worked. The form must name the
    target and carry refs.
    """
    fields = _form_for(_multi_tool_table(), "/lab-projects/submit")
    assert fields.get("source_target_id") == "t-abc12345"
    assert "candidate_refs" in fields
    assert "source_job_id" not in fields
    assert "source_campaign_id" not in fields
    assert "candidate_indices" not in fields


def test_campaign_mode_still_posts_a_source_campaign_id():
    """The pair. Adding the target branch above the campaign one must not
    capture the campaign page, whose refs are scoped by a different parentage
    test on the server.

    THE TWO ABSENCES ARE NOT SYMMETRY WITH THE TARGET SIBLING, they are load
    bearing since A91. `openCampaignModal` decides whether to label a candidate
    "· sub-job <id>" from `!!modal.querySelector('[name="source_job_id"]')`,
    because A91 gave job mode a `candidate_refs` input and `refsInput` stopped
    identifying scope. So a campaign modal that emitted `source_job_id` would
    read as single-job and drop the disambiguator from the ONE table that
    interleaves rows from several jobs and needs it. `candidate_indices`
    alongside would put a second payload on an arm whose server branch never
    reads one. Both were mutations that survived a 416-test run; the target-mode
    sibling already asserted both absences, campaign mode did not, and campaign
    mode is where they landed."""
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb", "_source_job_id": "j1"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", campaign_id="c-1")
    fields = _form_for(html, "/lab-projects/submit")
    assert fields.get("source_campaign_id") == "c-1"
    assert "source_target_id" not in fields
    assert "source_job_id" not in fields
    assert "candidate_indices" not in fields


def test_single_job_mode_posts_both_shortlist_fields():
    """The third arm, and the one payload contract A91 changed.

    This form used to be the only one carrying ``candidate_indices`` and the
    only one carrying no ``candidate_refs``; the second half of that is what
    this test asserted, and it is now false on purpose. The job branch posts
    BOTH. ``candidate_indices`` stays because it is the only shape a page served
    before the change carries, so a tab left open across the deploy still
    submits a shortlist; ``candidate_refs`` is added because it is the shape the
    other two arms take, it names a source job per entry rather than assuming
    one, and ``campaigns_submit`` prefers it whenever it parses to anything.

    ``candidate_indices`` is still the ONLY field this form has that the ref
    forms do not, which is why the branch cannot simply be deleted yet.

    Exactly one PARENT field per render is unchanged, and the two negative
    assertions are what hold it: the dispatcher tries ``source_target_id`` and
    then ``source_campaign_id`` first, so a job page carrying either would be
    routed to a parentage test built for a different parent.
    """
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="job-1",
                   tool_slug="bindcraft")
    fields = _form_for(html, "/lab-projects/submit")
    assert fields.get("source_job_id") == "job-1"
    assert "candidate_indices" in fields
    assert "candidate_refs" in fields
    assert "source_target_id" not in fields
    assert "source_campaign_id" not in fields


def test_campaign_mode_still_emits_the_lab_submit_form():
    """The pair. Removing the modal unconditionally satisfies the test above
    while deleting the shortlist feature from every campaign and job page."""
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb", "_source_job_id": "j1"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", campaign_id="c-1")
    assert "/lab-projects/submit" in html
    assert "campaign-modal-" in html


# ---------------------------------------------------------------------------
# The percentile ordinal, rendered
# ---------------------------------------------------------------------------

def test_the_percentile_cell_suffixes_every_rank_correctly():
    """The cell hardcoded "th", so "93th" rendered beside "97th" on the page.

    100 distinct values in one cohort put a row at every percentile from 0 to
    99, which is the whole reachable domain (rank_statistics clamps at 99), so
    this exercises every suffix class rather than sampling one.

    Read out of the RENDERED html rather than asserted against the helper: the
    helper has its own tests in tests/test_ranking.py, and the thing this test
    exists for is that the template calls it at all.
    """
    rows = [{
        "scores": {"ipTM": round(0.10 + i * 0.001, 4)},
        "pdb_key": f"d{i}.pdb",
        "_source_tool": "bindcraft", "_source_preset": "pilot",
        "_source_job_id": "j1", "_source_index": i,
    } for i in range(100)]
    ranked = ranking.rank_candidates(rows, limit=300,
                                     sort_mode=ranking.SORT_PERCENTILE)
    html = _render(candidates=ranked["rows"], columns=[], job_id="",
                   tool_slug="", target_id="t-1", multi_tool=True)

    # Pull every ordinal token out of the rendered page and compare the whole
    # set at once. Substring assertions are too weak here: "1st" in html is
    # satisfied by "21st" alone, so a cell that only ever emitted "21st" would
    # pass a bag of `in` checks.
    shown = set(re.findall(r"\b(\d{1,2}(?:st|nd|rd|th))\b", html))
    expected = {_english_ordinal(n) for n in range(100)}
    assert shown == expected, sorted(expected ^ shown)

    # And the defect itself, named explicitly so the diff records what shipped.
    # Word-anchored: a bare `"1th" not in html` is failed by the legitimate
    # "11th", which is the mistake the first draft of this test made.
    for wrong in ("93th", "1th", "2th", "3th", "21th", "22th", "23th"):
        assert not re.search(rf"\b{wrong}\b", html), wrong


def test_a_suppressed_percentile_still_renders_k_of_n_not_an_ordinal():
    """Below MIN_PERCENTILE_COHORT the cell shows position, not a percentile,
    so the ordinal must not appear at all. Pairs with the test above: a cell
    that always printed an ordinal would satisfy that one and break this."""
    rows = [{
        "scores": {"ipTM": 0.9 - i * 0.01},
        "pdb_key": f"d{i}.pdb",
        "_source_tool": "bindcraft", "_source_preset": "pilot",
        "_source_job_id": "j1", "_source_index": i,
    } for i in range(5)]
    ranked = ranking.rank_candidates(rows, limit=300,
                                     sort_mode=ranking.SORT_PERCENTILE)
    html = _render(candidates=ranked["rows"], columns=[], job_id="",
                   tool_slug="", target_id="t-1", multi_tool=True)

    assert "1 of 5" in html
    assert "too few for a meaningful percentile" in html


# ---------------------------------------------------------------------------
# Round 15 (independent split QC): the sort mode is a claim the page makes
#
# The sort toggle's own comment says "the export links have to carry the same
# mode for the file to match the screen", and the export docstring repeats it.
# The links carried nothing. The route-side test was even named
# test_the_sort_mode_is_forwarded_so_the_file_matches_the_screen and asserted
# only that the ROUTE reads ?sort when present, which was never in doubt.
# ---------------------------------------------------------------------------

def _export_hrefs(html):
    return re.findall(r'href="([^"]*/export\.[a-z]+[^"]*)"', html)


def test_the_export_links_carry_the_active_sort_mode():
    html = _render(candidates=[], columns=[], job_id="", tool_slug="",
                   target_id="t-abc12345", multi_tool=True, sort_mode="tool")
    hrefs = _export_hrefs(html)
    assert len(hrefs) == 3, hrefs
    assert all(h.endswith("?sort=tool") for h in hrefs), hrefs
    assert {h.split("?")[0].rsplit(".", 1)[1] for h in hrefs} == {"csv", "fasta", "zip"}


def test_no_sort_mode_means_no_query_string():
    """The pair. Appending an unconditional "?sort=" would satisfy the test
    above while sending an empty mode on every campaign and job page."""
    html = _render(candidates=[], columns=[], job_id="j1", tool_slug="bindcraft")
    hrefs = _export_hrefs(html)
    assert len(hrefs) == 3, hrefs
    assert all("?" not in h for h in hrefs), hrefs


# ---------------------------------------------------------------------------
# "Top" means top of the RANKING, not top of the current view
# ---------------------------------------------------------------------------

def _top_row_index(html):
    """Position (1-based) of the row carrying the Top badge, or None."""
    rows = _parse(html).rows
    for i, cells in enumerate(rows, 1):
        if "Top" in cells[0]:
            return i
    return None


def test_the_top_badge_marks_the_ranked_best_not_the_first_row_shown():
    """Under ?sort=tool the alphabetically-first tool leads the table, so
    `loop.first` is its best design rather than the target's best. The badge,
    its tooltip ("Top-ranked design across every tool run against this target")
    and the highlight rail all asserted otherwise, so bindcraft outranked
    rfdiffusion by spelling.

    rfdiffusion gets the larger cohort, so its best row's mid-rank fraction
    (1 - 0.5/40) beats bindcraft's (1 - 0.5/20) and it holds canonical rank 1
    while sorting second alphabetically.
    """
    rows = ([_row("bindcraft", "ipTM", 0.90 - 0.002 * i, job="job-bc", index=i)
             for i in range(20)]
            + [_row("rfdiffusion", "ipTM", 0.80 - 0.002 * i, job="job-rf", index=i)
               for i in range(40)])
    ranked = ranking.rank_candidates(rows, limit=None, sort_mode=ranking.SORT_TOOL)

    displayed = ranked["rows"]
    assert displayed[0]["_source_tool"] == "bindcraft", "fixture assumption"
    winner = [r for r in displayed if r.get("_rank_position") == 1]
    assert len(winner) == 1 and winner[0]["_source_tool"] == "rfdiffusion", (
        "fixture assumption: the canonical best must not be the first shown")

    html = _render(candidates=displayed, columns=[], job_id="", tool_slug="",
                   target_id="t-1", multi_tool=True, sort_mode="tool")
    badge_at = _top_row_index(html)
    assert badge_at is not None, "no Top badge rendered"

    # IDENTITY, not tool. An earlier version asserted only that the badged row
    # belonged to rfdiffusion, which holds canonical positions 1, 3 and 4 in
    # this fixture, so mutating the gate to `_rank_position == 3` or `== 4`
    # left it GREEN. Verified by mutation: with the tool assertion it caught
    # only `== 2`; with this one it catches every off-by-N.
    badged = displayed[badge_at - 1]
    assert (badged["_source_job_id"], badged["_source_index"]) == (
        winner[0]["_source_job_id"], winner[0]["_source_index"]
    ), (f"badge landed on display row {badge_at} "
        f"(_rank_position {badged.get('_rank_position')}), expected the row at "
        f"_rank_position 1")


def test_the_top_badge_is_on_row_one_under_the_default_sort():
    """The pair: anchoring on _rank_position must not move the badge in the
    mode where display order and rank order agree."""
    ranked = ranking.rank_candidates(_two_tool_rows(), limit=None,
                                     sort_mode=ranking.SORT_PERCENTILE)
    html = _render(candidates=ranked["rows"], columns=[], job_id="",
                   tool_slug="", target_id="t-1", multi_tool=True,
                   sort_mode="percentile")
    assert _top_row_index(html) == 1


# ---------------------------------------------------------------------------
# A pooled table numbers its rows globally, and single-tool is still pooled
# ---------------------------------------------------------------------------

def test_a_single_tool_target_table_numbers_rows_globally():
    """`cand.rank` is the SOURCE JOB's own rank, so it restarts at 1 for every
    sub-job and every campaign. One target run twice with bindcraft, two
    sub-jobs each, read 1,2,3,1,2,3,1,2,3,1,2,3 beside a CSV that ranked the
    same rows 1..12.

    The old condition was `multi_tool`, which understates pooling by exactly
    the case that reads worst: a single-tool target pools every run and sub-job
    of that tool.
    """
    cands = []
    for job in ("job-a", "job-b"):
        for i in range(3):
            c = _row("bindcraft", "ipTM", 0.9 - 0.01 * i, job=job, index=i)
            c["rank"] = i + 1          # what each sub-job's own results page shows
            cands.append(c)
    html = _render(candidates=cands, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", target_id="t-1", multi_tool=False)

    ranks = [cells[0].split()[0] for cells in _parse(html).rows]
    assert ranks == ["1", "2", "3", "4", "5", "6"], ranks


def test_a_genuine_single_job_table_still_shows_its_own_rank():
    """The pair. A job page pools nothing, so the rank the pipeline assigned is
    the right column and must survive."""
    cands = []
    for i in range(3):
        c = _row("bindcraft", "ipTM", 0.9 - 0.01 * i, job="job-a", index=i)
        c["rank"] = 90 + i
        cands.append(c)
    html = _render(candidates=cands, columns=["ipTM"], job_id="job-a",
                   tool_slug="bindcraft")

    ranks = [cells[0].split()[0] for cells in _parse(html).rows]
    assert ranks == ["90", "91", "92"], ranks


# ---------------------------------------------------------------------------
# Grouped by tool: the groups have to be VISIBLE (register item A82)
# ---------------------------------------------------------------------------

def _grouped_table(**kw):
    ranked = ranking.rank_candidates(_two_tool_rows(), limit=None,
                                     sort_mode=ranking.SORT_TOOL)
    params = dict(
        candidates=ranked["rows"], columns=[], job_id="", tool_slug="",
        target_id="t-1", multi_tool=True, sort_mode="tool",
        per_tool=ranked["tools"],
    )
    params.update(kw)
    return _parse(_render(**params))


def test_grouped_mode_draws_one_header_per_tool():
    """A82. `apply_sort_mode`'s SORT_TOOL branch only reordered the row list;
    the macro then rendered one flat tbody with no header, no separator and no
    subtotal, so the boundary between two tools was invisible and the only
    signal the mode had changed was that some rows moved."""
    table = _grouped_table()
    assert len(table.group_rows) == 2, table.group_rows
    assert table.group_rows[0].startswith("bindcraft")
    assert table.group_rows[1].startswith("rfantibody")


def test_a_group_header_carries_that_tools_counts():
    """The counts come from `agg.per_tool`, which the aggregate already builds
    and which nothing on this page read. 25 designs per tool, all shown, all
    carrying a resolvable metric."""
    table = _grouped_table()
    for line in table.group_rows:
        assert "25 of 25 shown" in line, line
        assert "25 ranked" in line, line


def test_a_group_header_spans_the_whole_table():
    """A short group header would leave the columns to its right looking like
    an empty data row."""
    table = _grouped_table()
    assert table.group_colspans == [len(table.header_text)] * 2, (
        table.group_colspans, table.header_text)


def test_the_row_number_restarts_inside_each_group():
    """A82's sharper half. `#` was `loop.index`, so it counted straight through
    the tool boundary (1,2,...,25,26,...) and actively argued the rows were one
    continuous cross-tool ranking -- the exact claim this table refuses to make
    everywhere else."""
    ranks = [cells[0].split()[0] for cells in _grouped_table().rows]
    assert ranks == [str(i + 1) for i in range(25)] * 2, ranks[:30]


def test_percentile_mode_keeps_one_global_numbering_and_no_group_headers():
    """The pair. Grouping must not leak into the default mode, whose numbering
    IS a single ranking and is correct as a running index."""
    ranked = ranking.rank_candidates(_two_tool_rows(), limit=None)
    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1", multi_tool=True,
                           sort_mode="percentile", per_tool=ranked["tools"]))
    assert table.group_rows == []
    ranks = [cells[0].split()[0] for cells in table.rows]
    assert ranks == [str(i + 1) for i in range(50)]


def test_a_one_tool_target_draws_no_group_header_under_sort_tool():
    """`apply_sort_mode` keys SORT_TOOL on `_source_tool` alone, so on a
    one-tool target it returns a byte-identical row list. A lone header over
    the whole table would announce a grouping that did not happen.

    One tool at ONE preset, so `multi_tool` is genuinely False here. The
    harder case, where the caller passes True, is the test below; this one
    would pass under a gate reading either flag and is kept only as its pair.
    """
    rows = [_row("bindcraft", "ipTM", 0.9 - 0.01 * i, job="job-a", index=i)
            for i in range(4)]
    ranked = ranking.rank_candidates(rows, limit=None,
                                     sort_mode=ranking.SORT_TOOL)
    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="bindcraft", target_id="t-1",
                           multi_tool=False, sort_mode="tool",
                           per_tool=ranked["tools"]))
    assert table.group_rows == []
    ranks = [cells[0].split()[0] for cells in table.rows]
    assert ranks == ["1", "2", "3", "4"], ranks


def test_one_tool_at_two_presets_draws_no_group_header_either():
    """ROUND 19 (B-1). The test above passes `multi_tool=False`, which is NOT
    what the caller passes for this shape. `multi_tool` means more than one
    COHORT, and one tool at two presets is two cohorts, so the aggregator
    sends True. The gate read `multi_tool`, so this rendered a LONE group
    header over rows `apply_sort_mode` had returned in percentile order --
    verbatim the failure the gate's own comment claimed to prevent, and the
    third recurrence of this misreading (A75, A77, now B-1).

    Reachable despite the hidden toggle: targets/detail.html gates the control
    on `agg.tools|length > 1`, but blueprints/targets.py reads `?sort` straight
    off the query string, so a pasted or bookmarked URL renders it.

    proteina because it is a tool the launch screen really does offer at more
    than one preset, and its `total_reward` is `-i_pAE` under protein_binder
    and an RF3 composite under ligand_binder, which is why the two presets are
    separate cohorts in the first place.
    """
    rows = ([_row("proteina", "total_reward", 12.0 - 0.1 * i,
                  job="job-p1", index=i, preset="protein_binder")
             for i in range(20)]
            + [_row("proteina", "total_reward", 9.0 - 0.1 * i,
                    job="job-p2", index=i, preset="ligand_binder")
               for i in range(20)])
    ranked = ranking.rank_candidates(rows, limit=None,
                                     sort_mode=ranking.SORT_TOOL)
    assert len({r["_cohort_preset"] for r in ranked["rows"]}) == 2, (
        "fixture assumption: two cohorts")
    assert list(ranked["tools"]) == ["proteina"], (
        "fixture assumption: exactly one tool")

    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1",
                           multi_tool=True, sort_mode="tool",
                           split_tools=["proteina"], per_tool=ranked["tools"]))
    assert table.group_rows == [], table.group_rows
    ranks = [cells[0].split()[0] for cells in table.rows]
    assert ranks == [str(i + 1) for i in range(40)], ranks[:6]


def test_two_tools_differing_only_in_case_still_draw_their_boundary():
    """ROUND 20 (A82 again). The gate counted distinct tools with a bare
    ``unique``, whose Jinja default is case-INSENSITIVE, while the group loop
    compares raw strings with ``!=`` and ``apply_sort_mode`` sorts
    case-SENSITIVELY. ``BindCraft`` beside ``bindcraft`` therefore gave the gate
    1 and the loop 2: the rows really were reordered into two blocks and no
    boundary was drawn between them, which is the exact failure the gate's own
    comment claimed it had made impossible.

    Two slugs differing only in case is not a shape this product ships today,
    and that is beside the point. The comment's claim is that the gate and the
    loop CANNOT disagree; one reachable example of them disagreeing is what
    makes the claim false.
    """
    rows = ([_row("BindCraft", "ipTM", 0.90 - 0.002 * i, job="job-a", index=i)
             for i in range(25)]
            + [_row("bindcraft", "ipTM", 0.80 - 0.002 * i, job="job-b", index=i)
               for i in range(25)])
    ranked = ranking.rank_candidates(rows, limit=None,
                                     sort_mode=ranking.SORT_TOOL)

    shown = [r["_source_tool"] for r in ranked["rows"]]
    assert shown[0] == "BindCraft" and shown[-1] == "bindcraft", (
        "fixture assumption: the sort really does separate the two blocks")
    assert sorted(ranked["tools"]) == ["BindCraft", "bindcraft"], ranked["tools"]

    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1", multi_tool=True,
                           sort_mode="tool", per_tool=ranked["tools"]))
    assert len(table.group_rows) == 2, table.group_rows
    # The sharper half: a drawn boundary that does not restart the counter is
    # still asserting one continuous cross-tool ranking.
    ranks = [cells[0].split()[0] for cells in table.rows]
    assert ranks == [str(i + 1) for i in range(25)] * 2, ranks[:30]


@pytest.mark.parametrize("name,kw", [
    ("compute campaign (runs/detail.html)", {"campaign_id": "c-1"}),
    ("single job (13 tools/*_results.html pages)", {"job_id": "job-1"}),
], ids=["campaign", "job"])
def test_a_sort_tool_campaign_or_job_table_draws_no_group_headers(name, kw):
    """The ``pooled`` half of the gate, which nothing pinned.

    No caller passes a sort mode outside target mode today, so deleting
    ``pooled`` from the gate left the entire suite green. Grouping is a claim
    only a pooled table can make: these two print the SOURCE JOB's own rank in
    the ``#`` column, so tool blocks drawn over them would restart a counter
    that already restarts for an unrelated reason.
    """
    rows = ([_row("bindcraft", "ipTM", 0.9 - 0.001 * i, job="job-a", index=i)
             for i in range(3)]
            + [_row("boltzgen", "ipTM", 0.5 - 0.001 * i, job="job-b", index=i)
               for i in range(3)])
    ranked = ranking.rank_candidates(rows, limit=None,
                                     sort_mode=ranking.SORT_TOOL)
    assert len(ranked["tools"]) == 2, "fixture assumption: two tools"

    table = _parse(_render(candidates=ranked["rows"], columns=["ipTM"],
                           tool_slug="bindcraft", sort_mode="tool",
                           per_tool=ranked["tools"], **kw))
    assert table.group_rows == [], (name, table.group_rows)


class _ProbeRow(dict):
    """A candidate row that counts SUBSCRIPT reads of ``_source_tool``.

    Jinja's ``map(attribute='_source_tool')`` resolves through
    ``environment.getitem``, i.e. ``row['_source_tool']``. Every OTHER read of
    that key in the macro is ``cand.get(...)``, and ``dict.get`` does not route
    through ``__getitem__``, so a non-zero count here means the group gate
    evaluated and nothing else in the macro can produce one.
    """

    reads = 0

    def __getitem__(self, key):
        if key == "_source_tool":
            _ProbeRow.reads += 1
        return dict.__getitem__(self, key)


def _probe_rows(*tools):
    _ProbeRow.reads = 0
    return [_ProbeRow(_row(t, "ipTM", 0.9 - 0.01 * i, job=f"job-{i}", index=i))
            for i, t in enumerate(tools)]


_UNGROUPABLE = [
    ("single job (13 tools/*_results.html pages)",
     {"job_id": "job-1", "sort_mode": "tool"}),
    ("compute campaign (runs/detail.html)",
     {"campaign_id": "c-1", "sort_mode": "tool"}),
    ("design target under the default sort",
     {"target_id": "t-1", "sort_mode": "percentile"}),
]


@pytest.mark.parametrize("name,kw", _UNGROUPABLE, ids=[m[0] for m in _UNGROUPABLE])
def test_the_group_gate_costs_nothing_where_it_cannot_group(name, kw):
    """ROUND 20 (B-11 again). B-1's fix landed as a standalone ``{% set %}``,
    which is a STATEMENT and not a lazy sub-expression: it ran on EVERY render
    of this shared macro, job and campaign mode included, where ``pooled`` is
    False and the result is discarded. That is an attrgetter per row plus a set
    build per campaign table for a value nobody reads, and ``unique`` hashes, so
    a job whose adapter left a non-hashable value under ``_source_tool`` 500'd a
    results page that had rendered fine until then.

    Folding the count into the gate's ``and`` chain makes it short-circuit.
    Counted rather than timed, on the one read that is unique to the gate.
    """
    rows = _probe_rows("bindcraft", "boltzgen")
    _render(candidates=rows, columns=["ipTM"], tool_slug="bindcraft", **kw)
    assert _ProbeRow.reads == 0, (name, _ProbeRow.reads)


def test_the_group_gate_does_evaluate_on_a_grouped_target_table():
    """The pair. A gate that never evaluated would satisfy every case above
    while silently switching grouping off everywhere."""
    rows = _probe_rows("bindcraft", "boltzgen")
    _render(candidates=rows, columns=["ipTM"], target_id="t-1",
            tool_slug="bindcraft", sort_mode="tool")
    assert _ProbeRow.reads == len(rows), _ProbeRow.reads


def test_grouped_mode_badges_one_row_in_the_whole_table_not_one_per_group():
    """THE DECISION A82 left open, pinned.

    A82 asked whether, once groups are visible, grouped mode should badge each
    group's own best instead of the target's. It should not, and does not: the
    badge's tooltip is a claim about the TARGET ("Top-ranked design across
    every tool run against this target"), and a badge whose meaning changes
    with the sort mode is a worse control than one that lands where it lands.
    The group header now supplies the context that made it read oddly.

    Two tools, two groups, ONE badge. WHERE it lands is pinned separately and
    non-trivially by test_the_top_badge_marks_the_ranked_best_not_the_first_
    row_shown, whose fixture puts the canonical winner outside the first group;
    this fixture's winner is in the first group, so counting is all this one
    can honestly assert.
    """
    table = _grouped_table()
    assert len(table.group_rows) == 2
    badged = [i for i, cells in enumerate(table.rows) if "Top" in cells[0]]
    assert len(badged) == 1, badged


# ---------------------------------------------------------------------------
# Action-bar hierarchy: download is the outcome, the handoff is an option (5.2)
# ---------------------------------------------------------------------------

class _ActionBar(HTMLParser):
    """The clickable controls INSIDE ``.cand-action-bar``, in document order.

    Scoped to that subtree by div depth. A page-wide search cannot answer "how
    many primary buttons are in the action bar", because the lab-submit modal
    lower down legitimately carries its own ``btn-primary`` submit.

    Each entry is ``(tag, class, text, href)``.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls: list[tuple[str, str, str, str]] = []
        self._depth = 0            # div depth inside the bar, 0 = outside
        self._pending: list | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif "cand-action-bar" in (a.get("class") or ""):
                self._depth = 1
        elif self._depth and tag in ("a", "button"):
            self._pending = [tag, a.get("class") or "", "", a.get("href") or ""]
            self._text = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1
        elif self._pending is not None and tag == self._pending[0]:
            self._pending[2] = " ".join("".join(self._text).split())
            self.controls.append(tuple(self._pending))
            self._pending = None

    def handle_data(self, data):
        if self._pending is not None:
            self._text.append(data)


def _bar(html: str) -> list:
    p = _ActionBar()
    p.feed(html)
    assert p.controls, "no controls found inside .cand-action-bar"
    return p.controls


def test_download_csv_is_the_only_primary_control_in_the_action_bar():
    """Phase 5.2. "Send shortlist to Ranomics lab" was the ONLY btn-primary in
    this bar and all three exports were secondary, which framed the CRO handoff
    as the goal of the results page rather than as one option after it. Per
    decision 5 download wins and there is exactly one primary."""
    controls = _bar(_multi_tool_table())
    primaries = [c for c in controls if "btn-primary" in c[1]]
    assert len(primaries) == 1, primaries
    assert primaries[0][2] == "Download CSV"
    assert primaries[0][3].endswith("/targets/t-abc12345/export.csv")


def test_the_lab_handoff_is_secondary_and_adjacent_not_above():
    """Demoted, not deleted. It stays in the same bar at equal weight with the
    exports rather than being moved or hidden."""
    controls = _bar(_multi_tool_table())
    handoff = [c for c in controls if "Send shortlist" in c[2]]
    assert len(handoff) == 1, controls
    assert "btn-secondary" in handoff[0][1]
    assert "btn-primary" not in handoff[0][1]


def test_the_zero_star_state_is_an_inline_hint_not_a_disabled_button():
    """The dead-button state. With no stars the button was `disabled` carrying
    only a `title`, which reads as broken software and is unhoverable on
    touch. The button stays live; the hint says what to do."""
    html = _multi_tool_table()
    assert 'id="shortlist-hint-t-abc12345"' in html
    send = [c for c in _bar(html) if "Send shortlist" in c[2]][0]
    # `disabled` is a bare boolean attribute, so HTMLParser reports it as a
    # separate attr rather than in class; assert on the raw tag instead.
    tag = re.search(r"<button[^>]*id=\"send-to-lab-btn-t-abc12345\"[^>]*>", html)
    assert tag is not None
    assert "disabled" not in tag.group(0), tag.group(0)
    assert send[0] == "button"


def test_the_star_tooltip_names_a_general_shortlist_not_lab_submission():
    """The star drives "Starred only (CSV)" as well as the optional handoff, so
    a tooltip naming one consumer hid the other.

    ROUND 19 (B-7). Asserted at BOTH sites, separately. The tooltip lives on
    the header cell AND on every star button, and a single `in html` check is
    satisfied by either one, so dropping it from the buttons -- the place a
    user actually hovers -- left this green.
    """
    html = _multi_tool_table()
    table = _parse(html)

    star_headers = [a for a in table.header_cells
                    if a.get("title") == "Star to shortlist"]
    assert len(star_headers) == 1, table.header_cells

    buttons = re.findall(r'<button[^>]*class="star-btn"[^>]*>', html)
    assert buttons, "no star buttons rendered at all"
    missing = [b for b in buttons if 'title="Star to shortlist"' not in b]
    assert not missing, missing[:2]

    assert "Click to shortlist for lab submission" not in html


def test_target_mode_offers_a_starred_only_csv_export():
    """The star earns its place whether or not the user ever contacts Ranomics.

    A POST, because the selection lives in sessionStorage and can run to
    hundreds of refs; the `refs` field is filled by candidate_table.js at
    submit time.
    """
    html = _multi_tool_table()
    fields = _form_for(html, "/targets/t-abc12345/export.csv")
    assert "refs" in fields
    labels = [c[2] for c in _bar(html)]
    assert "Starred only (CSV)" in labels, labels
    # THE TWO ATTRIBUTES THE JS BINDS ON, asserted because nothing else does.
    # candidate_table.js finds this form by `.cand-starred-export` and matches
    # `data-scope` against the table's scope before wiring its submit handler.
    # Rename or drop either and the form still renders, still posts, and still
    # returns a 200 -- carrying the render-time `refs="[]"`, so the user gets an
    # empty CSV named `_starred` with nothing to say it failed. Verified by
    # mutation: renaming the class alone left every other assertion here green.
    assert 'class="cand-starred-export" data-scope="t-abc12345"' in html


def test_campaign_mode_offers_no_starred_export():
    """Scoped, deliberately. Only the target export route reads `refs`; the
    job and campaign export routes are unchanged by this phase, so rendering
    the control there would post to an endpoint that ignores it and hand back
    the full file under a filename claiming otherwise."""
    rows = [{"scores": {"ipTM": 0.9}, "pdb_key": "d.pdb", "_source_job_id": "j1"}]
    html = _render(candidates=rows, columns=["ipTM"], job_id="",
                   tool_slug="bindcraft", campaign_id="c-1")
    labels = [c[2] for c in _bar(html)]
    assert "Starred only (CSV)" not in labels, labels
    assert "cand-starred-export" not in html


def test_a_tool_less_group_still_shows_its_counts():
    """ROUND 19 (B-6). `raw_tool` is the key `build_tool_stats` filed the row
    under ('' for a row carrying no tool); `this_tool` is what the header
    PRINTS, where '' becomes an em dash so the group is visible at all.

    Looking the stats up by the display fallback drops the counts for exactly
    that group. The template says so, correctly, in a comment -- but every
    other fixture in this file puts a tool on every row, so mutating
    `per_tool.get(raw_tool)` to `per_tool.get(this_tool)` survived the entire
    suite. A recovered job with no tool slug is the real shape.

    Asserted on the counts rather than on the dash: the label is a non-ASCII
    character and this is a claim about the LOOKUP, not about the glyph.
    """
    rows = ([_row("bindcraft", "ipTM", 0.9 - 0.001 * i, job="job-bc", index=i)
             for i in range(3)]
            + [_row("", "ipTM", 0.5 - 0.001 * i, job="job-x", index=i)
               for i in range(2)])
    ranked = ranking.rank_candidates(rows, limit=None,
                                     sort_mode=ranking.SORT_TOOL)
    assert "" in ranked["tools"], "fixture assumption: a tool-less cohort"

    table = _parse(_render(candidates=ranked["rows"], columns=[], job_id="",
                           tool_slug="", target_id="t-1", multi_tool=True,
                           sort_mode="tool", per_tool=ranked["tools"]))
    assert len(table.group_rows) == 2, table.group_rows
    untooled = [g for g in table.group_rows if not g.startswith("bindcraft")]
    assert len(untooled) == 1, table.group_rows
    # total and shown come straight from the stats dict; a missed lookup
    # renders the header with no counts at all.
    assert "2 of 2 shown" in untooled[0], untooled[0]
    assert "0 ranked" in untooled[0], untooled[0]
