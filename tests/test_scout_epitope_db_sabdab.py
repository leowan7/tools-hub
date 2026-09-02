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
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clean_caches():
    """No test may inherit another's index, per-UniProt cache, or backoff."""
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()
    epitope_db._RCSB_RETRY_AT = 0.0
    yield
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()
    epitope_db._RCSB_RETRY_AT = 0.0


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
    field: eight characters from columns 33-41 of a plain DBREF line, or
    twenty-two from columns 18-40 of a DBREF2.

    With no format check those eight bytes became a lookup target and a
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
