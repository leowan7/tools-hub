"""Guards for the SAbDab known-binder lookup in ``scout.epitope_db``.

Why this file exists
--------------------

The lookup was dead in production and no test noticed. SAbDab was rebuilt as a
single-page app; the per-structure endpoint the code called
(``opig.stats.ox.ac.uk/webapps/sabdab-sabpred/sabdab/summary/<pdb_id>/``) began
301-ing to that SPA and answering an identical ~1457-byte HTML shell for every
PDB id. ``_sabdab_entry_for_pdb`` read a leading ``<`` as "this structure is not
in the antibody database" and returned ``[]``, so every target — including EGFR,
one of the most antibody-co-crystallised proteins in the PDB — reported zero
known binders, at a measured cost of 41 HTTPS requests and ~1.9 CPU-seconds per
anonymous analysis.

The only test that touched this path
(``tests/test_scout_anonymous_access.py``'s ``stub_scoring``) monkeypatches
``fetch_known_binders`` to ``lambda *a, **k: []`` — that is, it asserts the
broken value. It is correct for what it does (isolating a ROUTE test from the
network) but it means the suite could never tell "feature works" from "feature
is dead". That is the failure this file is here to make impossible to repeat.

Design of the guards below, in order of what they would have caught:

1. A payload that is not the summary CSV — the actual failure — must RAISE,
   never parse to "no binders". Silence is the bug.
2. The URL must be the live API, not the retired webapps path.
3. ``query_sabdab`` must spawn NO threads. The old 40-thread fan-out is what
   made a threaded worker class unsafe; a regression there is a thread bomb,
   not just a slowdown.
4. A failed fetch must not be cached for a day, and must not poison the
   per-UniProt cache with a permanent zero.

Everything here is hermetic. The one test that talks to SAbDab is skipped
unless ``SCOUT_SABDAB_LIVE=1``, following ``tests/test_rls.py``.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
import requests

from scout import epitope_db


# ---------------------------------------------------------------------------
# Fixture payloads, captured verbatim from the live API on 2026-08-18
# ---------------------------------------------------------------------------
# The real 45-column header. Kept whole rather than trimmed to the six columns
# the parser reads, so that a column RENAME upstream shows up here as a real
# diff against a real payload instead of against a convenient invention.

_SUMMARY_HEADER = (
    "INSTANCE,PDB,SABDAB_ID,HEAVY_ID,LIGHT_ID,Hchain,Lchain,model,antigen_chain,"
    "antigen_type,antigen_name,short_header,date,SABDABdepo_date,SABDABupdate_date,"
    "compound,organism,organism_taxid,expression_system,expression_system_taxid,"
    "heavy_species,light_species,heavy_taxid,light_taxid,heavy_expression_system,"
    "light_expression_system,heavy_expression_system_taxid,"
    "light_expression_system_taxid,antigen_species,agtaxid,agexpression_system,"
    "agexpression_system_taxid,authors,method,resolution,r_free,r_factor,type,"
    "chainsharing_construct,chainsharing_construct_partners,"
    "non_chainsharing_construct,non_chainsharing_construct_partners,"
    "heavy_subclass,light_subclass,light_ctype"
)

# 1A2Y: Fab D1.3 against hen lysozyme. Multi-chain antigen ("C|D") and a
# quoted compound field containing commas, so this row also exercises the CSV
# quoting the old tab-separated parser never had to handle.
_ROW_1A2Y = (
    'pdb_00001a2y-B-A,pdb_00001a2y,sabdab2_H000EL0014,H000E,L0014,B,A,0,C|D,'
    'PROTEIN|ION,LYSOZYME|PHOSPHATE ION,COMPLEX (IMMUNOGLOBULIN/HYDROLASE),'
    '1998/01/13,20260605,20260806,"HEN EGG WHITE LYSOZYME, D18A MUTANT, IN '
    'COMPLEX WITH MOUSE MONOCLONAL ANTIBODY D1.3",mus musculus,10090,'
    'escherichia coli,562,mus musculus,mus musculus,10090,10090,'
    'escherichia coli,escherichia coli,562,562,gallus gallus,9031,'
    'saccharomyces cerevisiae,4932,"Tsuchiya, D.,Mariuzza, R.A.",XRAY,1.5,'
    '0.251,0.203,FV,NA,NA,NA,NA,IGHV2 (Musmus),IGKV12 (Musmus),K'
)

# 5LZ0: a llama nanobody with NO light chain and NO antigen. Every optional
# field is the literal string "NA", which is SAbDab's null and must not reach
# the UI as the word "NA".
_ROW_5LZ0 = (
    'pdb_00005lz0-A,pdb_00005lz0,sabdab2_H01H9L0000,H01H9,NA,A,NA,0,NA,NA,NA,'
    'IMMUNE SYSTEM,2016/09/29,20260605,20260605,Llama nanobody PorM_01,'
    'lama glama,9844,escherichia coli,562,lama glama,NA,9844,NA,'
    'escherichia coli,NA,562,NA,NA,NA,NA,NA,'
    '"Duhoo, Y.,Leone, P.,Roussel, A.",XRAY,1.6,0.232,0.203,SD-H,NA,NA,NA,NA,'
    'NA,NA,NA'
)

# 7K8M: Fab C102 against the SARS-CoV-2 RBD. Single-chain antigen.
_ROW_7K8M = (
    'pdb_00007k8m-A-B,pdb_00007k8m,sabdab2_H03AHL02C2,H03AH,L02C2,A,B,0,E,'
    'PROTEIN,Spike glycoprotein,VIRAL PROTEIN/IMMUNE SYSTEM,2020/09/27,'
    '20260605,20260806,"Structure of the SARS-CoV-2 receptor binding domain '
    'in complex with the human neutralizing antibody Fab fragment, C102",'
    'homo sapiens,9606,homo sapiens,9606,homo sapiens,homo sapiens,9606,9606,'
    'homo sapiens,homo sapiens,9606,9606,'
    'severe acute respiratory syndrome coronavirus 2,2697049,homo sapiens,'
    '9606,"Jette, C.A.,Barnes, C.O.,Bjorkman, P.J.",XRAY,3.2,0.2342,0.1753,'
    'FAB,NA,NA,NA,NA,IGHV3 (Homsap),IGKV3 (Homsap),K'
)

_SUMMARY_CSV = "\n".join([_SUMMARY_HEADER, _ROW_1A2Y, _ROW_5LZ0, _ROW_7K8M])

# The retired endpoint's actual reply, truncated. Every PDB id got this exact
# body, which is why every lookup came back empty.
_SPA_SHELL = (
    '<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="UTF-8" />\n'
    '    <link rel="icon" type="image/svg+xml" href="/SAbDab.svg" />\n'
    "    <title>SAbDab2</title>\n  </head>\n  <body>\n"
    '    <div id="root"></div>\n  </body>\n</html>\n'
)


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        # Deliberately not a stored dict: a real 204 carries an empty body, and
        # json() raising on it is the behaviour the 204 guard exists to skip.
        #
        # And it raises what requests raises. ``Response.json()`` wraps a decode
        # failure in ``requests.exceptions.JSONDecodeError``, which SUBCLASSES
        # the stdlib error but is not subclassed BY it, so a double raising the
        # bare stdlib error is not substitutable for a real response.
        #
        # Nothing here distinguishes them today, because the module still
        # catches a broad ``except Exception``. What the unfaithful double cost
        # was the NEXT change: narrowing that except to the requests type is the
        # natural tightening and is exactly right in production, where an HTML
        # proxy error page raises the requests type — but against a stdlib-only
        # double the error would sail straight past the narrowed handler and
        # turn this file red. A false alarm on a correct change, which is the
        # kind of noise that gets a real fix reverted.
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise requests.exceptions.JSONDecodeError(
                exc.msg, exc.doc, exc.pos
            ) from exc

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clean_caches():
    """No test may inherit another's index, per-UniProt cache, or backoff."""
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()
    epitope_db._RCSB_RETRY_AT = 0.0
    epitope_db._PDB_FILE_RETRY_AT = 0.0
    yield
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()
    epitope_db._RCSB_RETRY_AT = 0.0
    epitope_db._PDB_FILE_RETRY_AT = 0.0


@pytest.fixture
def served(monkeypatch):
    """Serve a chosen body from the summary URL, and count the fetches."""
    def _serve(body, status=200):
        calls = []

        def _fake_get(url, **kwargs):
            calls.append(url)
            return _FakeResponse(body, status)

        monkeypatch.setattr(epitope_db.requests, "get", _fake_get)
        return calls

    return _serve


# ---------------------------------------------------------------------------
# 1. A payload that is not the summary must be LOUD, not empty
# ---------------------------------------------------------------------------


class TestBadPayloadsAreRejected:
    """The exact rot that killed this feature, and its near neighbours.

    Each of these used to be indistinguishable from "this protein has no known
    antibodies". Parsing must raise so the caller logs and retries instead of
    caching a lie.
    """

    def test_the_spa_shell_is_rejected(self):
        """The retired endpoint's HTML reply must never parse as 'no binders'."""
        with pytest.raises(ValueError):
            epitope_db._parse_summary_csv(_SPA_SHELL)

    def test_an_empty_body_is_rejected(self):
        with pytest.raises(ValueError):
            epitope_db._parse_summary_csv("")

    def test_a_renamed_column_is_rejected(self):
        """If SAbDab renames a column, fail loudly rather than blank the field."""
        renamed = _SUMMARY_CSV.replace("antigen_chain", "antigen_chains", 1)
        with pytest.raises(ValueError) as exc:
            epitope_db._parse_summary_csv(renamed)
        assert "antigen_chain" in str(exc.value)

    def test_a_header_with_no_rows_is_rejected(self):
        """Right shape, no content — still not a usable database."""
        with pytest.raises(ValueError):
            epitope_db._parse_summary_csv(_SUMMARY_HEADER)

    def test_unrecognised_pdb_id_format_is_rejected(self):
        """A change to the id scheme must not yield an index of junk keys."""
        mangled = _SUMMARY_CSV.replace("pdb_0000", "XXXX_9999")
        with pytest.raises(ValueError):
            epitope_db._parse_summary_csv(mangled)


# ---------------------------------------------------------------------------
# 2. The parser produces what query_sabdab and the UI expect
# ---------------------------------------------------------------------------


class TestSummaryParsing:
    def test_extended_ids_are_indexed_by_classic_id(self):
        """RCSB and the coordinate download URL speak the classic 4-char id."""
        index = epitope_db._parse_summary_csv(_SUMMARY_CSV)
        assert set(index) == {"1A2Y", "5LZ0", "7K8M"}

    def test_fields_survive_the_trip(self):
        index = epitope_db._parse_summary_csv(_SUMMARY_CSV)
        row = index["7K8M"][0]
        assert row["Hchain"] == "A"
        assert row["Lchain"] == "B"
        assert row["antigen_chain"] == "E"
        assert row["resolution"] == "3.2"
        assert row["antigen_species"] == (
            "severe acute respiratory syndrome coronavirus 2"
        )

    def test_multi_chain_antigen_yields_one_chain(self):
        """_compute_contacts looks up ONE chain id in the structure.

        Handing it "C|D" finds no such chain and silently returns no contacts,
        which is how a working lookup would still show an empty interface.
        """
        index = epitope_db._parse_summary_csv(_SUMMARY_CSV)
        assert index["1A2Y"][0]["antigen_chain"] == "C"

    def test_na_placeholders_do_not_reach_the_ui(self):
        """SAbDab's null is the literal string "NA"."""
        row = epitope_db._parse_summary_csv(_SUMMARY_CSV)["5LZ0"][0]
        assert row["antigen_chain"] == ""
        assert row["antigen_species"] == ""

    def test_a_nanobody_is_classified_as_one(self):
        """End-to-end through the shape query_sabdab builds: no light chain."""
        row = epitope_db._parse_summary_csv(_SUMMARY_CSV)["5LZ0"][0]
        assert epitope_db._classify_binder(
            row["Hchain"] or None, row["Lchain"] or None
        ) == "VHH/Nanobody"

    def test_classic_id_helper_rejects_junk(self):
        assert epitope_db._classic_pdb_id("pdb_00007k8m") == "7K8M"
        assert epitope_db._classic_pdb_id("7k8m") == ""
        assert epitope_db._classic_pdb_id("") == ""
        assert epitope_db._classic_pdb_id("not_an_id_at_all") == ""


# ---------------------------------------------------------------------------
# 3. The endpoint itself
# ---------------------------------------------------------------------------


class TestEndpoint:
    def test_url_is_the_live_api_not_the_retired_webapp(self):
        """A revert to the SPA-shell path must fail here, not in production."""
        assert epitope_db.SABDAB_SUMMARY_URL.startswith(
            "https://sabdab.opig.stats.ox.ac.uk/api/"
        )
        assert "webapps/sabdab-sabpred" not in epitope_db.SABDAB_SUMMARY_URL

    def test_the_whole_database_is_fetched_once_not_per_lookup(self, served):
        calls = served(_SUMMARY_CSV)
        for _ in range(5):
            epitope_db._sabdab_summary_index()
        assert len(calls) == 1

    def test_a_valid_csv_under_an_error_status_is_not_an_index(self, served):
        """A 5xx carrying a perfectly good body is an outage, not a database.

        ``served`` has taken a ``status`` since it was written and not one of
        its call sites ever passed one, which left
        ``resp.raise_for_status()`` in ``_sabdab_summary_index`` completely
        unguarded: before this test existed, deleting that line kept the whole
        file green — verified by mutation at 34f9434, where the file was 52
        passed either way. (The count has moved a long way since -- #223 added
        a section and #224 added two test classes to this file -- so the number
        is quoted with the commit it was taken at rather than re-stated,
        because it is a historical measurement.) A cache or CDN
        answering a
        stale-but-parseable summary under a 503 would have been indexed as
        fact, and — because a served index is what ``fetch_known_binders``
        accepts as SAbDab having answered — used to write permanent misses.
        """
        served(_SUMMARY_CSV, status=503)
        assert epitope_db._sabdab_summary_index() == {}


# ---------------------------------------------------------------------------
# 4. No fan-out. This is the Phase 1 prerequisite, not a nicety.
# ---------------------------------------------------------------------------


class TestNoThreadFanOut:
    """``query_sabdab`` used to start one raw unbounded ``threading.Thread``
    per candidate PDB id, up to 40, on every anonymous ``/analyze``.

    Under a threaded worker class that multiplies by every concurrent request
    (measured worst case ~672 OS threads at 8 threads x 2 workers). The
    gunicorn worker class change is only safe while this stays at zero, so it
    is guarded rather than trusted.
    """

    def test_query_sabdab_starts_no_threads(self, monkeypatch, served):
        served(_SUMMARY_CSV)
        monkeypatch.setattr(
            epitope_db,
            "_rcsb_pdb_ids_for_uniprot",
            lambda *a, **k: ["1A2Y", "5LZ0", "7K8M"] * 14,  # 42 ids
        )

        started = []
        real_start = threading.Thread.start

        def _spy(self, *a, **k):
            started.append(self)
            return real_start(self, *a, **k)

        monkeypatch.setattr(threading.Thread, "start", _spy)

        before = threading.active_count()
        hits = epitope_db.query_sabdab("P00698")

        assert started == [], f"query_sabdab spawned {len(started)} thread(s)"
        assert threading.active_count() == before
        assert hits, "the lookup must still return binders without threads"

    def test_a_lookup_costs_one_request_regardless_of_candidate_count(
        self, monkeypatch, served
    ):
        calls = served(_SUMMARY_CSV)
        monkeypatch.setattr(
            epitope_db,
            "_rcsb_pdb_ids_for_uniprot",
            lambda *a, **k: ["1A2Y", "5LZ0", "7K8M"] * 14,
        )
        epitope_db.query_sabdab("P00698")
        epitope_db.query_sabdab("P00533")
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# 5. A miss caused by an outage must not become permanent
# ---------------------------------------------------------------------------


class TestFailureIsNotCachedForever:
    def test_a_failed_fetch_retries_soon_not_in_a_day(self, monkeypatch):
        def _boom(url, **kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(epitope_db.requests, "get", _boom)

        assert epitope_db._sabdab_summary_index() == {}
        # The short error TTL, not the 24-hour success TTL. Without this the
        # feature would stay dark for a day after a two-minute blip.
        remaining = epitope_db._SUMMARY_EXPIRES_AT - time.monotonic()
        assert remaining <= epitope_db._SUMMARY_ERROR_TTL_SEC
        assert epitope_db._SUMMARY_TTL_SEC > epitope_db._SUMMARY_ERROR_TTL_SEC

    def test_a_dead_upstream_is_not_refetched_on_every_lookup(self, monkeypatch):
        """The backoff has to actually short-circuit, not just set a clock.

        Checking ``_SUMMARY_EXPIRES_AT`` alone passes against a version that
        re-fetches every single time, because the timestamp is still written
        — the guard above did exactly that and was green while a cold worker
        made one 60-second-timeout request PER ANALYSIS. Counting the fetches
        is the assertion that has teeth.
        """
        calls = []

        def _boom(url, **kwargs):
            calls.append(url)
            raise RuntimeError("upstream down")

        monkeypatch.setattr(epitope_db.requests, "get", _boom)

        for _ in range(5):
            assert epitope_db._sabdab_summary_index() == {}

        assert len(calls) == 1, (
            f"a dead upstream cost {len(calls)} requests across 5 lookups; "
            f"the error TTL is not short-circuiting on a worker that has "
            f"never had a successful fetch"
        )

    def test_a_failed_refresh_keeps_serving_the_last_good_index(
        self, monkeypatch, served
    ):
        served(_SUMMARY_CSV)
        good = epitope_db._sabdab_summary_index()
        assert "7K8M" in good

        epitope_db._SUMMARY_EXPIRES_AT = 0.0  # force a refresh

        def _boom(url, **kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(epitope_db.requests, "get", _boom)
        assert "7K8M" in epitope_db._sabdab_summary_index()

    def test_no_binders_is_not_cached_while_upstream_is_down(self, monkeypatch):
        """``_CACHE`` has no expiry, so a miss written during an outage would
        outlive the outage for the whole life of the worker."""
        def _boom(url, **kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(epitope_db.requests, "get", _boom)
        monkeypatch.setattr(
            epitope_db, "_rcsb_pdb_ids_for_uniprot", lambda *a, **k: ["7K8M"]
        )

        assert epitope_db.fetch_known_binders("P0DTC2") == []
        assert "P0DTC2" not in epitope_db._CACHE

    def test_a_genuine_miss_is_cached(self, monkeypatch, served):
        """The counterpart: when the database IS readable, a miss is a fact."""
        served(_SUMMARY_CSV)
        monkeypatch.setattr(
            epitope_db, "_rcsb_pdb_ids_for_uniprot", lambda *a, **k: ["9ZZZ"]
        )
        assert epitope_db.fetch_known_binders("P99999") == []
        assert epitope_db._CACHE["P99999"] == []


class TestRcsbFailureIsNotCachedForever:
    """The same guard, for the OTHER database the lookup rides on.

    The class above covers SAbDab and was the whole of the guard. But
    ``query_sabdab`` needs two upstreams: RCSB names the candidate structures,
    SAbDab says which of them are antibody complexes. An RCSB outage produced
    exactly the empty list a target with no antibodies produces, while the
    SAbDab summary stayed perfectly readable — so the check passed, the miss
    went into an unexpiring ``_CACHE``, and that accession reported "no known
    binders" for the life of the gunicorn worker, long after RCSB recovered.

    RCSB is reached with ``requests.post``; the summary with ``requests.get``.
    These tests keep SAbDab healthy on purpose, so a pass can only come from
    the RCSB arm of the guard.
    """

    @staticmethod
    def _rcsb(monkeypatch, *, status=200, body="", exc=None):
        """Answer the RCSB search POST with a status/body, or raise ``exc``.

        Both failure modes are needed because the source treats them in
        separate branches — a 5xx returns early, a raise lands in the handler —
        and a guard added to only one of them looks identical from the outside.
        """
        calls = []

        def _fake_post(url, **kwargs):
            calls.append(url)
            if exc is not None:
                raise exc
            return _FakeResponse(body, status)

        monkeypatch.setattr(epitope_db.requests, "post", _fake_post)
        return calls

    def test_an_rcsb_outage_does_not_poison_the_cache(self, monkeypatch, served):
        served(_SUMMARY_CSV)  # SAbDab is fine. Only RCSB is down.
        self._rcsb(monkeypatch, status=503, body="service unavailable")

        assert epitope_db.fetch_known_binders("P00533") == []
        assert "P00533" not in epitope_db._CACHE, (
            "an RCSB 503 wrote a permanent 'no known binders' for P00533; "
            "_CACHE has no expiry, so that miss outlives the outage for the "
            "life of the worker"
        )

    def test_a_network_error_is_an_outage_not_a_zero(self, monkeypatch, served):
        """A raised exception and a 5xx are the same fact and must agree."""
        served(_SUMMARY_CSV)

        def _boom(url, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(epitope_db.requests, "post", _boom)

        assert epitope_db.fetch_known_binders("P00533") == []
        assert "P00533" not in epitope_db._CACHE

    def test_the_lookup_is_right_once_rcsb_recovers(self, monkeypatch, served):
        """Not caching is the means; answering correctly afterwards is the end.

        Asserting only on ``_CACHE`` would pass against a version that never
        re-queries, so this one walks the accession through the outage and out
        the other side and demands the binder.
        """
        served(_SUMMARY_CSV)
        self._rcsb(monkeypatch, status=503, body="")
        assert epitope_db.fetch_known_binders("P0DTC2") == []

        # The outage set the error-TTL backoff; recovery is only observable
        # once it lapses. Same move as the summary tests make with
        # ``_SUMMARY_EXPIRES_AT``, and it keeps the test off the clock.
        epitope_db._RCSB_RETRY_AT = 0.0

        monkeypatch.setattr(
            epitope_db, "_fetch_and_compute_contacts", lambda *a, **k: []
        )
        calls = self._rcsb(
            monkeypatch, body='{"result_set": [{"identifier": "7K8M"}]}'
        )
        binders = epitope_db.fetch_known_binders("P0DTC2")

        assert calls, "RCSB was never re-queried; the outage had been cached"
        assert [b["pdb_id"] for b in binders] == ["7K8M"]

    @pytest.mark.parametrize(
        "status,body",
        [
            (204, ""),                    # what the live API answers today
            (200, '{"result_set": []}'),  # the other shape a zero could take
        ],
        ids=["204-empty-body", "200-empty-result-set"],
    )
    def test_a_genuine_rcsb_zero_is_still_cached(
        self, monkeypatch, served, status, body
    ):
        """The counterpart, and the reason the sentinel is not just "always
        refetch": a zero from RCSB is an answer, not a failure. It is a fact,
        and it has to stay cheap — the first analysis of a target that resolves
        to an accession with no PDB entries pays this lookup, and the cache is
        what stops every later one paying it again.

        Parametrised because only the 204 shape is what the API sends today;
        pinning the 200-with-empty-result-set shape as well means a change in
        how RCSB represents "nothing matched" cannot silently turn a zero into
        a permanent outage.
        """
        served(_SUMMARY_CSV)
        calls = self._rcsb(monkeypatch, status=status, body=body)

        assert epitope_db.fetch_known_binders("P99999") == []
        assert epitope_db._CACHE["P99999"] == []

        epitope_db.fetch_known_binders("P99999")
        assert len(calls) == 1, "a cached genuine zero went back to RCSB"

    @pytest.mark.parametrize(
        "failure",
        [
            {"status": 503, "body": ""},
            {"exc": RuntimeError("connection reset")},
        ],
        ids=["http-503", "network-error"],
    )
    def test_a_dead_rcsb_is_not_probed_on_every_analysis(
        self, monkeypatch, served, failure
    ):
        """Refusing to cache the outage is correct; flooding the outage is not.

        Nothing is written to ``_CACHE`` during an RCSB outage, so without a
        backoff every analysis re-probes — and this probe is a 12-second
        request made inside a 2-slot semaphore (ANON_MAX_CONCURRENT_RUNS). A
        hung RCSB would hold both slots and hand every other visitor a 503
        BUSY, with no cache write left to damp it. This is the same bound
        ``_SUMMARY_ERROR_TTL_SEC`` already puts on a dead SAbDab, and counting
        the probes is the assertion with teeth: checking the timestamp alone
        passes against a version that re-probes every time and merely rewrites
        the clock.
        """
        served(_SUMMARY_CSV)
        calls = self._rcsb(monkeypatch, **failure)

        for _ in range(5):
            assert epitope_db.fetch_known_binders("P00533") == []

        assert len(calls) == 1, (
            f"a dead RCSB cost {len(calls)} probes across 5 analyses; the "
            f"error TTL is not short-circuiting"
        )
        # Still not cached — the backoff bounds the cost without reintroducing
        # the permanent zero. Both properties or neither.
        assert "P00533" not in epitope_db._CACHE

        remaining = epitope_db._RCSB_RETRY_AT - time.monotonic()
        assert 0 < remaining <= epitope_db._RCSB_ERROR_TTL_SEC

    def test_a_2xx_without_result_set_is_an_outage_not_a_zero(
        self, monkeypatch, served
    ):
        """A 2xx whose body is not the documented shape is unreadable.

        ``data.get("result_set", [])`` turned it into an empty candidate list,
        which the guard then wrote to the unexpiring cache as a fact — the
        exact silent-permanent-zero this change exists to remove, on the one
        input the sentinel did not cover. Reading ``data["result_set"]``
        instead lets the KeyError reach the handler that classifies outages.
        """
        served(_SUMMARY_CSV)
        self._rcsb(monkeypatch, status=200, body='{"error": "backend down"}')

        assert epitope_db.fetch_known_binders("P00533") == []
        assert "P00533" not in epitope_db._CACHE


class TestContactFailureIsNotCachedForever:
    """The same guard again, one layer down: the per-BINDER interface.

    The two classes above stop an outage pinning "this target has no known
    binders". This one stops an outage pinning "this known binder contacts
    nothing", which is the same silent permanent zero wearing a smaller hat and
    was deliberately left out of that change because it needed its own shape.

    Discarding the whole binder list because one coordinate download flaked
    would be wrong -- the list is the expensive part (an RCSB search plus the
    SAbDab index) and it was fine. So the interface is cached SEPARATELY from
    the list: absent until established, retried on the next lookup, and the
    list itself is never re-fetched. ``[]`` in ``contact_residues`` therefore
    means "settled -- do not ask again": a genuinely empty interface, or one
    of the two permanent absences (no PDB-format file, no chains to compute
    against). It never means "the host was down".

    RCSB's coordinate host is reached with ``requests.get``, the same verb as
    the SAbDab summary, so these tests dispatch on URL and keep both databases
    healthy on purpose: a pass can only come from the contact arm.
    """

    @staticmethod
    def _hosts(monkeypatch, *, status=200, exc=None, contacts=(18, 19, 20)):
        """Serve the summary, and answer files.rcsb.org however asked.

        Returns the list of coordinate URLs requested, so a test can count
        downloads rather than trust a timestamp.
        """
        downloads = []

        def _fake_get(url, **kwargs):
            if url.startswith(epitope_db.SABDAB_SUMMARY_URL):
                return _FakeResponse(_SUMMARY_CSV, 200)
            downloads.append(url)
            if exc is not None:
                raise exc
            return _FakeResponse("ATOM  (stand-in)", status)

        monkeypatch.setattr(epitope_db.requests, "get", _fake_get)
        # The parse is covered by tests/test_scout_mse_contacts.py against real
        # coordinates. What matters here is only that a readable body yields an
        # answer and an unreadable one never reaches this function at all.
        monkeypatch.setattr(
            epitope_db, "_compute_contacts", lambda *a, **k: list(contacts)
        )
        return downloads

    @staticmethod
    def _binders(monkeypatch, ids=("7K8M",)):
        monkeypatch.setattr(
            epitope_db, "_rcsb_pdb_ids_for_uniprot", lambda *a, **k: list(ids)
        )

    def test_a_503_is_not_written_down_as_an_empty_interface(self, monkeypatch):
        self._binders(monkeypatch)
        self._hosts(monkeypatch, status=503)

        binders = epitope_db.fetch_known_binders("P0DTC2")

        assert [b["pdb_id"] for b in binders] == ["7K8M"], (
            "the binder LIST must survive a coordinate download failing; "
            "only the interface is unknown"
        )
        assert "contact_residues" not in binders[0], (
            "a 503 from files.rcsb.org was recorded as 'this antibody contacts "
            "nothing'. _CACHE has no expiry, so that zero outlives the outage "
            "for the life of the worker, and the UI shows a known antibody "
            "touching nothing."
        )

    def test_the_interface_is_right_once_the_download_recovers(self, monkeypatch):
        """Not caching the failure is the means; healing is the end.

        Asserting only on the absent key passes against a version that never
        retries, so this one walks a binder through the outage and out.
        """
        self._binders(monkeypatch)
        self._hosts(monkeypatch, status=503)
        assert "contact_residues" not in epitope_db.fetch_known_binders("P0DTC2")[0]

        # The failed round set the backoff; recovery is only observable once it
        # lapses. Same move the RCSB tests make with _RCSB_RETRY_AT.
        epitope_db._PDB_FILE_RETRY_AT = 0.0
        downloads = self._hosts(monkeypatch, status=200)

        binders = epitope_db.fetch_known_binders("P0DTC2")
        assert downloads, "the coordinates were never re-downloaded"
        assert binders[0]["contact_residues"] == [18, 19, 20]

    def test_the_binder_list_is_not_re_fetched_to_repair_an_interface(
        self, monkeypatch
    ):
        """The expensive half must not be thrown away with the cheap half.

        A retry that also re-ran the RCSB search would put a 12-second request
        back inside anon_compute_slot on every analysis of an outage-affected
        target -- trading a permanent zero for a permanent cost.
        """
        searches = []

        def _search(uniprot_id, *a, **k):
            searches.append(uniprot_id)
            return ["7K8M"]

        monkeypatch.setattr(epitope_db, "_rcsb_pdb_ids_for_uniprot", _search)
        self._hosts(monkeypatch, status=503)
        epitope_db.fetch_known_binders("P0DTC2")

        epitope_db._PDB_FILE_RETRY_AT = 0.0
        self._hosts(monkeypatch, status=200)
        binders = epitope_db.fetch_known_binders("P0DTC2")

        assert binders[0]["contact_residues"] == [18, 19, 20]
        assert len(searches) == 1, (
            f"repairing one interface cost {len(searches)} RCSB searches; the "
            f"cached binder list was discarded along with the failed download"
        )

    def test_a_computed_empty_interface_is_cached(self, monkeypatch):
        """The counterpart, and the reason this is a sentinel and not "always
        refetch": an interface that really is empty is a fact, and
        re-downloading a multi-megabyte structure to re-derive it on every
        analysis is exactly the cost the cache exists to remove.
        """
        self._binders(monkeypatch)
        downloads = self._hosts(monkeypatch, status=200, contacts=())

        assert epitope_db.fetch_known_binders("P0DTC2")[0]["contact_residues"] == []
        epitope_db.fetch_known_binders("P0DTC2")
        assert len(downloads) == 1, "a computed empty interface went back to RCSB"

    def test_a_404_is_an_answer_not_an_outage(self, monkeypatch):
        """Structures too large for the legacy format are mmCIF-only and 404
        forever. Classifying that as an outage would re-download the same
        absence every TTL and, because the backoff is shared, stall every other
        target's interfaces behind one oversized entry.
        """
        self._binders(monkeypatch)
        downloads = self._hosts(monkeypatch, status=404)

        assert epitope_db.fetch_known_binders("P0DTC2")[0]["contact_residues"] == []
        epitope_db.fetch_known_binders("P0DTC2")
        assert len(downloads) == 1, "a 404 was retried as though it were an outage"

        # The timestamp alone proves nothing here -- the fixture initialises
        # it to 0.0, so asserting that is asserting the fixture. Whether a
        # SECOND accession can still download is the property that matters.
        # The host is healthy for it: if the 404 had armed the backoff,
        # _fetch_and_compute_contacts short-circuits to None before ever
        # asking, so a healthy host is what makes an armed backoff visible.
        self._binders(monkeypatch, ids=["1A2Y"])
        self._hosts(monkeypatch, status=200)
        assert epitope_db.fetch_known_binders("P00698")[0][
            "contact_residues"
        ] == [18, 19, 20], (
            "one entry with no PDB-format file tripped the SHARED backoff and "
            "darkened contact downloads for every other target"
        )

    @pytest.mark.parametrize(
        "failure",
        [
            {"status": 503},
            {"exc": RuntimeError("connection reset")},
        ],
        ids=["http-503", "network-error"],
    )
    def test_a_dead_coordinate_host_is_not_re_downloaded_every_analysis(
        self, monkeypatch, failure
    ):
        """Refusing to cache the outage is correct; flooding it is not.

        Nothing is written for a failed interface, so without a backoff every
        analysis re-downloads up to _MAX_CONTACT_STRUCTURES structures of
        0.5-5 MB, inside a 2-slot semaphore. Counting the downloads is the
        assertion with teeth: checking the timestamp alone passes against a
        version that re-downloads every time and merely rewrites the clock.
        """
        self._binders(monkeypatch)
        downloads = self._hosts(monkeypatch, **failure)

        for _ in range(5):
            epitope_db.fetch_known_binders("P0DTC2")

        assert len(downloads) == 1, (
            f"a dead files.rcsb.org cost {len(downloads)} downloads across 5 "
            f"analyses; the error TTL is not short-circuiting"
        )
        remaining = epitope_db._PDB_FILE_RETRY_AT - time.monotonic()
        assert 0 < remaining <= epitope_db._RCSB_ERROR_TTL_SEC

    def test_an_unreadable_body_never_arms_the_shared_backoff(self, monkeypatch):
        """The failure mode a shared timestamp has to survive.

        An earlier version armed the backoff whenever a ROUND resolved nothing.
        That reads as "the host is down" only on the first round: ``pending`` is
        recomputed each call as the entries still missing the key, so from round
        two on a round contains ONLY the entries that keep failing, and an
        all-failed round is guaranteed. One structure whose body will not parse
        therefore re-armed a process-wide 5-minute blackout on every lookup, and
        any target analysed inside that window came back with no interfaces at
        all -- the bug this file exists to close, re-created across targets
        instead of pinned to one.

        Running THREE rounds is the whole point: round one passed under the old
        code too, so a single-round version of this guard is worthless.
        """
        self._binders(monkeypatch, ids=["1A2Y", "7K8M"])
        # 200 with a body that cannot be read: the host is fine, the entry is
        # not. 7K8M's antibody chains are A,B and 1A2Y's are B,A, so the stub
        # can fail exactly one of them.
        monkeypatch.setattr(
            epitope_db.requests,
            "get",
            lambda url, **k: _FakeResponse(
                _SUMMARY_CSV
                if url.startswith(epitope_db.SABDAB_SUMMARY_URL)
                else "ATOM  (stand-in)",
                200,
            ),
        )
        monkeypatch.setattr(
            epitope_db,
            "_compute_contacts",
            lambda text, chain, ab, **k: None if ab == ["A", "B"] else [7],
        )

        for round_no in (1, 2, 3):
            binders = {
                b["pdb_id"]: b for b in epitope_db.fetch_known_binders("P0DTC2")
            }
            assert binders["1A2Y"]["contact_residues"] == [7]
            assert "contact_residues" not in binders["7K8M"]
            assert epitope_db._PDB_FILE_RETRY_AT == 0.0, (
                f"round {round_no}: one unparseable structure armed the SHARED "
                f"backoff. Every other target analysed in the next "
                f"{epitope_db._RCSB_ERROR_TTL_SEC}s gets no interfaces at all."
            )

    def test_an_unreadable_body_does_not_blank_another_target(self, monkeypatch):
        """The consequence spelled out, on a second accession.

        The guard above is about a timestamp; this one is about what a different
        user analysing a different protein actually gets back.
        """
        self._binders(monkeypatch, ids=["7K8M"])
        monkeypatch.setattr(
            epitope_db.requests,
            "get",
            lambda url, **k: _FakeResponse(
                _SUMMARY_CSV
                if url.startswith(epitope_db.SABDAB_SUMMARY_URL)
                else "ATOM  (stand-in)",
                200,
            ),
        )
        monkeypatch.setattr(epitope_db, "_compute_contacts", lambda *a, **k: None)
        for _ in range(3):
            epitope_db.fetch_known_binders("P0DTC2")

        # A different protein, whose own structure reads perfectly.
        self._binders(monkeypatch, ids=["1A2Y"])
        monkeypatch.setattr(epitope_db, "_compute_contacts", lambda *a, **k: [7])
        assert epitope_db.fetch_known_binders("P00698")[0]["contact_residues"] == [7]

    def test_a_transport_failure_does_arm_the_backoff(self, monkeypatch):
        """The other half, and the reason the backoff exists at all.

        A non-404 status or a raise from requests is not entry-specific -- it
        says the host is unhappy -- so it is the one thing allowed to arm the
        shared timestamp. Without this, the two guards above would pass against
        a version that has no backoff whatsoever and floods a dead host.
        """
        self._binders(monkeypatch)
        self._hosts(monkeypatch, status=503)
        epitope_db.fetch_known_binders("P0DTC2")

        remaining = epitope_db._PDB_FILE_RETRY_AT - time.monotonic()
        assert 0 < remaining <= epitope_db._RCSB_ERROR_TTL_SEC

    def test_an_interface_already_established_is_never_re_downloaded(
        self, monkeypatch
    ):
        """A repair round must touch only what is still missing."""
        self._binders(monkeypatch, ids=["1A2Y", "7K8M"])
        monkeypatch.setattr(
            epitope_db.requests,
            "get",
            lambda url, **k: _FakeResponse(
                _SUMMARY_CSV
                if url.startswith(epitope_db.SABDAB_SUMMARY_URL)
                else "ATOM  (stand-in)",
                503 if "7K8M" in url else 200,
            ),
        )
        monkeypatch.setattr(epitope_db, "_compute_contacts", lambda *a, **k: [7])
        binders = {b["pdb_id"]: b for b in epitope_db.fetch_known_binders("P0DTC2")}
        assert binders["1A2Y"]["contact_residues"] == [7]
        assert "contact_residues" not in binders["7K8M"]

        epitope_db._PDB_FILE_RETRY_AT = 0.0
        downloads = self._hosts(monkeypatch, status=200, contacts=[9])
        binders = {b["pdb_id"]: b for b in epitope_db.fetch_known_binders("P0DTC2")}
        assert binders["7K8M"]["contact_residues"] == [9]
        assert [u for u in downloads if "1A2Y" in u] == [], (
            "an interface that was already established was re-downloaded"
        )


class TestALateThreadCannotMutateAReturnedResult:
    """``join(timeout=...)`` means a contact thread can outlive the call.

    The old worker wrote its result straight into a dict that was, by then,
    already inside ``_CACHE`` and already returned to a caller -- and
    ``fetch_known_binders`` handed back the cache's own list object, so the
    late write landed in a structure the route was json.dump-ing at
    scout/routes.py. Two properties fix it and both are guarded here: a thread
    writes only into a local slot, and callers get copies.
    """

    @staticmethod
    def _healthy_summary(monkeypatch):
        monkeypatch.setattr(
            epitope_db, "_rcsb_pdb_ids_for_uniprot", lambda *a, **k: ["7K8M"]
        )
        monkeypatch.setattr(
            epitope_db.requests,
            "get",
            lambda url, **k: _FakeResponse(_SUMMARY_CSV, 200),
        )

    def test_a_thread_that_outruns_the_join_writes_nothing_shared(
        self, monkeypatch
    ):
        monkeypatch.setattr(epitope_db, "_CONTACT_JOIN_TIMEOUT_SEC", 0.05)
        self._healthy_summary(monkeypatch)

        release = threading.Event()

        def _slow(*a, **k):
            release.wait(10)
            return [1, 2, 3]

        monkeypatch.setattr(epitope_db, "_fetch_and_compute_contacts", _slow)

        binders = epitope_db.fetch_known_binders("P0DTC2")
        cached = epitope_db._CACHE["P0DTC2"]
        assert "contact_residues" not in binders[0]

        release.set()
        # Give the straggler every chance to land somewhere it should not.
        for _ in range(200):
            if any("contact_residues" in b for b in (*binders, *cached)):
                break
            time.sleep(0.01)

        assert "contact_residues" not in binders[0], (
            "a thread that outran the join wrote into the dict the caller "
            "already had; the route json.dump()s that object"
        )
        assert "contact_residues" not in cached[0], (
            "a thread that outran the join wrote into a dict already in _CACHE"
        )

    def test_the_caller_does_not_get_the_cache_s_own_objects(self, monkeypatch):
        self._healthy_summary(monkeypatch)
        monkeypatch.setattr(
            epitope_db, "_fetch_and_compute_contacts", lambda *a, **k: [1, 2, 3]
        )

        first = epitope_db.fetch_known_binders("P0DTC2")
        assert first is not epitope_db._CACHE["P0DTC2"]
        assert first[0] is not epitope_db._CACHE["P0DTC2"][0]

        first[0]["pdb_id"] = "TAMPERED"
        # The LISTS, not just a scalar key. A shallow dict() copy passes the
        # rebind above while still handing out the cache's own list objects,
        # so asserting only the rebind certifies more than it checks --
        # append is the mutation anyone would actually reach for.
        first[0]["contact_residues"].append(999)
        first[0]["ab_chains"].append("ZZ")

        again = epitope_db.fetch_known_binders("P0DTC2")[0]
        assert again["pdb_id"] == "7K8M", (
            "a caller rebinding a key on its own result rewrote the cache"
        )
        assert again["contact_residues"] == [1, 2, 3], (
            "a caller appending to its own contact_residues rewrote the "
            "cache: the copy is shallow and shares the list object"
        )
        assert again["ab_chains"] == ["A", "B"], (
            "a caller appending to its own ab_chains rewrote the cache"
        )


# ---------------------------------------------------------------------------
# 6. The known-positive check QC asked for
# ---------------------------------------------------------------------------


class TestKnownPositive:
    def test_a_known_antibody_target_returns_binders(self, monkeypatch, served):
        """Hermetic. Proves the whole chain assembles a binder from real bytes.

        The dead version returned [] here for every input, so this assertion
        is exactly the one the suite was missing.
        """
        served(_SUMMARY_CSV)
        monkeypatch.setattr(
            epitope_db,
            "_rcsb_pdb_ids_for_uniprot",
            lambda *a, **k: ["1A2Y", "7K8M", "9ZZZ"],
        )
        binders = epitope_db.query_sabdab("P00698")

        assert len(binders) == 2
        best = binders[0]  # sorted by resolution, best first
        assert best["pdb_id"] == "1A2Y"
        assert best["resolution"] == 1.5
        assert best["binder_type"] == "IgG/Fab"
        assert best["ab_chains"] == ["B", "A"]

    @pytest.mark.skipif(
        os.environ.get("SCOUT_SABDAB_LIVE") != "1",
        reason="set SCOUT_SABDAB_LIVE=1 to check the real SAbDab endpoint",
    )
    def test_live_endpoint_still_answers_with_the_expected_shape(self):
        """The only check that would catch the URL going stale AGAIN.

        Opt-in because the suite is otherwise hermetic, and because a network
        flake must not turn the whole build red. Run it when touching this
        module, and from a cron if this feature ever becomes load-bearing.
        """
        index = epitope_db._sabdab_summary_index()
        assert len(index) > 5_000, f"only {len(index)} entries — endpoint moved?"
        assert "7K8M" in index, "known antibody-antigen complex missing"


# ---------------------------------------------------------------------------
# 8. The accession is caller-controlled input, and the cache is not free
# ---------------------------------------------------------------------------


class TestAccessionIsValidated:
    """``_extract_uniprot_from_dbref`` reads a caller-uploaded accession
    field: on a plain DBREF the token from column 33 to the next space, and
    on a DBREF2 twenty-two characters from columns 18-40.

    With no format check those bytes became a lookup target and a
    permanent cache key: ``ZZ9QC001`` was accepted, resolved, and cached. The
    per-target cache is the main thing making the known-binder lookup
    affordable, and one uploaded line per request defeated it.
    """

    @pytest.mark.parametrize(
        "good",
        ["P00533", "P0DTC2", "P00698", "Q9Y6K9", "A0A123B4C5", "p00533"],
    )
    def test_real_accessions_survive(self, good):
        assert epitope_db._valid_accession(good) == good.upper()

    @pytest.mark.parametrize(
        "bogus",
        [
            "ZZ9QC001",   # the one QC executed
            "",
            "   ",
            "XXXXXXXX",
            "P0053",      # too short
            "P005333",    # 7 chars: neither the 6- nor the 10-char form
            "1P0533",     # must not start with a digit
            "P00533-2",   # isoform suffix is not an accession
            "../../etc",
            "P00533 OR 1=1",
        ],
    )
    def test_junk_is_refused(self, bogus):
        assert epitope_db._valid_accession(bogus) == ""

    def test_a_bogus_dbref_line_does_not_reach_the_lookup(self, tmp_path):
        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "DBREF  9XYZ A    1   129  UNP    ZZ9QC001 FAKE_ENTRY"
            "           1     129\n"
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000"
            "  1.00 20.00           C\nEND\n",
            encoding="utf-8",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_real_dbref_line_still_resolves(self, tmp_path):
        """The check must not cost the feature it protects: 1HEW's own DBREF."""
        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "DBREF  1HEW A    1   129  UNP    P00698   LYC_CHICK"
            "        19     147\n"
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000"
            "  1.00 20.00           C\nEND\n",
            encoding="utf-8",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "P00698"

    def test_fetch_refuses_a_bogus_accession_without_minting_a_cache_key(
        self, monkeypatch
    ):
        """Belt and braces: the check is also at the function that mints the
        key, because more than one resolver reaches it."""
        def _explode(*a, **k):
            raise AssertionError("query_sabdab was called with junk")

        monkeypatch.setattr(epitope_db, "query_sabdab", _explode)
        before = len(epitope_db._CACHE)
        assert epitope_db.fetch_known_binders("ZZ9QC001") == []
        assert len(epitope_db._CACHE) == before


class TestTheCacheIsBounded:
    """It has no TTL and lives for the whole worker, so it needs a ceiling.

    Validation stops arbitrary keys, but the space of REAL accessions with
    SAbDab entries is still thousands, and each is reachable by uploading a
    structure carrying the matching DBREF line.
    """

    def test_the_cap_holds_under_more_entries_than_the_cap(self, monkeypatch):
        monkeypatch.setattr(epitope_db, "_CACHE_MAX_ENTRIES", 8)
        for i in range(40):
            epitope_db._cache_put(f"key-{i}", [])
        assert len(epitope_db._CACHE) == 8

    def test_eviction_is_oldest_first(self, monkeypatch):
        monkeypatch.setattr(epitope_db, "_CACHE_MAX_ENTRIES", 3)
        for name in ("a", "b", "c", "d"):
            epitope_db._cache_put(name, [])
        assert set(epitope_db._CACHE) == {"b", "c", "d"}

    def test_the_shipped_cap_is_a_real_number(self):
        assert 0 < epitope_db._CACHE_MAX_ENTRIES <= 100_000

    def test_a_real_lookup_still_goes_through_the_bounded_put(self, monkeypatch):
        monkeypatch.setattr(
            epitope_db, "query_sabdab", lambda _key: [
                {"pdb_id": "1ABC", "antigen_chain": "A", "ab_chains": ["H", "L"]}
            ],
        )
        monkeypatch.setattr(
            epitope_db, "_fetch_and_compute_contacts", lambda *a, **k: [1, 2, 3]
        )
        monkeypatch.setattr(epitope_db, "_CACHE_MAX_ENTRIES", 2)
        for accession in ("P00533", "P00698", "P0DTC2"):
            assert epitope_db.fetch_known_binders(accession)
        assert len(epitope_db._CACHE) == 2


# ---------------------------------------------------------------------------
# 9. The accession does not always fit on the DBREF line
# ---------------------------------------------------------------------------


class TestTheTwoLineDbrefForm:
    """The wwPDB splits DBREF into DBREF1/DBREF2 whenever the accession is
    wider than the 8-character field on a plain DBREF line.

    That is the case for every 10-character accession -- the ``A0A...`` range,
    now a large fraction of TrEMBL. Matching only ``line.startswith("DBREF ")``
    skipped both halves, so step 1 returned "" and the chain fell through to
    the step-2 sequence search, which resolves almost nothing for experimental
    structures. The user got no protein name at all from a file that states
    the accession plainly. A QC sample of 409 real PDB-format depositions
    found 44 of them -- roughly 10.8% -- carrying the two-line record at
    all; 36 of those (8.8%) carry no plain DBREF, so the two-line form is
    their only source.

    Records below are copied byte-for-byte from real depositions (1HEW,
    5YTL, 6EBC, 21JI), so the column positions are the wwPDB's rather than
    this test's idea of them.
    """

    # Real 5YTL / 6EBC records. DBREF1 carries the entry NAME and the database;
    # DBREF2 carries the accession.
    DBREF1_A = "DBREF1 5YTL A    2   323  UNP                  A0A1W6VP04_GEOTD"
    DBREF2_A = "DBREF2 5YTL A     A0A1W6VP04                         31         352"
    DBREF1_B = "DBREF1 6EBC B    1   141  UNP                  A0A202B6V5_CHRVL"
    DBREF2_B = "DBREF2 6EBC B     A0A202B6V5                          1         141"
    # Real 1HEW record: the single-line form must keep working.
    DBREF_1HEW = (
        "DBREF  1HEW A    1   129  UNP    P00698   LYC_CHICK       19    147"
    )
    # Real 21JI chain A: a chain carrying BOTH forms. The two-line pair is a
    # 90-residue rat expression tag; the plain record is the 777-residue
    # protein the user actually uploaded.
    DBREF1_21JI = "DBREF1 21JI A  -66    23  UNP                  A0AA49QB00_RATRT"
    DBREF2_21JI = "DBREF2 21JI A     A0AA49QB00                          1          90"
    DBREF_21JI = "DBREF  21JI A   25   777  UNP    R8BKC2   R8BKC2_PHAM7    25    777"
    ATOM = (
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000"
        "  1.00 20.00           C"
    )

    def _write(self, tmp_path, *lines):
        pdb = tmp_path / "input.pdb"
        pdb.write_text("\n".join([*lines, self.ATOM, "END", ""]), encoding="utf-8")
        return pdb

    def test_the_accession_is_read_off_the_dbref2_line(self, tmp_path):
        """The bug: this returned "" even though the file names the accession."""
        pdb = self._write(tmp_path, self.DBREF1_A, self.DBREF2_A)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "A0A1W6VP04"

    def test_the_single_line_form_still_resolves(self, tmp_path):
        """The two-line branch must not cost the path that already worked."""
        pdb = self._write(tmp_path, self.DBREF_1HEW)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "P00698"

    def test_both_forms_in_one_file_each_resolve_to_their_own_chain(self, tmp_path):
        """Motivated by the 8 sampled files carrying both forms; the fixture
        itself pairs two real records that never co-occur in one deposition."""
        pdb = self._write(tmp_path, self.DBREF_1HEW, self.DBREF1_B, self.DBREF2_B)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "P00698"
        assert epitope_db._extract_uniprot_from_dbref(pdb, "B") == "A0A202B6V5"

    def test_a_pair_for_another_chain_is_not_borrowed(self, tmp_path):
        pdb = self._write(tmp_path, self.DBREF1_B, self.DBREF2_B)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_dbref2_naming_a_different_chain_is_not_borrowed(self, tmp_path):
        """A DBREF1 arming chain A followed by a DBREF2 naming chain B is a
        malformed pair -- but the file is caller-uploaded, so it is reachable.

        The chain column has to be re-checked on the DBREF2 line itself:
        arming alone does not cover this, and without the second check chain A
        silently inherits chain B's accession. That is the case this fixture
        exists to reach; the test above is satisfied by arming alone.
        """
        pdb = self._write(tmp_path, self.DBREF1_A, self.DBREF2_B)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_non_uniprot_pair_is_refused(self, tmp_path):
        """DBREF1 is the only half naming the database, so dropping its check
        would let this UniProt-shaped string through on a record that says GB."""
        pdb = self._write(
            tmp_path,
            "DBREF1 5YTL A    2   323  GB                   A0A1W6VP04_GEOTD",
            self.DBREF2_A,
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_malformed_accession_on_dbref2_is_still_validated(self, tmp_path):
        """``_valid_accession`` guards the two-line form too."""
        pdb = self._write(
            tmp_path,
            self.DBREF1_A,
            "DBREF2 5YTL A     ZZ9QC00123                        31         352",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_plain_dbref_beats_a_two_line_pair_on_the_same_chain(self, tmp_path):
        """Real 21JI chain A, in file order. Reading first-match-wins across
        both forms regressed this: the wwPDB writes the tag's pair first, so
        the 90-residue rat tag beat the 777-residue protein and the chain
        resolved to an accession that fails the downstream identity gate --
        losing a name the user previously got. The plain record wins.
        """
        pdb = self._write(
            tmp_path, self.DBREF1_21JI, self.DBREF2_21JI, self.DBREF_21JI
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "R8BKC2"

    def test_the_pair_still_wins_when_no_plain_dbref_names_the_chain(self, tmp_path):
        """Precedence must not cost the fix: a plain DBREF for a DIFFERENT
        chain does not suppress chain A's pair."""
        pdb = self._write(tmp_path, self.DBREF_21JI.replace("21JI A", "21JI B"),
                          self.DBREF1_A, self.DBREF2_A)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "A0A1W6VP04"

    def test_a_dbref1_for_another_chain_cannot_arm_this_one(self, tmp_path):
        """The mirror of the test above: DBREF1 names chain B, DBREF2 names
        chain A. Arming on the chain rather than a bool is what refuses it --
        with a bare flag, chain A silently borrows chain B's arming."""
        pdb = self._write(tmp_path, self.DBREF1_B, self.DBREF2_A)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_dbref2_consumes_its_dbref1(self, tmp_path):
        """A DBREF1 is spent by the next DBREF2 whether or not it matched.
        Without that, the unmatched chain-B DBREF2 leaves chain A armed and
        the following stray DBREF2 collects an accession it never earned."""
        pdb = self._write(
            tmp_path, self.DBREF1_A, self.DBREF2_B,
            "DBREF2 5YTL A     A0A1W6VP04                         31         352",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    @pytest.mark.parametrize(
        "truncated",
        [
            "DBREF1",
            "DBREF2",
            "DBREF ",
            "DBREF1 5YTL A",
            # 12 characters: one short of a readable chain column, and the
            # exact length the old code raised IndexError on. Widening the
            # guard to `< 12` puts that crash straight back.
            "DBREF  1HEW ",
        ],
    )
    def test_a_truncated_record_does_not_raise(self, tmp_path, truncated):
        """The file is caller-uploaded; a short record must not raise out of
        the lookup."""
        pdb = self._write(tmp_path, truncated)
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""
# 10. A plain DBREF whose accession overflows the field anyway
# ---------------------------------------------------------------------------


class TestAccessionFieldWidth:
    """The DBREF accession field holds 8 characters; accessions hold up to 10.

    The wwPDB's own answer is the DBREF1/DBREF2 pair that section 9 covers.
    AlphaFold DB does not use it: it writes the whole accession through the
    8-wide field of a PLAIN DBREF and lets the entry name shift right, so
    the two-line support above cannot reach it. Slicing columns 34-41
    therefore truncated ``A0A2K5QDT7`` to ``A0A2K5QD``, which is not an
    accession, so step 1 of ``resolve_uniprot_id`` failed silently and the
    chain fell through to the sequence-search fallback. The 10-character
    form was introduced when the 6-character space ran out, so it marks a
    late first-assignment date, not a review status. How the two relate is
    not measured here.
    """

    # The DBREF record of
    #   https://alphafold.ebi.ac.uk/files/AF-A0A2K5QDT7-F1-model_v6.pdb
    # byte for byte except its trailing pad to column 80. That .pdb records
    # no model version at all: the version lives in the URL, and its TITLE
    # "V2.0" is the pipeline. The companion .cif carries the entry id.
    AF_TREMBL_DBREF = (
        "DBREF  XXXX A    1   130  UNP    A0A2K5QDT7 A0A2K5QDT7_CEBIM"
        "     1    130\n"
    )

    def test_a_ten_character_accession_resolves(self, tmp_path):
        pdb = tmp_path / "input.pdb"
        pdb.write_text(self.AF_TREMBL_DBREF + "END\n", encoding="utf-8")
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "A0A2K5QDT7"

    def test_a_blank_accession_field_does_not_promote_the_next_column(self, tmp_path):
        """Reading to the next space must not walk past an empty field.

        Splitting on whitespace rather than on a single space walks to the
        next token and returns it as the accession. An entry name would
        not expose that — the format check rejects it for its underscore,
        so the assertion passes either way and the guard certifies false.
        The token here is a REAL accession, which the format check waves
        through, so only the parser can keep it out.
        """
        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "DBREF  1XYZ A    1   215  UNP             P00698       123    337"
            "\nEND\n",
            encoding="utf-8",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_a_truncated_dbref_line_does_not_raise(self, tmp_path):
        """A short DBREF line took ``line[12]`` out of range.

        The caller uploads the file, so the IndexError was reachable. It did
        not escape — the catch-all in ``scout/routes.py`` caught it and
        answered 500, losing the whole analysis over a line whose correct
        reading is just "no accession". ``scout/interfaces.py`` guards the
        same read for the same reason.
        """
        # Exactly 12 characters, so line[12] is the first index out of
        # range. An off-by-one in the guard (``>=`` for ``>``) still
        # raises on this line; a shorter fixture lets that mutation live.
        pdb = tmp_path / "input.pdb"
        pdb.write_text(
            "HEADER    short\nDBREF  1ABC \nEND\n",
            encoding="utf-8",
        )
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == ""

    def test_the_mmcif_branch_was_already_fine(self, tmp_path):
        """``_struct_ref`` reads named items, not columns, so it never truncated.

        Pinned rather than assumed: it is the branch the PDB one now matches.

        Two refs, not one. A single-entry fixture cannot tell a chain-matched
        answer from any file-level one, so deleting the strand-id loop would
        leave it green. #230 has since removed the "first UNP accession in the
        file" fallback that made that indistinguishable; two refs pin the
        chain scoping without depending on its absence. Chain B must return
        B's own accession.
        """
        pytest.importorskip("Bio.PDB.MMCIF2Dict")
        cif = tmp_path / "input.cif"
        cif.write_text(
            "data_AF-A0A2K5QDT7-F1\n"
            "loop_\n"
            "_struct_ref.id\n"
            "_struct_ref.db_name\n"
            "_struct_ref.pdbx_db_accession\n"
            "1 UNP A0A2K5QDT7\n"
            "2 UNP P00698\n"
            "loop_\n"
            "_struct_ref_seq.align_id\n"
            "_struct_ref_seq.ref_id\n"
            "_struct_ref_seq.pdbx_strand_id\n"
            "1 1 A\n"
            "2 2 B\n",
            encoding="utf-8",
        )
        assert epitope_db._extract_uniprot_from_dbref(cif, "A") == "A0A2K5QDT7"
        assert epitope_db._extract_uniprot_from_dbref(cif, "B") == "P00698"

    def test_the_ten_character_accession_answers_at_step_one(
        self, tmp_path, monkeypatch
    ):
        """The routing claim ``resolve_uniprot_id`` makes in its own docstring.

        Every other test here asserts the private extractor. What changed for
        the caller is WHICH STEP answers: a truncated accession failed the
        format check and fell through to the sequence search, which under
        #220 refuses on ambiguity — so the fix turns a wrong-or-absent
        annotation into the depositor's own. Make step 2 fatal, so reaching
        it cannot be mistaken for success.
        """
        pdb = tmp_path / "input.pdb"
        pdb.write_text(self.AF_TREMBL_DBREF + "END\n", encoding="utf-8")

        def _step_two_is_fatal(_seq):
            raise AssertionError("step 2 was reached; step 1 had the answer")

        monkeypatch.setattr(
            epitope_db, "_search_uniprot_by_sequence", _step_two_is_fatal
        )
        monkeypatch.setattr(
            epitope_db, "_extract_chain_sequence", lambda *a, **k: ([1], "MKV")
        )
        monkeypatch.setattr(
            epitope_db, "_fetch_uniprot_metadata",
            lambda acc: {"protein_name": "Somatotropin", "sequence": ""},
        )
        result = epitope_db.resolve_uniprot_id(pdb, "A")
        assert result["uniprot_id"] == "A0A2K5QDT7"
        assert result["source"] == "dbref"


# ---------------------------------------------------------------------------
# 11. The RCSB page size must not truncate the antibody complexes away
# ---------------------------------------------------------------------------


class TestRecallIsNotTruncatedByThePageSize:
    """``_RCSB_PROBE_LIMIT`` was 40, and that quietly destroyed recall.

    The search is an ``exact_match`` on a UniProt accession, so RCSB scores
    every hit exactly 1.0 and the ``sort_by: score`` block this code used to
    send was inert — the reply came back in identifier-ascending order whatever
    the direction, verified against the live API. PDB ids are roughly
    chronological and antibody complexes skew modern, so "the first 40" tended
    to miss them. Measured 2026-09-04 against RCSB and this module's own
    SAbDab index: of the 1340 antibody complexes for SARS-CoV-2 spike the
    first 40 ids held 16 (1.2% recall), and haemoglobin beta and insulin
    found none at all. It is a tendency rather than a law -- EGFR's 3-of-30
    is what a uniform draw gives -- and it bites hardest on exactly the
    heavily studied targets this feature exists for.

    Nor did it fail loudly, which is why no guard already here caught it. #215
    taught this path to tell "could not read" (``None``) from "read zero"
    (``[]``) so an outage stops writing permanent misses — but a truncated
    search is a SUCCESSFUL one that genuinely did read zero. It sails through
    that guard, and ``fetch_known_binders`` pins "no known binders" into a
    cache with no expiry. The /analyze route does log "0 binders found" at
    WARNING for it, but that line is character-for-character what a target
    with no antibodies logs, so it reads as a fact rather than a symptom.

    The stub below truncates server-side, exactly as RCSB does. That is the
    only shape in which the two property tests can fail if the constant
    regresses — a stub that returned everything regardless of ``rows`` would
    stay green at any page size at all.
    """

    # 60 decoys that all sort BEFORE the one real hit ("1" < "7") and match no
    # row in the fixture index, so the single antibody complex sits at position
    # 61 — past the old page size, reachable only by not truncating. Same shape
    # as production: old low-numbered entries first, the modern complex last.
    _DECOYS = [f"1B{n:02d}" for n in range(60)]
    _ALL_IDS = _DECOYS + ["7K8M"]

    @pytest.fixture
    def rcsb(self, monkeypatch):
        """Answer the RCSB search as RCSB does, and record the rows asked for."""
        asked = []

        def _fake_post(url, **kwargs):
            paginate = kwargs["json"]["request_options"]["paginate"]
            rows, start = paginate["rows"], paginate["start"]
            asked.append(rows)
            page = self._ALL_IDS[start:start + rows]
            return _FakeResponse(json.dumps({
                "total_count": len(self._ALL_IDS),
                "result_set": [{"identifier": i, "score": 1.0} for i in page],
            }))

        monkeypatch.setattr(epitope_db.requests, "post", _fake_post)
        return asked

    def test_a_complex_past_the_first_40_ids_is_still_found(self, served, rcsb):
        """The property itself. Red at any page size below 61."""
        served(_SUMMARY_CSV)
        binders = epitope_db.query_sabdab("P0DTC2")
        assert [b["pdb_id"] for b in binders] == ["7K8M"], (
            f"asked RCSB for {rcsb} rows and lost the only antibody complex"
        )

    def test_the_truncated_miss_is_not_pinned_in_the_cache(self, served, rcsb):
        """Truncation did not merely lose the hit, it made the loss permanent."""
        served(_SUMMARY_CSV)
        assert epitope_db.fetch_known_binders("P0DTC2")
        assert epitope_db._CACHE["P0DTC2"] != []

    def test_the_shipped_page_size_is_rcsbs_maximum(self):
        """Pins "no truncation at all", which the property test above cannot.

        A regression to some middling value — 500, say — would still satisfy
        the 61-id property while silently truncating spike's 2262 entries all
        over again. 10000 is RCSB's ceiling; 10001 is an HTTP 400, measured
        2026-09-04.
        """
        assert epitope_db._RCSB_ROWS_MAX == 10000
        assert epitope_db._RCSB_PROBE_LIMIT == epitope_db._RCSB_ROWS_MAX

    @pytest.mark.parametrize(
        "limit,expected", [(1, 1), (40, 40), (99_999, 10_000)]
    )
    def test_the_row_count_is_capped_at_rcsbs_maximum(self, limit, expected, rcsb):
        """Ask for more than RCSB allows and it 400s the whole search."""
        epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=limit)
        assert rcsb == [expected]

    @pytest.mark.parametrize("limit", [0, -5, 0.5, 0.9, 1e-9])
    def test_a_limit_below_one_answers_without_a_request(self, limit, rcsb):
        """"At most zero ids" is already answered, and answered with zero.

        The boundary is ONE, not zero. The payload floors with int(), so a
        fractional limit would send rows=0 -- an HTTP 200 with no result_set,
        which the strict read treats as unreadable and pays for with the SHARED
        backoff. An earlier draft guarded on ``> 0`` and let 0.5 straight
        through into exactly that; QC found it.

        An earlier draft clamped this UP to 1, which asked RCSB for a page and
        handed back an id the caller had explicitly not asked for. Asserting
        only the rows requested — as that draft's test did — cannot see it, so
        the RETURN VALUE is what is checked here.

        Not asking also matters: RCSB answers ``rows=0`` with an HTTP 200
        carrying no ``result_set`` at all (measured 2026-09-04), and #215's
        deliberately strict read treats that as unreadable and spends the
        SHARED error backoff on it, darkening every other accession for a full
        TTL.
        """
        assert epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=limit) == []
        assert rcsb == [], "a request was sent for a limit that needs none"
        assert epitope_db._RCSB_RETRY_AT == 0.0, "backoff spent on a no-op"

    def test_a_truncated_page_is_reported(self, rcsb, caplog):
        """The detection the original bug lacked, not just a bigger number.

        RCSB returns ``total_count`` in every reply and this code ignored it,
        which is why a page size of 40 could destroy recall in silence. Raising
        the ceiling alone only moves the number: P0DTD1 already returns 3668
        entries against a 10000 cap (measured 2026-09-04), so the day some
        accession crosses it the identical bug returns with the identical
        silence. Here the stub reports more matches than it serves.
        """
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=10)
        assert any(
            "truncated the entry list" in r.message and "P0DTC2" in str(r.args)
            for r in caplog.records
        ), f"no truncation warning; saw {[r.message for r in caplog.records]}"

    def test_the_truncation_warning_names_served_then_total(self, rcsb, caplog):
        """Order of the two counts, which a substring check cannot see.

        Swapping the arguments yields "61 of 10 returned" -- an operator
        reading that concludes RCSB sent MORE than it has and goes looking for
        the wrong bug. QC found this mutation surviving.
        """
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=10)
        msgs = [
            r.getMessage() for r in caplog.records
            if "truncated the entry list" in r.message
        ]
        assert msgs, "no truncation warning at all"
        assert "10 of 61 returned" in msgs[0], msgs[0]

    @pytest.mark.parametrize("bogus", ["61", 61.0, None, "lots"])
    def test_an_unreadable_total_count_is_not_silently_ignored(
        self, bogus, monkeypatch, caplog
    ):
        """A retype upstream must not disable the detector in silence.

        The whole point of reading total_count is that a truncation nobody can
        see is the bug. A bare ``isinstance(total, int)`` -- which an earlier
        draft used, and which QC caught surviving mutation -- turns any rename
        or retype into exactly that: no warning, no detection, forever. The
        numeric-looking values must still detect; the rest must at least say
        they cannot.
        """
        import logging  # noqa: PLC0415

        def _fake_post(url, **kwargs):
            return _FakeResponse(json.dumps({
                "total_count": bogus,
                "result_set": [{"identifier": "1ABC"}],
            }))

        monkeypatch.setattr(epitope_db.requests, "post", _fake_post)
        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            assert epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2") == ["1ABC"]
        # Match the unreadable-value warning specifically. Asserting only that
        # SOMETHING was logged — as an earlier draft did — is satisfied for the
        # numeric-looking values by the TRUNCATION warning, so those two params
        # silently duplicated another test instead of pinning this one. QC
        # caught that by deleting the truncation block and watching them fail.
        unreadable = [
            r for r in caplog.records if "no readable total_count" in r.message
        ]
        detected = [
            r for r in caplog.records if "truncated the entry list" in r.message
        ]
        assert unreadable or detected, (
            f"total_count={bogus!r} silently disabled the detector"
        )
        if isinstance(bogus, (int, float)) or (
            isinstance(bogus, str) and bogus.isdigit()
        ):
            assert detected, f"{bogus!r} is readable but was not used"
        else:
            assert unreadable, f"{bogus!r} is unreadable but nothing said so"

    def test_a_boolean_total_count_warns_instead_of_reporting_truncation(
        self, monkeypatch, caplog
    ):
        """``bool`` is an ``int`` in Python, so ``True > 0`` would report a
        truncation on an empty page. But rejecting it SILENTLY — as an earlier
        draft did — is the very thing the block's own comment forbids, so it
        has to go down the unreadable-value path and say so."""
        import logging  # noqa: PLC0415

        def _fake_post(url, **kwargs):
            return _FakeResponse(json.dumps(
                {"total_count": True, "result_set": []}
            ))

        monkeypatch.setattr(epitope_db.requests, "post", _fake_post)
        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2")
        assert not [
            r for r in caplog.records
            if "truncated the entry list" in r.message
        ]
        assert [r for r in caplog.records if "no readable total_count" in r.message]

    def test_an_infinite_total_count_does_not_arm_the_shared_backoff(
        self, monkeypatch, caplog
    ):
        """``int(float('inf'))`` raises OverflowError, which is NOT a subclass
        of ValueError.

        An earlier draft caught only (TypeError, ValueError), so this escaped
        into the outer handler: a readable result_set was thrown away, the
        reply was logged as "RCSB search failed", and the SHARED backoff was
        armed -- darkening the known-binder lookup for EVERY other accession
        for a full error TTL, on a reply that was perfectly usable.

        ``json.loads`` really does produce this: a bare ``Infinity`` token, or
        any number at or above 1e309.
        """
        import logging  # noqa: PLC0415

        def _fake_post(url, **kwargs):
            return _FakeResponse(
                '{"total_count": Infinity, "result_set": [{"identifier": "1ABC"}]}'
            )

        monkeypatch.setattr(epitope_db.requests, "post", _fake_post)
        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            assert epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2") == ["1ABC"]
        assert epitope_db._RCSB_RETRY_AT == 0.0, (
            "a readable reply armed the SHARED backoff"
        )
        assert [r for r in caplog.records if "no readable total_count" in r.message]

    @pytest.mark.parametrize(
        "limit,expected_rows", [(40.5, 40), (10_000.9, 10_000)]
    )
    def test_a_fractional_limit_is_coerced_not_shipped(
        self, limit, expected_rows, rcsb
    ):
        """``"rows": 40.5`` earns an HTTP 400 and spends the SHARED backoff."""
        epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=limit)
        assert rcsb == [expected_rows]

    def test_an_infinite_limit_is_clamped_not_raised(self, rcsb):
        """The other end of the same hazard the NaN guard closes.

        ``inf >= 1`` is True, so infinity sails past that guard and reaches the
        payload. An earlier draft coerced with ``min(int(limit), _RCSB_ROWS_MAX)``,
        and ``int(inf)`` raises OverflowError -- built OUTSIDE the ``try``, so it
        left the function as an exception rather than either documented return.
        Clamping before coercing means int() only ever sees a finite number.
        """
        epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2", limit=float("inf"))
        assert rcsb == [epitope_db._RCSB_ROWS_MAX]

    def test_a_nan_limit_does_not_reach_rcsb(self, rcsb):
        """NaN fails every comparison, so any bare ``<``/``<=`` bound is False
        for it and execution would carry on to the coercion, where ``int(NaN)``
        raises ValueError -- outside the ``try``, so it would leave the function
        as an exception rather than either documented return."""
        assert epitope_db._rcsb_pdb_ids_for_uniprot(
            "P0DTC2", limit=float("nan")
        ) == []
        assert rcsb == []
        assert epitope_db._RCSB_RETRY_AT == 0.0

    def test_a_complete_page_is_not_reported_as_truncated(self, rcsb, caplog):
        """The complement. A warning that always fires is noise, and noise on
        this path is what let the original silence go unnoticed."""
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING, logger=epitope_db.logger.name):
            epitope_db._rcsb_pdb_ids_for_uniprot("P0DTC2")
        assert not [
            r for r in caplog.records
            if "truncated the entry list" in r.message
        ]
