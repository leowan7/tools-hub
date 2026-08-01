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

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    env.globals["metric_glossary"] = metric_glossary.GLOSSARY
    env.globals["score_legends_for"] = score_legends.score_legends_for
    env.globals["format_metric_value"] = metric_glossary.format_value
    env.globals["score_legend_for"] = score_legends.get_legend
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
        sort_mode="",
    )
    params.update(kwargs)
    tmpl = _env().from_string(
        '{% from "components/candidate_table.html" import candidate_table %}'
        "{{ candidate_table(candidates, columns, job_id, tool_slug, clone_url,"
        "                   campaign_id, target_id, multi_tool, sort_mode) }}"
    )
    return tmpl.render(**params)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class _Table(HTMLParser):
    """Collects the first table's header cells and its body rows.

    Rows are split on ``tr``; a row's cells are the text of each ``td``.
    ``viewer_colspans`` collects the colspan of every viewer row's single cell,
    which is what the header-count assertion compares against.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.header_cells: list[dict] = []
        self.header_text: list[str] = []
        self.rows: list[list[str]] = []
        self.viewer_colspans: list[int] = []
        self._in_thead = False
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._row_is_viewer = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "thead":
            self._in_thead = True
        elif tag == "tr":
            self._row = []
            self._row_is_viewer = "viewer-row" in (a.get("class") or "")
        elif tag in ("th", "td"):
            self._cell = []
            if tag == "th" and self._in_thead:
                self.header_cells.append(a)
            if tag == "td" and self._row_is_viewer and a.get("colspan"):
                self.viewer_colspans.append(int(a["colspan"]))

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
            if self._row and not self._row_is_viewer:
                self.rows.append(self._row)
            self._row = None
            self._row_is_viewer = False

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

def test_target_mode_hides_the_shortlist_button():
    """It would POST source_job_id="" with candidate_indices=[]: the modal's
    hidden inputs branch on campaign_id and fall back to job_id, and in target
    mode both are empty. Phase 5.3 wires it."""
    html = _multi_tool_table()
    assert "Send shortlist to Ranomics lab" not in html


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


def test_target_mode_emits_no_lab_submit_form_at_all():
    """Hiding the opener is not the same as not shipping the form.

    Before this, the modal markup was emitted on a target page regardless: a
    real ``POST /lab-projects/submit`` carrying ``source_job_id=""``, sitting in
    the DOM behind ``display:none`` with no opener. Confirmed inert in a browser
    (``openCampaignModal`` is a window global with no internal callers, so with
    the bar gone nothing calls it), which makes this defence in depth rather
    than a live fix. A dead form pointed at a live endpoint is still not worth
    carrying.
    """
    html = _multi_tool_table()
    assert "/lab-projects/submit" not in html
    assert 'name="source_job_id"' not in html
    assert "campaign-modal-" not in html


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
    assert displayed[badge_at - 1]["_source_tool"] == "rfdiffusion", (
        f"badge landed on display row {badge_at}, "
        f"tool {displayed[badge_at - 1]['_source_tool']}")


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
