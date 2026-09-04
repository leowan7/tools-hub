"""Guards for the exact-sequence UniProt lookup in ``scout.epitope_db``.

Why this file exists
--------------------

``_search_uniprot_by_sequence`` returned "" for every sequence. It sent the
first 50 residues to UniProtKB's ``/uniprotkb/search`` as free text::

    params={"query": f"({sequence[:50]})", ...}

UniProtKB's text index does not store sequences, so the residues matched nothing
and the endpoint answered HTTP 200 with zero results -- indistinguishable from
"this protein is not in UniProt". Measured 2026-09-02 against the live API: the
exact 50-mer prefix of P00698, a 50-mer from its middle, and a 20-mer prefix all
returned 0 hits, while the plain-English query "lysozyme" on the SAME endpoint
returned P00698. The endpoint was up; the query shape was wrong.

That function is step 2 of ``resolve_uniprot_id``: it runs whenever step 1
returns nothing, and is the only route to a UniProt annotation for those
uploads. Enumerating which files those are has been wrong twice -- an mmCIF
carries no ``DBREF`` records at all yet resolves at step 1, and a PDB file
using the two-line ``DBREF1``/``DBREF2`` form has reference records the step-1
parser cannot read -- so this file no longer tries. Those users silently got no
protein name and no identity. A
previous audit edited the inside of this function -- removing a no-op
``idmapping/run`` POST, ``bd8442f`` -- without noticing it never returned
anything, because nothing asserted that it could.

The replacement asks UniProtKB for entries whose CRC64 checksum equals the
sequence's (``checksum:`` is an indexed field; free text is indexed too, it
just does not index SEQUENCES) and accepts only when exactly one exists.

Design of the guards below, in order of what they would have caught:

1. The request must be a checksum query carrying the real CRC64 of the sequence
   it was called with. Two different sequences are sent and each checksum is
   recomputed here, so a free-text revert and a hardcoded checksum both fail.
2. Raw residues must never appear in ANY outgoing request component again --
   the scan covers the whole kwargs blob and the URL, not just ``params``.
3. The endpoint constant must stay the live UniProtKB search API. The sibling
   SAbDab feature died from a retired URL that no test pinned.
4. An AMBIGUOUS match must return "". This is the finding that forced the
   design: one sequence carried by several species' entries produced a
   confident wrong answer the caller's identity gate cannot catch, because
   identical sequences always score 100%.
4b. The count must be taken over ALL of UniProtKB. Adding ``reviewed:true`` or
   ``fragment:false`` deletes the entries that CONSTITUTE the tie, turning a
   shared sequence into a fake unique hit -- measured at 16 wrong organisms in
   18 answers before the filters came out. A scripted fake cannot catch it by
   RESULT (the rows are whatever the fake serves), so the hermetic guard pins
   the QUERY, whole-string: any added conjunct is a subsetting predicate and
   brings the same failure back under a different spelling. Two-substring
   versions of that assert were tried and let ``taxonomy_id:9606`` through.
   ``TestLiveCapability`` covers the same ground against the real endpoint,
   but it is opt-in behind ``SCOUT_UNIPROT_LIVE=1`` and runs on a cadence in
   .github/workflows/uniprot-capability.yml, NOT on the PR gate -- so it is
   not what protects a merge.
5. The ambiguity decision must come from the response's total count, not from
   the number of rows returned. An earlier draft inferred it from
   ``len(results)``, which ``size`` caps -- so setting ``size=1`` silently made
   every ambiguous match look unique and accepted the wrong species again.
6. ``resolve_uniprot_id`` must still REACH step 2. Fixing the inside of a
   function nothing calls is how this bug survived its last audit.
7. Opt-in, against the live API: the capability itself.

The fake serves scripted bodies, so tests 1-6 prove request shape and decision
logic only. Test 7 is the sole check that UniProt still answers at all.
"""

from __future__ import annotations

import os

import pytest
import requests
from Bio.SeqUtils.CheckSum import crc64
from requests.structures import CaseInsensitiveDict

from scout import epitope_db


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
# Hand-written minimal bodies in the shape the code reads
# (``results[].primaryAccession`` plus the ``x-total-results`` header). The live
# reply carries extra fields -- ``entryType``, ``extraAttributes.uniParcId`` --
# that the code ignores; they are left out rather than half-copied, so nothing
# here claims to be a verbatim capture.

# Hen egg-white lysozyme precursor, 147 aa. Exactly one reviewed non-fragment
# entry carries this sequence, so it is the unambiguous case.
_P00698_SEQ = (
    "MRSLLILVLCFLPLAALGKVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGST"
    "DYGILQINSRWWCNDGRTPGSRNLCNIPCSALLSSDITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDV"
    "QAWIRGCRL"
)

# Human haemoglobin subunit beta, 147 aa. The AMBIGUOUS case: this exact
# sequence is also the reviewed bonobo (P68872) and chimpanzee (P68873) entry.
_P68871_SEQ = (
    "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKVKAHGKKVL"
    "GAFSDGLAHLDNLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGKEFTPPVQAAYQKVVAGV"
    "ANALAHKYH"
)

# Sentinel: tell the fake to omit x-total-results entirely.
_NO_HEADER = object()

# Human serum albumin, 609 aa. Present ONLY so the suite exercises a sequence
# longer than 147: every other payload here is <= 147 aa, and inertness gated on
# a length above that (``if len(sequence) >= 148: return ""``) passed the whole
# file, live tests included, before this existed.
_P02768_SEQ = (
    "MKWVTFISLLFLFSSAYSRGVFRRDAHKSEVAHRFKDLGEENFKALVLIAFAQYLQQCPFEDHVKLVNE"
    "VTEFAKTCVADESAENCDKSLHTLFGDKLCTVATLRETYGEMADCCAKQEPERNECFLQHKDDNPNLPR"
    "LVRPEVDVMCTAFHDNEETFLKKYLYEIARRHPYFYAPELLFFAKRYKAAFTECCQAADKAACLLPKLD"
    "ELRDEGKASSAKQRLKCASLQKFGERAFKAWAVARLSQRFPKAEFAEVSKLVTDLTKVHTECCHGDLLE"
    "CADDRADLAKYICENQDSISSKLKECCEKPLLEKSHCIAEVENDEMPADLPSLAADFVESKDVCKNYAE"
    "AKDVFLGMFLYEYARRHPDYSVVLLLRLAKTYETTLEKCCAAADPHECYAKVFDEFKPLVEEPQNLIKQ"
    "NCELFEQLGEYKFQNALLVRYTKKVPQVSTPTLVEVSRNLGKVGSKCCKHPEAKRMPCAEDYLSVVLNQ"
    "LCVLHEKTPVSDRVTKCCTESLVNRRPCFSALEVDETYVPKEFNAETFTFHADICTLSEKERQIKKQTA"
    "LVELVKHKPKATKEQLKAVMDDFAAFVEKCCKADDKETCFAEEGKKLVAASQAALGL"
)

# Chimpanzee von Hippel-Lindau tumour suppressor, K7BID8, 213 aa. The live
# proof that a reviewed/fragment filter is not a precision improvement: with
# the filter this sequence returns exactly ONE hit -- human P40337 -- and the
# wrong organism is asserted at "100.0% identity". Without it UniProt reports
# 5 entries (P40337, A0A2R9B5S8, K7BID8, A0A024R2F2, A0A2I3SEM6) and the tie is
# refused. Only the live API can show this; a scripted fake serves whatever
# rows it is handed either way.
_K7BID8_SEQ = (
    "MPRRAENWDEAEVGAEEAGVEEYGPEEDGGEESGAEESGPEESGPEELGAEEEMEAGRPRPVLRSVNS"
    "REPSQVIFCNRSPRVVLPVWLNFDGEPQPYPTLPPGTGRRIHSYRGHLWLFRDAGTHDGLLVNQTELF"
    "VPSLNVDGQPIFANITLPVYTLKERCLQVVRSLVKPENYRRLDIVRSLYEDLEDHPNVQKDLERLTQE"
    "RIAHQRMGD"
)

# Two near-neighbours of _P00698_SEQ that BRACKET the 0.70 identity gate:
# 0.680 must be rejected, 0.748 must be accepted. A single far-off fixture
# ("W" * 147, identity 0.034) proved only that the gate sits somewhere above
# 0.034 -- the body could read ``identity < 0.05`` while the constant stayed
# 0.70 and the whole suite passed. These two make the NUMBER load-bearing.
_BELOW_GATE_SEQ = _P00698_SEQ[:-50] + "W" * 50
_ABOVE_GATE_SEQ = _P00698_SEQ[:-40] + "W" * 40

_ONE_HIT = [{"primaryAccession": "P00698"}]
# Bonobo FIRST, deliberately. At ``size=1`` the live API really does return
# P68872 ahead of the human entry (measured, stable over repeats), so a fixture
# ordered human-first would let "just take the first row" return the CORRECT
# accession and the ambiguity guards would pass a broken implementation.
_THREE_HITS = [
    {"primaryAccession": "P68872"},
    {"primaryAccession": "P68871"},
    {"primaryAccession": "P68873"},
]


class _FakeResponse:
    """Models ``requests``: headers are case-insensitive, and the real wire
    header is ``X-Total-Results``. A plain lowercase dict here made the suite
    case-SENSITIVE where production is not -- it would have failed a correct
    change to the capitalised spelling, and passed a broken one.

    ``total=_NO_HEADER`` omits the header entirely. That is not the only way
    into the row-counting fallback -- production parses with ``int()`` under
    ``except (TypeError, ValueError)``, so a header that is PRESENT but not an
    integer lands there too. ``_FakeResponse`` can only ever write ``str(total)``,
    so ``_FakeHeaderResponse`` below covers that branch instead.
    """

    def __init__(self, rows, total, status=200):
        self._rows = rows
        self.status_code = status
        self.ok = status < 400
        self.headers = CaseInsensitiveDict()
        if total is not None:
            self.headers["X-Total-Results"] = str(total)

    def json(self):
        return {"results": self._rows}


class _FakeHeaderResponse(_FakeResponse):
    """Serves a VERBATIM ``x-total-results`` value, bypassing ``str(total)``.

    Exists to reach the branch ``_FakeResponse`` cannot: a header that is
    present but unparseable. ``"3.0"`` is the case that matters -- an upstream
    that ever formats the total as a float would silently drop production onto
    the row count, on the one decision this design exists to protect.
    """

    def __init__(self, rows, raw_header, status=200):
        super().__init__(rows, None, status)
        self.headers["X-Total-Results"] = raw_header


class _NetworkAttempted(BaseException):
    """Deliberately NOT an ``Exception``.

    Every network call site in ``epitope_db`` is wrapped in ``except
    Exception``, so a guard raising ``AssertionError`` is swallowed by the code
    it is guarding: a production change that let step 2 run on an empty
    accession made two real UniProt calls from this suite and stayed green.
    """


def _no_network(*args, **kwargs):
    raise _NetworkAttempted(
        f"real network call attempted: {args[:1]} -- this suite is hermetic"
    )


@pytest.fixture
def captured(monkeypatch):
    """Record every request; serve hits ONLY to a checksum query, and OBEY size.

    Both halves are load-bearing.

    Serving only checksum queries is what makes the behavioural tests real. An
    earlier draft answered any URL, so the original inert free-text
    implementation -- which reads ``results[0]["primaryAccession"]`` from the
    same endpoint -- passed them untouched.

    Obeying ``size`` is what lets the ambiguity tests exercise a state
    production can actually reach. An earlier draft returned three rows to a
    request that asked for two, so the reachable ambiguous case (two rows, a
    header total of three) was never tested and a ``size=1`` regression went
    unnoticed.

    ``total`` defaults to the row count. Passing it explicitly decouples the
    header from the rows on purpose -- that models the page-capped response, or
    (with ``total=_NO_HEADER``) a reply carrying no header at all.
    """
    def _serve(rows, total=None, status=200):
        calls = []
        if total is _NO_HEADER:
            full_total = None
        else:
            full_total = len(rows) if total is None else total

        def _fake_get(url, **kwargs):
            params = kwargs.get("params", {})
            calls.append({"url": url, "params": params, "kwargs": kwargs})
            if not str(params.get("query", "")).startswith("(checksum:"):
                return _FakeResponse([], 0, status)
            size = int(params.get("size", 25))
            return _FakeResponse(rows[:size], full_total, status)

        monkeypatch.setattr(epitope_db.requests, "get", _fake_get)
        return calls

    return _serve


# ---------------------------------------------------------------------------
# 1. The request shape -- the actual bug
# ---------------------------------------------------------------------------


class TestTheQueryIsAChecksumLookup:
    """The failure was a well-formed request to an index that cannot answer it.
    Asserting the RESULT alone cannot tell that apart from a real miss, so these
    assert what goes out on the wire."""

    @pytest.mark.parametrize(
        ("sequence", "label"),
        [(_P00698_SEQ, "P00698"), (_P68871_SEQ, "P68871"), (_P02768_SEQ, "P02768")],
    )
    def test_the_query_carries_that_sequence_s_own_crc64(
        self, captured, sequence, label
    ):
        """Two different sequences, each checksum recomputed here.

        Parametrising is the point: with a single sequence a HARDCODED checksum
        in the production code passed, because the one expected value and the
        one frozen value coincided.

        The 609-aa albumin case is here for a different reason: it is the only
        payload longer than 147, and without it inertness gated on length
        (``if len(sequence) >= 148: return ""``) passed every test in the file.
        """
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(sequence)

        assert calls, f"no request was made for {label}"
        expected = crc64(sequence).replace("CRC-", "")
        assert f"(checksum:{expected})" in calls[0]["params"]["query"]

    def test_the_two_sequences_produce_different_checksums(self):
        """Pins the parametrised test above to real work: if these coincided,
        a hardcoded checksum would satisfy both."""
        assert crc64(_P00698_SEQ) != crc64(_P68871_SEQ)

    def test_the_query_counts_all_of_uniprotkb_not_a_filtered_subset(self, captured):
        """No ``reviewed:true``, no ``fragment:false`` -- and that is the fix.

        Both filters read as precision improvements and are the opposite: they
        delete the sibling entries that MAKE a sequence ambiguous, so a shared
        sequence returns ``x-total-results: 1`` and the wrong organism is
        asserted at "100.0% identity". Measured over 240 non-model-organism
        chains: 18 accessions returned, 16 of them the wrong organism, every
        one with a real tie hidden behind the filters (a chimpanzee VHL chain
        came back as human P40337; the unfiltered count for it is 5).

        A scripted fake cannot catch a re-added filter by its RESULT -- the
        rows are whatever the fake serves -- so this asserts the wire, and
        asserts the WHOLE of it.
        """
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        expected = crc64(_P00698_SEQ).replace("CRC-", "")
        # Whole-string equality, not absence of the two words "reviewed" and
        # "fragment". A denylist only refuses the spellings someone already
        # thought of: `AND (taxonomy_id:9606)` -- which hardcodes the human
        # prior this function explicitly does not have -- and `AND
        # (active:true)` both passed the two-substring version this replaces,
        # the second one against the live API too. Any added conjunct is a
        # subsetting predicate and brings the wrong-organism failure back.
        assert calls[0]["params"]["query"] == f"(checksum:{expected})"
        # Without format=json a non-JSON body makes resp.json() raise, which
        # the broad except turns into "" -- the silent death, again.
        assert calls[0]["params"]["format"] == "json"
        # Unpinned, dropping this returns whole entries instead of one field:
        # correctness is unchanged, the response is orders of magnitude bigger,
        # on an anonymous request path.
        assert calls[0]["params"]["fields"] == "accession"

    def test_every_request_is_bounded_by_a_timeout(self, captured):
        """Asserts the VALUE, not just presence: ``timeout=3600`` is truthy
        and holds an anonymous compute slot for an hour."""
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        assert calls[0]["kwargs"]["timeout"] == epitope_db._REQUEST_TIMEOUT_SEC
        # And pin the constant: asserting only that the call matches it is
        # satisfied by raising the constant to an hour.
        assert epitope_db._REQUEST_TIMEOUT_SEC == 12

    def test_raw_residues_never_appear_anywhere_in_the_request(self, captured):
        """The old shape sent ``(MRSLLILVLCF...)`` as free text.

        Scans the WHOLE outgoing request with every 8-residue window. Scoping
        this to ``params`` was not enough: residues smuggled through ``headers=``
        or ``data=`` sailed past, and the docstring claimed otherwise. A 20-char
        needle against ``query`` alone let a 15-mer revert, a mid-sequence
        slice, and a sibling parameter all pass; a stride of 4 still missed the
        final three residues.
        """
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)

        assert calls, "no request made -- the scan below would pass vacuously"
        windows = [_P00698_SEQ[i:i + 8] for i in range(0, len(_P00698_SEQ) - 7)]
        for call in calls:
            blob = str(call["kwargs"]) + str(call["url"])
            for w in windows:
                assert w not in blob, f"sequence residues {w!r} leaked into {blob[:90]}"

    def test_one_residue_below_the_floor_makes_no_request(self, captured):
        """Derived from the constant, not a literal.

        Pinning the constant alone left the BODY free: the effective floor
        could be any value up to 147, and ``<`` could become ``<=``, with
        every test still green.
        """
        calls = captured(_ONE_HIT)
        short = _P00698_SEQ[:epitope_db._MIN_SEARCHABLE_LENGTH - 1]
        assert epitope_db._search_uniprot_by_sequence(short) == ""
        assert calls == [], "a sequence below the floor must cost no request"

    def test_exactly_at_the_floor_does_make_a_request(self, captured):
        """The other half of the boundary -- without it ``<`` -> ``<=`` and a
        floor set far too high both pass."""
        calls = captured(_ONE_HIT)
        at_floor = _P00698_SEQ[:epitope_db._MIN_SEARCHABLE_LENGTH]
        got = epitope_db._search_uniprot_by_sequence(at_floor)
        assert len(calls) == 1, "a sequence at the floor must be looked up"
        assert got == "P00698", "looked up, then discarded, is not a lookup"

    def test_the_length_floor_is_the_shipped_value(self):
        """Pinned so a silent drift is visible in review.

        The value is a judgement, not a derivation. It is not the safety
        mechanism either -- uniqueness is; the floor only declines to ask about
        peptides too short for a whole-sequence match to mean anything.
        """
        assert epitope_db._MIN_SEARCHABLE_LENGTH == 20


# ---------------------------------------------------------------------------
# 2. The endpoint itself
# ---------------------------------------------------------------------------


class TestTheEndpointIsPinned:
    """This repo has already shipped one silently-dead lookup whose cause was a
    retired URL that no test asserted (see tests/test_scout_epitope_db_sabdab.py
    'the retired webapps path'). A fake that routes on substrings cannot notice
    the difference, so the constant is pinned directly."""

    def test_the_search_url_is_the_live_uniprot_api(self):
        assert epitope_db.UNIPROTKB_SEARCH_URL == (
            "https://rest.uniprot.org/uniprotkb/search"
        )

    def test_the_request_goes_to_that_url(self, captured):
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        assert calls[0]["url"] == epitope_db.UNIPROTKB_SEARCH_URL


# ---------------------------------------------------------------------------
# 3. Reading the answer -- and refusing to guess
# ---------------------------------------------------------------------------


class TestTheAccessionIsResolved:
    def test_exactly_one_hit_resolves(self, captured):
        captured(_ONE_HIT)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00698"

    def test_an_ambiguous_match_returns_nothing(self, captured):
        """The finding that forced this design.

        Human, bonobo and chimpanzee haemoglobin beta are one sequence. Picking
        the first returned P68872 (bonobo) for a human chain at "100.0%
        identity", and the caller's >=70% gate CANNOT catch it -- identical
        sequences always score 100%. It also zeroed the known-binder lookup:
        P68872 indexes no PDB entries at all.
        """
        calls = captured(_THREE_HITS)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""
        assert calls, '"" must mean "asked and refused", not "never asked"'

    def test_a_two_way_tie_is_also_refused(self, captured):
        """The smallest ambiguous case. Tested separately from the three-way
        case above because an off-by-one in the comparison ("more than two")
        refuses a three-way tie while still accepting a two-way one.
        """
        calls = captured(_THREE_HITS[:2], total=2)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""
        assert calls

    def test_ambiguity_is_decided_by_the_total_not_the_row_count(self, captured):
        """The regression that ``size`` can otherwise hide.

        A page of ONE row whose header total says 2 is an ambiguous match. If
        the decision reads ``len(results)`` instead of the total, this looks
        unique and the wrong species is accepted -- which is exactly what
        setting ``size=1`` did, silently, with every other test still green.

        The total is 2 rather than 3 on purpose: it is the smallest ambiguous
        value, so this also kills the off-by-one ``total > 2``, which a
        three-way tie leaves green because the row-count conjunct rescues it.
        """
        calls = captured(_THREE_HITS[:1], total=2)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""
        assert calls

    def test_a_missing_header_falls_back_to_the_row_count(self, captured):
        """No test reached this branch before, yet it is the branch that makes
        ``size`` load-bearing. With no header a two-row page must still read as
        ambiguous.
        """
        calls = captured(_THREE_HITS, total=_NO_HEADER)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""
        assert calls

    def test_a_missing_header_still_accepts_a_lone_row(self, captured):
        """The other side of the fallback: one row and no header is unique."""
        captured(_ONE_HIT, total=_NO_HEADER)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00698"

    def test_a_header_of_one_with_two_rows_is_refused(self, captured):
        """The other direction of header/body disagreement.

        The tie check reads the header, so a header saying "1" alongside two
        rows would be accepted on the header's word alone. Live UniProt never
        does this, but a caching proxy or an API change could, and the failure
        mode is the one this whole design exists to prevent: confidently
        returning one of two candidates.
        """
        calls = captured(_THREE_HITS[:2], total=1)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""
        assert calls

    def test_the_page_size_still_admits_a_second_row(self, captured):
        """Belt and braces for the test above: if the header ever disappears,
        the fallback is ``len(results)``, which is only safe while the request
        asks for at least two rows.
        """
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        assert int(calls[0]["params"]["size"]) >= 2

    def test_a_genuine_miss_is_empty(self, captured):
        """A de-novo design has no UniProt entry, so "" is correct."""
        calls = captured([])
        design = ("MSEEELKKLAEELKKKAEELKKKSEEELKKLAEEAKKKAEELKKKSEEELKKLAEEL"
                  "KKKAEELKKKSEEELKKLAEEAKKKAEELKKK")
        assert epitope_db._search_uniprot_by_sequence(design) == ""
        assert calls, '"" must mean "asked and found nothing", not "never asked"'

    def test_a_malformed_accession_is_rejected(self, captured):
        """The accession is interpolated into a UniProtKB URL path and becomes a
        ``_CACHE`` key, and it arrives over the network.

        The payload survives any prefix/suffix trimming, so it reaches
        ``_valid_accession`` itself rather than dying on an earlier truthiness
        check -- an adversarial input neutralised before the guard it is named
        for proves nothing.
        """
        calls = captured([{"primaryAccession": "P00698/../../etc/passwd"}])
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""
        assert calls

    def test_an_http_error_is_empty_and_not_raised(self, captured):
        captured(_ONE_HIT, status=503)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""

    @pytest.mark.parametrize("raw", ["3.0", "abc", ""])
    def test_an_unparseable_header_falls_back_to_the_row_count(
        self, monkeypatch, raw
    ):
        """The branch ``_FakeResponse`` cannot reach.

        A present-but-unparseable header hits the same
        ``except (TypeError, ValueError)`` as a missing one. ``"3.0"`` is the
        real hazard: an upstream that formats the total as a float would drop
        production onto the row count, which ``size`` caps -- so a genuine tie
        would read as a unique match. Two rows here, so the fallback must still
        refuse.
        """
        def _fake_get(url, **kwargs):
            return _FakeHeaderResponse(_THREE_HITS[:2], raw)

        monkeypatch.setattr(epitope_db.requests, "get", _fake_get)
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""

    def test_a_transport_failure_is_empty_and_not_raised(self, monkeypatch):
        """A UniProt outage must degrade to "no annotation", never to a 500.

        ``resolve_uniprot_id`` does not wrap step 2, and the route's handler is
        a blanket ``except Exception -> 500``, so narrowing this function's own
        ``except Exception`` turns a UniProt outage into a failed analysis for
        the whole upload. Nothing covered the raise path -- the HTTP-error test
        above exercises ``resp.ok``, which is a different branch entirely.
        """
        def _boom(url, **kwargs):
            raise requests.exceptions.ConnectionError("upstream down")

        monkeypatch.setattr(epitope_db.requests, "get", _boom)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""

    def test_a_broken_biopython_is_loud_and_costs_no_request(
        self, monkeypatch, caplog
    ):
        """The import guard, which nothing exercised.

        Its comment says the WARNING level is load-bearing -- at DEBUG this
        returns "" for every sequence forever, which is exactly the silent
        death the whole change exists to undo. Downgrading the level, or
        falling through to some other checksum, both passed before this.
        """
        import builtins

        real_import = builtins.__import__

        def _fail(name, *args, **kwargs):
            if name == "Bio.SeqUtils.CheckSum":
                raise ImportError("no BioPython")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(builtins, "__import__", _fail)
        with caplog.at_level("WARNING", logger=epitope_db.logger.name):
            assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""
        assert any(
            r.levelname == "WARNING" and "crc64" in r.message for r in caplog.records
        ), "a dead checksum import must be LOUD, not a silent empty result"


# ---------------------------------------------------------------------------
# 4. The caller still reaches it
# ---------------------------------------------------------------------------


class TestStepTwoIsWiredUp:
    """Fixing the inside of a function nothing calls is how this bug survived
    its last audit, so these assert the call actually happens.

    Every test here patches ``requests`` as well. Without that, a production
    change that lets step 2 run on an empty accession sends REAL traffic to
    UniProt from a suite that claims to be hermetic -- green either way, the
    only symptom a slower run.
    """

    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        """Stub the two collaborators while RECORDING what each was handed.

        Both halves matter. Recording the sequence caught step 2 being passed
        ``chain_id``; recording ``(path, chain_id)`` catches the same class one
        level up -- a stub written ``lambda *a, **k:`` is blind to WHICH chain
        was read, so hardcoding chain "A", or swapping the two arguments, stayed
        green while every other chain silently broke.
        """
        pdb = tmp_path / "no_reference_records.pdb"
        pdb.write_text("END\n")
        seen = []
        extracted = []

        def _fake_search(seq):
            seen.append(seq)
            return "P00698"

        def _fake_extract(path, chain_id):
            extracted.append((path, chain_id))
            return [1], _P00698_SEQ

        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(epitope_db, "_extract_chain_sequence", _fake_extract)
        monkeypatch.setattr(epitope_db, "_search_uniprot_by_sequence", _fake_search)
        monkeypatch.setattr(
            epitope_db, "_fetch_uniprot_metadata",
            lambda acc: {"protein_name": "Lysozyme C", "sequence": _P00698_SEQ},
        )
        return pdb, seen, extracted

    def test_step_two_receives_the_chain_sequence(self, wired):
        """The argument, not just the call.

        A stub written ``lambda seq: "P00698"`` ignores what it is given, so
        passing ``chain_id`` -- a single letter -- instead of the sequence left
        this class green while the function was inert for every real chain.
        """
        pdb, seen, _ = wired
        epitope_db.resolve_uniprot_id(pdb, "A")
        assert seen == [_P00698_SEQ], (
            f"step 2 was handed {seen!r}, not the chain sequence"
        )

    def test_the_requested_chain_is_the_one_read(self, wired):
        """The sequence must come from the chain the caller asked for.

        Asserting only the sequence cannot see this: the stub returns the same
        constant whatever chain it is given, so hardcoding "A" -- or swapping
        the path and chain arguments -- satisfies it. Ask for "B", and require
        that "B" is what was read.
        """
        pdb, _, extracted = wired
        epitope_db.resolve_uniprot_id(pdb, "B")
        assert extracted == [(pdb, "B")], (
            f"_extract_chain_sequence was called with {extracted!r}, not "
            "(the uploaded path, the requested chain)"
        )

    def test_a_resolved_accession_is_reported_as_sequence_search(self, wired):
        pdb, _, _ = wired
        result = epitope_db.resolve_uniprot_id(pdb, "A")
        assert result["uniprot_id"] == "P00698"
        assert result["source"] == "sequence_search"

    def test_a_dbref_that_fails_validation_still_reaches_step_two(
        self, tmp_path, monkeypatch
    ):
        """The case the step-2 docstring names and no fixture reached.

        Every other test here writes a bare ``END``, so ``dbref_accession`` is
        always "" and the call site's ``search_accession != dbref_accession``
        term never sees a real value. Rewriting that term to
        ``not dbref_accession`` -- which skips step 2 entirely whenever ANY
        DBREF exists, including one just rejected -- passed the whole file.

        A structure with a wrong or stale DBREF is exactly what step 2 is for.
        """
        pdb = tmp_path / "wrong_dbref.pdb"
        pdb.write_text(
            "DBREF  9XYZ A    1   147  UNP    P12345   FAKE_TEST        1    147\n"
            "END\n"
        )

        # Assert the PRECONDITION. If the column arithmetic above is off, the
        # parser returns "" and this test degrades into a duplicate of the
        # bare-END ones -- passing while guarding nothing.
        assert epitope_db._extract_uniprot_from_dbref(pdb, "A") == "P12345", (
            "fixture must actually carry a readable DBREF, or it guards nothing"
        )

        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(
            epitope_db, "_extract_chain_sequence", lambda *a, **k: ([1], _P00698_SEQ)
        )
        monkeypatch.setattr(
            epitope_db, "_search_uniprot_by_sequence", lambda seq: "P00698"
        )
        # P12345 fails the identity gate; P00698 matches the chain.
        monkeypatch.setattr(
            epitope_db, "_fetch_uniprot_metadata",
            lambda acc: {
                "protein_name": "Lysozyme C" if acc == "P00698" else "Something else",
                "sequence": _P00698_SEQ if acc == "P00698" else _BELOW_GATE_SEQ,
            },
        )

        result = epitope_db.resolve_uniprot_id(pdb, "A")
        assert result["uniprot_id"] == "P00698", (
            "a rejected DBREF must not suppress the sequence-search fallback"
        )
        assert result["source"] == "sequence_search"

    def test_a_refusal_leaves_the_result_empty(self, tmp_path, monkeypatch):
        pdb = tmp_path / "no_reference_records.pdb"
        pdb.write_text("END\n")
        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(
            epitope_db, "_extract_chain_sequence", lambda *a, **k: ([1], _P00698_SEQ)
        )
        monkeypatch.setattr(epitope_db, "_search_uniprot_by_sequence", lambda seq: "")

        result = epitope_db.resolve_uniprot_id(pdb, "A")
        assert result["uniprot_id"] == ""
        assert result["source"] == ""

    @pytest.mark.parametrize(
        ("meta_sequence", "expected"),
        [(_BELOW_GATE_SEQ, ""), (_ABOVE_GATE_SEQ, "P00698")],
    )
    def test_the_identity_gate_sits_at_the_shipped_threshold(
        self, tmp_path, monkeypatch, meta_sequence, expected
    ):
        """Both wiring tests above hand back metadata identical to the chain,
        so identity is always 100% and the gate never fires -- it could be
        deleted, or ``must_validate`` flipped to False, unnoticed.

        The two fixtures BRACKET 0.70 (0.680 and 0.748), so the threshold is
        pinned from both sides: lowering the body's comparison to 0.05 accepts
        the first, raising it to 0.90 rejects the second. Pinning the constant
        alone did neither -- the constant stayed 0.70 while the body read any
        value it liked.

        Note this gate is dead weight on a REAL step-2 result: a checksum match
        is byte-equal to the entry's canonical sequence and scores 1.0 by
        construction. It only screens a hand-stubbed accession like this one.
        Uniqueness is what actually protects the step-2 path.
        """
        pdb = tmp_path / "no_reference_records.pdb"
        pdb.write_text("END\n")
        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(
            epitope_db, "_extract_chain_sequence", lambda *a, **k: ([1], _P00698_SEQ)
        )
        monkeypatch.setattr(
            epitope_db, "_search_uniprot_by_sequence", lambda seq: "P00698"
        )
        monkeypatch.setattr(
            epitope_db, "_fetch_uniprot_metadata",
            lambda acc: {"protein_name": "Lysozyme C", "sequence": meta_sequence},
        )

        # Keep the fixtures honest: if difflib ever scores these differently
        # they stop bracketing the gate and the test above silently weakens.
        identity = epitope_db._sequence_identity(meta_sequence, _P00698_SEQ)
        assert (identity < 0.70) == (expected == ""), (
            f"fixture no longer brackets the gate: scored {identity:.3f}"
        )
        assert epitope_db.resolve_uniprot_id(pdb, "A")["uniprot_id"] == expected
        assert epitope_db._MIN_VALIDATION_IDENTITY == 0.70

    def test_a_sequence_search_hit_is_dropped_when_it_cannot_be_validated(
        self, tmp_path, monkeypatch
    ):
        """``must_validate=True`` is what separates step 2 from step 1.

        A DBREF accession is depositor-annotated and is accepted even when
        UniProt is unreachable; a sequence-search guess has no such standing and
        must be dropped. The mismatched-sequence test above cannot see this --
        the identity check runs whenever a sequence is present, so it fires with
        ``must_validate`` either way. Only an EMPTY metadata sequence, i.e. the
        API being down, reaches the branch.
        """
        pdb = tmp_path / "no_reference_records.pdb"
        pdb.write_text("END\n")
        monkeypatch.setattr(epitope_db.requests, "get", _no_network)
        monkeypatch.setattr(
            epitope_db, "_extract_chain_sequence", lambda *a, **k: ([1], _P00698_SEQ)
        )
        monkeypatch.setattr(
            epitope_db, "_search_uniprot_by_sequence", lambda seq: "P00698"
        )
        monkeypatch.setattr(
            epitope_db, "_fetch_uniprot_metadata",
            lambda acc: {"protein_name": "Lysozyme C", "sequence": ""},
        )

        result = epitope_db.resolve_uniprot_id(pdb, "A")
        assert result["uniprot_id"] == "", (
            "an unvalidatable sequence-search guess must not be accepted"
        )


# ---------------------------------------------------------------------------
# 5. The capability itself
# ---------------------------------------------------------------------------


class TestLiveCapability:
    """The only checks that catch UniProt changing shape underneath us.

    Opt-in because the suite is otherwise hermetic and a network flake must not
    turn the build red.
    """

    @pytest.mark.skipif(
        os.environ.get("SCOUT_UNIPROT_LIVE") != "1",
        reason="set SCOUT_UNIPROT_LIVE=1 to check the real UniProt endpoint",
    )
    def test_the_live_endpoint_resolves_an_unambiguous_sequence(self):
        """The assertion the old code could never have passed: it returned ""
        for this exact sequence."""
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00698"

    @pytest.mark.skipif(
        os.environ.get("SCOUT_UNIPROT_LIVE") != "1",
        reason="set SCOUT_UNIPROT_LIVE=1 to check the real UniProt endpoint",
    )
    def test_the_live_ambiguous_sequence_is_still_refused(self):
        """Pairs with the test above rather than standing alone.

        On its own this would pass for the wrong reason -- a dead endpoint also
        returns "". It is the unambiguous test going green at the same time that
        makes this one mean "refused because ambiguous".
        """
        assert epitope_db._search_uniprot_by_sequence(_P68871_SEQ) == ""

    @pytest.mark.skipif(
        os.environ.get("SCOUT_UNIPROT_LIVE") != "1",
        reason="set SCOUT_UNIPROT_LIVE=1 to check the real UniProt endpoint",
    )
    def test_a_reviewed_filter_would_assert_the_wrong_organism(self):
        """The guard for the one regression a fake cannot catch.

        ``reviewed:true``/``fragment:false`` read as precision improvements and
        are the opposite: they delete the sibling entries that MAKE a sequence
        ambiguous. A scripted fake is blind to it -- the rows are whatever the
        fake serves -- so re-adding a filter only shows up against the real
        index.

        Chimpanzee VHL is the demonstration. Filtered, UniProt reports one hit
        and it is the HUMAN entry; unfiltered it reports the tie. The second
        assertion is what stops this passing for the wrong reason: without it a
        dead endpoint would satisfy the first.
        """
        assert epitope_db._search_uniprot_by_sequence(_K7BID8_SEQ) == "", (
            "a sequence five organisms share must be refused"
        )

        checksum = crc64(_K7BID8_SEQ).replace("CRC-", "")
        resp = requests.get(
            epitope_db.UNIPROTKB_SEARCH_URL,
            params={
                "query": (
                    f"(checksum:{checksum}) AND (reviewed:true) "
                    "AND (fragment:false)"
                ),
                "format": "json",
                "size": "2",
                "fields": "accession",
            },
            timeout=epitope_db._REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", [])
        assert [r["primaryAccession"] for r in rows] == ["P40337"], (
            "the filtered query no longer demonstrates the trap; if UniProt's "
            "curation changed, find another shared sequence rather than "
            "deleting this guard"
        )
