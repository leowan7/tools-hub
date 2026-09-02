"""The RENDERED cell and banner, not just the function behind them.

WHY THIS FILE EXISTS. tests/test_derived_verdicts.py pins ``judge`` well:
mutating the placeholder set, reinstating boltzgen's ipTM leg, turning
unmeasured into failed, re-carrying a verdict through job recovery, or
dropping the pLDDT rescale all fail it loudly. None of that reached the
templates. An independent reviewer mutated the rendering layer three ways --
made the cell render empty in every branch, disabled the banner outright, and
replaced the cell text with the bare words PASS / FAIL that this change exists
to delete -- and all 355 tests touching these templates stayed green. The old
banner test only grepped the template SOURCE for a string; it never rendered
it.

So every test here renders through the app's real Jinja environment and reads
the output. A template that produces nothing, or produces a word instead of a
measurement, fails here.
"""

from __future__ import annotations

import re

import pytest

from shared.score_legends import GATE_COLUMNS, gate_bar_text


@pytest.fixture(scope="module")
def env():
    """The app's REAL Jinja environment, inside a request context.

    Not a hand-built Environment with a few globals bolted on: the point of
    this file is that what ships renders, and a fixture that registers its own
    globals could not have caught a missing one.
    """
    from app import create_app

    app = create_app()
    with app.test_request_context("/"):
        yield app.jinja_env


def _render_table(env, tool, columns, candidates):
    tmpl = env.from_string(
        "{% from 'components/candidate_table.html' import candidate_table %}"
        "{{ candidate_table(candidates, columns, 'job-1', tool) }}"
    )
    return tmpl.render(candidates=candidates, columns=columns, tool=tool)


def _render_panel(env, tool, columns, candidates):
    tmpl = env.from_string(
        "{% from 'components/results_shell.html' import results_panel %}"
        "{{ results_panel(candidates, columns, tool, 'job-1') }}"
    )
    return tmpl.render(candidates=candidates, columns=columns, tool=tool)


def _cells(html):
    """The rendered text of every vs.-quality-bar cell, in order."""
    return [
        re.sub(r"<[^>]+>", "", c).strip()
        for c in re.findall(
            r'<td data-col="against_bar".*?</td>', html, re.S,
        )
    ]


BOLTZGEN_COLUMNS = ["ipTM", "pLDDT", "refolding_rmsd", "against_bar"]


# ---------------------------------------------------------------------------
# The cell says the measurement, never a word
# ---------------------------------------------------------------------------

def test_a_short_design_renders_the_reading_and_the_bar(env):
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"ipTM": 0.9, "pLDDT": 72.4, "refolding_rmsd": 1.0}},
    ])
    assert _cells(html) == ["pLDDT 72.4, below 80"]


def test_a_meeting_design_renders_the_bar_it_met(env):
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "refolding_rmsd": 1.0}},
    ])
    assert _cells(html) == [f"Meets {gate_bar_text('boltzgen')}"]


def test_an_unmeasured_design_says_which_leg_is_missing(env):
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"ipTM": 0.9, "pLDDT": 88.0}},
    ])
    assert _cells(html) == ["Not measured: Refolding RMSD"]


def test_a_short_design_with_a_gap_reports_the_gap_too(env):
    """One leg failing settles the verdict, but the cell may not then imply the
    other was measured. This is the ordinary shape of a job rebuilt from
    mid-run records, so it is not a corner case."""
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"ipTM": 0.9, "pLDDT": 60.0}},
    ])
    assert _cells(html) == ["pLDDT 60.0, below 80; Refolding RMSD not measured"]


def test_the_cell_never_renders_a_bare_verdict_word(env):
    """The whole point. A cell reading "pass", "fail" or "below threshold"
    tells a reader nothing they can check, and every one of those words came
    from a threshold that has since moved."""
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "refolding_rmsd": 1.0}},
        {"scores": {"ipTM": 0.9, "pLDDT": 60.0, "refolding_rmsd": 3.0}},
        {"scores": {"ipTM": 0.9}},
    ])
    cells = _cells(html)
    assert len(cells) == 3
    for text in cells:
        assert text, "the cell rendered empty"
        assert text.lower() not in ("pass", "fail", "below threshold", "-")
        # Every branch carries either a number or the name of a missing leg.
        assert re.search(r"\d", text) or "not measured" in text.lower()


def test_a_smoke_stub_is_never_judged(env):
    """Fabricated scores, so no bar is applied.

    "Not usable", not "not measured": the columns beside this row are full of
    invented numbers rather than empty. A smoke run emits one design, and at
    rank 1 the boltzgen stub is ipTM 0.46 / pLDDT 71.0 / RMSD 2.4 -- so what
    judging a stub actually produces is a confident "pLDDT 71.0, below 80"
    about a number no model ever computed."""
    html = _render_table(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 80.0, "refolding_rmsd": 1.5,
                    "filter_status": "stub (smoke)"}},
    ])
    assert _cells(html) == ["Not usable: smoke-test stub, scores fabricated"]


def _col_cells(html, col):
    import re as _re
    return [
        _re.sub(r"<[^>]+>", "", c).strip()
        for c in _re.findall(
            r'<td data-col="' + col + r'".*?</td>', html, _re.S,
        )
    ]


def test_the_verdict_never_quotes_a_number_the_column_shows_as_missing(env):
    """THE CELL AND THE COLUMN READ THE SAME VALUE, or the page contradicts
    itself in the space of two table cells.

    The metric columns used scores.get(col) while the judge resolved storage
    aliases and fell back to the record root. So a job rebuilt by
    shared/job_recovery -- which stores the interface PAE under the streamed
    name i_pae, precisely so the record stays judgeable -- rendered a dash in
    its i_pAE column beside a verdict cell quoting "i_pAE 12.50 A, above 10 A".
    """
    recovered = {
        "rank": 1, "pdb_key": "designs/d1.pdb",
        "scores": {"ipTM": 0.85, "pLDDT": 90.0, "i_pae": 12.5},
    }
    html = _render_table(
        env, "rfdiffusion", ["ipTM", "pLDDT", "i_pAE", "against_bar"],
        [recovered],
    )
    assert _col_cells(html, "i_pAE") == ["12.50"]
    assert _cells(html) == ["i_pAE 12.50 Å, above 10 Å"]


def test_a_root_keyed_row_renders_its_metrics(env):
    """boltz2 persists a flat designs[] row with lowercase root keys and no
    scores dict at all. Every metric column was a dash under a confident
    "Meets ..."."""
    html = _render_table(
        env, "boltz2", ["ipTM", "pLDDT", "n_hotspot_contacts", "against_bar"],
        [{"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6}],
    )
    assert _col_cells(html, "ipTM") == ["0.900"]
    assert _col_cells(html, "pLDDT") == ["93.0"]
    assert _cells(html) == [f"Meets {gate_bar_text('boltz2')}"]


def test_a_contact_count_renders_as_a_count(env):
    """The column formatted contacts at two decimals while the bar compares at
    zero, so a column reading "3.60" sat beside "Meets ... Hotspot hits 4"."""
    html = _render_table(
        env, "boltz2", ["n_hotspot_contacts", "against_bar"],
        [{"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6}],
    )
    assert _col_cells(html, "n_hotspot_contacts") == ["6"]


# ---------------------------------------------------------------------------
# The banner
# ---------------------------------------------------------------------------

def test_the_banner_fires_when_every_design_falls_short(env):
    html = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 60.0, "refolding_rmsd": 1.0}},
        {"scores": {"pLDDT": 55.0, "refolding_rmsd": 1.2}},
    ])
    assert "No design here reaches pLDDT 80." in html


def test_the_banner_names_only_legs_it_saw_fall_short(env):
    """Every row is short on pLDDT and NOT ONE ever measured the refold. Naming
    the whole bar would put half a sentence about a number this page does not
    have into the one banner whose job is to say only what was measured."""
    html = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 60.0}},
        {"scores": {"pLDDT": 55.0}},
    ])
    assert "No design here reaches pLDDT 80." in html
    assert "Refolding RMSD 1.5" not in html.split("</strong>")[0]


def test_the_banner_stays_silent_when_one_design_meets_the_bar(env):
    html = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 88.0, "refolding_rmsd": 1.0}},
        {"scores": {"pLDDT": 55.0, "refolding_rmsd": 1.2}},
    ])
    assert "No design here reaches" not in html


def test_the_banner_stays_silent_when_a_design_cannot_be_judged(env):
    """"None of these reaches the bar" is a claim the measurements do not
    support when one of them was never compared to it."""
    html = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 60.0, "refolding_rmsd": 3.0}},
        {"scores": {"ipTM": 0.9}},
    ])
    assert "No design here reaches" not in html


def test_the_banner_never_asserts_an_unfalsifiable_judgement(env):
    html = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 60.0, "refolding_rmsd": 3.0}},
    ])
    assert "fell below quality thresholds" not in html
    assert "No design here reaches pLDDT 80 and Refolding RMSD 1.5" in html


def test_the_banner_weakens_its_claim_when_rows_miss_different_legs(env):
    """A conjunction is read distributively. "No design here reaches pLDDT 80
    and Refolding RMSD 1.5" says nothing reached EITHER bar, which is false the
    moment one design missed on pLDDT while another missed on RMSD: each of
    those met the bar the other missed. The strong sentence is used only when
    every design missed every leg in the union."""
    mixed = _render_panel(env, "boltzgen", BOLTZGEN_COLUMNS, [
        {"scores": {"pLDDT": 60.0, "refolding_rmsd": 1.0}},   # pLDDT only
        {"scores": {"pLDDT": 88.0, "refolding_rmsd": 3.0}},   # RMSD only
    ])
    assert "No design here reaches" not in mixed
    assert (
        "Every design here falls short on at least one of "
        "pLDDT 80 and Refolding RMSD 1.5"
    ) in mixed


def test_a_tool_with_no_bar_renders_no_banner(env):
    """opendde has no pass/fail concept at all. Nothing may imply it does."""
    html = _render_panel(env, "opendde", ["ipTM", "pLDDT"], [
        {"scores": {"ipTM": 0.01, "pLDDT": 20.0}},
    ])
    assert "No design here reaches" not in html
    assert "Every design here falls short" not in html


@pytest.mark.parametrize("tool", ["opendde", "bindcraft", "proteina", "iggm",
                                  "esmfold2-design"])
def test_no_bar_tool_declares_no_bar_column(tool):
    """Asserting the column is absent from a render that was never given it is
    vacuous -- an earlier version of this test did exactly that. Ask the
    registry instead: a tool with no bar must not list the column anywhere,
    since every cell it produced would say "unjudged" forever."""
    from shared.result_columns import columns_for
    from shared.score_legends import tool_has_bar

    assert not tool_has_bar(tool)
    assert "against_bar" not in columns_for(tool)


def test_the_cell_still_renders_nothing_misleading_if_the_column_is_forced(env):
    """And if someone does add it to a no-bar tool's column list, the cell must
    not imply a judgement was made."""
    html = _render_table(env, "opendde", ["against_bar"], [
        {"scores": {"ipTM": 0.01}},
    ])
    # An em dash, not the bare string "Not measured:" with nothing after it,
    # which is exactly how a bar keyed on an unregistered slug looked on a
    # public page for the length of one commit.
    assert _cells(html) == ["&mdash;"]


# ---------------------------------------------------------------------------
# Every gating tool renders, on the shape it really stores
# ---------------------------------------------------------------------------

REAL_SHAPES = {
    "boltzgen": {"scores": {"ipTM": 0.5, "pLDDT": 88.0, "refolding_rmsd": 1.0}},
    "rfdiffusion": {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "i_pAE": 6.0}},
    "pxdesign": {"scores": {"ipTM": 0.9, "pLDDT": 88.0, "pAE": 3.0}},
    "rfantibody": {"scores": {"pLDDT": 88.0, "ipAE": 6.0, "pAE": 3.0}},
    # boltz2 persists a flat designs[] row, lowercase, pLDDT on 0-1.
    "boltz2": {"iptm": 0.9, "complex_plddt": 0.93, "n_hotspot_contacts": 6},
}


def test_every_gating_tool_has_a_render_fixture():
    assert set(REAL_SHAPES) == set(GATE_COLUMNS)


@pytest.mark.parametrize("tool", sorted(REAL_SHAPES))
def test_every_gating_tool_renders_a_meeting_cell(env, tool):
    """On the shape that tool actually persists, not a tidied one. This is the
    check that would have caught a bar keyed on a slug no tool uses: the cell
    came out as the bare string "Not measured:" with nothing after it."""
    html = _render_table(env, tool, ["against_bar"], [REAL_SHAPES[tool]])
    cells = _cells(html)
    assert cells == [f"Meets {gate_bar_text(tool)}"]
    assert cells[0] != "Meets "
    assert not cells[0].endswith(":")


# A guard for "the app registers every global these macros call" was written
# here and deleted. Jinja's find_undeclared_variables reports every {% set %}
# name in a macro body as undeclared, so the check only passed behind a hardcoded
# list of 20 template locals -- a list that rots on the next edit and gets
# silenced rather than fixed. It also duplicated cover this file already has:
# every test above renders through the app's REAL environment, so a macro
# calling a name create_app does not register fails them outright. That is how
# the raw_metric global was caught.
