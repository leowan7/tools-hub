"""Two ARTIFACTS that leave this site must not speak for a rejected design.

The Share text and the per-job FASTA export. Both hand something to a reader
with no page around it: the Share text is handed to the OWNER for a LinkedIn
or X compose box -- ``/jobs/<id>/share`` is ``@login_required`` and
``templates/job_detail.html`` defines no ``og_title`` block, so it is pasted,
not auto-published -- and the .fasta is opened later in a sequence tool.
Neither has a cell beside the number to carry a shortfall, so a design the
tool's own bar drops has to be either not named or named as dropped.

Real completed job ``2b917b54-0871-44af-a3d1-5d07ea5dcaeb`` (esmfold2-design,
PD-L1 minibinder, n_seeds=2), as it sits in the database:

    seed0: ipTM 0.9556, pI 11.95 -> the pipeline drops it
    seed1: ipTM 0.9354, pI  5.67 -> clears the bar

``candidates[0]`` is seed0. The share route read it blind; the FASTA numbered
it ``rank1`` with nothing to say it is the one design the tool's own worked
example exists to tell you not to order.

WHAT THE CARD ACTUALLY PUBLISHED WAS NOT 0.956, and an earlier version of this
docstring said it was. The metric was "first numeric key in ``scores``", and
``result`` is a ``jsonb`` column whose keys Postgres normalises by (length,
bytewise) -- so the first key is ``pI`` and the card read "Top score pI
11.955". The rejected design was named; its ipTM never was. See :func:`_jsonb`,
and note that the fixture here is built in the STORED order for that reason.

THE SURFACES REPAIRED ALONGSIDE THESE TWO are the TARGET count and the target
ranking table, driven through the aggregator in tests/test_aggregate_target.py
because that is their real path. THE CAMPAIGN COUNT IS NOT COVERED THERE and
an earlier version of this docstring claimed it was: its real path is
``blueprints/campaigns.py::_campaign_candidates_meeting_bar`` ->
``/campaigns/<id>/status.json``, which the aggregator never touches. See
:class:`TestCampaignSurfaces` below, which reaches it.

EVERY TEST IN A CLASS HERE GOES THROUGH A ROUTE, via the Flask test client.
Calling ``_top_score_for_share`` or ``candidates_to_fasta`` directly would
leave the route free to not call them, which is exactly how the sibling
surfaces stayed green through #241: the helper was right and nothing reached
it. The module-level tests are declared invariants and say so in their own
docstrings; an earlier version of this paragraph said EVERY test went through
a route, which was false of them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# THE VALUES AS JOB 2b917b54 ACTUALLY STORES THEM, unrounded, matching
# tests/test_jobs_compare_headline.py. Rounded stand-ins let a display-precision
# assertion span both the defect and its fix.
DROP_PI = 11.954517555236816
PASS_PI = 5.669371223449708
DROP_IPTM = 0.9555796384811401
PASS_IPTM = 0.9353567957878113

DROP_NAME = "design_0"
PASS_NAME = "design_1"


def _cand(name: str, iptm, pi, rank: int, *, sequence="MKTAYIAKQRQISFVK") -> dict:
    """One record in the shape ``tools/esmfold2_design/run_pipeline.py``
    stores under ``result["candidates"]``."""
    return {
        "rank": rank,
        "name": name,
        "pdb_key": f"designs/{name}_complex.pdb",
        "sequence": sequence,
        # IN THE STORED KEY ORDER, not the pipeline's. See _jsonb.
        "scores": _jsonb({"ipTM": iptm, "iPTM_proxy": None, "pI": pi,
                          "final_loss": 1.0}),
    }


def _job(**over):
    """Job 2b917b54 as stored: the rejected design is ``candidates[0]``."""
    result = {
        "status": "COMPLETED",
        "preset": "minibinder",
        "is_antibody": False,
        "candidates": [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 0),
            _cand(PASS_NAME, PASS_IPTM, PASS_PI, 1),
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


@pytest.fixture
def flask_app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _install_job(monkeypatch, job):
    """Auth and the DB read are stubbed; nothing else is.

    ``get_job`` is patched on ``blueprints.jobs`` because that is where the
    module-level import bound it. ``resolve_user_email_and_meta`` is imported
    INSIDE ``job_share``, so it has to be patched on ``shared.jobs`` instead --
    patching the blueprint would install an attribute the route never reads
    and the real function would still run.
    """
    import blueprints.jobs as bp
    import shared.jobs as shared_jobs

    monkeypatch.setattr(
        bp, "load_user_context", lambda: SimpleNamespace(user_id="u")
    )
    monkeypatch.setattr(
        bp, "get_job", lambda job_id, user_id=None: job
    )
    monkeypatch.setattr(
        shared_jobs,
        "resolve_user_email_and_meta",
        lambda user_id: ("share@example.com", {"allow_share": True}),
    )


def _client(flask_app):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "share@example.com"
    return client


def _share(flask_app, monkeypatch, job) -> dict:
    _install_job(monkeypatch, job)
    resp = _client(flask_app).post(f"/jobs/{job.id}/share")
    assert resp.status_code == 200, (
        f"share route did not render: {resp.status_code} {resp.get_data(as_text=True)[:200]}"
    )
    return json.loads(resp.get_data(as_text=True))


def _fasta(flask_app, monkeypatch, job) -> str:
    _install_job(monkeypatch, job)
    resp = _client(flask_app).get(f"/jobs/{job.id}/export.fasta")
    assert resp.status_code == 200, f"fasta route: {resp.status_code}"
    return resp.get_data(as_text=True)


def _headers(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if ln.startswith(">")]


_URL = "tools.ranomics.com"


def _clause(og_title: str) -> str | None:
    """The og:title's trailing claim, or None when it makes none.

    ASSERT ON THIS, NOT ON A PHRASE. ``"Top score" not in title`` was the
    original test for "the card says nothing about scores", and rewording the
    copy to "Top design" turned every one of those assertions into a sentence
    about a string the card can no longer produce -- true forever, of nothing.
    The card has exactly two shapes: it ends at the URL, or it appends one
    clause. This reads which.
    """
    head, sep, tail = og_title.partition(_URL)
    assert sep, f"og:title did not contain the site URL at all: {og_title!r}"
    return tail.strip(" .") or None


def _jsonb(scores: dict) -> dict:
    """``scores`` in the key order PostgREST actually returns.

    ``tool_jobs.result`` is a ``jsonb`` column
    (supabase/migrations/0005_tool_jobs.sql:33) and Postgres normalises jsonb
    object keys by (length, bytewise) -- it does NOT preserve insertion order.
    So a fixture written in the order ``run_pipeline.py`` emits is not the
    shape any route ever receives.

    THIS IS NOT PEDANTRY: it hid a live defect. The share card used to print
    the first numeric key of ``scores``, and under this ordering that is
    ``pI`` -- two characters, so it sorts ahead of ``ipTM`` every time. The
    card published "Top score pI 5.669", an isoelectric point announced as a
    score, while a hand-ordered fixture showed a reassuring "ipTM 0.935".
    """
    return {k: scores[k] for k in sorted(scores, key=lambda k: (len(k), k.encode()))}


def test_no_campaign_tool_needs_a_mode():
    """Two comments in blueprints/campaigns.py rest on this and neither can
    check it: the campaign FASTA export passes no preset, and the quality
    card's docstring says threading one "changes nothing for any tool
    campaigns can currently run". Both are true only while no moded tool is
    campaign-able. Adding one to SUPPORTED_TOOLS is a one-line change in
    another file, and it would leave those two sentences asserting something
    false about a card a customer reads.

    A moded tool arriving here fails safe -- no mode means no bar, which is
    what those surfaces show today -- so this fails as a prompt to update the
    prose and the export, not as a report of a live defect.
    """
    from shared.compute_campaigns import SUPPORTED_TOOLS
    from shared.score_legends import MODE_GATE_COLUMNS

    assert not (set(SUPPORTED_TOOLS) & set(MODE_GATE_COLUMNS))


class TestShareCard:
    def test_the_og_title_names_the_design_that_clears_the_bar(
        self, flask_app, monkeypatch,
    ):
        """Two independent defects meet on this card, and the fixture is in
        the STORED key order so neither can hide.

        WHICH DESIGN: seed0 is the one the pipeline drops, and the route read
        ``candidates[0]`` blind because the tool's bar needs the run's mode to
        exist at all.

        WHICH NUMBER: the metric used to be "first numeric key in ``scores``",
        which under jsonb ordering is ``pI``. So the card announced an
        isoelectric point as a score. It now names the tool's ranking metric.

        AND THE SUPERLATIVE IS QUALIFIED, because a bar applied here: seed0
        outranks seed1 on ipTM and was dropped, so the run's own results page
        still shows 0.956 at the top. A bare "Top score 0.935" contradicts it.
        """
        title = _share(flask_app, monkeypatch, _job())["og_title"]
        assert "0.956" not in title, (
            f"the rejected design's number reached a public share card: {title}"
        )
        assert "pI" not in title, (
            f"an isoelectric point was published as a score: {title}"
        )
        assert _clause(title) == "Top design meeting our quality bar: ipTM 0.935"

    def test_no_score_clause_when_nothing_clears_the_bar(
        self, flask_app, monkeypatch,
    ):
        """A run where every design fell short says nothing about scores
        rather than publishing the least-bad one. There is nowhere in an
        og:title to write the shortfall beside it."""
        job = _job(result_over={"candidates": [
            _cand(DROP_NAME, DROP_IPTM, DROP_PI, 0),
            _cand(PASS_NAME, PASS_IPTM, 10.2, 1),
        ]})
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title
        # And it still says the run happened -- the clause is dropped, not the
        # card.
        assert "ESMFold2" in title

    def test_the_mode_comes_off_the_result_not_the_stored_preset(
        self, flask_app, monkeypatch,
    ):
        """THAT ORDER. The stored preset says minibinder; the result records
        that the run was an antibody one. scfv has no entry in
        MODE_GATE_COLUMNS, so there is no bar and the leading design speaks
        for the run -- reading the preset first would apply the minibinder pI
        gate to a mode where pI is null by construction."""
        job = _job(result_over={"is_antibody": True})
        title = _share(flask_app, monkeypatch, job)["og_title"]
        # scfv has no entry in MODE_GATE_COLUMNS and esmfold2-design registers
        # no primary metric, so the honest answer is silence. Resolving the
        # PRESET first would read "minibinder", apply the pI/ipTM bar, and
        # publish a clause -- which is what makes this assertion discriminating
        # rather than merely quiet.
        assert _clause(title) is None, title

    def test_an_unranked_designs_list_gets_no_score_at_all(
        self, flask_app, monkeypatch,
    ):
        """THE REGRESSION THIS ROUTE'S WIDENING INTRODUCED, and the reason
        ``supports_headline_claim`` exists.

        Moving from ``result["candidates"]`` to ``candidate_records`` was a fix
        for the designs-only tools -- and it also reached shapes that are NOT
        ranked. af2 / colabfold / esmfold at the ``batch`` tier store one
        record per INDEPENDENTLY SUBMITTED sequence in submission order
        (``designs_out`` is appended to and never sorted), so ``designs[0]`` is
        whichever sequence the customer pasted first. Before the gate this
        route published "Top score plddt 55.000" -- seqA -- on a PUBLIC share
        card in a run where seqB scored 0.91.

        origin/main emitted no clause for these tools at all, so abstaining
        restores exactly what they had rather than inventing a third
        behaviour.
        """
        job = _job(
            tool="af2", preset="batch",
            result_over={"is_antibody": None, "candidates": None, "designs": [
                {"name": "seqA", "pdb_key": "designs/seqA.pdb",
                 "ptm": 0.400, "plddt": 0.55},
                {"name": "seqB", "pdb_key": "designs/seqB.pdb",
                 "ptm": 0.950, "plddt": 0.91},
            ]},
        )
        job.result.pop("candidates")
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title

    def test_a_bar_does_not_substitute_for_an_order(
        self, flask_app, monkeypatch,
    ):
        """A two-armed gate that also passed on ``tool_has_bar`` was written
        and retracted before shipping. ``headline_candidate`` DOES NOT
        RE-RANK: it returns the first record not shown to fall short, in
        stored order, so on an unranked list a bar only narrows WHICH
        arbitrary record is crowned.

        boltz2 is the case -- it declares a bar AND stores an unranked
        ``designs`` list built straight off the pasted sequences. Both designs
        here clear its bar; the first one listed is the worse one. A bar-armed
        gate would publish 0.710 with 0.950 in the same run.

        BOTH DESIGNS CLEAR DELIBERATELY, but NOT because a single-clearing
        fixture would leave this assertion green -- an earlier version of this
        docstring claimed that and a reviewer disproved it by building the
        variant: with only one clearing design the gate still passes, the card
        still speaks, and ``_clause(...) is None`` still fails. The reason is
        evidential rather than mechanical. The retracted arm's defect is that
        a bar NARROWS an arbitrary choice instead of ordering it, and that is
        only visible when more than one record survives the narrowing. One
        clearing design proves the gate rejects the shape; two prove WHY the
        bar could never have replaced it.
        """
        job = _job(
            tool="boltz2", preset="pilot",
            result_over={"is_antibody": None, "designs": [
                {"name": "b0", "pdb_key": "designs/b0.pdb", "rank": 0,
                 "iptm": 0.710, "plddt": 88.0, "n_hotspot_contacts": 5},
                {"name": "b1", "pdb_key": "designs/b1.pdb", "rank": 1,
                 "iptm": 0.950, "plddt": 90.0, "n_hotspot_contacts": 6},
            ]},
        )
        job.result.pop("candidates")
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title

    def test_a_tool_with_no_bar_is_untouched(self, flask_app, monkeypatch):
        """bindcraft declares no gate columns, so its records are
        ``unjudged`` -- which is not ``below`` -- and the card still leads
        with its stored first design. The widening must not swallow the eight
        registered tools that have no bar to fall short of."""
        job = _job(
            tool="bindcraft", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "bc_0", "pdb_key": "designs/bc_0.pdb",
                 "sequence": "MKTAY", "scores": {"ipTM": 0.701}},
            ]},
        )
        title = _share(flask_app, monkeypatch, job)["og_title"]
        # PLAIN "Top design", unqualified: no bar applied, so the pick really
        # is the container's rank 1 and nothing was dropped above it.
        assert _clause(title) == "Top design: ipTM 0.701", title


class TestFastaExport:
    def test_the_rejected_record_says_so_and_keeps_its_rank(
        self, flask_app, monkeypatch,
    ):
        """rank1 is still seed0 -- the CSV and the ZIP number the same rows
        off the same ``export_key``, so re-sorting only this format would end
        that silently -- but it now carries WHY it is a reject, and the design
        that clears the bar carries nothing."""
        headers = _headers(_fasta(flask_app, monkeypatch, _job()))
        assert len(headers) == 2, headers
        assert headers[0].startswith(">rank1_design_0_complex.pdb"), headers[0]
        assert "does not meet bar" in headers[0], headers[0]
        assert "pI 11.95" in headers[0], headers[0]
        assert headers[1] == ">rank2_design_1_complex.pdb", headers[1]

    def test_the_note_is_a_description_so_the_id_still_parses(
        self, flask_app, monkeypatch,
    ):
        """A FASTA id ends at the first whitespace; everything after it is
        free text. ``shared.exports._basename`` already strips whitespace OUT
        of the id for exactly that reason, so the note has to live on the
        other side of the boundary or it would truncate the provenance the id
        carries."""
        headers = _headers(_fasta(flask_app, monkeypatch, _job()))
        ident = headers[0][1:].split(None, 1)[0]
        assert ident == "rank1_design_0_complex.pdb", ident

    def test_the_sequences_are_untouched(self, flask_app, monkeypatch):
        body = _fasta(flask_app, monkeypatch, _job())
        seq_lines = [ln for ln in body.splitlines() if not ln.startswith(">")]
        assert seq_lines == ["MKTAYIAKQRQISFVK", "MKTAYIAKQRQISFVK"], seq_lines

    def test_an_scfv_run_gets_no_notes(self, flask_app, monkeypatch):
        """The scFv mode declares no bar (its proxy leg was added and removed
        in review -- a mode-scoped BAR cannot carry a mode-scoped LEGEND), so
        every record is unjudged and the file reads exactly as it did before
        this change."""
        job = _job(result_over={"is_antibody": True})
        headers = _headers(_fasta(flask_app, monkeypatch, job))
        assert headers == [">rank1_design_0_complex.pdb",
                           ">rank2_design_1_complex.pdb"], headers

    def test_a_tool_with_no_bar_gets_no_notes(self, flask_app, monkeypatch):
        job = _job(
            tool="bindcraft", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "bc_0", "pdb_key": "designs/bc_0.pdb",
                 "sequence": "MKTAY", "scores": {"ipTM": 0.10}},
            ]},
        )
        headers = _headers(_fasta(flask_app, monkeypatch, job))
        assert headers == [">rank1_bc_0.pdb"], headers

    def test_an_unusable_record_gets_its_own_disclosure_not_a_shortfall(
        self, flask_app, monkeypatch,
    ):
        """``verdict_text`` is the single renderer for a reason: a record can
        be BOTH below and carrying a declared placeholder, and hand-joining
        ``verdict.shortfalls`` drops the ``unusable`` half. boltzgen's
        refolding RMSD of exactly 0.00 is the placeholder the pipeline never
        produces; a pLDDT of 40 is a real shortfall beside it.
        """
        job = _job(
            tool="boltzgen", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "bg_0", "pdb_key": "designs/bg_0.pdb",
                 "sequence": "MKTAY",
                 "scores": {"pLDDT": 40.0, "refolding_rmsd": 0.0}},
            ]},
        )
        header = _headers(_fasta(flask_app, monkeypatch, job))[0]
        assert "does not meet bar" in header, header
        assert "not usable" in header, header


    def test_a_purely_unusable_record_renders_the_other_branch(
        self, flask_app, monkeypatch,
    ):
        """The SIBLING of the test above, and without it half the note-
        rendering code is unreached. A record can be unusable WITHOUT being
        below: boltzgen's refolding RMSD of exactly 0.00 is a declared
        placeholder while its pLDDT of 88 clears. ``judge`` returns
        ``unjudged`` + ``unusable`` and the header takes the second branch --
        a bare disclosure with no "does not meet bar" lead-in, because
        nothing was shown to fall short.

        Every other fixture in this file that is unusable is ALSO below, so
        that branch could be replaced with any string at all and stay green.
        """
        job = _job(
            tool="boltzgen", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "bg_0", "pdb_key": "designs/bg_0.pdb",
                 "sequence": "MKTAY",
                 "scores": {"pLDDT": 88.0, "refolding_rmsd": 0.0}},
            ]},
        )
        header = _headers(_fasta(flask_app, monkeypatch, job))[0]
        assert "does not meet bar" not in header, header
        assert header.endswith("[Not usable: Refolding RMSD]"), header

    def test_a_purely_unusable_pick_gets_no_share_clause(
        self, flask_app, monkeypatch,
    ):
        """The ``or verdict.unusable`` half of the share guard, which no other
        test reaches: every other non-qualifying fixture is ``below``, so
        narrowing the guard to ``verdict.verdict == "below"`` alone is
        invisible.

        This design's metrics are a declared placeholder, not a measurement.
        Without that half the card would publish ``Top design: ipTM 0.950``
        for a run whose numbers the pipeline could not produce.
        """
        job = _job(
            tool="boltzgen", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "bg_0", "pdb_key": "designs/bg_0.pdb",
                 "scores": {"ipTM": 0.95, "pLDDT": 88.0,
                            "refolding_rmsd": 0.0}},
            ]},
        )
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title


class TestTargetExportJudgesPerRow:
    """The MERGED target export, whose bar notes come from a branch nothing
    else covers.

    ``blueprints/targets.py`` passes NO scalar tool/preset -- its rows span
    every run on the target -- so every note in that file is produced by
    ``shared.exports._bar_scope`` reading each row's own ``_source_tool`` /
    ``_source_preset``. Disabling that branch entirely was invisible to the
    whole suite.
    """

    def test_a_merged_row_is_judged_by_its_own_provenance(self):
        from shared.exports import candidates_to_fasta

        rows = [
            {"_source_tool": "esmfold2-design", "_source_preset": "minibinder",
             "pdb_key": "drop.pdb", "sequence": "MK",
             "scores": {"ipTM": DROP_IPTM, "pI": DROP_PI}},
            {"_source_tool": "boltzgen", "_source_preset": "pilot",
             "pdb_key": "bg.pdb", "sequence": "MK",
             "scores": {"pLDDT": 88.0, "refolding_rmsd": 1.0}},
        ]
        headers = _headers(candidates_to_fasta(rows))
        # Judged with NO scalars passed, each against ITS OWN tool's bar.
        assert "does not meet bar: pI 11.95" in headers[0], headers[0]
        # And the boltzgen row, which clears its own different bar, is bare.
        assert headers[1] == ">rank2_boltzgen_bg.pdb", headers[1]

    def test_the_row_preset_selects_the_mode(self):
        """``_source_preset`` carries the resolved MODE on merged rows, so a
        row tagged scfv must read no bar even with a pI that would fail the
        minibinder one."""
        from shared.exports import candidates_to_fasta

        rows = [
            {"_source_tool": "esmfold2-design", "_source_preset": "scfv",
             "pdb_key": "ab.pdb", "sequence": "MK",
             "scores": {"ipTM": DROP_IPTM, "pI": DROP_PI}},
        ]
        assert _headers(candidates_to_fasta(rows)) == [
            ">rank1_esmfold2-design_ab.pdb"
        ]


class TestCampaignSurfaces:
    """The campaign half of this change, which the aggregator tests cannot see.

    ``blueprints/campaigns.py`` has TWO call sites in this diff and neither
    was covered: the export passes ``tool=`` to ``candidates_to_fasta`` and
    the quality card threads ``preset`` into ``count_candidates_meeting_bar``.
    A review found both mutable to no effect across the whole suite -- the
    export because nothing asserted on a campaign FASTA's contents, the count
    because ``_campaign_candidates_meeting_bar`` and ``/campaigns/<id>/
    status.json`` have no test references anywhere in the repo.
    """

    def test_the_campaign_fasta_carries_bar_notes(self, flask_app, monkeypatch):
        """The user-facing half, and it is a real behaviour change: campaign
        exports for every gating tool now disclose which designs fell short.

        Driven through GET /campaigns/<id>/export.fasta. Dropping the
        ``tool=`` argument leaves this file silent about a design nobody
        should order.
        """
        import blueprints.campaigns as bp
        from shared import compute_campaigns as cc

        monkeypatch.setattr(
            bp, "load_user_context", lambda: SimpleNamespace(user_id="u")
        )
        monkeypatch.setattr(
            cc, "aggregate_campaign_candidates",
            lambda cid, user_id=None, limit=None: {
                "tool": "boltzgen", "total": 2, "capped": False, "columns": [],
                "candidates": [
                    {"pdb_key": "good.pdb", "sequence": "MK",
                     "scores": {"pLDDT": 88.0, "refolding_rmsd": 1.0}},
                    {"pdb_key": "bad.pdb", "sequence": "MK",
                     "scores": {"pLDDT": 40.0, "refolding_rmsd": 1.0}},
                ],
            },
        )
        resp = _client(flask_app).get("/campaigns/c-1/export.fasta")
        assert resp.status_code == 200, resp.status_code
        headers = _headers(resp.get_data(as_text=True))
        assert headers[0] == ">rank1_good.pdb", headers[0]
        assert "does not meet bar: pLDDT 40.0, below 80" in headers[1], headers[1]

    def test_the_campaign_count_hands_each_child_its_preset(self, monkeypatch):
        """The count's ``preset`` argument, pinned by OBSERVING the call.

        It cannot be pinned by its effect: the preset only changes an answer
        for a tool in MODE_GATE_COLUMNS, and no such tool is campaign-able --
        which ``test_no_campaign_tool_needs_a_mode`` asserts in the other
        direction. Constructing a moded campaign tool to get an effect would
        build a state production forbids, so this asserts the WIRING instead
        and says so rather than dressing a spy up as a behaviour test.
        """
        import blueprints.campaigns as bp
        from shared import compute_campaigns as cc
        import shared.jobs as shared_jobs

        seen = []
        monkeypatch.setattr(
            bp, "get_service_client", lambda: object()
        )
        monkeypatch.setattr(
            cc, "iter_succeeded_children",
            lambda cid, client, columns=None: [
                {"result": {"candidates": [{"scores": {}}]}},
                {"result": {"candidates": [{"scores": {}}]}},
            ],
        )
        real = shared_jobs.count_candidates_meeting_bar

        def _spy(result, tool, preset=None):
            seen.append((tool, preset))
            return real(result, tool, preset)

        monkeypatch.setattr(shared_jobs, "count_candidates_meeting_bar", _spy)
        bp._campaign_candidates_meeting_bar("c-1", "boltzgen", "pilot")
        assert seen == [("boltzgen", "pilot"), ("boltzgen", "pilot")], seen
