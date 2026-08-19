"""One analysis costs ONE charge — and no route becomes free compute.

A Scout analysis is two HTTP requests sharing one rate-limit bucket:
``GET /scout/progress`` (the SSE stream that runs the pipeline) and
``POST /scout/analyze`` (finalise). Billing both meant a ceiling of 10 bought
five analyses, and QC measured the sixth researcher behind one university NAT
being refused with no concurrency involved at all.

The tempting fix is to drop the decorator from ``/scout/progress``. That
route is NOT a status poll — ``_run_worker`` calls ``run_pipeline``
unconditionally — so dropping it would make full-pipeline compute free, which
is a strictly worse hole than the one being closed. Instead the pair shares
one charge through a single-use follow-up credit.

Two halves to this file, and the second is the important one:

  * **the win**   — a legitimate analysis costs 1, and N researchers behind
    one address each get one;
  * **the proof** — every way of turning that credit into free compute is
    tried here and charged: replaying it, banking it, racing it, stealing it,
    calling either route on its own, and expiring it.

    pytest tests/test_scout_anon_charge_pairing.py -v
"""

from __future__ import annotations

import ast
import csv
import json
import logging
import shutil
import threading
from pathlib import Path

import pytest

from scout import ratelimit
from scout import routes as scout_routes
from scout.flags import _CSV_COLUMNS_BASE

TMP = Path("tmp")

IP_BUCKET = "scout_analyze"
SESSION_BUCKET = "scout_analyze:session"

# A body the meter must refuse to read, sized as an ABSOLUTE literal and
# deliberately NOT as a multiple of ``ratelimit._MAX_FOLLOWUP_BODY_BYTES``.
#
# A payload derived from the constant scales with it, so the bound could be
# raised from 4 KiB to 20 MB and every test below would stay green while the
# regression the bound exists to prevent came straight back — QC applied
# exactly that mutation and watched it survive. A literal turns "raise the
# bound past 1 MB" into red tests instead of a silent policy change. The real
# follow-up body is ~80 bytes, so nothing legitimate is anywhere near this.
BODY_OVER_THE_METER_BOUND = 1024 * 1024

# The one production framing under which the meter cannot see a body's size at
# all. Werkzeug leaves ``request.content_length`` as None when the request is
# chunked; ``wsgi.input_terminated`` is what makes the body still READABLE, so
# these requests are the shape Railway's edge would hand us if it ever
# re-framed /scout/analyze — not a body that merely fails to parse.
CHUNKED = {
    "headers": {"Transfer-Encoding": "chunked"},
    "environ_overrides": {"wsgi.input_terminated": True},
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    ratelimit.reset()
    yield app.test_client()
    ratelimit.reset()


@pytest.fixture
def reap_jobs():
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Make the whole analyse path run without freesasa or the network.

    ``run_pipeline`` is replaced with something that writes the
    ``results.csv`` a real run would have produced, because the accounting
    under test depends on ``/progress`` leaving that file behind for
    ``/analyze`` to find — that is what makes the second request the cheap
    half of one analysis rather than a second one.

    Yields the list of job ids it was run on, in order. Which job the pipeline
    actually executed is the other half of the diversion question: a charge
    landing on the right job while the WORK lands on another is exactly the
    defect QC measured, and only this list can see it.
    """
    ran_on: list[str] = []

    def _fake_pipeline(pdb_path, chain_id, progress_callback=None):
        ran_on.append(Path(pdb_path).parent.name)
        row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
        row.update({
            "epitope_id": "1",
            "residues": "A10,A11,A12,A13,A14,A15,A16",
            "residue_count": "7",
            "mean_rsa": "0.55",
            "composite_score": "0.72",
            "secondary_structure": "loop",
            "centroid_x": "1.0",
            "centroid_y": "2.0",
            "centroid_z": "3.0",
        })
        with (Path(pdb_path).parent / "results.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
            writer.writeheader()
            writer.writerow(row)

    monkeypatch.setattr("scout.pipeline.run_pipeline", _fake_pipeline)
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown"},
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])
    return ran_on


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _charges(bucket: str, key: str) -> int:
    """Hits recorded against one bucket key. 0 when never charged."""
    entry = ratelimit._WINDOWS.get((bucket, key))
    return entry[1] if entry else 0


def _ip_charges(ip: str = "127.0.0.1") -> int:
    return _charges(IP_BUCKET, ip)


def _hdr(ip: str | None) -> dict:
    return {"X-Forwarded-For": ip} if ip else {}


def _progress(client, job_id, chain="A", ip=None):
    """Drive the SSE route to completion, then release the stream."""
    resp = client.get(
        f"/scout/progress?job_id={job_id}&chain={chain}", headers=_hdr(ip)
    )
    try:
        return resp.status_code, resp.get_data(as_text=True)
    finally:
        resp.close()


def _analyze(client, job_id, chain="A", ip=None):
    return client.post(
        "/scout/analyze",
        json={"job_id": job_id, "chain": chain},
        headers=_hdr(ip),
    )


def _one_analysis(client, job_id, chain="A", ip=None):
    """The real front-end flow: stream, then finalise."""
    _progress(client, job_id, chain=chain, ip=ip)
    return _analyze(client, job_id, chain=chain, ip=ip)


def _session_id(client) -> str:
    with client.session_transaction() as sess:
        return sess[ratelimit.ANON_SESSION_KEY]


def _fresh_cookie(app, label: str):
    """A client carrying its own anonymous session id.

    NOT the same as a bare ``app.test_client()``: that one sends no cookie at
    all and therefore lands in the shared ``no-session`` bucket. Rotating
    cookies means presenting a DIFFERENT id each time, which is what an
    attacker gets for free and what the per-IP tier exists to catch.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[ratelimit.ANON_SESSION_KEY] = f"anon:{label}"
    return client


BOGUS_JOB = "3f8e0c92-0000-4000-8000-abc"


def _burn_the_per_ip_analyze_limit(app):
    """Put 127.0.0.1 over the per-IP analyze ceiling with cheap calls.

    A fresh cookie each time so the tighter session tier never fires — what
    the caller after this needs is a refusal from the PER-IP tier, which sits
    downstream of the credit check.
    """
    for i in range(scout_routes.ANON_ANALYZE_LIMIT):
        _analyze(_fresh_cookie(app, f"burn{i}"), BOGUS_JOB)
    assert _ip_charges() == scout_routes.ANON_ANALYZE_LIMIT


def _count_json_parses(monkeypatch) -> list:
    """Record every ``Request.get_json`` from here on into the returned list.

    Counting parses rather than timing them: the property under test is that
    the meter never reads an unbounded body ahead of the tier that refuses it,
    and a parse count states that directly instead of inferring it from a
    wall-clock number that flakes on a loaded box.
    """
    import flask  # noqa: PLC0415

    parses: list[int] = []
    real_get_json = flask.Request.get_json

    def _counting_get_json(self, *a, **kw):
        parses.append(1)
        return real_get_json(self, *a, **kw)

    monkeypatch.setattr(flask.Request, "get_json", _counting_get_json)
    return parses


# ---------------------------------------------------------------------------
# The win
# ---------------------------------------------------------------------------


class TestOneAnalysisIsChargedOnce:
    def test_the_pair_costs_one_not_two(self, client, stub_pipeline, reap_jobs):
        """The whole point. Before this, the same flow cost 2."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        assert _ip_charges() == 0, "intake must not touch the analyze bucket"

        resp = _one_analysis(client, job_id)

        assert resp.status_code == 200, resp.data
        assert _ip_charges() == 1, (
            "one analysis must cost exactly one charge in the per-IP analyze "
            "bucket; 2 means the /progress + /analyze pair is being billed "
            "twice again and the ceiling buys half what it reads"
        )

    def test_the_session_tier_is_charged_once_too(
        self, client, stub_pipeline, reap_jobs
    ):
        """A credit spent before EITHER tier, or the tight tier bites at half
        the number it advertises."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        _one_analysis(client, job_id)
        assert _charges(SESSION_BUCKET, _session_id(client)) == 1

    def test_six_researchers_behind_one_nat_all_get_through(
        self, app, stub_pipeline, reap_jobs
    ):
        """The goal question, measured the way QC measured it.

        Six distinct sessions, one address, sequential, no concurrency: each
        loads the example, streams, and finalises. QC's run on the unfixed
        code refused the sixth on its very first analysis.
        """
        ratelimit.reset()
        results = []
        for _ in range(6):
            researcher = app.test_client()
            job_id = researcher.get("/scout/example").get_json()["job_id"]
            results.append(_one_analysis(researcher, job_id).status_code)

        assert results == [200] * 6, f"a researcher was refused: {results}"
        assert _ip_charges() == 6, (
            f"six analyses must cost six charges, not {_ip_charges()}"
        )

    def test_the_wall_moved_from_five_analyses_to_ten(
        self, app, stub_pipeline, reap_jobs
    ):
        """Ten sessions, one address, one analysis each — the per-IP ceiling
        is now reached at the tenth rather than the fifth."""
        ratelimit.reset()
        codes = []
        for _ in range(scout_routes.ANON_ANALYZE_LIMIT):
            researcher = app.test_client()
            job_id = researcher.get("/scout/example").get_json()["job_id"]
            codes.append(_one_analysis(researcher, job_id).status_code)

        assert codes == [200] * scout_routes.ANON_ANALYZE_LIMIT, codes
        assert _ip_charges() == scout_routes.ANON_ANALYZE_LIMIT


# ---------------------------------------------------------------------------
# The proof: the charge cannot be evaded
# ---------------------------------------------------------------------------


class TestTheChargeCannotBeEvaded:
    """Every one of these would be free CPU if the credit were sloppy."""

    def test_progress_is_charged_on_every_single_call(
        self, client, stub_pipeline, reap_jobs
    ):
        """THE TRAP. /scout/progress runs the pipeline unconditionally, so a
        replayed job id must cost full price every time."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        for _ in range(4):
            _progress(client, job_id)
        assert _ip_charges() == 4, (
            "/scout/progress went partly free — it executes run_pipeline on "
            "every call, so every call must be charged"
        )

    def test_progress_never_spends_a_credit(self, client, stub_pipeline, reap_jobs):
        """PAIR_OPENS grants; it must never redeem, or two streams would run
        the pipeline twice for one charge."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        _progress(client, job_id)
        outstanding_before = dict(ratelimit._FOLLOWUP)
        _progress(client, job_id)

        assert _ip_charges() == 2
        assert set(ratelimit._FOLLOWUP) == set(outstanding_before), (
            "the second /progress consumed the credit instead of replacing it"
        )

    def test_a_credit_is_single_use(self, client, stub_pipeline, reap_jobs):
        """One stream, two finalises: the second is charged."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        _progress(client, job_id)
        _analyze(client, job_id)
        assert _ip_charges() == 1

        _analyze(client, job_id)
        assert _ip_charges() == 2, (
            "a replayed /scout/analyze rode free a second time; the credit is "
            "not single-use"
        )

    def test_credits_cannot_be_banked(self, client, stub_pipeline, reap_jobs):
        """Three streams then three finalises is not three free rides.

        At most one credit is outstanding per key, so a burst of cheap grants
        cannot be stockpiled and cashed in later.
        """
        job_id = client.get("/scout/example").get_json()["job_id"]
        for _ in range(3):
            _progress(client, job_id)
        assert _ip_charges() == 3

        for _ in range(3):
            _analyze(client, job_id)
        assert _ip_charges() == 5, (
            "credits were banked: three grants bought more than one free "
            f"follow-up (charges={_ip_charges()}, expected 3 + 2)"
        )

    def test_analyze_on_its_own_is_charged(self, client, stub_pipeline, reap_jobs):
        """No stream, no credit. This route can run the pipeline itself when
        results.csv is missing, so it must never be free by default."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        for _ in range(3):
            _analyze(client, job_id)
        assert _ip_charges() == 3

    def test_a_refused_request_grants_no_credit(
        self, app, stub_pipeline, reap_jobs
    ):
        """Only a charge that was TAKEN and ALLOWED buys a follow-up.

        Otherwise being rate limited would itself hand out a free analysis,
        which is the opposite of what a limiter is for.
        """
        ratelimit.reset()
        owner = _fresh_cookie(app, "owner")
        job_id = owner.get("/scout/example").get_json()["job_id"]

        # Exhaust the per-IP tier by rotating cookies past it.
        for i in range(scout_routes.ANON_ANALYZE_LIMIT):
            _analyze(_fresh_cookie(app, f"burn{i}"), job_id)
        assert _ip_charges() == scout_routes.ANON_ANALYZE_LIMIT

        outstanding = len(ratelimit._FOLLOWUP)
        status, body = _progress(_fresh_cookie(app, "refused"), job_id)
        assert ratelimit.REASON_RATE_LIMITED in body, body

        assert len(ratelimit._FOLLOWUP) == outstanding, (
            "a refused /scout/progress left a credit behind, so being rate "
            "limited would buy a free analysis"
        )

    def test_a_credit_cannot_be_diverted_to_a_different_job(
        self, client, stub_pipeline, reap_jobs
    ):
        """The expensive one, and the least obvious.

        A credit is only cheap to honour because the call that bought it left
        ``results.csv`` behind, which sends the paired ``/analyze`` down the
        finalise path. Spend it on a DIFFERENT job and that ``/analyze`` finds
        no results and runs the entire pipeline itself — so one charge would
        buy a stream AND a full pipeline, ~24 CPU-s instead of ~15.
        """
        paid_job = client.get("/scout/example").get_json()["job_id"]
        other_job = client.get("/scout/example").get_json()["job_id"]

        _progress(client, paid_job)
        assert _ip_charges() == 1

        _analyze(client, other_job)
        assert _ip_charges() == 2, (
            "a credit bought on one job paid for the analysis of another; "
            "that /analyze runs the whole pipeline itself when results.csv "
            "is missing, so the charge would buy ~24 CPU-s, not ~15"
        )

        # ...and the credit it could not divert is still there for its own job.
        _analyze(client, paid_job)
        assert _ip_charges() == 2, "the legitimate pairing was broken instead"

    def test_a_credit_cannot_be_spent_from_another_address(
        self, client, stub_pipeline, reap_jobs
    ):
        """ONE variable moves: the address.

        Same session and same job on both calls, so nothing but the per-IP
        half of the key can decide the outcome. An earlier version of this
        test also changed the job, and so stayed green when the address was
        dropped from the key entirely.
        """
        job_id = client.get("/scout/example").get_json()["job_id"]
        _progress(client, job_id, ip="203.0.113.7")
        assert _ip_charges("203.0.113.7") == 1

        _analyze(client, job_id, ip="198.51.100.4")
        assert _ip_charges("198.51.100.4") == 1, (
            "a credit paid for by one address was redeemed from another"
        )

    def test_a_credit_cannot_be_spent_by_another_session(
        self, app, stub_pipeline, reap_jobs
    ):
        """ONE variable moves: the session.

        Same address and same job id — a NAT neighbour who somehow learns it
        must still not be able to spend the credit its owner paid for.
        """
        ratelimit.reset()
        payer = _fresh_cookie(app, "payer")
        job_id = payer.get("/scout/example").get_json()["job_id"]
        _progress(payer, job_id)
        assert _ip_charges() == 1

        neighbour = _fresh_cookie(app, "neighbour")
        _analyze(neighbour, job_id)
        assert _ip_charges() == 2, (
            "a NAT neighbour redeemed a credit somebody else paid for"
        )

    def test_an_expired_credit_is_refused(
        self, client, monkeypatch, stub_pipeline, reap_jobs
    ):
        job_id = client.get("/scout/example").get_json()["job_id"]
        monkeypatch.setattr(ratelimit, "FOLLOWUP_TTL_SECONDS", -1.0)
        _progress(client, job_id)
        _analyze(client, job_id)
        assert _ip_charges() == 2, "an expired credit was still redeemable"

    def test_a_typo_in_the_pair_role_fails_loudly(self):
        """It would otherwise fail silently: no grant, no spend, and the pair
        quietly billed twice again."""
        with pytest.raises(ValueError, match="unknown pair role"):
            ratelimit.anon_rate_limit(
                "b", limit=1, window_seconds=1, pair="open"
            )

    def test_racing_two_finalises_against_one_credit(self):
        """Two concurrent /analyze calls, one credit: exactly one goes free.

        Exercised at the ledger rather than through the test client, because
        Flask's test client is not the concurrency this guards — two threads
        in one gthread worker are.
        """
        ratelimit.reset()
        key = ("anon:race", "198.51.100.9", "job-1")
        ratelimit._grant_followup(key)

        spent, barrier = [], threading.Barrier(8)

        def _try():
            barrier.wait()
            spent.append(ratelimit._spend_followup(key))

        threads = [threading.Thread(target=_try) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ratelimit.reset()

        assert sum(spent) == 1, (
            f"one credit produced {sum(spent)} free rides under a race"
        )


# ---------------------------------------------------------------------------
# Where the job id comes from
# ---------------------------------------------------------------------------


class TestTheMeterAndTheViewReadTheSameJobId:
    """The gap that let the credit be diverted with the job id IN the key.

    ``TestTheChargeCannotBeEvaded`` above tests the CONTENTS of the credit key
    exhaustively — drop the session, the address or the job from it and a test
    goes red. What nothing tested was how the job id in that key is DERIVED,
    and that is where the defect lived: the meter read the query string first
    and ``/scout/analyze`` read the body, so ``POST /scout/analyze?job_id=A``
    carrying ``{"job_id": "B"}`` keyed the credit on A and ran the pipeline on
    B. One charge, two full pipeline runs, ~24 CPU-s instead of ~15.

    Two mutations survived QC green in that gap — swapping the sources to
    body-first, and dropping ``.strip()``. Every test here moves EXACTLY ONE
    source while holding the other constant, and asserts both halves: who was
    charged, and which job the pipeline actually ran on. Either half alone
    stays green when only one side of the pair is changed.
    """

    def test_a_query_string_cannot_divert_the_credit_on_analyze(
        self, client, stub_pipeline, reap_jobs
    ):
        """THE EXPLOIT, measured on a real server by QC and pinned here.

        ONE variable moves: a query string is added to a POST that reads its
        body. The body still names the job the view will run, so if the meter
        and the view agree, this is an ordinary un-credited ``/analyze`` and
        is charged.
        """
        paid_job = client.get("/scout/example").get_json()["job_id"]
        other_job = client.get("/scout/example").get_json()["job_id"]

        _progress(client, paid_job)
        assert _ip_charges() == 1
        assert stub_pipeline == [paid_job]

        # The credit was bought on paid_job. Name it in the QUERY STRING while
        # the body — the only thing analyze() reads — names the other job.
        client.post(
            f"/scout/analyze?job_id={paid_job}",
            json={"job_id": other_job, "chain": "A"},
        )

        assert _ip_charges() == 2, (
            "a query string on POST /scout/analyze redeemed a credit bought "
            "for a different job: the meter keyed on the query value while "
            "the view ran the body value, so one charge bought two full "
            "pipeline runs"
        )
        assert stub_pipeline == [paid_job, other_job], (
            "the view did not run the job its body named, so the meter and "
            "the view are reading different sources again"
        )

    def test_an_oversize_body_cannot_divert_the_credit_either(
        self, client, stub_pipeline, reap_jobs
    ):
        """THE SAME EXPLOIT, wearing the one costume no other test shows it in.

        Every diversion test in this class sends a SMALL body, so all of them
        exercise the ordinary path through ``_metered_job_id`` and none of them
        exercises the body-size bound *for diversion*. Make that bound fail
        OPEN — oversize body, so fall back to ``request.args`` — and the whole
        of the exploit above comes back: one charge, two full pipeline runs,
        ~24 CPU-s instead of ~15. QC applied exactly that mutation and measured
        all 5,160 tests staying green while it ran.

        So the bound is not only a cost control; it is the last thing standing
        between an oversize POST and the query string. Same single variable as
        the test above — a query string on a POST that reads its body — with
        the body pushed past the bound.
        """
        paid_job = client.get("/scout/example").get_json()["job_id"]
        other_job = client.get("/scout/example").get_json()["job_id"]

        _progress(client, paid_job)
        assert _ip_charges() == 1
        assert stub_pipeline == [paid_job]

        client.post(
            f"/scout/analyze?job_id={paid_job}",
            json={
                "job_id": other_job,
                "chain": "A",
                "pad": "x" * BODY_OVER_THE_METER_BOUND,
            },
        )

        assert _ip_charges() == 2, (
            "an oversize body let a query string redeem a credit bought for a "
            "different job: the meter gave up on the body and fell back to "
            "the query instead of failing closed, so one charge bought two "
            "full pipeline runs again"
        )
        assert stub_pipeline == [paid_job, other_job], (
            "the view did not run the job its body named, so the meter and "
            "the view are reading different sources again"
        )

    def test_a_body_cannot_divert_the_credit_on_progress(
        self, client, stub_pipeline, reap_jobs
    ):
        """THE MIRROR IMAGE, which is why the fix is not "read the body first".

        A GET may legally carry a body, so a meter that preferred the body
        would key ``/scout/progress`` on a job the SSE route never touches.
        ONE variable moves: a JSON body is added to a GET that reads its query
        string.
        """
        paid_job = client.get("/scout/example").get_json()["job_id"]
        other_job = client.get("/scout/example").get_json()["job_id"]

        client.get(
            f"/scout/progress?job_id={paid_job}&chain=A",
            json={"job_id": other_job, "chain": "A"},
        ).close()

        assert _ip_charges() == 1
        assert stub_pipeline == [paid_job], (
            "/scout/progress ran a job its query string did not name"
        )

        # The credit must belong to the job the stream actually ran, so the
        # job the body named must NOT be able to spend it.
        _analyze(client, other_job)
        assert _ip_charges() == 2, (
            "a body on GET /scout/progress bought a credit for a job the "
            "stream never ran; the meter preferred the body over the query "
            "string, which is the same defect mirrored"
        )

        # ...and the real pairing is untouched.
        _analyze(client, paid_job)
        assert _ip_charges() == 2, "the legitimate pairing was broken instead"

    def test_a_whitespace_padded_job_id_still_redeems_its_own_credit(
        self, client, stub_pipeline, reap_jobs
    ):
        """The BODY side of ``.strip()``, and dropping it must go red.

        ``analyze()`` strips, so a padded id resolves to the same job and
        takes the cheap finalise path. A meter that did not strip would key
        the credit on `` A `` , miss it, and charge a second time for work
        already paid for. It fails closed, which is why nothing noticed — but
        it is an untested line in a security-relevant key derivation.

        This test pads only the body, and for one commit that was the whole of
        the coverage: QC re-ran the dropped-``.strip()`` mutation on the QUERY
        side and it survived green. The sibling below is the other half.
        """
        job_id = client.get("/scout/example").get_json()["job_id"]
        _progress(client, job_id)
        assert _ip_charges() == 1

        client.post("/scout/analyze", json={"job_id": f"  {job_id}  ", "chain": "A"})
        assert _ip_charges() == 1, (
            "a whitespace-padded job id missed the credit its own analysis "
            "bought; the meter is not normalising the id the way the view does"
        )
        assert stub_pipeline == [job_id], "the finalise path re-ran the pipeline"

    def test_a_whitespace_padded_job_id_in_the_query_grants_its_own_credit(
        self, client, stub_pipeline, reap_jobs
    ):
        """The QUERY side of the same line, which nothing covered until now.

        ``job_id_in_query`` strips too, and ``progress()`` calls it, so a
        padded id in the query string must run the real job and leave the
        credit under the id the finalise POST will name. Drop that ``.strip()``
        and the stream looks for a job directory called `` <id> `` , finds
        nothing, and the credit is keyed on a string no ``/analyze`` can ever
        present.
        """
        job_id = client.get("/scout/example").get_json()["job_id"]

        # ONE variable moves: %20 padding around the id in the query string.
        _progress(client, f"%20%20{job_id}%20%20")
        assert _ip_charges() == 1
        assert stub_pipeline == [job_id], (
            "a whitespace-padded query job id did not resolve to the real "
            "job, so the stream ran nothing; the meter and the view are no "
            "longer normalising the id the same way"
        )

        _analyze(client, job_id)
        assert _ip_charges() == 1, (
            "the credit bought by a whitespace-padded /scout/progress could "
            "not be redeemed by its own /scout/analyze; the meter keyed it on "
            "the unstripped id"
        )

    def test_every_paired_route_reads_the_source_its_meter_declares(self):
        """The structural half, so a THIRD paired route cannot get this wrong.

        The behavioural tests above pin ``/scout/analyze`` and
        ``/scout/progress`` as they are today. This reads ``scout/routes.py``
        itself and fails if any view decorated with ``job_id=<source>`` stops
        calling that same ``<source>`` — which is the only way the meter and
        the view can end up on different sources again.

        Parsed with ``ast`` rather than matched with a regex: a regex over
        source is the shape of guard this repo has already had certify false
        three times.
        """
        tree = ast.parse(
            Path(scout_routes.__file__).read_text(encoding="utf-8"),
            filename=scout_routes.__file__,
        )
        checked = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                if getattr(dec.func, "id", None) != "anon_rate_limit":
                    continue
                declared = next(
                    (
                        kw.value.id
                        for kw in dec.keywords
                        if kw.arg == "job_id" and isinstance(kw.value, ast.Name)
                    ),
                    None,
                )
                if declared is None:
                    continue
                called = {
                    sub.func.id
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                }
                checked[node.name] = (declared, declared in called)

        assert checked, (
            "no paired route declares job_id= at all — the meter is guessing "
            "the job id again, which is the diversion this replaced"
        )
        wrong = {n: d for n, (d, ok) in checked.items() if not ok}
        assert not wrong, (
            f"these views do not call the job id source their own decorator "
            f"declares, so the meter can charge one job while the view runs "
            f"another: {wrong}"
        )

    def test_a_paired_route_must_declare_where_its_job_id_lives(self):
        """No default, because a default is a guess. Import-time, not runtime:
        a paired route with no source would silently key every credit on ""."""
        with pytest.raises(ValueError, match="pair and job_id go together"):
            ratelimit.anon_rate_limit(
                "b", limit=1, window_seconds=1, pair=ratelimit.PAIR_CLOSES
            )
        with pytest.raises(ValueError, match="unknown job id source"):
            ratelimit.anon_rate_limit(
                "b",
                limit=1,
                window_seconds=1,
                pair=ratelimit.PAIR_CLOSES,
                job_id=lambda: "anything",
            )

    def test_neither_source_falls_back_when_its_own_is_empty(
        self, client, stub_pipeline, reap_jobs
    ):
        """THE ONE THAT SURVIVES A NAIVE FIX, and my own first attempt at it.

        A fallback only fires when the primary source is EMPTY, so a test that
        always supplies the primary never sees one. QC's surviving mutation
        was exactly a fallback, and so was the mutation of this test that I
        had to add this case to kill: with ``?job_id=A`` present, "query, then
        body" and "query only" are indistinguishable.

        Both requests below leave the primary source empty and populate the
        other one. Both must read "".
        """
        job_id = client.get("/scout/example").get_json()["job_id"]

        # GET /scout/progress: job id in the BODY, none in the query. The
        # stream runs nothing, so it must leave no credit behind for anyone.
        client.get(
            "/scout/progress?chain=A", json={"job_id": job_id, "chain": "A"}
        ).close()
        assert _ip_charges() == 1
        assert stub_pipeline == [], "the stream ran a job its query never named"
        assert not ratelimit._FOLLOWUP, (
            "GET /scout/progress fell back to the body and granted a credit "
            "for a job the stream never ran — free compute for the /analyze "
            "that redeems it"
        )

        # POST /scout/analyze: job id in the QUERY, none in the body. The view
        # will 400, so nothing may be redeemed on its behalf.
        _progress(client, job_id)
        assert _ip_charges() == 2
        outstanding = dict(ratelimit._FOLLOWUP)
        assert outstanding

        client.post(f"/scout/analyze?job_id={job_id}", json={"chain": "A"})
        assert _ip_charges() == 3
        assert set(ratelimit._FOLLOWUP) == set(outstanding), (
            "POST /scout/analyze fell back to the query string and burned a "
            "credit the view could not use"
        )

    def test_an_empty_job_id_never_grants_or_spends(
        self, client, stub_pipeline, reap_jobs
    ):
        """A "" credit would be redeemable by any request whose id the meter
        declined to read, which is a diversion by another name."""
        client.get("/scout/progress?chain=A").close()
        assert _ip_charges() == 1
        assert not ratelimit._FOLLOWUP, (
            "a /scout/progress with no job id left a credit keyed on \"\""
        )

    def test_a_refused_analyze_does_not_parse_a_large_body(
        self, app, monkeypatch, stub_pipeline, reap_jobs
    ):
        """Refusals must stay cheap, and the credit check runs BEFORE both
        tiers.

        Reading the body to build the credit key made a refused ``/analyze``
        parse up to ``MAX_CONTENT_LENGTH`` first — QC measured 0.056 s -> 0.45 s
        for an 18 MB body. Refused requests are unbounded by definition, so
        that made refusals ~8x cheaper to convert into worker wall time, which
        is backwards for a rate limiter.

        Asserted by counting parses rather than by timing, so it cannot flake.

        The payload is an ABSOLUTE size, not a multiple of the bound. Sized as
        ``_MAX_FOLLOWUP_BODY_BYTES * 4`` it scaled with the constant, so the
        bound could be raised to 20 MB with this test still green and the
        regression fully restored — which is the mutation QC ran.
        """
        ratelimit.reset()
        parses = _count_json_parses(monkeypatch)
        _burn_the_per_ip_analyze_limit(app)

        parses.clear()
        fat = {"job_id": BOGUS_JOB, "chain": "A",
               "pad": "x" * BODY_OVER_THE_METER_BOUND}
        refused = _fresh_cookie(app, "fat").post("/scout/analyze", json=fat)

        assert refused.status_code == 429, refused.get_data(as_text=True)
        assert parses == [], (
            f"a request that was going to be refused parsed its body first "
            f"({len(parses)} parses); the credit check must not read an "
            f"unbounded body ahead of the tiers that refuse it"
        )

    def test_a_refused_analyze_does_not_parse_a_body_of_unknown_length(
        self, app, monkeypatch, stub_pipeline, reap_jobs
    ):
        """The size bound is only as good as knowing the size.

        ``length is None`` is not belt-and-braces. Without it an attacker skips
        the bound entirely by sending ``Transfer-Encoding: chunked`` — no
        Content-Length, so nothing to compare — and the refusal cost comes
        straight back: QC measured 0.0053 s -> 0.1936 s on an 18 MB chunked
        body with that clause relaxed. The body here is small, because the
        property is "the meter did not read it", not "the body was big".
        """
        ratelimit.reset()
        parses = _count_json_parses(monkeypatch)
        _burn_the_per_ip_analyze_limit(app)

        parses.clear()
        refused = _fresh_cookie(app, "chunked").post(
            "/scout/analyze", json={"job_id": BOGUS_JOB, "chain": "A"}, **CHUNKED
        )

        assert refused.status_code == 429, refused.get_data(as_text=True)
        assert parses == [], (
            f"a refused request with no Content-Length was parsed anyway "
            f"({len(parses)} parses); an unknown length is an UNBOUNDED one, "
            f"so it is the framing an attacker picks to get the parse back"
        )

    def test_a_body_of_unknown_length_cannot_redeem_a_credit_and_says_so(
        self, client, stub_pipeline, reap_jobs, caplog
    ):
        """Fails closed — and refuses to fail closed QUIETLY.

        Declining an unreadable-size body costs the caller its credit, so the
        pair is billed twice. That is the right trade against an attacker, but
        it is also what happens to EVERY analysis if Railway's edge ever
        re-frames /scout/analyze as chunked: capacity silently halves back to
        five researchers per window while the refusal rate — the thing Phase 6
        would alarm on — does not move at all, because nothing is refused.

        So the condition is counted and announced. Both halves are asserted
        here: without the counter nobody in production can see it, and without
        the log nobody outside this process can.
        """
        job_id = client.get("/scout/example").get_json()["job_id"]
        _progress(client, job_id)
        assert _ip_charges() == 1
        assert ratelimit.unmetered_bodies == 0

        with caplog.at_level(logging.WARNING, logger="scout.ratelimit"):
            client.post(
                "/scout/analyze",
                json={"job_id": job_id, "chain": "A"},
                **CHUNKED,
            )

        assert _ip_charges() == 2, (
            "a body with no Content-Length redeemed a follow-up credit; the "
            "meter read a body whose size it could not check first"
        )
        assert ratelimit.unmetered_bodies == 1, (
            "the one framing that silently halves anonymous capacity in "
            "production went uncounted"
        )
        assert any(
            "no Content-Length" in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), (
            "nothing was logged, so an edge re-framing every /scout/analyze "
            "as chunked would halve capacity with no signal anywhere"
        )


# ---------------------------------------------------------------------------
# Two tiers
# ---------------------------------------------------------------------------


class TestTwoTiers:
    def test_the_session_tier_bites_before_the_ip_tier(
        self, client, stub_pipeline, reap_jobs
    ):
        """The tight tier is the only limit an ordinary visitor should meet."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        codes = [
            _analyze(client, job_id).status_code
            for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT + 1)
        ]
        assert codes[-1] == 429
        assert codes[:-1] == [200] * scout_routes.ANON_ANALYZE_SESSION_LIMIT, codes

        refusal = _analyze(client, job_id)
        assert refusal.get_json()["reason"] == ratelimit.REASON_SESSION_LIMITED, (
            "a session-limited caller was told their NETWORK is over the "
            "limit, which is both wrong and the wrong call to action"
        )

    def test_a_session_refusal_does_not_spend_the_shared_ip_budget(
        self, client, stub_pipeline, reap_jobs
    ):
        """A session over its own allowance must not go on burning the
        allowance its whole institution draws from."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT + 5):
            _analyze(client, job_id)

        assert _ip_charges() == scout_routes.ANON_ANALYZE_SESSION_LIMIT, (
            f"refused requests still charged the per-IP bucket "
            f"({_ip_charges()} hits); the session tier must return first"
        )

    def test_rotating_cookies_still_lands_on_the_ip_tier(
        self, app, stub_pipeline, reap_jobs
    ):
        """Cookies are free to rotate, which is exactly why the per-IP tier
        is the true bound and the session tier is not."""
        ratelimit.reset()
        job_id = app.test_client().get("/scout/example").get_json()["job_id"]

        codes = []
        for i in range(scout_routes.ANON_ANALYZE_LIMIT + 2):
            last = _analyze(_fresh_cookie(app, f"rot{i}"), job_id)
            codes.append(last.status_code)

        assert codes.count(429) == 2, codes
        assert last.get_json()["reason"] == ratelimit.REASON_RATE_LIMITED, (
            "rotating the session cookie walked past the per-IP ceiling"
        )

    def test_callers_with_no_session_share_one_bucket(self, app, reap_jobs):
        """Minting a session key per request would hand a cookie-less sprayer
        an unlimited supply of fresh session buckets.

        Sharing one bucket also means such a sprayer meets the TIGHT tier
        rather than the generous one, which is the right way round.
        """
        ratelimit.reset()
        for _ in range(3):
            _analyze(app.test_client(), "3f8e0c92-0000-4000-8000-abcdefabcdef")
        assert _charges(SESSION_BUCKET, ratelimit._NO_SESSION_KEY) == 3

    def test_a_cookie_less_caller_is_not_told_to_sign_in(self, app, reap_jobs):
        """The shared no-session bucket is fine; the message was a lie.

        One cookie-less sprayer exhausts ``_NO_SESSION_KEY`` for everybody,
        and the next cookie-less caller is a visitor whose browser is blocking
        cookies. That costs them nothing they had — with no session id they
        cannot own a job directory, so every analysis 404s regardless — but
        telling them to "sign in to keep going" cannot help them either,
        because the login session is a cookie too. Phase 5 turns the ordinary
        session message into a signup funnel, so this must stay separated.
        """
        ratelimit.reset()
        job = "3f8e0c92-0000-4000-8000-abcdefabcdef"
        for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT):
            _analyze(app.test_client(), job)

        refused = _analyze(app.test_client(), job)
        body = refused.get_json()
        assert refused.status_code == 429
        assert body["reason"] == ratelimit.REASON_SESSION_LIMITED
        assert "cookies" in body["error"].lower(), body["error"]
        assert body["error"] != ratelimit._SESSION_LIMIT_MESSAGE, (
            "a caller with no session was told to sign in, which cannot fix "
            "their problem — the login session is a cookie too"
        )

    def test_the_sse_route_reports_the_session_tier_distinctly(
        self, client, stub_pipeline, reap_jobs
    ):
        """EventSource cannot read a non-2xx body, so both tiers leave as
        HTTP 200 text/event-stream and only ``reason`` separates them."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        status = body = None
        for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT + 1):
            status, body = _progress(client, job_id)

        assert status == 200
        frame = json.loads(body.split("data: ", 1)[1])
        assert frame["reason"] == ratelimit.REASON_SESSION_LIMITED, frame

    def test_signed_in_callers_meet_neither_tier(
        self, client, stub_pipeline, reap_jobs
    ):
        job_id = client.get("/scout/example").get_json()["job_id"]
        with client.session_transaction() as sess:
            sess["user_email"] = "someone@example.com"
            sess["user_id"] = "u-paid"
        for _ in range(scout_routes.ANON_ANALYZE_LIMIT + 4):
            _analyze(client, job_id)
        assert _ip_charges() == 0
        assert not ratelimit._FOLLOWUP


# ---------------------------------------------------------------------------
# The credit ledger is bounded, and drops the RIGHT entries
# ---------------------------------------------------------------------------


class TestTheCreditLedgerIsBounded:
    def test_it_does_not_grow_without_limit(self, monkeypatch):
        ratelimit.reset()
        monkeypatch.setattr(ratelimit, "_MAX_KEYS", 50)
        monkeypatch.setattr(ratelimit, "_EVICT_BATCH", 10)
        for i in range(400):
            ratelimit._grant_followup((f"anon:{i}", "198.51.100.1", "job-1"))
        size = len(ratelimit._FOLLOWUP)
        ratelimit.reset()
        assert size <= 50, f"credit table grew to {size} entries"

    def test_eviction_here_fails_closed(self, monkeypatch):
        """The OPPOSITE ordering to _WINDOWS, and deliberately so.

        Dropping a counter re-allows a limited caller — the reset an attacker
        sprays for. Dropping a credit merely charges a caller who would have
        ridden free. So this table evicts soonest-to-expire and the counter
        table evicts lowest-hit-count, and the two policies must not be
        unified.
        """
        ratelimit.reset()
        monkeypatch.setattr(ratelimit, "_MAX_KEYS", 20)
        monkeypatch.setattr(ratelimit, "_EVICT_BATCH", 5)
        victim = ("anon:victim", "198.51.100.2", "job-1")
        ratelimit._grant_followup(victim)
        for i in range(200):
            ratelimit._grant_followup((f"anon:spray{i}", "198.51.100.3", "job-1"))
        survived = ratelimit._spend_followup(victim)
        ratelimit.reset()

        assert survived is False, (
            "an evicted credit still redeemed — eviction here must cost the "
            "caller a charge, never grant one"
        )
