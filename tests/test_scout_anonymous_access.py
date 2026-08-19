"""Epitope Scout is reachable without an account — and safely.

Seven binder-design tool pages tell a first-time visitor to "start here
first, it is free and runs in about 30 seconds". That promise is only true
if ``/scout`` renders, loads the 1HEW example, accepts an upload and scores
a chain for someone who has never signed in.

Opening it also opens the app's only unauthenticated upload + compute path,
so the tests below split into two halves:

  * the promise  — anonymous GET/POST reach every step of the flow;
  * the controls — size cap, parse validation, per-IP rate limit, live-job
    bound, per-session job confidentiality, and the handoff still gated.

    pytest tests/test_scout_anonymous_access.py -v
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path

import pytest

from scout import routes as scout_routes
from scout import ratelimit
from scout.flags import _CSV_COLUMNS_BASE
from scout.jobs import count_job_dirs, create_job_dir, read_owner

TMP = Path("tmp")

# The scoring pipeline needs freesasa, which is a C extension this repo does
# not install on Windows dev boxes. Two ways round it, both used below:
#   * ``stub_scoring`` pre-writes the results.csv the pipeline would have
#     produced, so /analyze skips it entirely and the ROUTE (auth, ownership,
#     rate limit, slot, response shape) is still tested everywhere;
#   * ``requires_freesasa`` skips the one test that insists on the real
#     numbers, so a machine that does have it exercises the true path.
requires_freesasa = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("freesasa") is None,
    reason="freesasa is not installed in this environment",
)


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
    """Delete every job dir this test created, whatever the outcome."""
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


# A minimal but genuinely parseable PDB: one chain, enough residues that
# scout.parser reports a chain rather than an error.
def _tiny_pdb(n_residues: int = 12) -> bytes:
    lines = []
    for i in range(1, n_residues + 1):
        lines.append(
            f"ATOM  {i:5d}  CA  ALA A{i:4d}    "
            f"{i * 3.8:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           C"
        )
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


def _upload(client, data: bytes, filename: str = "target.pdb"):
    return client.post(
        "/scout/upload",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def _write_results_csv(job_id: str) -> None:
    """Drop in the ``results.csv`` a real pipeline run would have written."""
    import csv

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
    with (TMP / job_id / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def stub_scoring(monkeypatch):
    """Neutralise the pipeline's freesasa + network dependencies.

    What is under test here is the anonymous ROUTE, not the biophysics: that
    an unauthenticated caller reaches the scorer, owns the result, and gets it
    back. The numbers themselves are ``scout.pipeline``'s own tests' business.
    """
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown"},
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])


def _login(client, *, user_id="u-anon-test", email="someone@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id


# ---------------------------------------------------------------------------
# The promise: the whole flow works with no account
# ---------------------------------------------------------------------------


class TestAnonymousCanRunScout:
    def test_landing_page_renders(self, client):
        resp = client.get("/scout/")
        assert resp.status_code == 200
        assert b"Epitope Scout" in resp.data

    def test_landing_page_is_not_a_login_redirect(self, client):
        """The regression this whole change exists to prevent."""
        resp = client.get("/scout/", follow_redirects=False)
        assert resp.status_code != 302
        assert "/login" not in resp.headers.get("Location", "")

    def test_landing_page_offers_the_example(self, client):
        assert b"Load example (1HEW)" in client.get("/scout/").data

    def test_example_loads(self, client, reap_jobs):
        resp = client.get("/scout/example")
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["filename"] == "1HEW.pdb"
        assert body["chains"], "example must expose at least one chain"
        assert uuid.UUID(body["job_id"])

    def test_upload_accepts_a_valid_pdb(self, client, reap_jobs):
        resp = _upload(client, _tiny_pdb())
        assert resp.status_code == 200, resp.data
        assert resp.get_json()["chains"][0]["id"] == "A"

    def test_analyze_scores_the_example(self, client, stub_scoring, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        resp = client.post(
            "/scout/analyze",
            json={"job_id": job_id, "chain": "A"},
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        assert body["epitopes"], "a scored surface must come back"
        assert body["epitopes"][0]["composite_score"] > 0

    @requires_freesasa
    def test_analyze_runs_the_real_pipeline(self, client, stub_scoring, reap_jobs):
        """The same flow with nothing stubbed but the network lookups."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 200, resp.data
        assert resp.get_json()["epitopes"]

    def test_results_are_readable_back(self, client, stub_scoring, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert client.get(f"/scout/pdb/{job_id}").status_code == 200
        assert client.get(f"/scout/download/{job_id}").status_code == 200

    def test_progress_stream_opens(self, client, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")
        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        assert b'"stage"' in resp.get_data()

    def test_known_binders_reach_the_response(
        self, client, stub_scoring, monkeypatch, reap_jobs
    ):
        """A found binder must survive the trip to the JSON body.

        ``stub_scoring`` above patches ``fetch_known_binders`` to return [],
        which is right for the tests whose subject is the route's auth and
        ownership — but it was the ONLY thing in the suite touching this path,
        and [] is exactly what the broken production lookup returned. So no
        test ever exercised a NON-empty result, and the route swallows any
        exception from the lookup into `known_binders = []`
        (scout/routes.py). A lookup that works while the route silently drops
        its output is the same invisible failure in a different place.

        The lookup itself is covered by tests/test_scout_epitope_db_sabdab.py;
        this covers the hand-off.
        """
        binder = {
            "pdb_id": "1A2Y", "binder_type": "IgG/Fab", "species": "gallus gallus",
            "resolution": 1.5, "affinity": "", "antigen_chain": "C",
            "ab_chains": ["B", "A"], "contact_residues": [18, 19, 20],
        }
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {
                "uniprot_id": "P00698", "protein_name": "Lysozyme C",
                "identity_pct": "100",
            },
        )
        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders", lambda *a, **k: [binder]
        )

        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        resp = client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        )
        assert resp.status_code == 200, resp.data

        got = resp.get_json()["known_binders"]
        assert got, "a found binder was dropped between the lookup and the body"
        assert got[0]["pdb_id"] == "1A2Y"
        assert got[0]["contact_residues"] == [18, 19, 20]


# ---------------------------------------------------------------------------
# Confidentiality: an anonymous job belongs to one session
# ---------------------------------------------------------------------------


class TestAnonymousJobsAreNotEnumerable:
    def test_job_id_is_a_uuid4(self, client, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        assert uuid.UUID(job_id).version == 4

    def test_owner_marker_is_a_random_session_id(self, client, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        owner = read_owner(TMP / job_id)
        assert owner and owner.startswith(scout_routes.ANON_OWNER_PREFIX)
        assert len(owner) > len(scout_routes.ANON_OWNER_PREFIX) + 16

    def test_another_anonymous_session_cannot_read_the_job(
        self, app, client, stub_scoring, reap_jobs
    ):
        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        stranger = app.test_client()  # separate cookie jar, no session
        assert stranger.get(f"/scout/pdb/{job_id}").status_code == 404
        assert stranger.get(f"/scout/download/{job_id}").status_code == 404
        assert stranger.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 404

    def test_a_signed_in_stranger_cannot_read_it_either(self, app, client, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        stranger = app.test_client()
        _login(stranger, user_id="some-other-user")
        assert stranger.get(f"/scout/pdb/{job_id}").status_code == 404

    @pytest.mark.parametrize(
        "bad", ["not-a-uuid", "../../etc/passwd", "1", ""]
    )
    def test_guessed_ids_are_rejected(self, client, bad):
        assert client.get(f"/scout/pdb/{bad}").status_code in (404, 308)

    def test_read_route_does_not_mint_a_session(self, client):
        """A crawler hitting a job URL must not allocate an owner id."""
        client.get(f"/scout/pdb/{uuid.uuid4()}")
        with client.session_transaction() as sess:
            assert scout_routes.ANON_SESSION_KEY not in sess

    def test_job_survives_signing_in_mid_flow(self, client, reap_jobs):
        """Anonymous run, then sign in for the handoff — the job is still ours."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        _login(client)
        assert client.get(f"/scout/pdb/{job_id}").status_code == 200


# ---------------------------------------------------------------------------
# Abuse controls
# ---------------------------------------------------------------------------


class TestUploadValidation:
    def test_oversized_upload_is_rejected(self, client, reap_jobs):
        payload = b"ATOM  \n" * ((scout_routes.ANON_MAX_UPLOAD_BYTES // 7) + 1000)
        assert len(payload) > scout_routes.ANON_MAX_UPLOAD_BYTES
        resp = _upload(client, payload)
        assert resp.status_code == 413
        assert b"limit" in resp.data

    def test_oversized_upload_leaves_nothing_on_disk(self, client, reap_jobs):
        before = count_job_dirs(scout_routes.ANON_OWNER_PREFIX)
        payload = b"ATOM  \n" * ((scout_routes.ANON_MAX_UPLOAD_BYTES // 7) + 1000)
        _upload(client, payload)
        assert count_job_dirs(scout_routes.ANON_OWNER_PREFIX) == before

    @pytest.mark.parametrize("name", ["evil.exe", "target.txt", "x.pdb.gz", "noext"])
    def test_wrong_extension_is_rejected(self, client, name, reap_jobs):
        resp = _upload(client, _tiny_pdb(), filename=name)
        assert resp.status_code == 400
        assert b"Unsupported file type" in resp.data or b"file type" in resp.data

    def test_malformed_pdb_is_rejected(self, client, reap_jobs):
        resp = _upload(client, b"this is not a structure at all\n" * 50)
        assert resp.status_code == 422

    def test_malformed_pdb_leaves_nothing_on_disk(self, client, reap_jobs):
        before = count_job_dirs(scout_routes.ANON_OWNER_PREFIX)
        _upload(client, b"this is not a structure at all\n" * 50)
        assert count_job_dirs(scout_routes.ANON_OWNER_PREFIX) == before

    def test_missing_file_is_rejected(self, client):
        resp = client.post("/scout/upload", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400


class TestRateLimit:
    def test_intake_limit_triggers(self, client, reap_jobs):
        statuses = [
            _upload(client, b"garbage\n").status_code
            for _ in range(scout_routes.ANON_INTAKE_LIMIT + 2)
        ]
        assert 429 in statuses, statuses
        # The limit must bite only after the allowance is spent.
        assert statuses.index(429) >= scout_routes.ANON_INTAKE_LIMIT

    def test_rate_limited_response_carries_retry_after(self, client, reap_jobs):
        resp = None
        for _ in range(scout_routes.ANON_INTAKE_LIMIT + 2):
            resp = _upload(client, b"garbage\n")
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) > 0
        assert resp.get_json()["error"]

    def test_analyze_limit_triggers(self, client):
        statuses = [
            client.post("/scout/analyze", json={"job_id": "x", "chain": "A"}).status_code
            for _ in range(scout_routes.ANON_ANALYZE_LIMIT + 2)
        ]
        assert 429 in statuses, statuses

    def test_progress_limit_returns_an_sse_error_not_a_429_body(self, client):
        """EventSource cannot read a 429, so the SSE route degrades in-band."""
        resp = None
        for _ in range(scout_routes.ANON_ANALYZE_LIMIT + 2):
            resp = client.get("/scout/progress?job_id=x&chain=A")
        assert resp.mimetype == "text/event-stream"
        payload = json.loads(resp.get_data(as_text=True).split("data: ", 1)[1])
        assert payload["stage"] == "error"
        assert "Too many" in payload["msg"]

    def test_signed_in_users_are_not_ip_limited(self, client, reap_jobs):
        _login(client)
        statuses = [
            _upload(client, b"garbage\n").status_code
            for _ in range(scout_routes.ANON_INTAKE_LIMIT + 3)
        ]
        assert 429 not in statuses, statuses

    def test_separate_buckets_do_not_share_an_allowance(self, client):
        for _ in range(scout_routes.ANON_ANALYZE_LIMIT + 2):
            client.post("/scout/analyze", json={"job_id": "x", "chain": "A"})
        # Intake untouched, so it must still answer normally (400, not 429).
        resp = client.post("/scout/upload", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_limiter_keys_on_ip_not_session(self, app):
        """A fresh cookie jar from the same address does not reset the count."""
        ratelimit.reset()
        first, second = app.test_client(), app.test_client()
        for _ in range(scout_routes.ANON_INTAKE_LIMIT):
            first.post("/scout/upload", data={}, content_type="multipart/form-data")
        resp = second.post(
            "/scout/upload", data={}, content_type="multipart/form-data"
        )
        ratelimit.reset()
        assert resp.status_code == 429


class TestLiveJobBounds:
    def test_per_session_bound_refuses_further_jobs(self, client, monkeypatch, reap_jobs):
        monkeypatch.setattr(scout_routes, "ANON_MAX_LIVE_JOBS_PER_SESSION", 2)
        codes = [client.get("/scout/example").status_code for _ in range(4)]
        assert codes[:2] == [200, 200]
        assert 429 in codes[2:], codes

    def test_global_bound_refuses_further_jobs(self, client, monkeypatch, reap_jobs):
        monkeypatch.setattr(scout_routes, "ANON_MAX_LIVE_JOBS", 0)
        resp = client.get("/scout/example")
        assert resp.status_code == 503
        assert b"capacity" in resp.data

    def test_signed_in_users_are_not_capacity_bound(self, client, monkeypatch, reap_jobs):
        monkeypatch.setattr(scout_routes, "ANON_MAX_LIVE_JOBS", 0)
        monkeypatch.setattr(scout_routes, "ANON_MAX_LIVE_JOBS_PER_SESSION", 0)
        _login(client)
        assert client.get("/scout/example").status_code == 200


class TestConcurrencyBound:
    """How many anonymous pipelines may run at once, distinct from how often."""

    def test_analyze_refuses_when_the_pool_is_full(self, client, monkeypatch, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        monkeypatch.setattr(scout_routes, "ANON_MAX_CONCURRENT_RUNS", 0)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 503
        assert b"busy" in resp.data

    def test_progress_reports_busy_in_band(self, client, monkeypatch, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        monkeypatch.setattr(scout_routes, "ANON_MAX_CONCURRENT_RUNS", 0)
        resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")
        assert resp.mimetype == "text/event-stream"
        payload = json.loads(resp.get_data(as_text=True).split("data: ", 1)[1])
        assert payload["stage"] == "error"
        assert "busy" in payload["msg"]

    def test_signed_in_users_never_consume_a_slot(
        self, client, monkeypatch, stub_scoring, reap_jobs
    ):
        _login(client)
        monkeypatch.setattr(scout_routes, "ANON_MAX_CONCURRENT_RUNS", 0)
        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 200, resp.data

    def test_slot_is_released_after_a_run(self, client, stub_scoring, reap_jobs):
        """A leaked slot would wedge the pool at 'full' for the process life."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        _write_results_csv(job_id)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert ratelimit.inflight_anon_runs() == 0

    def test_slot_is_released_when_the_pipeline_raises(self, client, monkeypatch, reap_jobs):
        job_id = client.get("/scout/example").get_json()["job_id"]
        monkeypatch.setattr(
            "scout.pipeline.run_pipeline",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert ratelimit.inflight_anon_runs() == 0

    def test_slot_is_released_when_the_stream_is_abandoned(self, client, reap_jobs):
        """Closing the response mid-stream must give the slot back."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")
        resp.close()
        assert ratelimit.inflight_anon_runs() == 0


class TestCountJobDirs:
    def test_counts_only_the_matching_prefix(self, tmp_path):
        create_job_dir("anon:aaa", base_dir=tmp_path)
        create_job_dir("anon:bbb", base_dir=tmp_path)
        create_job_dir("real-user-id", base_dir=tmp_path)
        assert count_job_dirs("anon:", base_dir=tmp_path) == 2
        assert count_job_dirs("anon:aaa", base_dir=tmp_path) == 1
        assert count_job_dirs("real-user-id", base_dir=tmp_path) == 1

    def test_empty_prefix_counts_nothing(self, tmp_path):
        create_job_dir("anon:aaa", base_dir=tmp_path)
        assert count_job_dirs("", base_dir=tmp_path) == 0

    def test_ignores_sibling_tenants_under_shared_tmp(self, tmp_path):
        (tmp_path / "calibration").mkdir()
        (tmp_path / "pdb_compare").mkdir()
        assert count_job_dirs("anon:", base_dir=tmp_path) == 0


# ---------------------------------------------------------------------------
# The parts that still require an account
# ---------------------------------------------------------------------------


class TestStillGated:
    @pytest.mark.parametrize(
        "path",
        [
            "/scout/feasibility",
            "/scout/feasibility/download/" + str(uuid.uuid4()),
        ],
    )
    def test_feasibility_get_requires_login(self, client, path):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_feasibility_analyze_requires_login(self, client):
        resp = client.post("/scout/feasibility/analyze", json={"job_id": "x", "chain": "A"})
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_handoff_requires_login(self, client, reap_jobs):
        """The handoff writes a user-keyed scout_handoffs row — it needs an id."""
        job_id = client.get("/scout/example").get_json()["job_id"]
        resp = client.post(
            "/scout/handoff/tool",
            data={
                "tool": "rfantibody",
                "scout_job_id": job_id,
                "hotspot_residues": "10,11",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_results_table_degrades_the_feasibility_link_when_anonymous(self, client):
        body = client.get("/scout/").get_data(as_text=True)
        assert "var SCOUT_AUTHENTICATED = false;" in body
        assert "Sign in to assess" in body

    def test_results_table_links_straight_through_when_signed_in(self, client):
        _login(client)
        body = client.get("/scout/").get_data(as_text=True)
        assert "var SCOUT_AUTHENTICATED = true;" in body


class TestRateLimitKeyCannotBeChosenByTheCaller:
    """The limiter is only worth anything if its key is not client-supplied.

    Security QC (2026-08-18) broke the original limiter exactly this way:
    with a fixed address 14 intakes gave {200: 10, 429: 4}, but rotating
    ``X-Forwarded-For`` gave {200: 40, 429: 0} across 40 intakes, because
    ``_client_ip`` read the LEFTmost hop — the one the caller writes.

    Railway's edge sits in front of the app. Whether it *appends* to
    X-Forwarded-For or *overwrites* it was never confirmed, so both are
    modelled below and the limit must hold under each.
    """

    # The address the real (attacking) client connects from. Under append
    # semantics the edge puts this at the END of whatever the client sent.
    REAL = "198.51.100.9"

    def _intake(self, client, xff):
        return client.post(
            "/scout/upload",
            data={},
            content_type="multipart/form-data",
            headers={"X-Forwarded-For": xff},
        )

    def test_rotating_forwarded_for_still_trips_the_limit_append_semantics(
        self, client
    ):
        """Edge APPENDS: the forged prefix rotates, our hop does not."""
        statuses = [
            self._intake(client, f"10.9.9.{i}, {self.REAL}").status_code
            for i in range(scout_routes.ANON_INTAKE_LIMIT + 5)
        ]
        assert 429 in statuses, f"rotating XFF defeated the limiter: {statuses}"
        assert statuses.index(429) >= scout_routes.ANON_INTAKE_LIMIT

    def test_rotating_forwarded_for_still_trips_the_limit_overwrite_semantics(
        self, client
    ):
        """Edge OVERWRITES: whatever the client forged never reaches us."""
        statuses = [
            self._intake(client, self.REAL).status_code
            for _ in range(scout_routes.ANON_INTAKE_LIMIT + 5)
        ]
        assert 429 in statuses, f"limiter did not trip: {statuses}"
        assert statuses.index(429) >= scout_routes.ANON_INTAKE_LIMIT

    def test_a_long_forged_chain_does_not_dodge_the_limit(self, client):
        """Padding the header with many rotating hops must not move our hop.

        The pad rotates every request, so any implementation that picks a
        hop counted from the LEFT — at whatever index — sees a fresh key
        each time and never trips.
        """
        statuses = []
        for n in range(scout_routes.ANON_INTAKE_LIMIT + 5):
            pad = ", ".join(f"172.16.{n}.{i}" for i in range(15))
            statuses.append(self._intake(client, f"{pad}, {self.REAL}").status_code)
        assert 429 in statuses, f"padded XFF defeated the limiter: {statuses}"

    def test_analyze_bucket_resists_the_same_attack(self, client):
        """QC broke /analyze the same way, so pin it too."""
        statuses = [
            client.post(
                "/scout/analyze",
                json={"job_id": "x", "chain": "A"},
                headers={"X-Forwarded-For": f"10.9.9.{i}, {self.REAL}"},
            ).status_code
            for i in range(scout_routes.ANON_ANALYZE_LIMIT + 5)
        ]
        assert 429 in statuses, f"rotating XFF defeated /analyze: {statuses}"

    def test_genuinely_distinct_clients_keep_separate_allowances(self, client):
        """Negative control.

        Without this, an implementation that collapsed every caller onto one
        bucket (or onto "") would pass every test above while rate-limiting
        the whole internet as a single user.
        """
        statuses = [
            self._intake(client, f"10.9.9.1, 203.0.113.{i}").status_code
            for i in range(scout_routes.ANON_INTAKE_LIMIT + 5)
        ]
        assert 429 not in statuses, f"distinct clients shared a bucket: {statuses}"


class TestCounterTableEviction:
    """Memory pressure must not become a way to clear the limiter.

    The table is bounded, but the bound has to degrade gracefully: the
    original code cleared the WHOLE table once it filled, so spraying unique
    keys re-allowed every previously-limited caller. That makes the pressure
    itself the attack.
    """

    def test_a_limited_key_stays_limited_through_eviction(self, monkeypatch):
        ratelimit.reset()
        # Shrink the table so the eviction path runs in milliseconds.
        monkeypatch.setattr(ratelimit, "_MAX_KEYS", 50)
        monkeypatch.setattr(ratelimit, "_EVICT_BATCH", 10)

        allowed = True
        for _ in range(4):
            allowed, _ = ratelimit.hit("b", "victim", limit=2, window_seconds=600)
        assert allowed is False, "victim was never limited to begin with"

        # Spray an order of magnitude more unique keys than the table holds.
        for i in range(600):
            ratelimit.hit("b", f"spray-{i}", limit=2, window_seconds=600)

        allowed, _ = ratelimit.hit("b", "victim", limit=2, window_seconds=600)
        ratelimit.reset()
        assert allowed is False, "eviction re-allowed a previously limited caller"

    def test_the_table_stays_bounded_under_a_spray(self, monkeypatch):
        """Graceful degradation, not unbounded growth — the original goal."""
        ratelimit.reset()
        monkeypatch.setattr(ratelimit, "_MAX_KEYS", 50)
        monkeypatch.setattr(ratelimit, "_EVICT_BATCH", 10)
        for i in range(600):
            ratelimit.hit("b", f"spray-{i}", limit=2, window_seconds=600)
        size = len(ratelimit._WINDOWS)
        ratelimit.reset()
        assert size <= 50, f"counter table grew past its bound: {size}"

    def test_expired_entries_are_reclaimed_before_anything_live_is_evicted(
        self, monkeypatch
    ):
        """Expiry sweep runs first, so lapsed windows cost no live eviction.

        A table full of ALREADY-EXPIRED windows must collapse to near-empty
        via the sweep. If the sweep were dropped, the same load would instead
        sit at the cap and be handled by eviction, leaving the table full.
        """
        ratelimit.reset()
        monkeypatch.setattr(ratelimit, "_MAX_KEYS", 50)
        monkeypatch.setattr(ratelimit, "_EVICT_BATCH", 10)
        # A 0-second window is already expired by the time the next call runs.
        for i in range(60):
            ratelimit.hit("b", f"lapsed-{i}", limit=2, window_seconds=0)
        size = len(ratelimit._WINDOWS)
        ratelimit.reset()
        assert size < 20, f"expired windows were not reclaimed: {size} entries held"
