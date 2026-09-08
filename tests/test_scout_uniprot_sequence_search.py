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
uploads. Enumerating which files those are keeps going stale, so this file no
longer tries. Among the reversals: the mmCIF file-level accession fallback was
removed, sending chains that no ``_struct_ref_seq`` row names here; the
two-line ``DBREF1``/``DBREF2`` form went from unreadable at step 1 to readable;
and the 8-char DBREF column stopped truncating wide accessions, so files that
reached step 2 on an A0A... accession resolve at step 1 now. This sentence used
to carry a count of how often it had been wrong, and the count went stale the
same way, so it no longer carries one. Those users silently got no protein name
and no identity. A previous audit edited the inside of this function --
removing a no-op ``idmapping/run`` POST, ``bd8442f`` -- without noticing it
never returned anything, because nothing asserted that it could.

The replacement asks UniProtKB for entries whose CRC64 checksum equals the
sequence's (``checksum:`` is an indexed field; free text is indexed too, it
just does not index SEQUENCES) and accepts a lone entry, or a tie whose members
all name the SAME organism.

Design of the guards below, in order of what they would have caught:

1. The request must be a checksum query carrying the real CRC64 of the sequence
   it was called with. Two different sequences are sent and each checksum is
   recomputed here, so a free-text revert and a hardcoded checksum both fail.
2. Raw residues must never appear in ANY outgoing request component again --
   the scan covers the whole kwargs blob and the URL, not just ``params``.
3. The endpoint constant must stay the live UniProtKB search API. The sibling
   SAbDab feature died from a retired URL that no test pinned.
4. A match ambiguous ABOUT ORGANISM must return "". This is the finding that
   forced the design: one sequence carried by several species' entries
   produced a confident wrong answer the caller's identity gate cannot catch,
   because identical sequences always score 100%.
4a. A tie whose members all name ONE organism must RESOLVE. Strict uniqueness
   refused those too, and they carry none of the risk -- if nothing in the tie
   disagrees about organism, there is no organism to be wrong about. It cost
   36.5% of previously correct answers over random reviewed human entries and
   40.5% over named therapeutic targets; the same-organism share of that is
   34% and 47%. The guards come in pairs (resolve / still refuse) because
   either half alone is satisfied by a constant.
   The near-miss is guarded by NAME: "prefer the reviewed entry when the tie
   has exactly one reviewed member" reads as the same idea and is
   ``reviewed:true`` respelled -- it returns human P40337 for a chimpanzee VHL
   chain, and all 35 measured losses have exactly one reviewed member. The two
   tie fixtures carry the same reviewed count and resolve differently, so an
   implementation keyed on that count cannot pass both. They are not matched
   on everything else -- five rows against two -- so a tie-SIZE rule passes
   the pair and is killed by the page-boundary tests instead.
   Comparing organisms needs the WHOLE tie, which makes ``size`` load-bearing
   in a second way: a body capped by ``size`` has members that were never
   looked at, and an unseen member is the one that could disagree. Both sides
   of that boundary are tested, because "refuse whenever the page is full"
   and "accept the first page" each pass one side.
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
5. The ambiguity decision comes from the response's total count, not from
   the number of rows returned. An earlier draft inferred it from
   ``len(results)``, which ``size`` caps -- so setting ``size=1`` silently made
   every ambiguous match look unique and accepted the wrong species again.
   One exception, and it is guarded: a reply carrying NO total leaves the rows
   as the only evidence, so a page that did not fill is taken as the whole tie
   and a page that did fill is refused as possibly truncated.
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
# Hand-written minimal bodies in the shape the code reads:
# ``results[].primaryAccession`` and ``results[].organism.scientificName``,
# plus the ``x-total-results`` header. The live reply carries more --
# ``extraAttributes.uniParcId``, the organism's ``commonName`` and ``lineage``
# -- left out rather than half-copied, so nothing here claims to be a verbatim
# capture. ``entryType`` IS carried even though the code never reads it,
# because the tie fixtures exist partly to prove a reviewed-preference rule
# was NOT implemented, and that rule needs the field to be available.

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

# Human transthyretin, 147 aa. The live SAME-ORGANISM tie: UniProt carries
# exactly two entries for this sequence, reviewed P02766 and unreviewed
# E9KL36, and BOTH are Homo sapiens (measured 2026-09-04). Used only by
# TestLiveCapability -- the hermetic tie fixtures are HER2, because what those
# tests need is the row shape, not a sequence the real index agrees with.
_P02766_SEQ = (
    "MASHRLLLLCLAGLVFVSEAGPTGTGESKCPLMVKVLDAVRGSPAINVAVHVFRKAADDTWEPFASGK"
    "TSESGELHGLTTEEEFVEGIYKVEIDTKSYWKALGISPFHEHAEVVFTANDSGPRRYTIAALLSPYSY"
    "STTAVVTNPKE"
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
# Bonobo FIRST, deliberately: a fixture ordered human-first would let "just
# take the first row" return the CORRECT accession, and the ambiguity guards
# would pass a broken implementation. That ordering is a fixture CHOICE, not a
# transcription. At ``size=1`` the live API does return P68872 first (measured,
# stable over repeats) -- and the shipped request DID ask for one row until
# #220 replaced the free-text lookup, which is exactly what made that ordering
# load-bearing. At the size=2 that followed and the current size=25 the live
# order is human-first. The live tie is also four entries, not three -- D9YZU5, an
# unreviewed Homo sapiens duplicate.
# Each row carries the organism the live entry carries, because the refusal is
# now DECIDED on that field. Without it these rows would still be refused --
# every organism reads as "" and an all-empty set fails closed -- so the tests
# below would pass while proving nothing about the comparison they name.
_THREE_HITS = [
    {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P68872",
     "organism": {"scientificName": "Pan paniscus", "taxonId": 9597}},
    {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P68871",
     "organism": {"scientificName": "Homo sapiens", "taxonId": 9606}},
    {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P68873",
     "organism": {"scientificName": "Pan troglodytes", "taxonId": 9598}},
]

# A SAME-organism tie: HER2/ERBB2, the reviewed human entry plus one unreviewed
# human duplicate. Membership, organisms and entry types are as measured
# 2026-09-04 (x-total-results: 2); the row shape is trimmed to the fields the
# code reads, like the payloads above, so this is not a verbatim capture.
# Nothing here disagrees about organism, so there is no organism to be wrong
# about and the tie resolves.
_SAME_ORGANISM_TIE = [
    {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P04626",
     "organism": {"scientificName": "Homo sapiens", "taxonId": 9606}},
    {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "X5DNK3",
     "organism": {"scientificName": "Homo sapiens", "taxonId": 9606}},
]

# The five entries that carry _K7BID8_SEQ, measured 2026-09-04. This is the
# adversarial fixture for the alternative that must NOT be implemented:
# "prefer the reviewed entry when the tie has exactly one reviewed member".
# Exactly one row here is reviewed -- and it is the HUMAN one, for a chimpanzee
# chain. ``entryType`` is present precisely so that rule is AVAILABLE to the
# implementation; a fixture without it would make the alternative merely
# impossible to write rather than demonstrably wrong.
_VHL_TIE = [
    {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P40337",
     "organism": {"scientificName": "Homo sapiens", "taxonId": 9606}},
    {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "A0A2R9B5S8",
     "organism": {"scientificName": "Pan paniscus", "taxonId": 9597}},
    {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "K7BID8",
     "organism": {"scientificName": "Pan troglodytes", "taxonId": 9598}},
    {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "A0A024R2F2",
     "organism": {"scientificName": "Homo sapiens", "taxonId": 9606}},
    {"entryType": "UniProtKB unreviewed (TrEMBL)", "primaryAccession": "A0A2I3SEM6",
     "organism": {"scientificName": "Pan troglodytes", "taxonId": 9598}},
]


# One organism, spelled consistently in BOTH fields. An earlier version took
# an ``organism`` argument while hardcoding ``taxonId: 9606``, so any caller
# passing a different name produced rows that were two organisms by name and
# one by taxon id -- a fixture that silently disagrees with itself depending
# on which field the implementation reads.
_HUMAN = {"scientificName": "Homo sapiens", "taxonId": 9606}
_CHIMP = {"scientificName": "Pan troglodytes", "taxonId": 9598}


def _same_organism_rows(n):
    """``n`` distinct, well-formed accessions that all name ONE organism.

    Used for the page-boundary tests, where what matters is the row COUNT
    against ``_MAX_TIE_ROWS``. The accessions are synthetic but must satisfy
    ``_valid_accession``, because the first one is what a resolving call
    returns.
    """
    return [
        {"entryType": "UniProtKB unreviewed (TrEMBL)",
         "primaryAccession": f"P0{i:03d}1",
         "organism": dict(_HUMAN)}
        for i in range(n)
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
        # Unpinned, dropping this returns whole entries instead of two fields:
        # correctness is unchanged, the response is orders of magnitude bigger,
        # on an anonymous request path. ``organism_name`` is load-bearing in the
        # other direction -- it is what resolves a same-organism tie -- and is
        # asserted here rather than only in the behavioural tests because the
        # fake serves whatever rows it is handed, so dropping the field from the
        # WIRE would leave every one of them green.
        #
        # Membership, not whole-string equality. The denylist argument that
        # justifies pinning the whole ``query`` does NOT transfer: a field is
        # not a predicate, so an added one cannot subset the result set. Whole
        # -string equality here would forbid reordering, and forbid adding
        # ``protein_name`` -- which would let the caller skip a round trip.
        # The bound keeps the response-size concern the paragraph above states.
        fields = set(calls[0]["params"]["fields"].split(","))
        assert {"accession", "organism_name"} <= fields
        assert len(fields) <= 3, "fields is growing towards whole entries"

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
        mechanism either -- agreement about organism is; the floor only
        declines to ask about peptides too short for a whole-sequence match to
        mean anything.
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

    def test_the_page_asks_for_a_whole_tie(self, captured):
        """Belt and braces for the test above, and for the organism comparison.

        If the header ever disappears the fallback is ``len(results)``, and a
        page that filled is refused rather than trusted. The comparison
        needs more than that: organism is compared ACROSS a tie's members, so
        the page has to be able to hold one. Asserting the CONSTANT rather than
        a floor of 2 is what stops the request quietly reverting to ``size=2``
        while ``_MAX_TIE_ROWS`` still reads 25.
        """
        calls = captured(_ONE_HIT)
        epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        assert calls[0]["params"]["size"] == str(epitope_db._MAX_TIE_ROWS)
        assert epitope_db._MAX_TIE_ROWS >= 2

    def test_a_genuine_miss_is_empty(self, captured, caplog):
        """A de-novo design has no UniProt entry, so "" is correct.

        The caplog half is what makes the "" mean something. Delete the
        ``total < 1`` guard in production and this test still passed:
        ``results[0]`` on an empty list raises ``IndexError``, the blanket
        ``except Exception`` turns it into "", and the assertion above cannot
        tell a deliberate refusal from a crash that was swallowed -- which is
        the exact silent-death shape this whole file was written against.
        """
        calls = captured([])
        design = ("MSEEELKKLAEELKKKAEELKKKSEEELKKLAEEAKKKAEELKKKSEEELKKLAEEL"
                  "KKKAEELKKKSEEELKKLAEEAKKKAEELKKK")
        with caplog.at_level("DEBUG", logger=epitope_db.logger.name):
            assert epitope_db._search_uniprot_by_sequence(design) == ""
        assert calls, '"" must mean "asked and found nothing", not "never asked"'
        assert not [
            r for r in caplog.records
            if "UniProt sequence search failed" in r.getMessage()
        ], "a miss must be a decision, not an exception that was swallowed"

    def test_a_zero_total_beside_rows_is_refused_and_says_so(
        self, captured, caplog
    ):
        """A header of 0 with rows in the body is a DISAGREEMENT, not a miss.

        Both halves are asserted because the guard that distinguishes them was
        unconstrained in both directions when it was added: ``if results:``,
        ``if True:``, ``if not results:`` and ``if False:`` all passed the
        file, since nothing served ``total=0`` with a non-empty body and the
        genuine-miss test filtered on a different message.

        Refusing is the safe half and was never in doubt; what needed pinning
        is that the operator is TOLD, because a silent "" here reads in a log
        exactly like "this protein is not in UniProt".
        """
        calls = captured(_SAME_ORGANISM_TIE, total=0)
        with caplog.at_level("INFO", logger=epitope_db.logger.name):
            assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""
        assert calls
        assert any(
            "returned" in r.getMessage() and "total of 0" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_genuine_miss_stays_quiet(self, captured, caplog):
        """The other direction of the same guard.

        Inverting it to ``if not results:`` logs "reported a total of 0 but
        returned 0 rows" on an ordinary miss -- a de-novo design, the common
        case -- which is precisely the misleading line the guard was added to
        avoid. Without this, that inversion passes.
        """
        captured([])
        design = ("MSEEELKKLAEELKKKAEELKKKSEEELKKLAEEAKKKAEELKKKSEEELKKLAEEL"
                  "KKKAEELKKKSEEELKKLAEEAKKKAEELKKK")
        with caplog.at_level("INFO", logger=epitope_db.logger.name):
            assert epitope_db._search_uniprot_by_sequence(design) == ""
        assert not [
            r for r in caplog.records if "total of" in r.getMessage()
        ], "a plain miss must not be reported as a header/body disagreement"

    def test_the_row_cap_cannot_exceed_what_uniprot_accepts(self):
        """``size`` above 500 is HTTP 400, which this function reads as "no
        match" -- so an over-large cap does not widen the net, it kills every
        lookup. Verified live: 500 resolves, 501 answers nothing.

        What this pins is the VALUE 500, which the module-level assert cannot:
        that assert bounds _MAX_TIE_ROWS against the ceiling, so moving the
        ceiling moves the bound with it and stays true. Restating the
        inequality here would add nothing -- the assert runs at import, so a
        false one errors collection before any test body runs.
        """
        assert epitope_db._MAX_TIE_ROWS_CEILING == 500

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
# 3b. Resolving a tie that has nothing to disagree about
# ---------------------------------------------------------------------------


class TestASameOrganismTieResolves:
    """Uniqueness across all of UniProtKB was broader than the risk it manages.

    The risk is a confident WRONG ORGANISM: one sequence carried by several
    species' entries, returned as one of them at "100.0% identity". When every
    tied entry names the same organism there is no organism to be wrong about,
    and refusing costs recall for nothing -- measured at 36.5% of previously
    correct answers over random reviewed human entries and 40.5% over named
    therapeutic targets, of which the same-organism case is 34% and 47%.

    Either the rows are all in hand or the tie is refused. That is what makes
    the comparison evidence rather than a sample: ``size`` caps the body, so a
    tie larger than one page is refused unseen rather than judged on its first
    ``_MAX_TIE_ROWS`` members.
    """

    def test_a_same_organism_tie_resolves(self, captured):
        """The recovery, and the headline of this change.

        Its paired negatives -- without which "accept every tie" would pass
        this one -- are the reviewed-preference test and the
        buried-disagreement test below.

        Two entries, both Homo sapiens: the reviewed HER2 record and one
        unreviewed human duplicate. Nothing in the tie disagrees about
        organism, so the tie is not evidence of ambiguity about the only thing
        the refusal protects.
        """
        calls = captured(_SAME_ORGANISM_TIE)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P04626"
        assert calls, "a resolution must still be an ANSWER, not a skipped call"

    def test_a_tie_disagreeing_only_in_its_LAST_member_is_refused(
        self, captured
    ):
        """Every other multi-organism fixture in this file disagrees at row 1.

        That made the whole suite satisfiable by comparing a PREFIX of the
        rows. All of these passed every other test here:

            organisms = {org(r) for r in results[:2]}
            organisms = {org(r) for r in results[:3]}
            if org(results[0]) != org(results[1]): return ""

        and each is the production bug verbatim -- a tie whose first rows are
        human and whose last row is Pan resolves to a human accession for a
        chimpanzee chain at "100.0% identity". A prefix is not the tie.

        The disagreeing member is deliberately LAST in a long run: merely
        reordering a five-row fixture kills ``[:2]`` but leaves ``[:3]``
        alive. This is also what makes the class docstring's claim -- that
        the comparison is evidence and not a sample -- true rather than
        asserted.
        """
        rows = _same_organism_rows(epitope_db._MAX_TIE_ROWS - 1) + [
            {"entryType": "UniProtKB unreviewed (TrEMBL)",
             "primaryAccession": "K7BID8", "organism": dict(_CHIMP)},
        ]
        assert len(rows) == epitope_db._MAX_TIE_ROWS
        calls = captured(rows, total=epitope_db._MAX_TIE_ROWS)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "", (
            "a tie was resolved without reading all of it -- the one member "
            "that disagrees is the last one"
        )
        assert calls

    def test_the_reviewed_preference_alternative_is_not_what_shipped(
        self, captured
    ):
        """The near-miss that looks like this fix and is the OLD BUG.

        "Prefer the reviewed entry when the tie has exactly one reviewed
        member" resolves the same losses -- all 35 measured ones have exactly
        one reviewed member -- and is ``reviewed:true`` in another spelling,
        because what that filter does is delete the unreviewed siblings which
        constitute the tie.

        Chimpanzee VHL is the demonstration, hermetically. Five entries, three
        organisms, exactly ONE of them reviewed: human P40337. The reviewed
        rule returns it for a chimpanzee chain at "100.0% identity", which is
        verbatim the failure the refusal exists to prevent. ``entryType`` is in
        the fixture so that rule is AVAILABLE to the implementation; what is
        asserted here is that it was not taken.
        """
        calls = captured(_VHL_TIE)
        got = epitope_db._search_uniprot_by_sequence(_K7BID8_SEQ)

        assert got != "P40337", (
            "returned the lone REVIEWED entry of a three-organism tie -- that "
            "is the reviewed:true filter wearing a hat, and it asserts a human "
            "accession for a chimpanzee chain"
        )
        assert got == "", "a tie spanning three organisms must be refused"
        assert calls, '"" must mean "asked and refused", not "never asked"'

    def test_the_reviewed_count_is_not_what_makes_a_tie_resolvable(
        self, captured
    ):
        """Isolates the variable the test above confounds.

        ``_VHL_TIE`` and ``_SAME_ORGANISM_TIE`` BOTH have exactly one reviewed
        member and they resolve differently, so no implementation keyed on the
        reviewed count can satisfy both.

        They are NOT matched on everything else: the VHL tie has five rows to
        HER2's two, so "resolve ties of at most N members" also passes this
        pair. That mutant dies elsewhere -- on the 25-row tie that resolves,
        and on the 25-row tie whose last member disagrees -- not here.
        """
        def _lone_reviewed(rows):
            return sum(
                1 for r in rows
                if r["entryType"] == "UniProtKB reviewed (Swiss-Prot)"
            ) == 1

        assert _lone_reviewed(_VHL_TIE) and _lone_reviewed(_SAME_ORGANISM_TIE)

        captured(_SAME_ORGANISM_TIE)
        resolved = epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        captured(_VHL_TIE)
        refused = epitope_db._search_uniprot_by_sequence(_K7BID8_SEQ)

        assert (resolved, refused) == ("P04626", ""), (
            "same reviewed count, opposite outcomes -- organism is the "
            "discriminator, curation status is not"
        )

    def test_the_reviewed_member_wins_whatever_row_it_arrives_in(
        self, captured
    ):
        """The tie-break, isolated from UniProt's ordering.

        Every live tie probed so far returns the reviewed row first, so
        ``test_a_same_organism_tie_resolves`` above is satisfied by a bare
        ``results[0]`` and passes with or without the pick. Reversing the
        fixture is what separates them: only an implementation that reads
        ``entryType`` still answers P04626.

        This is a tie-break among rows the organism comparison has ALREADY
        accepted, not a reason to accept. ``_VHL_TIE`` is the paired negative
        -- one reviewed member, three organisms, still refused -- and it is
        what goes red if this pick is ever hoisted above that comparison.
        """
        captured(list(reversed(_SAME_ORGANISM_TIE)))
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P04626"

    def test_a_tie_with_no_reviewed_member_still_resolves(self, captured):
        """The fallback half of the pick, which has its own way to be wrong.

        A pick written as ``[r for r in results if reviewed][0]`` raises here,
        and one defaulting to ``None`` returns "" -- either way a resolvable
        single-organism tie becomes a silent miss. Two unreviewed duplicates
        and nothing curated is a shape this function must not choke on. All
        nine probed ties carry exactly one reviewed member, so a zero-reviewed
        tie is unmeasured rather than known to occur -- which is a reason to
        pin the fallback, not to assume it unreachable.
        """
        rows = _same_organism_rows(2)
        assert not any(
            r["entryType"] == "UniProtKB reviewed (Swiss-Prot)" for r in rows
        ), "the fixture must hold nothing for the pick to find"

        captured(rows)
        assert (epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
                == rows[0]["primaryAccession"])

    def test_a_tie_with_no_organism_field_fails_closed(self, captured):
        """Dropping ``organism_name`` must cost recall, never correctness.

        If the field ever leaves the ``fields=`` parameter, every row arrives
        without an organism. The resulting set must read as "cannot tell",
        which refuses -- the behaviour that shipped before organisms were
        compared -- and NOT as "one organism, and it is the empty string",
        which would accept every tie in UniProtKB.
        """
        blind = [{"primaryAccession": r["primaryAccession"]} for r in _VHL_TIE]
        calls = captured(blind)
        assert epitope_db._search_uniprot_by_sequence(_K7BID8_SEQ) == ""
        assert calls

    @pytest.mark.parametrize(
        ("name", "label"),
        [
            ("", "empty string"),
            (" ", "a single space"),
            ("\t\n", "tab and newline"),
            ("   ", "three spaces"),
            (9606, "a non-string (the taxon id)"),
            (None, "None"),
        ],
    )
    def test_an_organism_that_is_not_a_NAME_is_not_one_organism(
        self, captured, caplog, name, label
    ):
        """A one-element set is not agreement if the element is not a name.

        Every value here collapses ``{scientificName}`` to a single-member
        set, so a bare ``len(organisms) != 1`` accepts all of them. Bare
        FALSINESS closes only two of the six -- `""` and `None`; `" "`,
        `"\t\n"`, `"   "` and the integer are all truthy. Parametrising is the point -- with only
        the empty-string case, dropping ``.strip()`` and dropping the
        ``isinstance`` both survived the entire file (measured, round 2), so
        the normalisation looked tested and was not.

        The integer case matters beyond tidiness: ``taxonId`` sits in the same
        payload, so "compare the taxon id instead" is a real edit someone
        might make halfway, leaving a number in the name slot.
        """
        rows = [
            {"entryType": r["entryType"],
             "primaryAccession": r["primaryAccession"],
             "organism": {"scientificName": name}}
            for r in _SAME_ORGANISM_TIE
        ]
        calls = captured(rows)
        with caplog.at_level("DEBUG", logger=epitope_db.logger.name):
            got = epitope_db._search_uniprot_by_sequence(_P00698_SEQ)
        assert got == "", f"a tie whose organism is {label} was resolved"
        assert calls

        # The return value alone cannot tell a DECISION from a swallowed
        # exception, and for the two non-string cases that distinction is the
        # whole test: drop the ``isinstance`` and they still return "", via
        # AttributeError into the blanket except. Asserting the refusal
        # message is what kills that mutant.
        messages = [r.getMessage() for r in caplog.records]
        assert any("names an organism" in m for m in messages), (
            f"organism {label} was refused by accident, not by the guard: "
            f"{messages}"
        )

    def test_a_lone_hit_needs_no_organism(self, captured):
        """A single entry is not a tie, so the comparison does not apply.

        There is nothing to disagree with it, and requiring an organism here
        would refuse entries with thin metadata for no gain in correctness.

        The row carries ``"organism": None`` rather than simply omitting the
        key so that this is not an exact copy of
        ``TestTheAccessionIsResolved.test_exactly_one_hit_resolves``, which an
        earlier version was, adding no coverage at all.

        It does NOT reach the ``or {}`` in the production comprehension, and an
        earlier draft of this docstring wrongly claimed it was the only input
        that did: a lone hit never enters the ``total > 1`` block at all.
        ``test_a_tie_with_no_organism_field_fails_closed`` is what exercises
        that expression, by omitting the key on a TIE.
        """
        captured([{"primaryAccession": "P00698", "organism": None}])
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00698"

    def test_a_tie_that_exactly_fills_the_page_still_resolves(self, captured):
        """The permissive side of the page boundary.

        ``_MAX_TIE_ROWS`` rows with a header total that AGREES means the whole
        tie was seen. Without this, "refuse whenever the page is full" passes
        every other test in this class while silently capping recall one short.
        """
        rows = _same_organism_rows(epitope_db._MAX_TIE_ROWS)
        captured(rows, total=epitope_db._MAX_TIE_ROWS)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00001"

    def test_a_tie_larger_than_the_page_is_refused_unseen(self, captured):
        """The restrictive side, and the reason the cap is not arbitrary.

        The body is capped by ``size``; the header is not. A total above the
        rows returned means some member was never looked at, and an unseen
        member is exactly the one that could name a different organism -- so
        "every row I fetched says Homo sapiens" is not evidence about the tie.
        Live histone H4 is this case: 845 entries, of which the first 500
        rows alone span 480 organisms.

        This lands on the general ``total != len(results)`` branch, which
        ``test_ambiguity_is_decided_by_the_total_not_the_row_count`` also
        reaches; it is here for the size the cap makes reachable, not because
        the branch is otherwise untested. Its sibling above -- the tie that
        exactly fills the page -- is the load-bearing half of the pair, being
        the only thing that kills "refuse whenever the page is full".
        """
        rows = _same_organism_rows(epitope_db._MAX_TIE_ROWS)
        calls = captured(rows, total=epitope_db._MAX_TIE_ROWS + 1)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""
        assert calls

    def test_a_full_page_with_no_header_is_refused(self, captured):
        """Both caps at once, which is the case neither test above reaches.

        With no usable header the fallback counts ROWS, and a full page is
        indistinguishable from a truncated one. Reading it as a complete tie
        would accept an 845-way cross-organism tie on its first 25 members.
        """
        rows = _same_organism_rows(epitope_db._MAX_TIE_ROWS)
        calls = captured(rows, total=_NO_HEADER)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == ""
        assert calls

    def test_a_short_page_with_no_header_still_resolves(self, captured):
        """The other side of that one: a page that did NOT fill is complete.

        Without this, "refuse whenever the header is missing" passes the test
        above and the row-count fallback quietly becomes dead code.
        """
        captured(_SAME_ORGANISM_TIE, total=_NO_HEADER)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P04626"

    def test_one_row_short_of_the_cap_with_no_header_still_resolves(
        self, captured
    ):
        """Pins the no-header threshold to ``>= _MAX_TIE_ROWS`` exactly.

        The refusing side of this boundary is tested at 25 rows and the
        resolving side, above, at 2 -- leaving 23 rows of slack in which the
        threshold could sit. Measured by sweeping it: the guard could be
        lowered anywhere from 25 down to 3 with the whole file still green.
        24 rows is the value that closes the gap.
        """
        rows = _same_organism_rows(epitope_db._MAX_TIE_ROWS - 1)
        captured(rows, total=_NO_HEADER)
        assert epitope_db._search_uniprot_by_sequence(_P00698_SEQ) == "P00001"

    def test_the_row_cap_is_the_shipped_value(self):
        """Pinned so a silent drift is visible in review.

        25 is a judgement backed by measurement, not a derivation: all nine
        same-organism ties PROBED against the live API on 2026-09-04 are TWO
        entries -- a probe set, not a census, so this bounds the observed
        cases and not every case -- and the ties that are genuinely large are
        the cross-species
        kind that stays refused whatever this is set to (NRAS 95 entries/91
        organisms; histone H4 845 entries, 480 distinct organisms in the first
        500 rows alone). Lowering it costs recall silently. Raising it mostly
        widens the page fetched for ties that refuse anyway -- but not only
        that: a same-organism tie larger than the current value would change
        from a refusal into an answer, which is a recall change and wants
        measuring rather than assuming.
        """
        assert epitope_db._MAX_TIE_ROWS == 25


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
        Agreement about organism is what actually protects the step-2 path.
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
    def test_the_live_same_organism_tie_resolves(self):
        """The recovery against the real index, which a fake cannot show.

        Transthyretin is a genuine two-entry tie in UniProtKB -- reviewed
        P02766 plus unreviewed E9KL36, both Homo sapiens -- so this asserts
        that a real tie really does resolve, and to the reviewed accession.
        The hermetic tests can only prove the decision logic on rows this
        suite invented; the tie itself, and UniProt's ordering of it, are
        facts about the live index.

        This test does NOT catch UniProt curating the duplicate away: the tie
        would become a lone hit, which still resolves to P02766 and still
        passes here. That degradation is caught by the tie-SIZE assertion in
        ``test_the_live_tie_fixtures_still_have_the_shape_they_claim``. What
        this one catches is a second ORGANISM acquiring the sequence, which
        must start refusing.
        """
        assert epitope_db._search_uniprot_by_sequence(_P02766_SEQ) == "P02766"

    @pytest.mark.skipif(
        os.environ.get("SCOUT_UNIPROT_LIVE") != "1",
        reason="set SCOUT_UNIPROT_LIVE=1 to check the real UniProt endpoint",
    )
    def test_the_live_tie_fixtures_still_have_the_shape_they_claim(self):
        """The hermetic fixtures are transcriptions of a live answer, and an
        out-of-date transcription proves nothing about production.

        Checks the properties the tests above actually rest on: the VHL tie
        still spans more than one organism with exactly one reviewed member
        (so it is still the adversarial case for reviewed-preference); the
        transthyretin tie is still a TIE and still spans one organism (so
        ``test_the_live_same_organism_tie_resolves`` is still exercising tie
        resolution rather than quietly degrading into a lone-hit test); and
        ``_SAME_ORGANISM_TIE``, the hermetic recovery fixture, still matches
        the live index it was transcribed from.

        The tie-SIZE assertion is the one that earns its keep. Checking only
        that transthyretin spans one organism is satisfied trivially by a
        single row, so if UniProt ever merged the duplicate away, every live
        test here would stay green while nothing proved a tie resolves.
        """
        def _live_rows(sequence):
            checksum = crc64(sequence).replace("CRC-", "")
            resp = requests.get(
                epitope_db.UNIPROTKB_SEARCH_URL,
                params={
                    "query": f"(checksum:{checksum})",
                    "format": "json",
                    "size": str(epitope_db._MAX_TIE_ROWS),
                    "fields": "accession,organism_name",
                },
                timeout=epitope_db._REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

        vhl = _live_rows(_K7BID8_SEQ)
        organisms = {r["organism"]["scientificName"] for r in vhl}
        reviewed = [
            r for r in vhl
            if r["entryType"] == "UniProtKB reviewed (Swiss-Prot)"
        ]
        assert len(organisms) > 1, (
            "the VHL tie no longer spans organisms, so it no longer "
            "demonstrates the reviewed-preference trap -- find another "
            "shared sequence rather than deleting the guard"
        )
        assert len(reviewed) == 1 and reviewed[0]["primaryAccession"] == "P40337", (
            "the VHL tie no longer has exactly one reviewed member, and that "
            "member human -- which is what made reviewed-preference return a "
            "human accession for a chimpanzee chain"
        )

        ttr = _live_rows(_P02766_SEQ)
        assert len(ttr) > 1, (
            "transthyretin is no longer a TIE -- the live same-organism test "
            "still passes on a lone hit, so without this nothing would prove "
            "that a real tie resolves; pick another two-entry same-organism "
            "sequence rather than deleting this guard"
        )
        assert len({r["organism"]["scientificName"] for r in ttr}) == 1, (
            "the transthyretin tie now spans organisms, so it is no longer a "
            "same-organism recovery case and must start refusing"
        )

        # _SAME_ORGANISM_TIE is a transcription of the live HER2 answer and
        # nothing else in the suite would notice it drifting. Its sequence is
        # 1255 aa, too bulky to embed for one assertion, so it is fetched.
        # raise_for_status is load-bearing: without it a 503 yields an
        # EMPTY sequence, whose checksum is a well-formed query that answers
        # 200 with zero rows -- so an outage would be reported below as
        # "_SAME_ORGANISM_TIE no longer matches the live HER2 answer", which
        # is fixture drift, not downtime.
        fasta = requests.get(
            "https://rest.uniprot.org/uniprotkb/P04626.fasta",
            timeout=epitope_db._REQUEST_TIMEOUT_SEC,
        )
        fasta.raise_for_status()
        her2_seq = "".join(fasta.text.splitlines()[1:])
        assert len(her2_seq) > 1000, "P04626 FASTA came back truncated"
        her2 = _live_rows(her2_seq)
        assert [r["primaryAccession"] for r in her2] == [
            r["primaryAccession"] for r in _SAME_ORGANISM_TIE
        ], "_SAME_ORGANISM_TIE no longer matches the live HER2 answer"
        assert len({r["organism"]["scientificName"] for r in her2}) == 1

    @pytest.mark.skipif(
        os.environ.get("SCOUT_UNIPROT_LIVE") != "1",
        reason="set SCOUT_UNIPROT_LIVE=1 to check the real UniProt endpoint",
    )
    def test_the_shipped_fields_value_still_carries_entryType(self):
        """The reviewed pick reads a field the request does not ask for.

        ``entryType`` cannot simply be requested. It is not a valid ``fields``
        value, and naming it answers HTTP 400 -- which
        ``_search_uniprot_by_sequence`` reports as "no match", so that
        "hardening" would kill every lookup rather than secure one. It arrives
        on every row regardless.

        That leaves a coupling to UniProt's default payload, and it fails
        SILENTLY: if entryType ever stops arriving, the pick reverts to row 0
        -- the ordering dependency it exists to remove -- with no exception
        and no hermetic failure, because every fixture in this file hard-codes
        the field. Only the live index can notice.

        Both directions are asserted. The second is what stops a future reader
        "fixing" the first by adding entryType to the request.
        """
        params = {
            "query": "(checksum:%s)" % crc64(_P02766_SEQ).replace("CRC-", ""),
            "format": "json",
            "size": str(epitope_db._MAX_TIE_ROWS),
        }
        shipped = requests.get(
            epitope_db.UNIPROTKB_SEARCH_URL,
            params=dict(params, fields="accession,organism_name"),
            timeout=epitope_db._REQUEST_TIMEOUT_SEC,
        )
        shipped.raise_for_status()
        rows = shipped.json().get("results", [])
        assert rows, "the transthyretin checksum answered nothing"
        assert all("entryType" in row for row in rows), (
            "UniProt stopped returning entryType for the shipped fields "
            "value, so the reviewed pick has silently reverted to row 0"
        )

        asked = requests.get(
            epitope_db.UNIPROTKB_SEARCH_URL,
            params=dict(params, fields="accession,organism_name,entryType"),
            timeout=epitope_db._REQUEST_TIMEOUT_SEC,
        )
        assert asked.status_code == 400, (
            "entryType became a valid fields value (HTTP %s); the comment on "
            "the request saying it must not be added is now wrong"
            % asked.status_code
        )

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
            "a sequence carried by five entries across three organisms must "
            "be refused"
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
