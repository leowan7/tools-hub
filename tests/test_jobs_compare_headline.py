"""Regression: /jobs/compare must not lead with a design the tool's bar drops.

The compare screen is the worst place for the ``best design is the rejected
one`` class: a side-by-side view exists to rank, so the design it puts first
is read as the answer.

SURFACE TWO, NOT SIX. #241 fixed this class in three WRITERS on one tool
(modal_app._aggregate, run_pipeline._pick_best and the results template), and
its own commit message records five consumers as NOT reached by it: the
campaign and target counts, the target ranking table, the completion email,
the share card's og:title, and the FASTA export's rank0. This page was the
second surface repaired.

ALL FIVE ARE NOW CLOSED, each through its own real path: the share text and
the FASTA export in tests/test_esmfold2_reject_surfaces.py, the counts and the
ranking table through the aggregator in tests/test_aggregate_target.py, and
the completion email separately in 058f6c1 (#255), which resolves the mode in
shared/email.py::_top_candidate_summary and passes it to both
headline_candidate and verdict_text. An earlier version of this paragraph said
the email was still open; that stopped being true when this branch rebased
onto that commit.

Real completed job ``2b917b54-0871-44af-a3d1-5d07ea5dcaeb`` (esmfold2-design,
PD-L1 minibinder, n_seeds=2), as it sits in the database:

    seed0: ipTM 0.9556, pI 11.95 -> the pipeline drops it
    seed1: ipTM 0.9354, pI  5.67 -> clears the bar

``candidates[0]`` is seed0, and the compare page took it blind. Its "Top
candidates" table then ranked the two on ipTM with no pI column, no bar and no
verdict, so both halves of the page agreed on the wrong answer.

TestComparePage DRIVES THE ROUTE, through the test client. Every function in
the chain can be correct while the route never calls one of them -- that is
how the sibling surfaces stayed green through a reviewer's mutations, and
rendering the template with hand-built ``columns`` reproduces the same blind
spot one level down.

The other two classes call the mechanism directly, and are NOT a substitute:
TestModeAwareBar and TestHeadlineCandidate pin behaviour a rendered page
cannot distinguish (an unusable-vs-absent metric, a boundary at the third
decimal). An earlier version of this paragraph claimed the whole file went
through the route, which was false of roughly half of it.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from shared.jobs import headline_candidate
from shared.score_legends import (
    MODE_GATE_COLUMNS,
    gate_columns,
    judge,
    result_mode,
    tool_has_bar,
    verdict_text,
)

DROP_NAME = "design_0"
PASS_NAME = "design_1"

# THE VALUES AS JOB 2b917b54 ACTUALLY STORES THEM, unrounded. Rounded stand-ins
# (11.95 / 5.67 / 0.9556 / 0.9354) made the display-precision assertions pass
# under BOTH the old flat "%.3f" and the new format_metric_value: "5.67" is a
# substring of "5.670", so the tolerance spanned the defect and its fix. With
# the real value "%.3f" renders "5.669" and the assertion can tell them apart.
DROP_PI = 11.954517555236816
PASS_PI = 5.669371223449708
DROP_IPTM = 0.9555796384811401
PASS_IPTM = 0.9353567957878113


def _cand(name: str, iptm, pi, rank: int, status: str | None = None,
          proxy=None, plddt=None) -> dict:
    """One record in the shape tools/esmfold2_design/run_pipeline.py stores
    under ``result["candidates"]``.

    ``plddt`` is a STAND-IN and esmfold2-design writes no pLDDT at all --
    neither its candidates[] nor its designs[] carries one, so the column is
    "—" on every real job of this tool. It is set here on the 0-1 scale that
    boltz2, proteina and esmfold do write, because the behaviour under test
    belongs to the TEMPLATE, not to the tool: this page renders pLDDT across
    tools, so a 0-1 row and a 0-100 row must be brought onto one scale before
    they are read against each other, and the template cannot tell which tool
    produced the number. Do not read this fixture as evidence about what
    esmfold2-design stores.
    """
    scores: dict = {"ipTM": iptm, "iPTM_proxy": proxy, "pI": pi,
                    "final_loss": 1.0}
    if plddt is not None:
        scores["pLDDT"] = plddt
    if status is not None:
        scores["filter_status"] = status
    return {"rank": rank, "name": name, "pdb_key": f"{name}_complex.pdb",
            "sequence": "MKTAY", "scores": scores}


def _job(**over):
    """Job 2b917b54 as stored: the rejected design is candidates[0]."""
    result = {
        "status": "COMPLETED",
        "preset": "minibinder",
        "is_antibody": False,
        "best_sequence": "LLRRLLRR",
        "candidates": [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 0, "drop", plddt=0.842),
            _cand(PASS_NAME, PASS_IPTM, PASS_PI, 1, "strict_pass", plddt=0.887),
        ],
    }
    result.update(over.pop("result_over", {}))
    job = SimpleNamespace(
        id="2b917b54-0871-44af-a3d1-5d07ea5dcaeb",
        tool="esmfold2-design",
        preset="minibinder",
        status="succeeded",
        result=result,
        inputs={},
        gpu_seconds_used=120,
    )
    for key, value in over.items():
        setattr(job, key, value)
    return job


def _other_job(job_id: str = "aaaaaaaa-0000-0000-0000-000000000000"):
    """A second column, so the route's two-job minimum is met.

    DELIBERATELY UNREMARKABLE: distinct design names, and both of its designs
    clear the bar. A copy of the job under test as the second column made
    every negative assertion hollow -- "above 6 is not on the page" cannot be
    checked when a second column is putting it there.
    """
    return _job(
        id=job_id,
        result_over={"candidates": [
            _cand("other_0", 0.90, 5.00, 0, "strict_pass", plddt=0.901),
            _cand("other_1", 0.88, 5.20, 1, "strict_pass", plddt=0.873),
        ]},
    )


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _render_compare(flask_app, monkeypatch, jobs) -> str:
    """GET the real URL. Auth and the DB read are stubbed; nothing else is.

    Through the test client rather than by calling the view function, so URL
    routing and the login decorator are exercised too -- the route is only
    reachable in production through both.
    """
    import blueprints.jobs as bp
    import shared.jobs as shared_jobs

    monkeypatch.setattr(
        bp, "load_user_context", lambda: SimpleNamespace(user_id="u")
    )
    monkeypatch.setattr(
        shared_jobs, "list_jobs_by_ids", lambda user_id, ids: list(jobs)
    )
    ids = ",".join(j.id for j in jobs)
    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_email"] = "compare@example.com"
        resp = client.get(f"/jobs/compare?ids={ids}")
    assert resp.status_code == 200, (
        f"compare route did not render: {resp.status_code} "
        f"{resp.headers.get('Location')}"
    )
    return resp.get_data(as_text=True)


def _flat(html: str) -> str:
    return re.sub(r"\s+", " ", html)


def _candidate_rows(html: str) -> list[tuple[str, str, str, str]]:
    """``(rank cell, ipTM, pLDDT, Bar)`` per row of the FIRST per-job table.

    The rank cell is returned raw so the star marking the headline row is
    visible to a test; the table renders no design name, rank is the identity
    within a job.

    ALL FOUR CELLS. An earlier version returned ``(cells[0], cells[1],
    cells[3])`` and silently discarded the pLDDT column, so nothing in this
    file could see whether that cell was normalised.
    """
    flat = _flat(html)
    start = flat.find("Top candidates")
    # THE HEADER, not only the cells. Deleting the "Bar" <th> left every
    # assertion in this file green: the row parser counts <td>s, so a table
    # shipping a fourth data column under three headings was invisible to it.
    head = flat[flat.find("<thead>", start):flat.find("</thead>", start)]
    headings = [
        re.sub(r"<[^>]+>", "", c).strip()
        for c in re.findall(r"<th[^>]*>(.*?)</th>", head)
    ]
    assert headings == ["#", "ipTM", "pLDDT", "Bar"], headings
    body = flat[flat.find("<tbody>", start):flat.find("</tbody>", start)]
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row)
        ]
        assert len(cells) == 4, cells
        rows.append(tuple(cells))
    assert rows, "the per-job candidate table rendered no rows"
    return rows


def _metric_row(html: str, key: str) -> list[str]:
    """The Shared-metrics row for ``key``, as its rendered cell strings."""
    flat = _flat(html)
    start = flat.find(f"<strong>{key}</strong>")
    assert start != -1, f"no Shared-metrics row for {key}"
    end = flat.find("</tr>", start)
    assert end != -1
    cells = [
        re.sub(r"<[^>]+>", "", c).strip()
        for c in re.findall(r"<td[^>]*>(.*?)</td>", flat[start - 40:end])
    ]
    # Drop the label cell; what is left is one cell per visible column.
    return [c for c in cells if c != key]


def _design_row(html: str) -> str:
    """The shared-metrics table's "Design" row: which design each column is.

    Anchored on the row's own cell and closed at its own ``</tr>``. An earlier
    version sliced from "Design" to "ipTM", which is empty on every render:
    the per-job tables above carry an ipTM header long before this row. A
    hollow slice makes every assertion over it pass vacuously in one direction
    and fail in the other, which is how it was caught.
    """
    flat = _flat(html)
    start = flat.find(">Design</td>")
    assert start != -1, "the shared-metrics table has no Design row"
    end = flat.find("</tr>", start)
    assert end != -1
    return flat[start:end]


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------

class TestComparePage:
    def test_the_headline_is_not_the_stored_first_candidate(
        self, flask_app, monkeypatch
    ):
        """The whole defect, at the route. The Shared-metrics table names the
        design it is describing, and on job 2b917b54 that must be seed1."""
        html = _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        design_row = _design_row(html)
        assert PASS_NAME in design_row, html[:400]
        assert DROP_NAME not in design_row
        # AND THE ROW STATES THE VERDICT, resolved under the run's MODE. Only
        # the per-job Bar cell pinned that; the Design row renders through the
        # same verdict_text and dropping its preset there left every test
        # green -- for a mode-scoped bar the sentence silently becomes empty,
        # so the row would name a design and say nothing about it.
        assert "Meets pI 6 and ipTM 0.75" in design_row, design_row

    def test_the_headline_numbers_are_the_passing_designs(
        self, flask_app, monkeypatch
    ):
        """Not just the name. The ipTM row must carry 0.935, the passing
        design's number -- a page that named seed1 and then printed seed0's
        0.956 beside it would be a worse bug than the one being fixed."""
        html = _flat(
            _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        )
        assert "0.935" in html
        assert "0.956" not in html

    def test_the_gating_column_is_on_the_page(self, flask_app, monkeypatch):
        """pI is what decides this run and it had no column at all. Both the
        measured value and the bar it is measured against have to render, or
        the page states a ranking it gives the reader no way to check."""
        html = _flat(
            _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        )
        # The rejected design's own reading, with the threshold it
        # missed. (A bare `"pI" in html` sat here too and was strictly
        # subsumed by this line.)
        assert "pI 11.95, above 6" in html
        # And the passing design's, in the shared-metrics row, AS THE CELL
        # RENDERS IT. The stored value is 5.669371...; ".2f" gives "5.67" and
        # the flat "%.3f" this replaced gives "5.669". A substring check
        # cannot tell those apart -- "5.67" sits inside "5.670" -- which is
        # why the old fixture's pre-rounded 5.67 made this assertion hollow.
        assert ">5.67<" in re.sub(r"\s+", "", html)
        assert "5.669" not in html

    def test_the_rejected_design_is_marked_where_it_still_ranks_first(
        self, flask_app, monkeypatch
    ):
        """The per-job table keeps the pipeline's order ON PURPOSE, so seed0
        is still the first row. A bare ipTM column there repeats the same
        misleading ranking; the Bar cell is what stops it, and the row the
        page actually leads with is marked so the two tables cannot be read
        as disagreeing."""
        rows = _candidate_rows(
            _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        )
        # Row 1 is still the highest-ipTM design, and it now says why it is
        # not the pick.
        assert rows[0][1] == "0.96"
        assert rows[0][3] == "pI 11.95, above 6"
        # Row 2 is the design the page leads with: lower ipTM, meets the bar,
        # carries the star.
        assert rows[1][1] == "0.94"
        # The whole judgement sentence, from verdict_text -- the single
        # renderer. A bare word here was this cell hand-rolling its own text,
        # which is how the "below" case came to drop its "not usable" half.
        assert rows[1][3] == "Meets pI 6 and ipTM 0.75"
        assert "&#9733;" in rows[1][0] or "★" in rows[1][0], rows[1][0]
        assert "★" not in rows[0][0] and "&#9733;" not in rows[0][0]

    def test_a_run_where_nothing_clears_the_bar_says_so(
        self, flask_app, monkeypatch
    ):
        """The fallback. With both designs over pI 6 the page still has to
        show one -- and must not present it as the pick, which is the failure
        mode the fallback would otherwise re-create."""
        job = _job(result_over={"candidates": [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1),
            _cand(PASS_NAME, PASS_IPTM, 10.20, 2),
        ]})
        html = _flat(_render_compare(flask_app, monkeypatch, [job, _other_job()]))
        assert "nothing in this run clears the bar" in html
        assert "pI 11.95, above 6" in html

    def test_an_unmeasured_design_is_not_narrated_as_failing(
        self, flask_app, monkeypatch
    ):
        """Unmeasured is unjudged, never failed -- the rule the whole derived
        -verdict design rests on. A run with no pI at all (an scFv result read
        in minibinder mode, or a row rebuilt by job recovery) must lead with
        its rank 1 and claim nothing about a bar."""
        job = _job(result_over={"candidates": [
            _cand(DROP_NAME, DROP_IPTM, None, 1),
            _cand(PASS_NAME, PASS_IPTM, None, 2),
        ]})
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        design_row = _design_row(html)
        assert DROP_NAME in design_row
        assert "clears the bar" not in html
        assert "above 6" not in html

    def test_a_stale_verdict_word_cannot_move_the_answer(
        self, flask_app, monkeypatch
    ):
        """Both labels inverted against their own measurements. A record
        stamped by a container version whose bar has since moved must not be
        able to float the reject or sink the good design."""
        job = _job(result_over={"candidates": [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1, "strict_pass"),
            _cand(PASS_NAME, PASS_IPTM, PASS_PI, 2, "drop"),
        ]})
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        design_row = _design_row(html)
        assert PASS_NAME in design_row

    def test_the_result_beats_the_stored_preset_when_they_disagree(
        self, flask_app, monkeypatch
    ):
        """THE RESOLUTION ORDER, and it needs the two to DISAGREE to be
        testable at all. Every other fixture here stores preset "minibinder"
        beside is_antibody False, so both orders give the same answer and a
        mutation to ``mode = j.preset`` left this file green -- the guard was
        certifying a property it could not see.

        The disagreement is the ordinary one: a job row whose preset is the
        old default string, with the result recording what actually ran.
        Read preset-first and the mode is "default", which resolves to no bar,
        and the page leads with the reject again -- the whole defect, restored
        by the resolution order alone."""
        job = _job(preset="default")
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        assert PASS_NAME in _design_row(html)
        assert "pI 11.95, above 6" in _flat(html)

    def test_the_stored_preset_is_the_fallback_when_the_result_is_silent(
        self, flask_app, monkeypatch
    ):
        """The other half of ``result_mode(...) or job.preset``. A result with
        no is_antibody flag at all -- an older row, or one rebuilt by job
        recovery -- still gets its bar from the preset rather than none."""
        job = _job(preset="minibinder")
        job.result = {k: v for k, v in job.result.items() if k != "is_antibody"}
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        assert PASS_NAME in _design_row(html)
        assert "pI 11.95, above 6" in _flat(html)

    def test_a_placeholder_row_does_not_headline_over_a_real_pass(
        self, flask_app, monkeypatch
    ):
        """UNUSABLE IS SUNK, even though it is ``unjudged`` like an absent
        metric. A declared placeholder -- boltzgen's 0.00 refolding RMSD here
        -- is evidence the pipeline could not produce a reading, and
        shared/ranking sinks exactly these rows. A predicate of
        ``verdict != "below"`` alone floated one to the headline ahead of a
        design that genuinely met the bar, and the page printed no shortfall
        for it because an unjudged pick rendered nothing at all."""
        rows = [
            # falls short: real measurements, under the bar
            {"rank": 1, "name": "short",
             "scores": {"pLDDT": 70.0, "refolding_rmsd": 2.4}},
            # a DECLARED placeholder: 0.00 is boltzgen's stand-in, not a refold
            {"rank": 2, "name": "placeholder",
             "scores": {"pLDDT": 88.0, "refolding_rmsd": 0.0}},
            # the design that actually clears it
            {"rank": 3, "name": "genuine",
             "scores": {"pLDDT": 88.0, "refolding_rmsd": 1.2}},
        ]
        job = _job(tool="boltzgen", preset="default",
                   result_over={"candidates": rows, "is_antibody": None})
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        design_row = _design_row(html)
        assert "genuine" in design_row, design_row
        assert "placeholder" not in design_row

    def test_a_shortfall_beside_a_placeholder_renders_both_halves(
        self, flask_app, monkeypatch
    ):
        """THE PAGE MUST NOT PRINT HALF A JUDGEMENT. A record can fall short
        on one gate leg while another leg holds a declared placeholder, and
        when every record in a run is that shape it is also the pick. The
        Design row hand-joined ``shortfalls`` and dropped the ``unusable``
        half, so the page asserted "pLDDT 60.0, below 80" about a design whose
        refolding RMSD was a stand-in the pipeline could not compute.

        Fixed by rendering through verdict_text, which composes every state --
        the same renderer the results table uses, and the reason its docstring
        says there must only ever be one.

        BOTH TABLES ARE CHECKED SEPARATELY. The per-job Bar cell and the
        Design row are two independent renderers of the same judgement, so a
        page-wide ``in html`` assertion is satisfied by either one -- mutating
        each back to a hand-rolled join left this test green until the
        assertions were scoped.
        """
        rows = [
            {"rank": 0, "name": "short_and_standin",
             "scores": {"pLDDT": 60.0, "refolding_rmsd": 0.0}},
        ]
        job = _job(tool="boltzgen", preset="default",
                   result_over={"candidates": rows, "is_antibody": None})
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])

        both_halves = "pLDDT 60.0, below 80; Refolding RMSD not usable"
        # The per-job table's Bar cell.
        assert _candidate_rows(html)[0][3] == both_halves
        # The Shared-metrics Design row, independently.
        design_row = _design_row(html)
        assert both_halves in design_row, design_row

    def test_the_legacy_designs_shape_is_judged_not_waved_through(
        self, flask_app, monkeypatch
    ):
        """The route reads candidate_records, which falls through to
        ``designs[]`` for the legacy/single-seed rows that pre-dated the
        candidates contract. Those rows spell the isoelectric point
        ``isoelectric_point``, so without an alias the pI leg resolved to
        nothing, every record came back unjudged, and the page led with the
        pI 11.95 reject again -- the whole defect, restored silently on one of
        the two shapes this tool writes."""
        job = _job(result_over={
            "candidates": None,
            "designs": [
                {"rank": 1, "name": DROP_NAME, "sequence": "MKTAY",
                 "iptm": DROP_IPTM, "isoelectric_point": DROP_PI},
                {"rank": 2, "name": PASS_NAME, "sequence": "MKTAY",
                 "iptm": PASS_IPTM, "isoelectric_point": PASS_PI},
            ],
        })
        del job.result["candidates"]
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        assert PASS_NAME in _design_row(html)
        assert "pI 11.95, above 6" in _flat(html)

    def test_more_than_four_jobs_does_not_overstate_what_it_checked(
        self, flask_app, monkeypatch
    ):
        """The Shared metrics table renders only the first four columns, but
        the sentence above it says "All N jobs ran X" over ALL of them. Read
        off the visible four, that sentence asserted a fact about jobs the
        page never looked at. Five columns, and the fifth is a different
        tool."""
        jobs = [_job()] + [
            _other_job(f"{c}{c}{c}{c}{c}{c}{c}{c}-0000-0000-0000-000000000000")
            for c in "abc"
        ]
        odd = _other_job("eeeeeeee-0000-0000-0000-000000000000")
        odd.tool = "boltzgen"
        jobs.append(odd)
        html = _flat(_render_compare(flask_app, monkeypatch, jobs))
        assert "Showing first 4 of 5" in html
        assert "jobs ran" not in html, (
            "the page claimed every job ran one tool while a 5th ran another"
        )
        assert "Cross-tool comparison shown on shared metrics only" in html

    def test_both_tables_put_plddt_on_the_same_scale(
        self, flask_app, monkeypatch
    ):
        """THE ONE PAGE WHERE TWO TABLES SHOW THE SAME MEASUREMENT. The
        per-job table normalised 0-1 pLDDT to 0-100; the Shared metrics table
        directly below it rendered the stored number raw, so one printed 88.7
        and the other 0.887 for the same design. This table exists to be read
        ACROSS tools, which is exactly where a 0-1 reading against a 0-100 one
        stops being a comparison.

        Asserted on the RENDERED cells, because a call-count guard cannot say
        WHICH table normalises -- and a fixture with no pLDDT at all cannot
        say anything.
        """
        html = _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        # Shared metrics: the headline design's pLDDT, normalised, at ".1f".
        assert _metric_row(html, "pLDDT")[0] == "88.7"
        # The per-job table's own cells, on the same scale.
        assert [r[2] for r in _candidate_rows(html)] == ["84.2", "88.7"]

    def test_the_headline_row_names_its_position_when_it_is_not_rank_1(
        self, flask_app, monkeypatch
    ):
        """The per-job table shows three rows. A bare "not rank 1" therefore
        sends a reader looking for a row that need not be on the page, so the
        disclosure names the position. Nothing pinned this sentence at all
        before, and nothing pinned it naming a number."""
        html = _render_compare(flask_app, monkeypatch, [_job(), _other_job()])
        design_row = _design_row(html)
        assert "not the first design listed" in design_row
        assert "row 2 of 2" in design_row, design_row
        # AND THE DISCLOSURE MUST NOT SAY "rank". The # column beside it
        # prints a POSITION, not the design's stored rank, and the two are
        # not recoverable from each other. This page preserves ORDER
        # (blueprints/jobs.py renders candidate_records with no sort) but not
        # a constant offset: several pipelines bind the loop index before the
        # failure ``continue``, so a dropped design leaves a gap and shifts
        # every row after it. On the
        # tool's own results page af2 and iggm re-sort by score as well.
        # Calling the position a rank would misdescribe it.
        #
        # Scoped to the sentence, NOT the whole cell: the cell also carries
        # the design NAME, and this tool's own shipped example payload names
        # designs "seed0_rank0"/"seed1_rank1". A cell-wide check would fire on
        # the repo's own fixture for a reason that has nothing to do with the
        # defect.
        disclosure = design_row[design_row.find("not the first design listed"):]
        disclosure = disclosure[:disclosure.find("</div>")]
        assert "rank" not in disclosure.lower(), disclosure
        # THE NUMBER AND THE SENTENCE MUST AGREE, which is the defect this
        # assertion replaced rather than a restatement of it. It used to read
        # ``.startswith("1")`` -- the SECOND row labelled 1, because the column
        # echoed a 0-based stored rank while this sentence said "row 2 of 2".
        #
        # PIN THE FIXTURE FIRST. `loop.index` prints "2" in row 2 whatever the
        # payload says, so on its own the assertion below cannot fail for the
        # reason it exists. It is the 0-based fixture that gives it teeth: a
        # template regressed to `cand.get('rank', loop.index)` prints rank 1 =
        # "1" and fails. Renumber _job()'s candidates 1,2 and that regression
        # would print "2" and slip through, so the ranks are held here -- the
        # guard the old assertion's failure message claimed to be.
        assert [c["rank"] for c in _job().result["candidates"]] == [0, 1], (
            "this test needs a 0-based fixture to tell a position from an "
            "echoed rank; renumber it and the assertion below goes blind"
        )
        assert _candidate_rows(html)[1][0].startswith("2"), (
            "the # column disagrees with the 'row 2 of 2' disclosure beside it"
        )

    def test_a_tool_with_no_bar_keeps_its_stored_order(
        self, flask_app, monkeypatch
    ):
        """The change must be inert for every tool that declares no bar --
        proteina, iggm, bindcraft. Nothing is judged, so nothing moves."""
        job = _job(tool="proteina")
        html = _render_compare(flask_app, monkeypatch, [job, _other_job()])
        design_row = _design_row(html)
        assert DROP_NAME in design_row

    def test_a_job_with_no_candidates_still_renders(
        self, flask_app, monkeypatch
    ):
        """The empty shapes the old ``cands[0] if cands else {}`` handled, and
        the None the new resolver returns instead."""
        empty = _job(id="bbbbbbbb-0000-0000-0000-000000000000",
                     result_over={"candidates": []})
        html = _flat(_render_compare(flask_app, monkeypatch, [_job(), empty]))
        assert "No candidates returned." in html

    def test_a_legacy_wrapped_result_is_read_not_skipped(
        self, flask_app, monkeypatch
    ):
        """Rows written before the pipeline-return unwrap nest everything
        under ``result["output"]``. The page used to read ``result[
        "candidates"]`` directly and saw nothing at all on those; both the
        candidate list AND the mode have to survive the same trip, or the
        candidates come back and are judged against no bar."""
        wrapped = _job(result_over={})
        inner = wrapped.result
        wrapped.result = {"status": "COMPLETED", "output": inner}
        html = _render_compare(flask_app, monkeypatch,
                               [wrapped, _other_job()])
        design_row = _design_row(html)
        assert PASS_NAME in design_row
        assert "pI 11.95, above 6" in html


# --------------------------------------------------------------------------
# The mechanism the route uses
# --------------------------------------------------------------------------

class TestModeAwareBar:
    def test_the_mode_comes_from_the_result_not_the_preset(self):
        """The one resolution order the whole thing depends on. A stored
        preset can be the default string while the result records what the
        run actually did, so reading the preset first reads the other way
        round from templates/tools/esmfold2_design_results.html."""
        assert result_mode({"is_antibody": True}) == "scfv"
        assert result_mode({"is_antibody": False}) == "minibinder"
        # Absent, non-bool, and not-a-dict all decline to guess, so the
        # caller's `or job.preset` fallback is what fires.
        assert result_mode({}) is None
        assert result_mode({"is_antibody": "yes"}) is None
        assert result_mode(None) is None
        # The wrapped legacy shape, which candidate_records also unwraps.
        assert result_mode({"output": {"is_antibody": True}}) == "scfv"

    def test_the_tool_still_has_no_bar_without_a_mode(self):
        """The property that keeps this change from reaching the campaign
        counts, the target ranking table and shared/ranking, none of which can
        supply a mode. Removing it would re-label delivered work everywhere."""
        assert tool_has_bar("esmfold2-design") is False
        assert gate_columns("esmfold2-design") == ()
        assert judge(
            "esmfold2-design", _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1)
        ).verdict == "unjudged"

    def test_a_preset_cannot_move_a_tool_whose_bar_is_its_own(self):
        """A caller that hands every column its job's preset must change
        nothing for a GATE_COLUMNS tool. Its bar is a property of the tool."""
        # Anchor first: without this the equality holds vacuously if
        # boltzgen ever loses its bar and both sides become ().
        assert gate_columns("boltzgen"), "boltzgen has no bar to compare"
        assert gate_columns("boltzgen", "minibinder") == gate_columns("boltzgen")
        assert gate_columns("boltzgen", "nonsense") == gate_columns("boltzgen")

    def test_the_mode_decides_which_legs_apply(self):
        """pI is null by construction on an scFv run, so it can only ever be a
        MINIBINDER leg -- as a tool-wide one it would leave every antibody
        design permanently unjudged. That scoping is the whole reason this is
        not a GATE_COLUMNS entry."""
        assert gate_columns("esmfold2-design", "minibinder") == ("pI", "ipTM")
        # An scFv record: no pI, and it must not be judged against one.
        scfv = _cand("ab_0", 0.30, None, 1, proxy=0.62)
        assert judge("esmfold2-design", scfv, preset="scfv").verdict == (
            "unjudged"
        )
        # The same record in minibinder mode falls short on the ipTM leg its
        # mode does gate: measured and under 0.75.
        mini = judge("esmfold2-design", scfv, preset="minibinder")
        assert mini.verdict == "below"
        assert any("ipTM" in s for s in mini.shortfalls)

    def test_scfv_declares_no_bar_and_the_proxy_carries_no_legend(self):
        """A MODE-SCOPED BAR CANNOT CARRY A MODE-SCOPED LEGEND, which is why
        the scFv leg was removed rather than reworded.

        MODE_GATE_COLUMNS is keyed on (tool, mode); SCORE_LEGENDS is keyed on
        (tool, column) and ``score_legends_for(tool)`` hands the set out with
        no mode. The tool's own results page lists iPTM_proxy in BOTH modes
        and renders that legend as the column tooltip, and the completion
        email picks the first scored column that HAS a legend -- so an
        scFv-worded legend went to customers on minibinder runs, where the
        column holds a different quantity and _classify gates on neither it
        nor 0.50. The column was also blank in every production run.
        """
        from shared.score_legends import score_legends_for

        assert gate_columns("esmfold2-design", "scfv") == ()
        assert "iPTM_proxy" not in score_legends_for("esmfold2-design")
        # An scFv design with a measured proxy is therefore unjudged, not
        # judged against a bar this tool cannot state for that mode.
        scfv = _cand("ab_0", 0.30, None, 1, proxy=0.62)
        assert judge(
            "esmfold2-design", scfv, preset="scfv"
        ).verdict == "unjudged"

    def test_an_unknown_mode_reads_no_bar_rather_than_the_wrong_one(self):
        """A job whose stored preset is neither mode (an old default string)
        falls through to no bar, which is the honest answer -- not to
        whichever mode happens to be first in the map."""
        assert gate_columns("esmfold2-design", "default") == ()
        assert judge(
            "esmfold2-design", _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1),
            preset="default",
        ).verdict == "unjudged"

    def test_the_pi_divergence_is_a_rounding_WINDOW_not_a_point(self):
        """BOTH EDGES, and both sides of the comparison.

        The pipeline gates on the RAW ``pi < 6.0``; judge compares ``<= good``
        against the DISPLAYED value, which this column shows at ".2f". The two
        therefore disagree for every reading that is at or above 6.0 and still
        renders "6.00" -- a WINDOW, not the single point "exactly 6.00 and
        nowhere else" the comment used to claim.

        THE EDGE IS DERIVED, NOT HARDCODED. Where ".2f" stops printing "6.00"
        is a float-representation detail (6.005 is stored a hair below and
        still renders 6.00), and a literal for it would pin trivia and go
        stale if the declared precision changed. Ask format_value instead, so
        this test states the rule and survives a precision change by moving
        with it.
        """
        from shared.metric_glossary import format_value
        from tools.esmfold2_design.run_pipeline import _classify

        def _v(pi):
            return judge("esmfold2-design", _cand("d", 0.80, pi, 1),
                         preset="minibinder").verdict

        # Below the bar: both sides pass, whatever it displays as.
        assert _v(5.999) == "meets"
        assert _classify(False, 0.80, None, None, 5.999) == "strict_pass"

        # INSIDE the window: at or above the bar, still displaying "6.00".
        inside = [p for p in (6.0, 6.001, 6.004)
                  if format_value("pI", p) == "6.00"]
        assert len(inside) == 3, "the premise moved; re-derive the window"
        for pi in inside:
            assert _v(pi) == "meets", pi
            # THE PIPELINE'S SIDE, so the divergence cannot quietly cease to
            # exist by _classify's operator changing under us.
            assert _classify(False, 0.80, None, None, pi) == "drop", pi

        # OUTSIDE it: the first reading that renders above the bar closes the
        # divergence, and both sides agree again.
        outside = 6.0051
        assert format_value("pI", outside) != "6.00"
        assert _v(outside) == "below"
        assert _classify(False, 0.80, None, None, outside) == "drop"

    def test_the_bar_is_compared_at_the_precision_the_page_prints(self):
        """A cell reading "5.67" beside a verdict asserting "pI 5.669" is a
        sentence a reader can only read as a bug. score_legends compares at
        the glossary's declared format, so the format has to be declared."""
        from shared.metric_glossary import format_value

        assert format_value("pI", 5.669371223449708) == "5.67"
        # And the shortfall sentence carries that same rendering.
        verdict = judge("esmfold2-design", _cand("d", 0.80, DROP_PI, 1),
                        preset="minibinder")
        assert "pI 11.95, above 6" in verdict.shortfalls[0]

    def test_the_bar_text_and_the_shortfall_text_both_need_the_mode(self):
        """Every sentence-builder that resolves gate columns takes the preset,
        and for a moded tool each returns nothing without it. Only
        ``gate_bar_text`` was reachable from a test before, via verdict_text;
        the other two could drop their argument and stay green."""
        from shared.score_legends import gate_bar_text, shortfall_bar_text

        assert tool_has_bar("esmfold2-design", "minibinder") is True
        assert tool_has_bar("esmfold2-design") is False

        with_mode = gate_bar_text("esmfold2-design", "minibinder")
        assert "pI 6" in with_mode and "ipTM 0.75" in with_mode
        assert gate_bar_text("esmfold2-design") == ""

        # Restricted to the legs a page actually saw fall short.
        assert shortfall_bar_text(
            "esmfold2-design", ("pI",), "minibinder"
        ) == "pI 6"
        assert shortfall_bar_text("esmfold2-design", ("pI",)) == ""

    def test_meets_never_renders_as_a_bare_word(self):
        """verdict_text composes "Meets " + the bar, and for a moded tool the
        bar resolves to nothing without the mode. The bare "Meets" is the same
        shape as the empty "Not measured:" a mis-keyed slug once put in every
        cell on this tool's page."""
        meets = judge("esmfold2-design", _cand(PASS_NAME, PASS_IPTM, PASS_PI, 1),
                      preset="minibinder")
        assert meets.verdict == "meets"
        with_mode = verdict_text("esmfold2-design", meets, preset="minibinder")
        assert with_mode.startswith("Meets ") and len(with_mode) > len("Meets ")
        assert "pI 6" in with_mode and "ipTM 0.75" in with_mode
        assert verdict_text("esmfold2-design", meets) == ""


class TestHeadlineCandidate:
    def test_it_returns_the_first_not_shown_to_fall_short(self):
        rows = [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1),
            _cand(PASS_NAME, PASS_IPTM, PASS_PI, 2),
        ]
        top, verdict = headline_candidate(
            rows, "esmfold2-design", preset="minibinder"
        )
        assert top["name"] == PASS_NAME
        assert verdict.verdict == "meets"

    def test_it_falls_back_to_the_first_record_with_its_verdict(self):
        """And hands back the ``below`` judgement, so a caller cannot present
        the fallback as a pick without also having the shortfall in hand."""
        rows = [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 1),
            _cand(PASS_NAME, 0.9354, 10.2, 2),
        ]
        top, verdict = headline_candidate(
            rows, "esmfold2-design", preset="minibinder"
        )
        assert top["name"] == DROP_NAME
        assert verdict.verdict == "below"

    def test_the_fallback_prefers_a_measured_shortfall_to_a_stand_in(self):
        """When nothing qualifies, WHICH reject leads still matters. A real
        shortfall tells the reader something ("Refolding RMSD 2.40 A, above
        1.5"); a placeholder tells them only that the pipeline failed. The
        first draft took whichever came first in storage order, which put the
        stand-in first here -- and shared/ranking orders them the other way,
        so the headline and the table under it disagreed about the least bad
        design."""
        placeholder = {"rank": 0, "name": "stand_in",
                       "scores": {"pLDDT": 88.0, "refolding_rmsd": 0.0}}
        measured = {"rank": 1, "name": "measured",
                    "scores": {"pLDDT": 70.0, "refolding_rmsd": 2.4}}
        top, verdict = headline_candidate([placeholder, measured], "boltzgen")
        assert top["name"] == "measured"
        assert verdict.verdict == "below"
        # And with no measured reject at all, the stand-in is still shown --
        # a page with candidates has to show one -- carrying its own note.
        only_stand_in, v2 = headline_candidate([placeholder], "boltzgen")
        assert only_stand_in["name"] == "stand_in"
        assert v2.unusable

    def test_a_partly_stand_in_row_does_not_beat_a_cleanly_measured_one(self):
        """BOTH AT ONCE IS A REAL SHAPE, and missing it was a regression.

        ``judge`` returns "below" WITH a non-empty ``unusable`` when one gate
        leg falls short and a DIFFERENT leg holds a declared placeholder. A
        fallback preferring ``verdict == "below"`` alone therefore promoted
        such a row over a purely-unusable one -- and the page's branch chain
        answers "below" first, so the "not usable" note then rendered nowhere.
        A confident shortfall sentence replaced an honest disclosure about a
        design whose other metric was never measured.
        """
        pure = {"rank": 0, "name": "pure_standin",
                "scores": {"pLDDT": 88.0, "refolding_rmsd": 0.0}}
        both = {"rank": 1, "name": "standin_and_short",
                "scores": {"pLDDT": 60.0, "refolding_rmsd": 0.0}}
        # The premise: judge really does return both facts at once.
        v_both = judge("boltzgen", both)
        assert v_both.verdict == "below" and v_both.unusable

        top, verdict = headline_candidate([pure, both], "boltzgen")
        assert top["name"] == "pure_standin"
        assert verdict.unusable, "the placeholder disclosure must survive"

    def test_unjudged_is_eligible_not_sunk(self):
        """Judging unmeasured rows as failures is the mistake that sank 240
        recovered pxdesign rows at ipTM 0.99 below 100 rows at 0.70."""
        rows = [_cand(DROP_NAME, DROP_IPTM, None, 1),
                _cand(PASS_NAME, PASS_IPTM, PASS_PI, 2)]
        top, verdict = headline_candidate(
            rows, "esmfold2-design", preset="minibinder"
        )
        assert top["name"] == DROP_NAME
        assert verdict.verdict == "unjudged"

    def test_empty_and_malformed_inputs(self):
        assert headline_candidate([], "esmfold2-design")[0] is None
        assert headline_candidate(None, "esmfold2-design")[0] is None
        assert headline_candidate(["nope", 3], "esmfold2-design")[0] is None
        top, _v = headline_candidate(
            ["nope", _cand(PASS_NAME, PASS_IPTM, PASS_PI, 1)], "esmfold2-design",
            preset="minibinder",
        )
        assert top["name"] == PASS_NAME


def _recovered_job(job_id: str = "cccccccc-0000-0000-0000-000000000000"):
    """A column whose result a RECOVERY wrote, in that writer's exact shape.

    ``recover_stuck_job_result`` returns ``{candidates, candidate_count,
    backfilled}`` and nothing else (shared/job_recovery.py:287-291);
    scripts/finalize_stuck_job.py:76-80 writes the identical dict. The
    candidates are ``_candidate_from_partial``'s output, which copies only
    ipTM, pLDDT and i_pae off a streamed partial and keys the file as
    ``f"designs/{basename}"`` (shared/job_recovery.py:72-92). So there is no
    ``is_antibody``, no ``filter_status`` and no ``pI`` here -- the base
    ``_job`` fixture's result carries all three and would not be this shape.

    THE SECOND DESIGN IS THE BETTER ONE, and the list is in arrival order, not
    quality order: that is what ``backfilled`` records.
    """
    return SimpleNamespace(
        id=job_id,
        tool="esmfold2-design",
        preset="minibinder",
        status="succeeded",
        result={
            "candidates": [
                {"rank": 0, "pdb_key": "designs/d_first.pdb",
                 "scores": {"ipTM": 0.80, "pLDDT": 0.86}},
                {"rank": 1, "pdb_key": "designs/d_second.pdb",
                 "scores": {"ipTM": 0.99, "pLDDT": 0.93}},
            ],
            "candidate_count": 2,
            "backfilled": True,
        },
        inputs={},
        gpu_seconds_used=120,
    )


class TestARecoveredColumnIsNotGated:
    """This page keeps rendering a backfilled column. The sibling surface does not.

    THESE TESTS INVERT THE USUAL SHAPE ON PURPOSE. Everywhere else in this
    class of work a test fails WITHOUT the gate; these three fail if
    ``shared.jobs.supports_headline_claim`` is ever added to
    ``blueprints/jobs.py::jobs_compare``. That is the decision they exist to
    pin, and the reason is that this page does not make the claim the gate
    refuses.

    WHAT THE GATE REFUSES is a SINGULAR "the top design of this run". The
    completion email makes exactly that claim and publishes the pick and
    nothing else, so a run whose order recovery invented sends a customer one
    number that is not the run's best -- measured at ipTM 0.800 with a 0.99 in
    the same result (tests/test_job_complete_email_headline.py::
    test_a_recovered_run_gets_no_email_callout).

    THIS PAGE MAKES NO SUCH CLAIM. Nothing attached to ``col.top`` is a
    superlative: the shared-metrics row is labelled "Design", the star's
    tooltip says "The design this run leads with", and the only other line is
    "not the first design listed -- row N of M". The subtitle's own comment
    (templates/jobs_compare.html:10-23) records that three drafts of a "best"
    sentence were written and all three deleted, because
    ``headline_candidate`` does not re-rank. And the runner-up is PUBLISHED,
    with its own numbers, four lines above the headline in the same panel --
    which is what ``test_the_better_runner_up_is_published_beside_it`` pins.

    WHAT THE GATE WOULD COST, measured by simulating it on this fixture: with
    ``col.top`` None for the recovered column, ``raw_metric({}, key)`` is not a
    number for every key, no key is shared, and the whole Shared-metrics table
    collapses -- for EVERY column -- to "No shared metrics to compare (jobs use
    different scoring schemas)". Both jobs here are esmfold2-design with the
    same schema, so the page would delete a working table and print a false
    reason for doing it.

    NOT CLOSED BY THIS: the per-column heading "Top candidates (N total)" IS a
    superlative over a list nothing ranked. It reads ``col.candidates``, not
    ``col.top``, so the gate would not have reached it either. Left alone
    rather than fixed here -- ``_candidate_rows`` in this file anchors on that
    literal string, so renaming it is a wider diff than this change.
    """

    def test_a_backfilled_column_still_shows_its_shared_metrics(
        self, flask_app, monkeypatch,
    ):
        html = _render_compare(
            flask_app, monkeypatch, [_recovered_job(), _other_job()],
        )
        assert "No shared metrics to compare" not in _flat(html), (
            "gating col.top on supports_headline_claim empties the "
            "shared-metrics table for EVERY column and blames a schema "
            "difference between two jobs of the same tool"
        )
        # Three decimals here and two in the per-job table above, because this
        # cell goes through ``format_metric_value`` (the glossary's precision)
        # and that one is a flat ``%.2f``.
        assert _metric_row(html, "ipTM") == ["0.800", "0.900"], (
            _metric_row(html, "ipTM")
        )
        assert _metric_row(html, "pLDDT") == ["86.0", "90.1"], (
            _metric_row(html, "pLDDT")
        )

    def test_the_better_runner_up_is_published_beside_it(
        self, flask_app, monkeypatch,
    ):
        """Why the singular claim is not being made: the reader sees both.

        The starred row is the arrival-order first, at ipTM 0.80. The 0.99 is
        on the same page, in the same panel, un-starred and one row down. An
        email carrying the same pick carries only the pick.
        """
        html = _render_compare(
            flask_app, monkeypatch, [_recovered_job(), _other_job()],
        )
        rows = _candidate_rows(html)
        assert rows[0][1] == "0.80", rows
        assert "&#9733;" in rows[0][0] or "★" in rows[0][0], rows[0][0]
        assert rows[1][1] == "0.99", rows
        assert "&#9733;" not in rows[1][0] and "★" not in rows[1][0], (
            rows[1][0]
        )

    def test_the_design_row_makes_no_superlative_claim(
        self, flask_app, monkeypatch,
    ):
        """The words are the evidence, so they are the assertion.

        If a superlative is ever added to this row the reasoning above stops
        holding and the gate becomes the right answer -- this is the test that
        says so out loud rather than leaving the decision undated.
        """
        html = _render_compare(
            flask_app, monkeypatch, [_recovered_job(), _other_job()],
        )
        row = _design_row(html)
        assert "designs/d_first.pdb" in row, row
        for word in ("best", "top ", "winner", "highest", "strongest",
                     "leading"):
            assert word not in row.lower(), (
                f"the Design row now claims a superlative ({word!r}); a "
                f"backfilled column cannot support one, so this page needs "
                f"shared.jobs.supports_headline_claim after all: {row!r}"
            )


def test_every_moded_leg_is_declared_for_a_real_mode():
    """A mode key nothing resolves to is a silently disabled bar -- the exact
    trap ``esmfold2_design`` vs ``esmfold2-design`` sprang on this same tool.
    ``result_mode`` is the only producer of these keys, so it is the authority
    on what they may be."""
    produced = {result_mode({"is_antibody": True}),
                result_mode({"is_antibody": False})}
    for tool, modes in MODE_GATE_COLUMNS.items():
        unknown = set(modes) - produced
        assert not unknown, (
            f"{tool} declares gate columns for {sorted(unknown)}, which "
            f"result_mode never returns; reachable modes are {sorted(produced)}"
        )
