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

import csv
import json
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
    """
    def _fake_pipeline(pdb_path, chain_id, progress_callback=None):
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
