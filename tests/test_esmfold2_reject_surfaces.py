"""Two ARTIFACTS that leave this site must not speak for a rejected design.

The share card's og:title and the per-job FASTA export. Both hand something
to a reader with no page around it: the og:title goes into a LinkedIn or X
compose box off a PUBLIC share URL, and the .fasta is opened later in a
sequence tool. Neither has a cell beside the number to carry a shortfall, so
a design the tool's own bar drops has to be either not named or named as
dropped.

Real completed job ``2b917b54-0871-44af-a3d1-5d07ea5dcaeb`` (esmfold2-design,
PD-L1 minibinder, n_seeds=2), as it sits in the database:

    seed0: ipTM 0.9556, pI 11.95 -> the pipeline drops it
    seed1: ipTM 0.9354, pI  5.67 -> clears the bar

``candidates[0]`` is seed0. The share route read it blind and published its
0.956 as "Top score"; the FASTA numbered it ``rank1`` with nothing to say it
is the one design the tool's own worked example exists to tell you not to
order.

SURFACES THREE AND FOUR of the class #241 opened and #248 continued. The
other two repaired here -- the campaign/target counts and the target ranking
table -- are cohort-level and are driven through the aggregator in
tests/test_aggregate_target.py, because that is their real path.

EVERY TEST HERE GOES THROUGH THE ROUTE, via the Flask test client. Calling
``_top_score_for_share`` or ``candidates_to_fasta`` directly would leave the
route free to not call them, which is exactly how the sibling surfaces stayed
green through #241: the helper was right and nothing reached it.
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
        "scores": {"ipTM": iptm, "iPTM_proxy": None, "pI": pi,
                   "final_loss": 1.0},
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
        """The whole defect in one assertion: 0.956 is seed0's ipTM and seed0
        is the design the pipeline drops. It reached a PUBLIC card because the
        route read ``candidates[0]`` and the tool's bar needs the run's mode
        to exist at all."""
        payload = _share(flask_app, monkeypatch, _job())
        title = payload["og_title"]
        assert "0.956" not in title, (
            f"the rejected design's ipTM reached a public share card: {title}"
        )
        assert "ipTM 0.935" in title, title

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
        assert "Top score" not in title, title
        # And it still says the run happened -- the clause is dropped, not the
        # card.
        assert "tools.ranomics.com" in title and "ESMFold2" in title

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
        assert "ipTM 0.956" in title, title

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
        assert "ipTM 0.701" in title, title


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
