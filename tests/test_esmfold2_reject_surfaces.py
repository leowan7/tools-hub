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

THE SHARE AND FASTA TESTS GO THROUGH THEIR ROUTE, via the Flask test client,
because calling ``_top_score_for_share`` or ``candidates_to_fasta`` directly
would leave the route free to not call them -- exactly how the sibling
surfaces stayed green through #241: the helper was right and nothing reached
it.

NOT EVERY TEST HERE DOES, and two earlier versions of this paragraph claimed
otherwise -- first of all tests, then of all tests in a class, which was still
false of three added in the same commit. The exceptions, each stated in its
own docstring: the THREE module-level tests, ``TestTargetExportJudgesPerRow``
(the target export merges rows across runs, so the unit under test is the
per-row provenance branch rather than any one route), and
``TestCampaignSurfaces``'s count test, which observes a call because its
effect is unreachable.
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
    original test for "the card says nothing about scores". #266 reworded the
    copy to "One design at", turning every one of those assertions into a
    sentence about a string the card can no longer produce -- true forever, of
    nothing.
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
    text quoted "Top score pI 5.669", an isoelectric point announced as a
    score, while a hand-ordered fixture showed a reassuring "ipTM 0.935".
    """
    return {k: scores[k] for k in sorted(scores, key=lambda k: (len(k), k.encode()))}


def test_the_plddt_preference_is_complete():
    """``blueprints.jobs._PLDDT_PREFERENCE`` must cover every spelling in
    ``metric_glossary.PLDDT_COLUMNS``.

    That constant is a FROZENSET, and the share card cannot iterate it to
    choose which number to publish -- picking a metric out of a set is the
    same non-determinism that let dict order publish an isoelectric point. So
    the card carries its own ORDERED copy, and this holds the two together: a
    tenth spelling added to the glossary would otherwise be unreachable on the
    card with nothing failing.
    """
    from blueprints.jobs import _PLDDT_PREFERENCE
    from shared.metric_glossary import PLDDT_COLUMNS

    assert set(_PLDDT_PREFERENCE) == set(PLDDT_COLUMNS)
    assert len(_PLDDT_PREFERENCE) == len(set(_PLDDT_PREFERENCE)), "duplicate"


def test_the_plddt_preference_prefers_the_canonical_spelling():
    """COMPLETENESS IS NOT ORDER, and the guard above pins only the former.

    Reversing the tuple leaves the whole suite green unless some record
    carries TWO spellings -- which is the only situation the ordering exists
    to decide. The production comment says "canonical first"; this is what
    makes that claim checkable rather than decorative.
    """
    from blueprints.jobs import _share_headline_metric

    record = {"scores": {"pLDDT": 90.0, "af2_plddt": 0.10}}
    # af2 has no bar and no ranking metric, so the pLDDT arm decides.
    assert _share_headline_metric("af2", None, record) == ("pLDDT", 90.0)


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
        isoelectric point as a score. It now names a leg of the run's own
        quality bar (``blueprints.jobs._share_headline_metric``).

        AND THE SENTENCE CROWNS NOTHING, because a bar applied here: seed0
        outranks seed1 on ipTM and was dropped, so the run's own results page
        still shows 0.956 at the top. "Top score 0.935" would contradict that
        page; "One design at 0.935" does not.
        """
        title = _share(flask_app, monkeypatch, _job())["og_title"]
        assert "0.956" not in title, (
            f"the rejected design's number reached the share text: {title}"
        )
        assert "pI" not in title, (
            f"an isoelectric point was published as a score: {title}"
        )
        assert _clause(title) == "One design at ipTM 0.935"

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
        route quoted "Top score plddt 55.000" -- seqA -- in a run where seqB
        scored 0.91.

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

    def test_a_recovered_run_gets_no_score_at_all(
        self, flask_app, monkeypatch,
    ):
        """THE CANONICAL SHAPE WITHOUT THE ORDER BEHIND IT.

        The test above abstains on the ``designs`` SHAPE. This one carries
        ``candidates`` -- the shape that test admits -- and must still abstain,
        because no container ranked it. ``recover_stuck_job_result`` rebuilds
        ``candidates`` for ANY tool, with no tool branch above it
        (shared/job_recovery.py:286-291), filling it from the streamed partials
        by ``.append()`` or else from a Storage file listing by ``enumerate``,
        neither of which sorts (shared/job_recovery.py:126-146). The row is
        then stored ``succeeded`` (shared/jobs.py:1055), so it reaches this
        route exactly as a webhook row would.

        The fixture's FIRST record is the worse of the two, which is the whole
        point: stream order is arrival order, not rank.
        """
        recovered = {
            "is_antibody": None,
            "candidates": [
                {"rank": 1, "name": "d0", "pdb_key": "designs/d0.pdb",
                 "sequence": "MK", "scores": _jsonb({"pLDDT": 55.0})},
                {"rank": 2, "name": "d1", "pdb_key": "designs/d1.pdb",
                 "sequence": "MK", "scores": _jsonb({"pLDDT": 91.0})},
            ],
            "candidate_count": 2,
            "backfilled": True,
        }
        job = _job(tool="af2", preset="batch", result_over=recovered)
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title

        # THE CONTROL. The same records without the flag DO get a clause, so
        # the silence above is the flag's doing and not the fixture quietly
        # failing some other leg of the chain.
        job = _job(tool="af2", preset="batch",
                   result_over=dict(recovered, backfilled=False))
        unflagged = _clause(_share(flask_app, monkeypatch, job)["og_title"])
        assert unflagged == "One design at pLDDT 55.000", unflagged

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

        BOTH DESIGNS CLEAR DELIBERATELY, and this docstring has now been
        wrong about WHY twice. It first claimed a single-clearing fixture
        would leave the assertion green; a reviewer disproved that. It then
        said the card "still speaks" under that variant -- which a SECOND
        reviewer disproved, because these records keyed their metrics at the
        root in lowercase and the metric chain resolved nothing, so the card
        was silent for a reason unrelated to the gate and this test passed
        even with the retracted two-armed gate restored. The keys are spelled
        canonically now, and the chain resolves aliases besides. The reason is
        evidential rather than mechanical. The retracted arm's defect is that
        a bar NARROWS an arbitrary choice instead of ordering it, and that is
        only visible when more than one record survives the narrowing. One
        clearing design proves the gate rejects the shape; two prove WHY the
        bar could never have replaced it.
        """
        job = _job(
            tool="boltz2", preset="pilot",
            result_over={"is_antibody": None, "designs": [
                # Metrics under ``scores`` with the canonical spellings, so
                # the card is silent because of the SHAPE GATE and not because
                # the chain failed to resolve a column. With these at the root
                # as `iptm`/`plddt` this test passed under the very gate it
                # exists to forbid.
                {"name": "b0", "pdb_key": "designs/b0.pdb", "rank": 0,
                 "scores": {"ipTM": 0.710, "pLDDT": 88.0,
                            "n_hotspot_contacts": 5}},
                {"name": "b1", "pdb_key": "designs/b1.pdb", "rank": 1,
                 "scores": {"ipTM": 0.950, "pLDDT": 90.0,
                            "n_hotspot_contacts": 6}},
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
        assert _clause(title) == "One design at ipTM 0.701", title


    def test_a_purely_unusable_pick_gets_no_share_clause(
        self, flask_app, monkeypatch,
    ):
        """The ``or verdict.unusable`` half of the share guard, which no other
        test reaches: every other non-qualifying fixture is ``below``, so
        narrowing the guard to ``verdict.verdict == "below"`` alone is
        invisible.

        This design's metrics are a declared placeholder, not a measurement.
        Without that half the card would publish ``One design at ipTM 0.950``
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


class TestFastaExport:
    def test_the_rejected_record_says_so_and_keeps_its_rank(
        self, flask_app, monkeypatch,
    ):
        """rank1 is still seed0 -- the CSV numbers the same rows off the
        same ``export_key``, so re-sorting only this format would end that
        silently (the ZIP names entries from ``pdb_key``, not the rank) -- but it now carries WHY it is a reject, and the design
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

        Before this test and its share-card sibling, every unusable fixture
        in this file was ALSO below, so this branch could have been replaced
        with any string at all and stayed green.
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
            # THE SAME METRICS UNDER THE OTHER MODE. Without this row the test
            # is a bare negative -- "no mode" and "scfv" both produce a bare
            # header, so dropping _source_preset entirely left it green. The
            # pair makes the mode the ONLY difference between two rows whose
            # numbers are identical.
            {"_source_tool": "esmfold2-design", "_source_preset": "minibinder",
             "pdb_key": "mb.pdb", "sequence": "MK",
             "scores": {"ipTM": DROP_IPTM, "pI": DROP_PI}},
        ]
        headers = _headers(candidates_to_fasta(rows))
        assert headers[0] == ">rank1_esmfold2-design_ab.pdb", headers[0]
        assert "does not meet bar: pI 11.95" in headers[1], headers[1]


class TestCampaignSurfaces:
    """The campaign half of this change, which the aggregator tests cannot see.

    ``blueprints/campaigns.py`` has TWO call sites in this diff and neither
    was covered: the export passes ``tool=`` to ``candidates_to_fasta`` and
    the quality card threads ``preset`` into ``count_candidates_meeting_bar``.
    A review found both mutable to no effect across the whole suite -- the
    export because nothing asserted on a campaign FASTA's contents, the count
    because at 3dbcb9d ``_campaign_candidates_meeting_bar`` and
    ``/campaigns/<id>/status.json`` had no test references anywhere in the
    repo -- this class is the first.
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


class TestTheHeadlineMetricChain:
    """WHICH number the share text quotes, per tool.

    Three of these four are regressions an independent review caught in the
    FIRST repair, which preferred the tool's registered ranking metric over
    the bar: boltzgen, rfantibody and proteina. The iggm case is NOT one --
    that tool quoted nothing before the repair and nothing after it, and is
    here because the alias resolution it needs was added later.
    """

    def _clause_for(self, flask_app, monkeypatch, tool, preset, scores, **over):
        job = _job(
            tool=tool, preset=preset,
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "c0", "pdb_key": "designs/c0.pdb",
                 "sequence": "MK", "scores": _jsonb(scores)},
            ], **over},
        )
        return _clause(_share(flask_app, monkeypatch, job)["og_title"])

    def test_boltzgen_quotes_a_leg_of_its_own_bar(
        self, flask_app, monkeypatch,
    ):
        """A NUMBER QUOTED UNDER A BAR MUST BE ONE THAT BAR READS.

        boltzgen's registered ranking metric is ipTM, and ipTM is DELIBERATELY
        not a leg of its bar -- BoltzGen refolds the design alone, so its ipTM
        is not the cofold quantity 0.70 describes (see GATE_COLUMNS). The
        first repair quoted "ipTM 0.410" beside "meeting our quality bar": a
        number excluded from that bar, stamped as clearing it, and one an
        outside reader reads as "this does not bind". The wording stamps
        nothing now, but the clause is still emitted ONLY when the bar was met
        (``blueprints.jobs._top_score_for_share``), so the number printed
        beside it has to be one the bar read.
        """
        clause = self._clause_for(
            flask_app, monkeypatch, "boltzgen", "pilot",
            {"ipTM": 0.41, "pLDDT": 88.0, "refolding_rmsd": 1.0},
        )
        assert clause == "One design at pLDDT 88.000", clause
        assert "0.41" not in clause

    def test_an_integer_reading_keeps_its_type(
        self, flask_app, monkeypatch,
    ):
        """AN INT PRINTS AS AN INT, with the float beside it as the control.

        ``plddt_on_100`` hands back the ORIGINAL object for a value already on
        0-100 rather than its own ``float()`` copy, and names THIS route's
        og:title as the reason it bothers
        (shared/metric_glossary.py:405-411). A blanket ``float()`` in
        ``_reading`` undid that one line later, so a stored int 88 read
        "88.000" on the share card while the results page read "88" -- two
        surfaces disagreeing about a number neither of them computed.
        """
        common = {"ipTM": 0.41, "refolding_rmsd": 1.0}
        as_int = self._clause_for(
            flask_app, monkeypatch, "boltzgen", "pilot",
            dict(common, pLDDT=88),
        )
        as_float = self._clause_for(
            flask_app, monkeypatch, "boltzgen", "pilot",
            dict(common, pLDDT=88.0),
        )
        assert as_int == "One design at pLDDT 88", as_int
        assert as_float == "One design at pLDDT 88.000", as_float

    def test_rfantibody_does_not_quote_a_lower_is_better_metric(
        self, flask_app, monkeypatch,
    ):
        """rfantibody's ranking metric is ipAE, where LOWER is better, so
        quoting it made the worse of two passing designs print the bigger
        figure on a text read without context. Its bar also has a
        higher-is-better leg, and that is what gets quoted."""
        clause = self._clause_for(
            flask_app, monkeypatch, "rfantibody", "pilot",
            # Values that actually CLEAR rfantibody's bar (pLDDT >= 80,
            # ipAE <= 10, pAE <= 5). A first draft used pAE 7.0, which the
            # bar rejects, so the route abstained and the assertion failed
            # for a reason unrelated to the metric choice under test.
            {"ipAE": 6.4, "pAE": 4.2, "pLDDT": 91.0},
        )
        assert clause == "One design at pLDDT 91.000", clause
        assert "ipAE" not in clause

    def test_proteina_does_not_quote_an_unexplainable_negative(
        self, flask_app, monkeypatch,
    ):
        """THE REPAIR WAS WORSE THAN THE BUG HERE, which is why this case
        exists. proteina registers ``total_reward``, which is NEGATIVE in real
        data (the 64 records of the shipped example span -0.1827 to -0.9555)
        and carries no ``score_legends`` entry, so the chain has no direction
        for it. (``metric_glossary`` does describe it and the tool's results
        table renders it -- an earlier draft said "no legend anywhere on the
        site", which is false.) The first repair quoted "total_reward -0.183" where the
        original defect had quoted a readable ``af2_iptm 0.891``.

        The chain now requires a legend before it will quote a ranking metric,
        so proteina falls through to model confidence.
        """
        clause = self._clause_for(
            flask_app, monkeypatch, "proteina", "protein_binder",
            {"total_reward": -0.1827, "af2_iptm": 0.8906, "af2_plddt": 0.885},
        )
        assert clause == "One design at af2_plddt 88.500", clause
        assert "-0." not in clause, "a negative number reached the share text"

    def test_a_root_level_alias_still_resolves(self, flask_app, monkeypatch):
        """iggm stores ``n_epitope_contacts`` for the declared
        ``epitope_contacts``. Without ``normalize_candidate`` the chain
        resolves nothing and the tool goes silent while its own results table
        shows the number."""
        job = _job(
            tool="iggm", preset="default",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "i0", "pdb_key": "designs/i0.pdb",
                 "sequence": "MK", "n_epitope_contacts": 7},
            ]},
        )
        clause = _clause(_share(flask_app, monkeypatch, job)["og_title"])
        # "7", not "7.000": a CONTACT COUNT has no thousandths, and this
        # assertion pinned "7.000" for as long as ``_reading`` coerced every
        # value through ``float()``. See
        # ``test_an_integer_reading_keeps_its_type``.
        assert clause == "One design at epitope_contacts 7", clause


class TestTheBarDecidesWhetherThereIsAClause:
    def test_an_unmeasured_pick_under_a_bar_says_nothing(
        self, flask_app, monkeypatch,
    ):
        """WHETHER A BAR APPLIED IS A PROPERTY OF THE RUN, NOT THE RECORD.

        The first repair branched on the pick's verdict, under a comment
        asserting that a non-meets pick means no bar applied. It does not.
        ``headline_candidate`` returns the first record not shown to fall
        short and does not re-rank (shared/jobs.py:196), so a REJECTED record
        0 followed by an UNMEASURED record 1 yields "unjudged" with the bar
        very much applied -- and the card then quoted record 1's number while
        a higher-ranked design sat dropped above it.

        pxdesign gates on ipTM AND pLDDT. Record 0 is rejected on pLDDT at
        ipTM 0.99; record 1 carries no pLDDT at all.
        """
        job = _job(
            tool="pxdesign", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "d0", "pdb_key": "designs/d0.pdb",
                 "sequence": "MK", "scores": {"ipTM": 0.99, "pLDDT": 40.0}},
                {"rank": 1, "name": "d1", "pdb_key": "designs/d1.pdb",
                 "sequence": "MK", "scores": {"ipTM": 0.80}},
            ]},
        )
        title = _share(flask_app, monkeypatch, job)["og_title"]
        assert _clause(title) is None, title

    def test_a_barless_tool_still_gets_a_clause(self, flask_app, monkeypatch):
        """The pair: with no bar, nothing can have been dropped above the
        pick, so the clause is the plain truth and must survive the fix
        above."""
        job = _job(
            tool="bindcraft", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "b0", "pdb_key": "designs/b0.pdb",
                 "sequence": "MK", "scores": {"ipTM": 0.801}},
            ]},
        )
        clause = _clause(_share(flask_app, monkeypatch, job)["og_title"])
        assert clause == "One design at ipTM 0.801", clause


class TestTheShareTextRefusesNonsense:
    """Values that must never be quoted, whatever the bar says.

    Found by running the correctness lens's hunt list by hand after the agent
    itself died on a rate limit mid-review. Both are reachable through the
    real route and neither was refused.
    """

    def _bindcraft(self, flask_app, monkeypatch, scores):
        job = _job(
            tool="bindcraft", preset="pilot",
            result_over={"is_antibody": None, "candidates": [
                {"rank": 0, "name": "c0", "pdb_key": "designs/c0.pdb",
                 "sequence": "MK", "scores": scores},
            ]},
        )
        return _clause(_share(flask_app, monkeypatch, job)["og_title"])

    def test_a_smoke_stub_is_never_quoted(self, flask_app, monkeypatch):
        """THE EARLY RETURN IN ``judge`` IS WHY THIS WAS REACHABLE.

        The smoke tier invents deterministic scores when no model output
        exists, and ``judge`` marks that ``unusable`` -- but only AFTER
        returning early for a tool with no gate columns. So a bindcraft /
        proteina / iggm stub comes back a plain "unjudged" with an EMPTY
        ``unusable`` and passes the bar guard, while the SAME stub on pxdesign
        is caught. That asymmetry is what made this look covered.

        Probed before the guard: this record quoted "ipTM 0.990".
        Pre-existing rather than introduced -- the old first-numeric-key rule
        quoted it too -- but this function is where the decision lives now,
        and shared/exports.py already refuses to hand these numbers over
        unmarked.
        """
        clause = self._bindcraft(
            flask_app, monkeypatch,
            {"ipTM": 0.99, "filter_status": "stub (smoke)"},
        )
        assert clause is None, clause

    def test_a_real_score_on_the_same_tool_still_speaks(
        self, flask_app, monkeypatch,
    ):
        """The pair, so the test above cannot pass by silencing the tool."""
        clause = self._bindcraft(flask_app, monkeypatch, {"ipTM": 0.801})
        assert clause == "One design at ipTM 0.801", clause

    @pytest.mark.parametrize(
        "label,value",
        [("nan", float("nan")), ("inf", float("inf")),
         ("-inf", float("-inf")), ("bool", True)],
    )
    def test_a_non_finite_or_boolean_value_is_never_quoted(
        self, flask_app, monkeypatch, label, value,
    ):
        """``f"{float('nan'):.3f}"`` is the word "nan", so before the guard
        this route emitted "ipTM nan" -- and "ipTM inf" -- in text
        a person pastes somewhere.

        This pipeline does produce NaN: tools/esmfold2_design writes
        ``float("nan")`` for the CDR proxy on every non-antibody design and
        carries a ``_finite`` helper to strip it. The bool case is separate:
        ``isinstance(True, int)`` is True, so a stored ``true`` formatted as
        "ipTM 1.000".
        """
        clause = self._bindcraft(flask_app, monkeypatch, {"ipTM": value})
        assert clause is None, f"{label} -> {clause}"
