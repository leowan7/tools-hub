"""ONE shape answer for the per-candidate array, and one test for it.

``result["candidates"]``, ``result["designs"]`` and ``result["sequences"]``
are read by eleven functions across three modules. Each used to spell its own
``isinstance(x, list)``, so the eleven could disagree -- and every
disagreement was SILENT. A shape one reader accepted and another rejected did
not raise; it cost a wrong count, a wrong headline noun, a wrong failure_class
or a wrong persisted blob. ``shared.jobs.is_candidate_array`` is now the one
place THE ELEVEN answer it, and this file is where that is tested.
``shared/exports.py::_dict_candidates`` answers the same question separately
and on purpose -- ``shared.jobs.is_candidate_array``'s docstring records the
import that keeps it out. The negative sweep below WALKS that module but
cannot reach that function, so nothing here pins its answer;
``tests/test_malformed_candidate_row_render.py::
test_the_export_and_render_accessors_agree_by_value`` is what holds the two
level.

THE FIXTURES NEVER WRITE THE SAME ROWS UNDER BOTH KEYS, and that is the point
of them. ``candidate_records`` and ``candidate_count`` walk
``("candidates", "designs")`` in order: a reader narrowed back to ``list``
rejects a tuple under the first key and FALLS THROUGH to the second, so a
fixture carrying identical rows under both keys gets the right answer out of
the wrong branch and the mutation goes green. Every payload below therefore
carries one key, or two keys of DIFFERENT lengths.

What this file does NOT own:

* the EMPTY-array short-circuit -- an empty ``candidates`` stops the key
  search rather than falling through to ``designs``, for a tuple exactly as
  for a list. That is a KEY-ORDER decision, not a shape one;
  ``shared.jobs.candidate_count``'s docstring carries the reason and
  ``tests/test_malformed_candidate_row_render.py::
  test_a_tuple_container_is_counted_the_same_everywhere`` asserts it.
* key PREFERENCE (``candidates`` over ``designs``), held by
  ``test_export_shapes.py::test_candidates_preferred_when_both_keys_present``
  and ``test_campaign_passed_filters.py::
  test_candidates_preferred_over_designs_when_both_present``.
"""

from __future__ import annotations

import ast
import pathlib
import uuid

import pytest

from shared import email as email_mod
from shared.jobs import (
    ToolJob,
    _slim_result_for_persist,
    candidate_count,
    candidate_records,
    classify_terminal_state,
    display_rows,
    is_candidate_array,
    supports_headline_claim,
)

_REPO = pathlib.Path(__file__).resolve().parents[1]

# The array's three key spellings. Named here rather than inline so the
# negative AST sweep below and the prose above cannot drift apart.
_ARRAY_KEYS = ("candidates", "designs", "sequences")


def _job(**over) -> ToolJob:
    """A succeeded job row. ``from_row`` normalises, so the tuple payloads
    below are asserted to survive it before anything is read off them."""
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "bindcraft",
        "preset": "pilot",
        "status": "succeeded",
        "inputs": {},
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 10,
        "created_at": "2026-04-30T12:00:00Z",
        "started_at": None,
        "completed_at": "2026-04-30T12:00:05Z",
    }
    base.update(over)
    return ToolJob.from_row(base)


# ---------------------------------------------------------------------------
# 1. The predicate itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [[], [{}], (), ({},), [1, 2], (1, 2)])
def test_a_list_and_a_tuple_are_both_the_array(value):
    assert is_candidate_array(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"candidates": []},
        0,
        1,
        # A str/bytes IS a sequence, and every reader would iterate it into
        # characters or count its length as a design count.
        "candidates",
        b"candidates",
        # No stable order, and the array is indexed BY POSITION downstream.
        {1, 2},
        frozenset({1, 2}),
    ],
)
def test_nothing_else_is_the_array(value):
    assert is_candidate_array(value) is False


def test_a_generator_is_not_the_array():
    """A one-shot iterable would let whichever reader ran first consume the
    rows out from under every reader after it."""
    assert is_candidate_array(x for x in ()) is False


# ---------------------------------------------------------------------------
# 2. Every reader, over a list AND over a tuple
#
# Each case states the answer a list gets and asserts a tuple gets the same
# one. Narrowing that reader's gate back to ``list`` must change the tuple
# answer -- the "narrowed" comment on each case names what it changes to.
# ---------------------------------------------------------------------------

class TestJobsReaders:
    def test_candidate_records_keeps_every_row_in_position(self):
        """Never filter, never renumber: ``shared/storage.py`` stages the
        design at the index the user posted, so dropping row 1 would ship the
        wet lab the design from row 2."""
        rows = ({"pdb_key": "a.pdb"}, "not a mapping", {"pdb_key": "c.pdb"})
        recs = candidate_records({"candidates": rows})
        assert recs == list(rows)
        assert recs[2]["pdb_key"] == "c.pdb"
        # Copied to a list, because the return type is a list either way.
        assert type(recs) is list

    def test_candidate_records_hands_back_the_same_list(self):
        """A list comes back BY REFERENCE, as it did before the predicate
        landed -- dfba7e2 returned ``recs`` itself. Pinned because a plain
        ``return list(recs)`` passes every other assertion in this class."""
        rows = [{"pdb_key": "a.pdb"}]
        assert candidate_records({"candidates": rows}) is rows

    def test_candidate_records_reads_a_tuple_under_either_key(self):
        # Asymmetric on purpose: a narrowed reader rejects the 1-row tuple
        # under "candidates" and falls through to the 3-row "designs" list.
        both = {
            "candidates": ({"pdb_key": "a.pdb"},),
            "designs": [{"pdb_key": "x.pdb"}, {"pdb_key": "y.pdb"},
                        {"pdb_key": "z.pdb"}],
        }
        assert [r["pdb_key"] for r in candidate_records(both)] == ["a.pdb"]
        # And the designs-only shape, which has no second key to fall to.
        designs_only = {"designs": ({"pdb_key": "x.pdb"}, {"pdb_key": "y.pdb"})}
        assert len(candidate_records(designs_only)) == 2

    def test_candidate_count_counts_a_tuple(self):
        # narrowed: falls through to designs and answers 3.
        both = {
            "candidates": ({"pdb_key": "a.pdb"},),
            "designs": [{}, {}, {}],
        }
        assert candidate_count(both) == 1
        assert candidate_count({"designs": ({}, {})}) == 2
        # Still None for a shape that does not state a count at all.
        assert candidate_count({"something_else": (1, 2)}) is None

    def test_display_rows_coerces_a_tuple_without_renumbering(self):
        # narrowed: returns [].
        rows = ({"pdb_key": "a.pdb"}, "junk", {"pdb_key": "c.pdb"})
        out = display_rows(rows)
        assert len(out) == 3
        assert out[1] == {}
        assert out[2]["pdb_key"] == "c.pdb"

    def test_supports_headline_claim_holds_for_a_tuple(self):
        # narrowed: False, and the completion email drops its score callout.
        assert supports_headline_claim(
            {"candidates": ({"pdb_key": "a.pdb"},)}, "bindcraft"
        ) is True
        # The backfilled flag still overrides the shape.
        assert supports_headline_claim(
            {"candidates": ({"pdb_key": "a.pdb"},), "backfilled": True},
            "bindcraft",
        ) is False

    def test_zero_yield_is_detected_through_a_tuple(self):
        # narrowed: "succeeded", so a zero-yield run bills as a normal one
        # and never reaches the yield-rate monitor.
        assert classify_terminal_state(
            status="succeeded", result={"candidates": ()}
        ) == "completed_no_yield"
        assert classify_terminal_state(
            status="succeeded", result={"candidates": ({"pdb_key": "a"},)}
        ) == "succeeded"

    def test_slim_for_persist_strips_a_tuple_array(self):
        # narrowed: returns the payload untouched, so the multi-MB inline
        # b64 goes to the single PostgREST UPDATE that cannot carry it.
        result = {
            "candidates": (
                {"pdb_key": "designs/d0.pdb", "pdb_content_b64": "QVRPTQo="},
                {"pdb_key": "designs/d1.pdb", "pdb_content_b64": "QVRPTQo="},
            ),
        }
        out = _slim_result_for_persist(result)
        assert [c["pdb_key"] for c in out["candidates"]] == [
            "designs/d0.pdb", "designs/d1.pdb"
        ]
        assert not any("pdb_content_b64" in c for c in out["candidates"])
        # JSON has no tuple, and this value is about to be serialised.
        assert type(out["candidates"]) is list
        # Input never mutated.
        assert result["candidates"][0]["pdb_content_b64"] == "QVRPTQo="


# ---------------------------------------------------------------------------
# 2b. The shared/jobs.py readers DECLINE a non-array, in their own vocabulary
#
# The reject side used to be spelled once per reader, across SIX test files:
# test_campaign_passed_filters, test_export_shapes,
# test_job_complete_email_headline, test_malformed_candidate_row_render,
# test_modal_webhook_finalize and test_target_lab_handoff. Collected here
# because it is the same question: these are the values the predicate answers
# False to, and each reader's "not stated" answer is a different value --
# [] , None, False, "succeeded", the payload untouched.
#
# The shared/email.py readers and blueprints/admin.py are NOT on this side of
# it. What this commit moved for them is the ACCEPT side -- a tuple, covered
# by TestEmailReaders below. Widening a gate from ``list`` to
# ``(list, tuple)`` adds an accepted shape and removes no rejected one, so
# their reject side is unchanged here and stays where it already lived.
# ---------------------------------------------------------------------------

# A result whose per-candidate array is absent, mistyped, or not a result.
_NOT_AN_ARRAY_RESULT = [
    None,
    {},
    "not a dict",
    {"candidates": "nope"},
    {"candidates": {"a": 1}},
    {"something_else": [1, 2]},
]


@pytest.mark.parametrize("bad", _NOT_AN_ARRAY_RESULT)
def test_every_result_reader_declines_a_non_array(bad):
    assert candidate_records(bad) == []
    # NOT [] -- candidate_count exists to separate "read zero" from "not
    # stated", which is the whole reason it is a second function.
    assert candidate_count(bad) is None
    assert supports_headline_claim(bad, "bindcraft") is False
    assert classify_terminal_state(status="succeeded", result=bad) == "succeeded"
    # Passthrough, not a coercion: an unreadable payload is persisted as it
    # arrived rather than rewritten into a shape this module invented.
    assert _slim_result_for_persist(bad) is bad


@pytest.mark.parametrize("bad", [None, {}, "candidates", 5, {"a": 1}, b"xy"])
def test_display_rows_declines_a_non_array(bad):
    """display_rows takes the ROWS, not the result, so its reject side is its
    own case."""
    assert display_rows(bad) == []


class TestEmailReaders:
    def test_the_empty_headline_names_sequences_for_a_tuple(self):
        # narrowed: "no candidates" -- MPNN told it produced none of a thing
        # it does not produce.
        job = _job(result={"sequences": ()})
        assert isinstance(job.result["sequences"], tuple)
        assert email_mod._empty_noun(job) == "no sequences"

    def test_the_empty_summary_names_sequences_for_a_tuple(self):
        # narrowed: the candidates copy, which prescribes binder length and
        # hotspot knobs an MPNN form does not have.
        job = _job(result={"sequences": ()})
        summary = email_mod._result_summary(job, tone="empty")
        assert "no sequences were returned" in summary

    def test_the_success_summary_counts_a_tuple_of_sequences(self):
        # narrowed: falls past to candidate_records, finds nothing, and says
        # "Your run finished" instead of naming the 2 sequences.
        job = _job(tool="mpnn", result={"sequences": ("MKTAYIAKQR", "GGSGGSGGSG")})
        assert "2 sequences returned" in email_mod._result_summary(
            job, tone="success"
        )

    @pytest.mark.parametrize("key", _ARRAY_KEYS)
    def test_an_empty_tuple_array_is_an_empty_result(self, key):
        # narrowed: False, so the customer gets "your run is ready" and a
        # green View results button over a page showing nothing.
        assert email_mod._is_empty_result(_job(result={key: ()})) is True
        # Non-empty is not empty, under the same key and the same shape.
        assert email_mod._is_empty_result(
            _job(result={key: ({"pdb_key": "a.pdb"},)})
        ) is False

    def test_the_score_callout_survives_a_tuple(self):
        # narrowed: six empty strings, and the mail leads with no number.
        job = _job(
            result={
                "candidates": (
                    {"pdb_key": "designs/d0.pdb", "scores": {"ipTM": 0.81}},
                ),
            },
        )
        label, value, _caption, pdb_key, _verdict, _pos = (
            email_mod._top_candidate_summary(job=job, tone="success")
        )
        assert label and value
        assert value == "0.810"
        assert pdb_key == "designs/d0.pdb"


# ---------------------------------------------------------------------------
# 3. No reader spells the gate itself
#
# The behaviour tests above pin the eleven readers that exist today. These read
# the SOURCE, so a twelfth reader -- or a widening that reverts one of the
# eleven -- fails here instead of shipping a silent disagreement. The negative
# sweep walks the TREE rather than _READERS: a reader added to a module nobody
# thought to list is exactly the failure a hand-listed set cannot see.
# ---------------------------------------------------------------------------

# module -> the functions that read a per-candidate array.
_READERS = {
    "shared/jobs.py": (
        "candidate_records",
        "candidate_count",
        "display_rows",
        "supports_headline_claim",
        "classify_terminal_state",
        "_slim_result_for_persist",
    ),
    "shared/email.py": (
        "_empty_noun",
        "_top_candidate_summary",
        "_is_empty_result",
        "_result_summary",
    ),
    # INERT, and listed as such rather than sold as a fix: ``parsed`` there
    # comes from ``json.loads``, and JSON has no tuple, so the widened shape
    # cannot reach that reader. It routes through the predicate anyway so the
    # sweep below has nothing to except.
    "blueprints/admin.py": ("admin_campaign_save_results",),
}

# (module, function) pairs allowed to keep a raw list/tuple isinstance test,
# because theirs is not a shape question about the array.
_NOT_A_SHAPE_GATE = {
    # Return-TYPE conversion, run after is_candidate_array has already
    # answered the shape: an already-list array is returned by reference.
    ("shared/jobs.py", "candidate_records"),
    # The gate here is over ``rounds``, which is not one of the three
    # array keys; the ``sequences`` read beside it does route through
    # the predicate. This entry excuses the FUNCTION and not that one
    # gate, so reverting the ``sequences`` read would be excused HERE --
    # what catches it is test_every_reader_routes_through_the_one_predicate
    # above, which covers this pair through _READERS.
    ("blueprints/admin.py", "admin_campaign_save_results"),
}

# Every module the negative sweep reads, DERIVED from the tree. _READERS is a
# pin on the readers that exist today; this is the net under it, and it has to
# cover modules this file does not name -- blueprints/jobs.py already reads
# ``sequences`` for the FASTA export without gating on its shape at all.
# shared/ + blueprints/ only, deliberately: tools/ and the repo root
# produce results or parse request specs rather than reading the arrays
# out of ``job.result``. A reader that moves there is outside this net.
_SWEPT_MODULES = tuple(
    sorted(
        path.relative_to(_REPO).as_posix()
        for tree in ("shared", "blueprints")
        for path in (_REPO / tree).rglob("*.py")
    )
)


def _top_level_functions(path: pathlib.Path):
    """(name, node) for every module-level function and method.

    Nested defs are NOT yielded separately, so a gate inside a closure is
    attributed to the reader that encloses it rather than escaping the sweep
    under a private name.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{sub.name}", sub


def _calls_named(node, name: str) -> bool:
    return any(
        isinstance(c, ast.Call)
        and isinstance(c.func, ast.Name)
        and c.func.id == name
        for c in ast.walk(node)
    )


def _list_or_tuple_isinstance_gates(node) -> int:
    """How many ``isinstance(x, list|tuple)`` tests this function spells."""
    n = 0
    for call in ast.walk(node):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "isinstance"
            and len(call.args) == 2
        ):
            continue
        second = call.args[1]
        names = list(second.elts) if isinstance(second, ast.Tuple) else [second]
        if any(
            isinstance(e, ast.Name) and e.id in ("list", "tuple") for e in names
        ):
            n += 1
    return n


@pytest.mark.parametrize(
    ("module", "func"),
    [(m, f) for m, fs in _READERS.items() for f in fs],
)
def test_every_reader_routes_through_the_one_predicate(module, func):
    by_name = dict(_top_level_functions(_REPO / module))
    assert func in by_name, f"{module}::{func} is gone -- update _READERS"
    # A reader may reach the predicate through a pinned reader that calls it:
    # _top_candidate_summary answers its shape question with
    # supports_headline_claim, which is itself in _READERS above.
    via = _ROUTES_VIA.get((module, func), "is_candidate_array")
    assert _calls_named(by_name[func], via), (
        f"{module}::{func} reads a per-candidate array without calling {via}"
    )


_ROUTES_VIA = {
    ("shared/email.py", "_top_candidate_summary"): "supports_headline_claim",
}


def _reads_an_array_key(node) -> bool:
    """Does this function READ one of the array's keys?

    A key spelled as a dict-literal KEY is a write, not a read.
    ``blueprints/lab_projects.py::_ordered_shortlist`` returns a ``designs``
    key while gating a campaign COLUMN with its own isinstance test; counting
    that as a read would buy an allowlist entry for an unrelated gate, and an
    allowlist is the thing this sweep exists to avoid needing.
    """
    written = {
        key
        for sub in ast.walk(node)
        if isinstance(sub, ast.Dict)
        for key in sub.keys
        if isinstance(key, ast.Constant)
    }
    return any(
        isinstance(c, ast.Constant)
        and c.value in _ARRAY_KEYS
        and c not in written
        for c in ast.walk(node)
    )


@pytest.mark.parametrize("module", _SWEPT_MODULES)
def test_no_reader_spells_its_own_shape_gate(module):
    """A reader that LOOKS UP one of the keys and re-answers shape fails HERE.

    Any function under ``shared/`` or ``blueprints/`` that reads one of the
    array's keys and also spells a raw ``list``/``tuple`` isinstance test is
    answering the shape question locally -- which is the defect this predicate
    exists to end. The module set is derived from the tree by
    :data:`_SWEPT_MODULES`, so a reader in a module this file never heard of
    is swept too.

    CEILING, and it has a LIVE instance -- this sweep is not a claim that the
    repo holds only one shape gate. Both halves must be true of one function:
    :func:`_reads_an_array_key` requires it to name a key, and
    :func:`_list_or_tuple_isinstance_gates` requires a literal
    ``list``/``tuple`` inside an ``isinstance`` call. So a reader HANDED the
    array is missed however it gates -- ``shared/exports.py``'s
    ``_dict_candidates`` is exactly that today, and passes this sweep while
    keeping its own ``(list, tuple)`` line for the import reason
    :func:`shared.jobs.is_candidate_array` records. On the gate half,
    ``type(x) is list``, ``isinstance(x, Sequence)`` or a module-level
    ``(list, tuple)`` constant would pass too.
    """
    offenders = []
    for name, node in _top_level_functions(_REPO / module):
        if name == "is_candidate_array":
            continue  # the sink itself; it is where the gate belongs
        if (module, name) in _NOT_A_SHAPE_GATE:
            continue
        if _reads_an_array_key(node) and _list_or_tuple_isinstance_gates(node):
            offenders.append(name)
    assert not offenders, (
        f"{module}: {offenders} spell their own shape gate over a "
        "per-candidate array. Call shared.jobs.is_candidate_array instead, or "
        "add the pair to _NOT_A_SHAPE_GATE with the reason it is not a shape "
        "question."
    )


def test_the_allowlist_is_not_stale():
    """Every allowlisted pair must still exist AND still carry a gate.

    An entry that stopped applying would silently excuse whatever took its
    name later.
    """
    for module, func in sorted(_NOT_A_SHAPE_GATE):
        by_name = dict(_top_level_functions(_REPO / module))
        assert func in by_name, f"{module}::{func} is gone -- drop the entry"
        assert _list_or_tuple_isinstance_gates(by_name[func]), (
            f"{module}::{func} no longer spells a list/tuple isinstance test "
            "-- drop its _NOT_A_SHAPE_GATE entry"
        )


# ---------------------------------------------------------------------------
# 4. esmfold2-design writes BOTH keys, from one array
# ---------------------------------------------------------------------------

def test_esmfold2_candidates_are_one_per_design():
    """The page and the export must never report different totals.

    esmfold2_design writes ``designs`` AND ``candidates`` into one result
    (tools/esmfold2_design/modal_app.py, ``_aggregate``, returned inline under
    the ``smoke_result`` key), and the two are read by DIFFERENT surfaces: the
    results template renders ``display_rows(output.get('designs'))`` for its
    totals while the CSV, FASTA and ZIP go through ``candidate_records``, which
    prefers ``candidates``. Different lengths would put one number on the page
    and another in the file, with nothing raising.

    They cannot differ, and this is the fact that makes that true: the
    ``candidates`` array is a comprehension over ``designs``, one row each, in
    order. The orchestrator then concatenates both across seeds inside ONE loop
    over ``successes`` and sorts each on the same iPTM value, which preserves
    the equal length. Assert the comprehension, because it is the load-bearing
    half -- a hand-built ``candidates`` list is where a divergence would enter.

    It is NOT the only tool that writes both: tools/proteina/run_pipeline.py
    appends to ``out_designs`` and ``out_candidates`` together inside one loop,
    which keeps those two equal by the same kind of construction. This test
    pins esmfold2's comprehension and NOT proteina's paired appends -- a tool
    that writes both keys has to bring its own pin.
    """
    tree = ast.parse(
        (_REPO / "tools" / "esmfold2_design" / "run_pipeline.py").read_text(
            encoding="utf-8"
        )
    )
    comps = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.ListComp)
        and any(
            isinstance(t, ast.Name) and t.id == "candidates" for t in node.targets
        )
    ]
    assert len(comps) == 1, (
        "expected exactly one `candidates = [...]` comprehension in "
        "run_pipeline.py"
    )
    comp = comps[0]
    assert len(comp.generators) == 1, "a second `for` would break the 1:1 count"
    gen = comp.generators[0]
    assert isinstance(gen.iter, ast.Name) and gen.iter.id == "designs", (
        "candidates must be built over `designs`, one row each"
    )
    assert not gen.ifs, "a filter here renumbers the ranks the page shows"


# ---------------------------------------------------------------------------
# 5. This file's own promises, enforced rather than trusted.
# ---------------------------------------------------------------------------

def test_no_fixture_writes_the_same_row_count_under_both_keys():
    """The module docstring's central promise, checked instead of asserted.

    A payload carrying equal-length arrays under two of the array keys lets a
    reader narrowed back to ``list`` reject the first key, FALL THROUGH to the
    second and still return the right number: the mutation goes green and the
    guard certifies nothing. Prose cannot notice a fixture added later, so
    this walks the file.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        lengths = {
            key.value: len(val.elts)
            for key, val in zip(node.keys, node.values)
            if isinstance(key, ast.Constant)
            and key.value in _ARRAY_KEYS
            and isinstance(val, (ast.List, ast.Tuple))
        }
        if len(lengths) > 1:
            assert len(set(lengths.values())) == len(lengths), (
                f"line {node.lineno}: {lengths} -- two array keys of equal "
                "length let a narrowed reader fall through and still pass"
            )


def test_the_prose_count_matches_the_reader_pin():
    """``_READERS`` and the module docstring carry the same number twice.

    A count inside the file it counts cannot self-correct, so assert the two
    copies agree. ONLY the count is asserted: the prose describing this
    reader set in ``shared/jobs.py`` (:func:`is_candidate_array` and
    :func:`display_rows`) is not pinned to it, so a twelfth reader has to
    move that by hand.
    """
    spelled = {11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen"}
    n = sum(len(v) for v in _READERS.values())
    assert spelled.get(n, f"<{n}>") in __doc__, (
        f"_READERS holds {n} readers; the module docstring says otherwise"
    )
    assert len(_READERS) == 3 and "across three modules" in __doc__


def _staged_candidate_call_sites() -> int:
    """How many ``stage_campaign_candidates`` calls pass ``candidate_records``.

    The number is carried in prose and derived nowhere, so derive it here.
    """
    tree = ast.parse(
        (_REPO / "blueprints/lab_projects.py").read_text(encoding="utf-8")
    )
    return sum(
        1
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "stage_campaign_candidates"
        for kw in call.keywords
        if kw.arg == "candidates"
        and isinstance(kw.value, ast.Call)
        and isinstance(kw.value.func, ast.Name)
        and kw.value.func.id == "candidate_records"
    )


# Sentences that spell the count above, with ``{n}`` where the number
# goes. All are prose about the SAME derived quantity.
_CALL_SITE_COUNT_PROSE = (
    (
        "shared/jobs.py",
        "the {n} ``candidate_records(job.result)`` call sites in "
        "``blueprints/lab_projects.py``",
    ),
    (
        "shared/storage.py",
        "the {n} `candidates=candidate_records(job.result)` call sites in "
        "blueprints/lab_projects.py",
    ),
    ("shared/storage.py", "at the one sink all {n} lab-handoff callers"),
)


def _squeezed(path: pathlib.Path) -> str:
    """File text with comment markers and ALL whitespace removed.

    These sentences live in a docstring and in a ``#`` comment that wraps
    mid-expression, so only a wrap-insensitive compare survives a reflow.
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        out.append(stripped[1:] if stripped.startswith("#") else stripped)
    return "".join("".join(out).split())


@pytest.mark.parametrize(("module", "sentence"), _CALL_SITE_COUNT_PROSE)
def test_the_call_site_count_in_prose_matches_the_source(module, sentence):
    """A staging call site added or removed must move the prose counting them.

    :func:`shared.jobs.display_rows`' docstring and the
    ``shared/storage.py::stage_campaign_candidates`` comment each spell that
    count in words, derived from nothing -- the same defect
    :func:`test_the_prose_count_matches_the_reader_pin` guards for the reader
    count. A call site added in ``blueprints/lab_projects.py`` fails here --
    see CEILING.

    CEILING: :func:`_staged_candidate_call_sites` requires BOTH names
    spelled bare -- the form all three use today (``from shared.storage
    import stage_campaign_candidates``). One added as
    ``storage.stage_campaign_candidates(...)``, as
    ``jobs.candidate_records(...)``, or under an alias, slips the count
    and leaves this guard silent.
    """
    n = _staged_candidate_call_sites()
    spelled = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
    want = sentence.format(n=spelled.get(n, f"<{n}>"))
    assert "".join(want.split()) in _squeezed(_REPO / module), (
        f"{module}: blueprints/lab_projects.py has {n} staging call sites, "
        f"but this file's prose does not carry that count -- expected the "
        f"sentence {want!r}"
    )
