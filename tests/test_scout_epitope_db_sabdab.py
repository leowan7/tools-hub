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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clean_caches():
    """No test may inherit another's index or per-UniProt cache."""
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()
    yield
    epitope_db._reset_summary_cache()
    epitope_db._CACHE.clear()


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
