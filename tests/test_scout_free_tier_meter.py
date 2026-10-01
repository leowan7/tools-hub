"""Signing in to Epitope Scout must never buy you LESS than staying anonymous.

THE DEFECT THIS CLOSES, measured 2026-09-30 on ``2ef5a167``. Scout had two
meters whose populations did not overlap:

* ``scout.quota.requires_scout_quota`` — 3 completed runs per trailing 30
  days, on ``/scout/analyze`` and ``/scout/progress``. It passed through when
  ``session["user_email"]`` was absent, so an anonymous visitor never met it.
* ``scout.ratelimit.anon_rate_limit`` — the window counters. It returned early
  on ``session["user_email"]``, so a signed-in caller never met those.

Net effect: an anonymous visitor got the window allowance and a signed-in free
user got three runs a month, while the refusal copy and the site's CTA both
pushed people into creating the account. Signing up was a downgrade.

The fix deletes the per-user cap and keys the window counters on the account,
so on the analysis routes the window tier is the one meter for signed-in and
anonymous callers alike. The per-IP tier stays anonymous-only on purpose - see
the exemption comment in ``scout.ratelimit`` - which is what makes signing in
strictly better rather than merely equal. This file holds the properties that
make that true, plus the
regression guard for a second bug the old arrangement had — see
``test_a_recorded_run_can_still_be_streamed``.

Most tests here probe the METER and nothing else, by hitting
``/scout/progress`` with a job id that does not exist: the decorator runs
before the view, so the charge lands and the view then answers ``job_expired``.
That keeps them off the disk cap in ``GET /scout/example``
(``ANON_MAX_LIVE_JOBS_PER_SESSION``), which is a different mechanism with a
different population and would otherwise be the thing they measured.

    pytest tests/test_scout_free_tier_meter.py -v
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from scout import ratelimit
from scout import routes as scout_routes
from scout.flags import _CSV_COLUMNS_BASE

pytestmark = pytest.mark.usefixtures("isolate_supabase")

TMP = Path("tmp")

# Every reason this module's two tiers can refuse with. Anything else a
# progress response can carry (``bad_request``, ``job_expired``) comes from the
# VIEW, which means the meter let the request through — the distinction the
# probe below is built on.
METER_REASONS = {
    ratelimit.REASON_RATE_LIMITED,
    ratelimit.REASON_SESSION_LIMITED,
    ratelimit.REASON_SIGNED_IN_LIMITED,
    ratelimit.REASON_NO_SESSION,
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(autouse=True)
def clean_windows():
    """One empty meter per test, in both directions.

    ``ratelimit._WINDOWS`` is module state shared by every test in the process
    and these tests work by exhausting an allowance, so a leaked window makes
    the NEXT test start part-spent and fail for a reason that has nothing to do
    with it.
    """
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def reap_jobs():
    """Delete only the job dirs this test created.

    ``tmp/`` is shared with every other worktree and with the dev server, and a
    reaper that rmtree'd it wholesale has fired in production before
    (``scout.jobs.cleanup_old_jobs``). Snapshot, then remove the difference.
    """
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Run the analyse path without freesasa or the network.

    ``/scout/progress`` has to leave a ``results.csv`` behind for
    ``/scout/analyze`` to finalise, so the stub writes one rather than skipping
    the work entirely.
    """

    def _fake_pipeline(pdb_path, chain_id, progress_callback=None):
        row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
        row.update({
            "epitope_id": "1",
            "chain_id": chain_id,
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
        lambda *a, **k: {
            "uniprot_id": "",
            "protein_name": "",
            "identity_pct": "unknown",
            "source": "",
        },
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])


def _anonymous(app, anon_id="anon:probe"):
    """A visitor who is not signed in but whose cookies work.

    The anon id is planted rather than earned: it is minted lazily by
    ``scout.routes._current_owner_key`` on upload/example, and a visitor
    without one shares the single cookie-less bucket, which is a third
    population with its own message. This is the ordinary anonymous visitor,
    not the cookie-blocked one.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[ratelimit.ANON_SESSION_KEY] = anon_id
    return client


def _signed_in(app, email="free@example.com", user_id="u-free"):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id
    return client


def _progress(client, job_id, chain="A"):
    resp = client.get(f"/scout/progress?job_id={job_id}&chain={chain}")
    try:
        return resp.status_code, resp.get_data(as_text=True)
    finally:
        resp.close()


def _frame(body):
    """The last SSE ``data:`` frame of an event-stream body, or None.

    Refusals carry ``msg``, not ``error``: the JSON and SSE shapes of a refusal
    differ (``scout.ratelimit._refuse``), and this file only reads the SSE one.
    """
    if "data: " not in body:
        return None
    return json.loads(body.rsplit("data: ", 1)[1].split("\n", 1)[0])


def _drain_the_meter(client, limit=60):
    """Hit ``/scout/progress`` until the METER refuses.

    Returns ``(requests_the_meter_allowed, the_refusal_frame)``. The job id is
    deliberately one that does not exist: the decorator charges before the view
    runs, so an allowed request still costs its charge and then comes back
    ``job_expired`` from the view.
    """
    allowed = 0
    for _ in range(limit):
        status, body = _progress(client, "no-such-job")
        assert status == 200, status  # sse=True: refusals are 200 streams
        frame = _frame(body) or {}
        if frame.get("reason") in METER_REASONS:
            return allowed, frame
        allowed += 1
    raise AssertionError(f"the meter allowed {limit} requests and never refused")


def _session_hits(key):
    """Hits recorded in the per-session analyze bucket under one key."""
    entry = ratelimit._WINDOWS.get(("scout_analyze:session", key))
    return entry[1] if entry else 0


# ---------------------------------------------------------------------------
# The inversion itself
# ---------------------------------------------------------------------------


def test_signing_in_does_not_change_the_allowance(app):
    """THE property, and the one the whole change exists for.

    Asserted as EQUALITY, not as ``signed_in >= anonymous``. The defect was an
    inequality one way and the obvious over-correction is an inequality the
    other way; both are bugs, and only equality says "one meter".

    The number itself is not restated here. ``scout.routes`` owns it
    (``ANON_ANALYZE_SESSION_LIMIT``), and a test that hardcodes a cap's value is
    how the real number drifts away from the advertised one — which is exactly
    what the deleted template fallback ``q.runs_cap || 3`` did.
    """
    anon_allowed, anon_refusal = _drain_the_meter(_anonymous(app))
    ratelimit.reset()
    signed_allowed, signed_refusal = _drain_the_meter(_signed_in(app))

    assert anon_allowed > 0, "the anonymous visitor got nothing; the probe is broken"
    assert signed_allowed == anon_allowed, (
        f"signed in got {signed_allowed} requests, anonymous got {anon_allowed}. "
        "Signing in must not change the allowance in either direction."
    )
    # Same TIER too, not merely the same count: an equal number reached by a
    # different mechanism is a coincidence this test should not accept. The two
    # reasons differ by design — a signed-in refusal must not be counted as a
    # conversion opportunity (scout/ratelimit.py::REASON_SIGNED_IN_LIMITED) —
    # so what is asserted is that both came from the per-SESSION tier and
    # neither from the per-IP one, which would mean a different allowance.
    assert anon_refusal["reason"] == ratelimit.REASON_SESSION_LIMITED
    assert signed_refusal["reason"] == ratelimit.REASON_SIGNED_IN_LIMITED


def test_a_signed_in_caller_is_metered_at_all(app):
    """Deleting the cap must not leave a signed-in user metered by nothing.

    This is the failure mode the half-done fix has. ``requires_scout_quota``
    was the ONLY thing bounding a signed-in caller, so removing it without also
    removing ``anon_rate_limit``'s ``session["user_email"]`` early exit would
    have made signed-in Scout compute unlimited — ~9 CPU-s per request at the
    8 MB cap (``scout/routes.py``, the ``pair=PAIR_OPENS`` comment on
    ``/scout/progress``).
    """
    allowed, refusal = _drain_the_meter(_signed_in(app))
    assert refusal["reason"] in METER_REASONS, refusal
    assert allowed <= scout_routes.ANON_ANALYZE_SESSION_LIMIT, allowed


def test_two_signed_in_users_do_not_share_a_bucket(app):
    """``_session_key`` must key a signed-in caller on their own identity.

    Not cosmetic. ``scout.routes._current_owner_key`` returns the signed-in key
    before it ever mints ``scout_anon_id``, so a signed-in session carries no
    anonymous id — and the fallback for "no id" is ONE shared bucket,
    ``ratelimit._NO_SESSION_KEY``. Without the signed-in branch in
    ``_session_key`` every signed-in user in the fleet would spend a single
    allowance between them and be refused with "allow cookies", which is the
    inversion back again in a new shape.
    """
    for user_id in ("u-a", "u-b"):
        client = _signed_in(app, email=f"{user_id}@example.com", user_id=user_id)
        _progress(client, "no-such-job")

    shared = _session_hits(ratelimit._NO_SESSION_KEY)
    hits_a = _session_hits(ratelimit._USER_KEY_PREFIX + "u-a")
    hits_b = _session_hits(ratelimit._USER_KEY_PREFIX + "u-b")
    assert shared == 0, (
        f"signed-in callers landed in the shared cookie-less bucket ({shared} "
        "hits) — they would spend one allowance between every user in the fleet"
    )
    assert (hits_a, hits_b) == (1, 1), (hits_a, hits_b)


def test_a_signed_in_user_does_not_share_a_bucket_with_an_anonymous_one(app):
    """And the signed-in key must not collide with an anon id either.

    ``_USER_KEY_PREFIX`` is what keeps the two namespaces apart. Dropping it
    would make a signed-in user whose id happened to equal some visitor's
    ``scout_anon_id`` share that visitor's allowance.
    """
    _progress(_anonymous(app, anon_id="u-collide"), "no-such-job")
    _progress(_signed_in(app, user_id="u-collide"), "no-such-job")
    assert _session_hits("u-collide") == 1
    assert _session_hits(ratelimit._USER_KEY_PREFIX + "u-collide") == 1


# ---------------------------------------------------------------------------
# What the refusal says
# ---------------------------------------------------------------------------


def test_the_signed_in_refusal_does_not_tell_them_to_sign_in(app):
    """The session-tier message is a funnel line written for anonymous visitors.

    Handing it to someone already signed in offers them a door they are
    standing behind, and calls their allowance "the free Epitope Scout
    allowance" when there is now one allowance for everybody. Before
    2026-09-30 a signed-in caller could not reach this message at all — the
    limiter exempted them — so the string was never wrong until the exemption
    went away.
    """
    _, refusal = _drain_the_meter(_signed_in(app))
    message = refusal["msg"]
    assert message == ratelimit._SIGNED_IN_LIMIT_MESSAGE, message
    lowered = message.lower()
    assert "sign in" not in lowered, message
    assert "free account" not in lowered, message


def test_an_anonymous_refusal_still_offers_the_account(app):
    """Negative control for the test above.

    The funnel is still there for the population it was written for. Without
    this, deleting the sign-in sentence from every message would satisfy the
    signed-in test for entirely the wrong reason.
    """
    _, refusal = _drain_the_meter(_anonymous(app))
    assert refusal["msg"] == ratelimit._SESSION_LIMIT_MESSAGE
    assert "sign in" in refusal["msg"].lower(), refusal


def test_a_signed_in_caller_is_not_charged_the_shared_ip_bucket(app, reap_jobs):
    """The per-IP tier stays anonymous-only, and that is what makes signing in
    BETTER rather than merely equal.

    That bucket is ONE budget for a whole address. Charging identified users
    against it would mean any anonymous stranger behind an institution's NAT
    could lock out a signed-in colleague, and one signed-in researcher working
    through their own allowance would spend most of their institution's. The
    exemption predates this change; removing the run cap deliberately did not
    widen the tier to cover signed-in callers.
    """
    client = _signed_in(app)
    _drain_the_meter(client)
    assert ratelimit._WINDOWS.get(("scout_analyze", "127.0.0.1")) is None, (
        "a signed-in caller charged the shared per-IP bucket; a stranger on "
        "the same NAT can now lock them out"
    )
    # The control: an anonymous caller DOES charge it, so the assertion above
    # is about who is exempt and not about a bucket nothing ever writes to.
    ratelimit.reset()
    _drain_the_meter(_anonymous(app))
    assert ratelimit._WINDOWS.get(("scout_analyze", "127.0.0.1")) is not None


def test_a_signed_in_analysis_is_one_charge_not_two(app, stub_pipeline, reap_jobs):
    """One analysis costs a signed-in caller ONE session charge.

    The per-IP exemption must skip only that tier's ``hit``, never return
    early from the decorator, because the ``PAIR_OPENS`` credit is granted
    BELOW it (``scout/ratelimit.py::anon_rate_limit``). An early return would
    leave the following ``POST /scout/analyze`` with no credit to spend, so it
    would be charged a second time and a signed-in user's real allowance would
    be HALF the anonymous one — the inversion back again, in the shape the
    obvious implementation of the fix produces.

    Measured against the anonymous cost for the same pair rather than against
    the literal 1, so the two populations cannot drift apart silently.
    """
    signed = _signed_in(app)
    job_id = signed.get("/scout/example").get_json()["job_id"]
    before = _session_hits(ratelimit._USER_KEY_PREFIX + "u-free")
    _progress(signed, job_id)
    assert signed.post(
        "/scout/analyze", json={"job_id": job_id, "chain": "A"}
    ).status_code == 200
    signed_cost = _session_hits(ratelimit._USER_KEY_PREFIX + "u-free") - before

    ratelimit.reset()
    anon = _anonymous(app)
    anon_job = anon.get("/scout/example").get_json()["job_id"]
    _progress(anon, anon_job)
    assert anon.post(
        "/scout/analyze", json={"job_id": anon_job, "chain": "A"}
    ).status_code == 200
    anon_cost = _session_hits("anon:probe")

    assert signed_cost == 1, (
        f"one signed-in analysis cost {signed_cost} session charges, not 1 — "
        "the PAIR_OPENS credit was not granted, so the allowance is halved"
    )
    assert signed_cost == anon_cost, (signed_cost, anon_cost)


# ---------------------------------------------------------------------------
# The second bug, which the cap caused and its removal closes
# ---------------------------------------------------------------------------


def test_a_recorded_run_can_still_be_streamed(app, stub_pipeline, reap_jobs):
    """Re-opening a finished run's stream must work. It did not, before.

    ``@requires_scout_quota`` sat on ``/scout/progress`` as well as on
    ``/scout/analyze``, and ``record_scout_run`` fires at the END of
    ``/scout/analyze``. So a free user's third analysis went:

        GET  /scout/progress   check used=2 cap=3  -> allowed
        POST /scout/analyze    check used=2 cap=3  -> allowed, records, used=3
        GET  /scout/progress   check used=3 cap=3  -> REFUSED

    MEASURED, not reasoned about: a probe on ``2ef5a167`` drove exactly that
    sequence against a stubbed quota and the re-poll came back ``302`` to
    ``/scout/``. Worse than the status suggests, because ``/scout/progress`` is
    read by ``EventSource``, which cannot surface a redirect to an HTML page —
    the stream of a run the user had already spent their quota on simply died
    with no error frame at all.

    No QUOTA can refuse a second look at a completed run any more, which is what
    this asserts. It does NOT assert the window limiter is absent: a re-poll is
    a fresh charge against it like any other request. Only that the same job can
    be streamed again from inside the allowance.
    """
    client = _signed_in(app)
    job_id = client.get("/scout/example").get_json()["job_id"]

    status, _ = _progress(client, job_id)
    assert status == 200, status
    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, (resp.status_code, resp.get_data(as_text=True))

    status, body = _progress(client, job_id)
    assert status == 200, f"re-poll of an already-completed run got {status}"
    frame = _frame(body)
    assert frame is not None, body[:300]
    assert not frame.get("reason"), f"re-poll of a completed run was refused: {frame}"


# ---------------------------------------------------------------------------
# The data trail the cap used to read
# ---------------------------------------------------------------------------


def test_the_run_ledger_is_still_written(app, stub_pipeline, reap_jobs, monkeypatch):
    """Removing the REFUSAL must not silently remove the DATA TRAIL.

    ``public.scout_runs`` rows outlive the cap that used to count them: they are
    the retained analytics and provenance record, including which
    secondary-structure branch ran (``ss_method``, written beside the
    ``record_scout_run`` call in ``scout/routes.py``). Nothing reads them to
    refuse a request any more, so a regression here would be invisible in
    behaviour and only this test would notice.
    """
    recorded = []
    monkeypatch.setattr(
        "scout.routes.record_scout_run",
        lambda email, **kw: recorded.append((email, kw)) or True,
    )
    client = _signed_in(app)
    job_id = client.get("/scout/example").get_json()["job_id"]
    _progress(client, job_id)
    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert recorded, "a completed signed-in analysis wrote no scout_runs row"
    assert recorded[0][0] == "free@example.com", recorded


def test_an_anonymous_run_writes_no_ledger_row(
    app, stub_pipeline, reap_jobs, monkeypatch
):
    """The other half of the retention promise, which is a PII statement.

    An anonymous Scout run must reach no Supabase row; the guard is the
    ``if _email:`` around the ``record_scout_run`` call in ``scout/routes.py``.
    Unchanged by this work — asserted here because the surrounding quota code
    that also read the session email was deleted around it.
    """
    recorded = []
    monkeypatch.setattr(
        "scout.routes.record_scout_run",
        lambda email, **kw: recorded.append((email, kw)) or True,
    )
    client = app.test_client()
    job_id = client.get("/scout/example").get_json()["job_id"]
    _progress(client, job_id)
    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert not recorded, f"an anonymous run wrote a Supabase row: {recorded}"


# ---------------------------------------------------------------------------
# The mechanism is gone, not merely unreferenced
# ---------------------------------------------------------------------------


def test_the_cap_mechanism_no_longer_exists():
    """A dormant cap is one import away from coming back.

    Absence of the NAMES, not a value: that is the only form of this assertion a
    future edit cannot satisfy by setting the cap to a large number, and the
    brief was explicit that a mechanism should be deleted rather than tuned.
    """
    import scout.quota as quota

    for gone in (
        "FREE_TIER_RUN_CAP",
        "UNLIMITED_TIERS",
        "requires_scout_quota",
        "quota_status",
        "_count_runs_last_30d",
        "_get_tier",
    ):
        assert not hasattr(quota, gone), (
            f"scout.quota.{gone} is back. The per-user free-tier run cap was "
            "removed on 2026-09-30 and must not return in this form."
        )
    assert hasattr(quota, "record_scout_run"), "the ledger writer must stay"


def test_the_quota_endpoint_is_gone(app):
    """``GET /scout/quota`` served ``runs_cap`` / ``runs_remaining``.

    Both are meaningless now, and an endpoint answering 200 with an invented cap
    is worse than one that 404s — the template's ``q.runs_cap || 3`` fallback
    was exactly how a stale number outlived the Python constant.
    """
    assert app.test_client().get("/scout/quota").status_code == 404


def test_no_shipped_front_end_surface_still_advertises_a_run_cap():
    """The cap's numbers lived in markup, JS and CSS as well as in Python.

    Greps the two artifacts a visitor actually receives rather than the whole
    repo: QC history on this codebase is that the surface which survives a
    deletion is the template or prose next to the code, not the code.

    The route is matched as ``fetch('/scout/quota`` and not as the bare path,
    because the path still appears in two comments that record what was removed
    and why. Those are the deletion's audit trail; a grep that forbade them
    would be pressure to delete the explanation along with the code.
    """
    root = Path(__file__).resolve().parent.parent
    page = (root / "templates" / "scout" / "index.html").read_text(encoding="utf-8")
    for dead in (
        "runs_cap",
        "runs_remaining",
        "scout-quota-pill",
        "fetch('/scout/quota",
    ):
        assert dead not in page, f"{dead!r} still ships on the Scout page"
    # Same reasoning for the stylesheet: the old class name survives in the
    # section comment that records the rename, so what must be absent is a RULE
    # for it, not the string.
    css = (root / "static" / "scout.css").read_text(encoding="utf-8")
    assert ".scout-quota-pill {" not in css
    assert ".scout-quota-pill--warn" not in css
