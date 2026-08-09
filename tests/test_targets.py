"""Unit tests for shared/targets.py.

The fake Supabase client here deliberately models PostgREST's RESTRICTIONS,
not just its happy path:

* every response is clamped to ``_MAX_ROWS`` (1000, matching
  ``supabase/config.toml``), and ``.limit()`` is clamped identically — only
  ``.range()`` escapes it;
* ``.eq()`` filters actually filter, so an owner-scope test can fail.

A fake more permissive than the backend turns these tests into decoration:
they pass while the code is broken. Do not "simplify" the clamp or the filters
out — they are load-bearing.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

# These tests assert ownership and isolation, so they must not consult the
# live database that app.py's load_dotenv() would otherwise hand them.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.storage import StorageError
from shared.targets import (
    TARGET_READ_ABSENT,
    TARGET_READ_OK,
    TARGET_READ_UNAVAILABLE,
    DesignTarget,
    TargetRead,
    _target_storage_key,
    archive_target,
    campaign_ids_for_target,
    create_target,
    find_target_by_sha256,
    get_target,
    list_targets_for_user,
    read_target,
    unarchive_target,
)

# Mirrors supabase/config.toml. PostgREST caps every response at this many
# rows and clamps .limit() to it; .range() paging is the only way past.
_MAX_ROWS = 1000


class _Resp:
    def __init__(self, data):
        self.data = data


class _NotBuilder:
    """PostgREST's ``.not_.is_(col, "null")`` negation chain.

    Two callers use it, and a fake lacking it fails DIFFERENTLY and worse for
    each. ``list_targets_for_user(archived_only=True)`` would raise, get
    swallowed by its except, and return an empty list, which an archived-only
    test reads as "no archived targets". ``unarchive_target`` would do the
    same and return False -- which is the ANSWER
    ``test_unarchive_reports_false_when_the_target_was_never_archived``
    asserts, so that test would pass on an AttributeError while the filter it
    exists to pin was never exercised.
    """

    def __init__(self, table):
        self._table = table

    def is_(self, col, _val):
        self._table._neg_filters.append((col, None))
        return self._table


class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._op = "select"
        self._filters = []
        self._neg_filters = []
        self._payload = None
        self._range = None
        self._limit = None
        self._single = False
        self._projection = None
        self._order = None

    @property
    def not_(self):
        return _NotBuilder(self)

    # -- builders ----------------------------------------------------------
    def select(self, *cols, **_k):
        self._op = "select"
        # PostgREST returns EXACTLY the columns named. A fake that ignored this
        # is what let an explicit column list silently drop a column added by a
        # later migration, twice on this codebase.
        joined = ",".join(str(c) for c in cols)
        if joined and joined != "*":
            self._projection = [c.strip() for c in joined.split(",") if c.strip()]
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, fields):
        self._op = "update"
        self._payload = fields
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def is_(self, col, _val):
        self._filters.append((col, None))
        return self

    def order(self, col, **kw):
        # Really sorts. Without this, `find_target_by_sha256`'s entire contract
        # ("the MOST RECENT live target with this hash") would be unverifiable
        # and a reversed order would go unnoticed.
        self._order = (col, bool(kw.get("desc")))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def single(self):
        self._single = True
        return self

    # -- execution ---------------------------------------------------------
    def _matching(self):
        rows = self._store.setdefault(self._name, [])
        return [
            r for r in rows
            if all(r.get(c) == v for c, v in self._filters)
            and all(r.get(c) != v for c, v in self._neg_filters)
        ]

    def execute(self):
        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", str(uuid.uuid4()))
            self._store.setdefault(self._name, []).append(row)
            return _Resp([row])
        if self._op == "update":
            hit = self._matching()
            for r in hit:
                r.update(self._payload)
            return _Resp(list(hit))
        if self._op == "delete":
            hit = self._matching()
            rows = self._store.setdefault(self._name, [])
            self._store[self._name] = [r for r in rows if r not in hit]
            return _Resp(list(hit))

        rows = self._matching()
        if self._order is not None:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: str(r.get(col) or ""), reverse=desc)
        if self._range is not None:
            start, end = self._range
            rows = rows[start:end + 1]
        elif self._limit is not None:
            # PostgREST clamps .limit() to max_rows the same way it clamps a
            # bare select. Asking for more does not get you more.
            rows = rows[:min(self._limit, _MAX_ROWS)]
        rows = rows[:_MAX_ROWS]
        if self._projection is not None:
            rows = [{k: r.get(k) for k in self._projection} for r in rows]
        if self._single:
            if not rows:
                raise RuntimeError("no rows")
            return _Resp(rows[0])
        return _Resp(rows)


class _FakeClient:
    def __init__(self, store=None):
        self.store = store if store is not None else {}

    def table(self, name):
        return _FakeTable(self.store, name)


class _Upload:
    """Stands in for shared.pdb_intake.TargetUpload."""

    def __init__(self, data=b"ATOM  \n", filename="t.pdb", kind="pdb"):
        self.data = data
        self.filename = filename
        self.content_type = "chemical/x-pdb"
        self.kind = kind
        self.sha256 = "abc123"
        self.chain_summary = {
            "total_standard_residues": 210,
            "chains": [
                {
                    "chain_id": "A",
                    "standard_residue_count": 210,
                    "hetatm_resnames": [],
                    "water_count": 0,
                    "min_resnum": 1,
                    "max_resnum": 210,
                },
                {
                    "chain_id": "L",
                    "standard_residue_count": 0,
                    "hetatm_resnames": ["HEM"],
                    "water_count": 0,
                    "min_resnum": None,
                    "max_resnum": None,
                },
            ],
        }


@pytest.fixture
def fake():
    client = _FakeClient()
    with patch("shared.targets.get_service_client", return_value=client):
        yield client


# ---------------------------------------------------------------------------
# Storage key safety
# ---------------------------------------------------------------------------


def test_storage_key_is_uuid_validated():
    """upload_input interpolates job_id into the object key VERBATIM, so a
    non-UUID id would write outside the owner's prefix."""
    tid = str(uuid.uuid4())
    assert _target_storage_key(tid) == f"target-{tid}"
    with pytest.raises(ValueError):
        _target_storage_key("../../other-user")


# ---------------------------------------------------------------------------
# create_target
# ---------------------------------------------------------------------------


def test_create_target_stages_under_the_owner_scoped_key(fake):
    up = _Upload()
    with patch(
        "shared.targets.upload_input", return_value="u-1/target-x/t.pdb"
    ) as staged:
        target = create_target(user_id="u-1", upload=up, name="HER2")

    assert target is not None
    kwargs = staged.call_args.kwargs
    assert kwargs["user_id"] == "u-1"
    assert kwargs["job_id"] == f"target-{target.id}"
    assert target.storage_path == "u-1/target-x/t.pdb"
    assert target.sha256 == "abc123"
    assert target.byte_size == len(up.data)
    # The inspection is persisted so the launch form never re-parses the file.
    assert target.chain_summary["chains"][0]["chain_id"] == "A"


def test_create_target_rolls_back_the_row_when_staging_fails(fake):
    """A target that exists without its structure renders as a launchable card
    whose every run dies on an empty input URL."""
    with patch("shared.targets.upload_input", side_effect=StorageError("boom")):
        with pytest.raises(StorageError):
            create_target(user_id="u-1", upload=_Upload())

    assert fake.store.get("design_targets") == []


def test_create_target_rolls_back_when_the_path_cannot_be_recorded(fake):
    """Bytes staged but the row not pointing at them is the same unusable
    half-made target, so it must roll back too."""
    with patch("shared.targets.upload_input", return_value="u-1/target-x/t.pdb"), \
            patch("shared.targets._update_target", return_value=False):
        with pytest.raises(StorageError):
            create_target(user_id="u-1", upload=_Upload())

    assert fake.store.get("design_targets") == []


def test_create_target_without_an_upload_keeps_storage_path_null(fake):
    """proteina's curated-benchmark path legitimately has no structure."""
    with patch("shared.targets.upload_input") as staged:
        target = create_target(user_id="u-1", upload=None, name="curated")
    staged.assert_not_called()
    assert target is not None
    assert target.storage_path is None


# ---------------------------------------------------------------------------
# Chain-qualified hotspots (design_targets.hotspot_spec, migration 0041)
#
# design_targets.hotspot_residues is integer[], so a hotspot that names its
# protomer cannot be stored in it. On an IgG1 Fc — both chains numbered
# 234-444 — storing the bare 241 means every later run re-prefills it as A241
# and silently designs against one protomer.
# ---------------------------------------------------------------------------


def _stored_row(fake):
    rows = fake.store.get("design_targets") or []
    assert len(rows) == 1, rows
    return rows[0]


def test_a_chain_qualified_hotspot_is_written_to_both_columns(fake):
    """The text column carries the chain; the integer column keeps the number
    so every reader that predates 0041 still sees a hotspot rather than NULL."""
    with patch("shared.targets.upload_input", return_value="u-1/t/t.pdb"):
        target = create_target(
            user_id="u-1", upload=_Upload(), target_chain="A B",
            hotspot_residues=["A241", "B241"],
        )
    row = _stored_row(fake)
    assert row["hotspot_spec"] == ["A241", "B241"]
    assert row["hotspot_residues"] == [241, 241], (
        "_clean_int_list would int('A241'), fail, skip it, and store NULL")
    assert target.hotspot_spec == ["A241", "B241"]
    assert target.effective_hotspots == ["A241", "B241"]


def test_a_bare_hotspot_target_never_names_the_new_column(fake):
    """Additive means the pre-existing shape is untouched — and the INSERT must
    not even MENTION a column the database may not have yet, so a deploy that
    lands ahead of the migration cannot break ordinary target creation."""
    with patch("shared.targets.upload_input", return_value="u-1/t/t.pdb"):
        target = create_target(
            user_id="u-1", upload=_Upload(), target_chain="A",
            hotspot_residues=[42, 88],
        )
    row = _stored_row(fake)
    assert "hotspot_spec" not in row, (
        "an INSERT naming hotspot_spec fails outright before 0041 is applied")
    assert row["hotspot_residues"] == [42, 88]
    assert target.hotspot_spec == []
    assert target.effective_hotspots == [42, 88]


def test_a_row_from_before_the_migration_still_loads(fake):
    """No backfill, so most rows have no such key at all."""
    target = DesignTarget.from_row({
        "id": str(uuid.uuid4()), "user_id": "u-1",
        "hotspot_residues": [241, 243],
    })
    assert target.hotspot_spec == []
    assert target.effective_hotspots == [241, 243]


def test_a_hotspot_is_never_silently_dropped_for_want_of_a_chain_list(fake):
    """split_hotspot reads a prefix only against a KNOWN chain list, so with no
    upload and no target_chain it returns (None, None) for every prefixed
    token — and both columns would be written NULL. The hotspot would vanish on
    save, silently, which is the exact failure this change exists to remove."""
    with patch("shared.targets.upload_input") as staged:
        target = create_target(
            user_id="u-1", upload=None, name="curated",
            hotspot_residues=["A241", "B241"],
        )
    staged.assert_not_called()
    row = _stored_row(fake)
    assert row["hotspot_residues"] == [241, 241]
    assert row["hotspot_spec"] == ["A241", "B241"]
    assert target.effective_hotspots == ["A241", "B241"]


def test_the_known_chain_list_beats_the_single_letter_fallback():
    """ORDER MATTERS, and nothing else pins it.

    On an mmCIF target whose chain is "A2", the token "A296" is residue 96 on
    chain A2 — not residue 296 on chain A. split_hotspot gets that right BECAUSE
    it is given the chain list, so it has to be consulted first; the
    single-letter regex is only the last resort for when no chain list exists.
    Reversing the two is silent and produces a plausible wrong answer.
    """
    from shared.targets import _split_stored_hotspot

    assert _split_stored_hotspot("A296", ["A2"]) == ("A2", 96)
    assert _split_stored_hotspot("A296", ["A", "B"]) == ("A", 296)
    assert _split_stored_hotspot("A296", []) == ("A", 296)
    assert _split_stored_hotspot("296", ["A", "B"]) == (None, 296)
    assert _split_stored_hotspot("zzz", ["A"]) == (None, None)


def test_the_epitope_column_keeps_its_strict_integer_coercion(fake):
    """The hotspot helper is deliberately not shared with the epitope field."""
    with patch("shared.targets.upload_input", return_value="u-1/t/t.pdb"):
        create_target(
            user_id="u-1", upload=_Upload(), target_chain="A",
            epitope_residues=[32, 45],
        )
    row = _stored_row(fake)
    assert row["epitope_residues"] == [32, 45]
    assert "hotspot_spec" not in row


def test_the_form_prefill_carries_the_chain_back_to_the_next_run():
    """The defect this closes on the read side: a hotspot pinned to protomer B
    that comes back as a bare 241 gets promoted onto A by whatever reads it."""
    from shared.targets import target_defaults_for_form

    dimer = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", target_chain="A B",
        hotspot_residues=[241, 241], hotspot_spec=["A241", "B241"],
    )
    assert target_defaults_for_form(dimer)["hotspot_residues"] == "A241,B241"

    plain = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", target_chain="A",
        hotspot_residues=[42, 88],
    )
    assert target_defaults_for_form(plain)["hotspot_residues"] == "42,88"


def test_to_dict_adds_the_new_key_without_changing_the_old_one():
    """Existing consumers of to_dict()["hotspot_residues"] must be unaffected.

    ``to_dict`` has NO production callers (grep: only this test), so it mirrors
    the two stored columns and nothing more. ``effective_hotspots`` is where the
    choice between them lives, and it is what the templates and the run prefill
    actually read.
    """
    dimer = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", target_chain="A B",
        hotspot_residues=[241, 241], hotspot_spec=["A241", "B241"],
    )
    out = dimer.to_dict()
    assert out["hotspot_residues"] == [241, 241]
    assert out["hotspot_spec"] == ["A241", "B241"]


def test_the_target_chain_seeds_the_chain_list_when_there_is_no_upload(fake):
    """DELETING THE ``target_chain`` SEED IN ``_hotspot_chain_ids`` IS OTHERWISE
    INVISIBLE. With a single-letter chain the one-letter fallback in
    ``_split_stored_hotspot`` reaches the same answer, and an upload that names
    the same chain supplies it through the chain summary anyway — so the two
    differ only for a MULTI-CHARACTER chain id that ONLY ``target_chain``
    contributes. (Brute-forced over 3696 seed/upload/token combinations while
    writing this: no other shape changes an answer.)

    On a target whose chain is ``"A2"``, ``"A2296"`` is residue 296. Without the
    seed, ``split_hotspot`` has no chain list, the fallback regex takes the one
    leading letter, and the integer column is written 2296 — a residue that is
    not on the structure, saved silently.

    REACHABILITY, stated so this is not read as a live production path: a
    two-character prefix cannot arrive from the web form today, because
    ``blueprints/targets._parse_residue_list`` accepts only one letter plus an
    integer. This drives ``create_target`` directly, which is its own contract
    and is what a multi-character chain id would go through if that parser is
    ever widened. The seed is defensive until then.

    STILL UNCOVERED, and deliberately not fixed here: deleting the OTHER half of
    ``_hotspot_chain_ids`` — the loop that adds the upload's chains — leaves 300
    tests green (measured). Pinning it needs the same multi-character token,
    so a test for it would presuppose the ``_parse_residue_list`` decision that
    is still open.
    """
    from shared.targets import _clean_hotspot_ints, _hotspot_chain_ids

    assert _hotspot_chain_ids("A2", None) == ["A2"]
    with patch("shared.targets.upload_input") as staged:
        target = create_target(
            user_id="u-1", upload=None, target_chain="A2",
            hotspot_residues=["A2296"],
        )
    staged.assert_not_called()
    row = _stored_row(fake)
    assert row["hotspot_residues"] == [296], "the target_chain seed was not used"
    assert row["hotspot_spec"] == ["A2296"]
    assert target.effective_hotspots == ["A2296"]
    # The counterfactual, so the assertion above is a measurement and not a
    # coincidence: with no chain list the same token reads as residue 2296.
    assert _clean_hotspot_ints(["A2296"], []) == [2296]


def test_the_fallback_never_invents_a_multi_letter_chain():
    """``_LONE_HOTSPOT_RE`` is ONE letter on purpose, and nothing pinned that.

    It runs only after ``split_hotspot`` has already failed — i.e. when no chain
    list confirms the prefix — so widening it to ``[A-Za-z]+`` would let the
    save invent a chain ``"AB"`` that nothing has confirmed exists.
    ``blueprints/targets._parse_residue_list`` restricts a stored hotspot to one
    letter plus an integer, which is exactly the shape this may recover.

    The cost of the restriction, stated rather than hidden: a genuine
    multi-character chain with NO chain list to confirm it is dropped from both
    columns. Supplying the chain list is what recovers it, which is the whole
    reason ``_hotspot_chain_ids`` exists.
    """
    from shared.targets import _split_stored_hotspot

    assert _split_stored_hotspot("A296", []) == ("A", 296)
    assert _split_stored_hotspot("AB296", []) == (None, None)
    assert _split_stored_hotspot("AB296", ["AB"]) == ("AB", 296)


def test_a_deploy_ahead_of_migration_0041_does_not_say_try_again(fake):
    """A chain-prefixed INSERT names a column a pre-0041 database does not have,
    so it fails EVERY time. Returning None routed it to "Could not save the
    target. Try again in a moment." — an instruction to retry an operation that
    cannot succeed until a migration lands.

    Only this identified failure raises; anything else keeps the pre-existing
    ``return None`` so the generic message still covers a genuine transient.
    """
    from shared.targets import TargetSchemaError

    class _Missing(Exception):
        pass

    boom = _Missing(
        "{'message': \"Could not find the 'hotspot_spec' column of "
        "'design_targets' in the schema cache\", 'code': 'PGRST204'}"
    )
    with patch.object(type(fake), "table", side_effect=boom):
        with pytest.raises(TargetSchemaError) as exc:
            create_target(
                user_id="u-1", upload=None, target_chain="A B",
                hotspot_residues=["A241", "B241"],
            )
    msg = str(exc.value)
    assert "try again" not in msg.lower(), msg
    assert "hotspot" in msg.lower() and "chain" in msg.lower(), msg


def test_the_missing_column_detector_matches_the_real_postgrest_error():
    """The detector is a substring test on the DRIVER'S OWN text, so it is only
    ever as good as that text. Pinned against the installed client rather than
    a hand-written string: a supabase-py upgrade that stops naming the column
    on the exception turns this red, instead of silently downgrading the save
    error back to "Could not save the target. Try again in a moment."
    """
    from postgrest.exceptions import APIError

    from shared.targets import _names_missing_column

    real = APIError({
        "message": "Could not find the 'hotspot_spec' column of "
                   "'design_targets' in the schema cache",
        "code": "PGRST204", "hint": None, "details": None,
    })
    assert _names_missing_column(real, "hotspot_spec")
    assert not _names_missing_column(real, "epitope_residues")


def test_an_unidentified_insert_failure_still_returns_none(fake):
    """The generic path is unchanged: a transient really may clear on a retry,
    and this must not start claiming a migration is missing for every blip."""
    with patch.object(type(fake), "table", side_effect=RuntimeError("timeout")):
        assert create_target(
            user_id="u-1", upload=None, target_chain="A B",
            hotspot_residues=["A241", "B241"],
        ) is None


def test_a_bare_hotspot_insert_failure_is_never_blamed_on_the_migration(fake):
    """The row does not even name ``hotspot_spec`` on a bare-hotspot target, so
    the missing column cannot be the cause and must not be offered as one."""
    with patch.object(
        type(fake), "table",
        side_effect=RuntimeError("could not find the 'hotspot_spec' column"),
    ):
        assert create_target(
            user_id="u-1", upload=None, target_chain="A",
            hotspot_residues=[241],
        ) is None


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_get_target_is_owner_scoped(fake):
    with patch("shared.targets.upload_input", return_value="u-1/target-x/t.pdb"):
        target = create_target(user_id="u-1", upload=_Upload())

    assert get_target(target.id, user_id="u-1") is not None
    # copy_input/download_input do no ownership check of their own, so this
    # fetch is the whole tenancy boundary for a target: storage path read.
    assert get_target(target.id, user_id="u-2") is None


def test_campaign_ids_for_target_is_owner_scoped(fake):
    fake.store["compute_campaigns"] = [
        {"id": "c-1", "target_id": "t-1", "user_id": "u-1"},
        {"id": "c-2", "target_id": "t-1", "user_id": "u-2"},
    ]
    assert campaign_ids_for_target("t-1", user_id="u-1") == (["c-1"], True)


def test_campaign_ids_for_target_reports_a_failed_read_as_incomplete(fake):
    """ROUND 19 (A-7). The ids and the completeness flag are one answer.

    This returned its partial list from inside its own ``except``, so the one
    caller -- the wet-lab shortlist's parentage check -- could not tell a
    transient database fault from "that design does not belong to this
    target", and quietly dropped designs the user had starred and paid to
    compute.
    """
    def _boom(*_a, **_kw):
        raise RuntimeError("PostgREST is down")

    fake.store["compute_campaigns"] = [
        {"id": "c-1", "target_id": "t-1", "user_id": "u-1"},
    ]
    with patch.object(fake, "table", side_effect=_boom):
        assert campaign_ids_for_target("t-1", user_id="u-1") == ([], False)


# ---------------------------------------------------------------------------
# read_target: the three-outcome read (register item A90)
#
# `get_target` answers None for a target that is not there, one that is not the
# caller's, and a read that never completed. The lab-handoff gate in
# blueprints/lab_projects.py has to act differently on the last of those -- it
# refuses the submission and says so, instead of bouncing the user to an
# unrelated list in silence -- so the difference has to survive the read.
# ---------------------------------------------------------------------------


def _boom_table(*_a, **_kw):
    raise RuntimeError("PostgREST is down")


def test_read_target_reports_ok_for_a_row_that_is_there(fake):
    row = _seed_target(fake, id="t-live")
    read = read_target("t-live", user_id="u-1")
    assert read.outcome == TARGET_READ_OK
    assert read.target is not None and read.target.id == row["id"]
    assert read.unavailable is False


def test_read_target_reports_absent_when_the_read_matched_no_row(fake):
    """A read that COMPLETED and found nothing. The whole point of `.limit(1)`:
    under `.single()` the fake raises (as PostgREST does) and this is
    indistinguishable from the fault in the next test, which is the defect A90
    filed."""
    read = read_target("t-nope", user_id="u-1")
    assert read.outcome == TARGET_READ_ABSENT
    assert read.target is None
    assert read.unavailable is False, (
        "an absent target is a verdict about the target, not about the database"
    )


def test_read_target_reports_unavailable_when_the_query_raises(fake):
    _seed_target(fake, id="t-live")
    with patch.object(fake, "table", side_effect=_boom_table):
        read = read_target("t-live", user_id="u-1")
    assert read.outcome == TARGET_READ_UNAVAILABLE
    assert read.target is None
    assert read.unavailable is True


def test_read_target_reports_unavailable_with_no_service_client():
    """No client is not "no target". `get_target` cannot say so."""
    with patch("shared.targets.get_service_client", return_value=None):
        read = read_target("t-live", user_id="u-1")
    assert read.outcome == TARGET_READ_UNAVAILABLE
    assert read.unavailable is True


def test_absent_and_unavailable_are_distinct_target_outcomes(fake):
    """THE PIN. Both of these hand back ``target is None``, so anything that
    collapses the pair -- reverting to `.single()`, or a caller that reads only
    the target -- makes a two-second database fault indistinguishable from a
    permanent verdict on the one action that hands work to a wet lab.

    Written as one test over both outcomes rather than two, because the claim is
    about the DIFFERENCE and a pair of separate assertions can each keep passing
    while the difference disappears.
    """
    absent = read_target("t-nope", user_id="u-1")
    with patch.object(fake, "table", side_effect=_boom_table):
        unreadable = read_target("t-nope", user_id="u-1")
    assert absent.target is None and unreadable.target is None
    assert absent.outcome != unreadable.outcome
    assert absent.unavailable is False
    assert unreadable.unavailable is True


def test_read_target_applies_the_owner_scope(fake):
    """``user_id`` is a QUERY FILTER, so another tenant's target matches no row
    and comes back ABSENT -- not OK, and not a distinct "forbidden" outcome,
    which would mean reading a row the scope exists to withhold. This read is the
    same tenancy boundary `get_target` documents: copy_input/download_input do no
    ownership check of their own.

    Dropping ``.eq("user_id", ...)`` from `read_target` reds this: the fake
    really filters, so the row would come back and the outcome would be OK.
    """
    _seed_target(fake, id="t-mine", user_id="u-1")
    theirs = read_target("t-mine", user_id="u-2")
    assert theirs.outcome == TARGET_READ_ABSENT
    assert theirs.target is None
    # And the unscoped read still works, so the test above failed on the scope
    # rather than on the id.
    assert read_target("t-mine").outcome == TARGET_READ_OK


class _RaisingAtExecute:
    """Builds like any other query and fails at ``execute()``.

    The raising fake above patches ``client.table`` and so fails BEFORE any
    query is built. Both faults are inside `read_target`'s ``try`` and both must
    report UNAVAILABLE, but only the first was exercised: a ``try`` narrowed to
    the ``client.table(...)`` line alone would have left every test here green
    while an ``execute()`` raise escaped as a 500 out of the lab-handoff gate
    and out of the target detail page.
    """

    def __getattr__(self, _name):
        return lambda *_a, **_k: self

    def execute(self):
        raise RuntimeError("PostgREST timed out")


def test_read_target_reports_unavailable_when_execute_raises(fake):
    _seed_target(fake, id="t-live")
    with patch.object(fake, "table", return_value=_RaisingAtExecute()):
        read = read_target("t-live", user_id="u-1")
    assert read.outcome == TARGET_READ_UNAVAILABLE
    assert read.target is None
    assert read.unavailable is True


# ---------------------------------------------------------------------------
# TargetRead's two guards, and the invariant underneath them
#
# The class docstring claimed "no `__bool__` and no truthiness of any kind"
# while having neither guard: every instance was unconditionally truthy, and
# `frozen=True` GENERATED an `__eq__`, so `read == TARGET_READ_OK` answered
# False in silence on a read that had succeeded. The precedent for both is
# `tools/proteina/_canary_scoring.py::Verdict`, which paid for the same two
# holes in the same order; `tests/test_proteina_canary.py` pins them there.
# ---------------------------------------------------------------------------


def test_a_target_read_refuses_to_be_used_as_a_boolean(fake):
    """Asserted on the OK read FIRST, because that is where the default
    behaviour was most dangerous: `if read:` was True there and True on an
    unreadable one, so the natural spelling of "did this work" could not fail.
    """
    _seed_target(fake, id="t-live")
    for read in (
        read_target("t-live", user_id="u-1"),
        read_target("t-nope", user_id="u-1"),                  # absent
        TargetRead(None, TARGET_READ_UNAVAILABLE),
    ):
        with pytest.raises(TypeError):
            bool(read)
        with pytest.raises(TypeError):
            if read:            # noqa: SIM103 - the spelling under test
                pass
        with pytest.raises(TypeError):
            not read


def test_a_target_read_refuses_to_be_compared_with_an_outcome_string(fake):
    """`__bool__` raising leaves a hole exactly its own size unless `__eq__`
    closes it too: the frozen dataclass's generated `__eq__` returned False
    SILENTLY for `read == TARGET_READ_OK` on a read that had succeeded, which
    reads as a clean negative rather than as a mistake.

    Every route into `__eq__` is covered, because closing only the direct one
    leaves three spellings of the same error working.
    """
    _seed_target(fake, id="t-live")
    read = read_target("t-live", user_id="u-1")
    assert read.outcome == TARGET_READ_OK
    with pytest.raises(TypeError):
        read == TARGET_READ_OK
    with pytest.raises(TypeError):
        TARGET_READ_OK == read              # the reflected comparison
    with pytest.raises(TypeError):
        read != TARGET_READ_ABSENT          # `!=` routes through `__eq__`
    with pytest.raises(TypeError):
        read in (TARGET_READ_OK, TARGET_READ_ABSENT)      # and so does `in`
    # And the cross-family mixup, which is the half a comparison guard CAN
    # catch: all three read families spell OK as the string "ok", so this raises
    # for being a string at all rather than for being the wrong one.
    from shared.jobs import JOB_READ_OK
    with pytest.raises(TypeError):
        read == JOB_READ_OK


def test_two_target_reads_still_compare_as_values(fake):
    """Refusing the string comparison must not cost ordinary equality, and it
    must not cost hashability either: declaring `__eq__` sets `__hash__` to
    None, which would make a frozen value type unusable in a set.

    THE SECOND READ IS BUILT FROM A SECOND, INDEPENDENTLY CONSTRUCTED TARGET,
    and that is the whole content of the test rather than a detail of it.
    `__eq__` compares `(self.target, self.outcome) == (other.target,
    other.outcome)`, and tuple `==` short-circuits on IDENTITY per element --
    so two reads sharing one DesignTarget compare equal whether the payload
    comparison works or not, and this test passed unchanged against an `__eq__`
    rewritten to `self.target is other.target`. Two `read_target` calls against
    the same seeded row give two distinct objects with equal fields, which is
    the property
    `tests/test_proteina_canary.py::test_two_verdicts_still_compare_as_values`
    gets from building its second Verdict with its own `{"k": 1}`.
    """
    _seed_target(fake, id="t-live")
    _seed_target(fake, id="t-other")
    target = read_target("t-live", user_id="u-1").target
    twin = read_target("t-live", user_id="u-1").target
    assert twin is not target, "two reads must build two objects"
    assert twin == target, "and they must be equal to each other by value"
    a = TargetRead(target, TARGET_READ_OK)
    assert a == TargetRead(twin, TARGET_READ_OK)
    assert a != TargetRead(None, TARGET_READ_ABSENT)
    assert TargetRead(None, TARGET_READ_ABSENT) != TargetRead(
        None, TARGET_READ_UNAVAILABLE)
    # A DIFFERENT target, so equality is not merely "same outcome".
    assert a != TargetRead(
        read_target("t-other", user_id="u-1").target, TARGET_READ_OK)
    assert len({a, TargetRead(twin, TARGET_READ_OK)}) == 1
    # Not equal to some other type, and not raising either: only strings raise.
    assert a != 17


def test_a_target_read_that_is_ok_must_carry_a_target():
    """The invariant nothing enforced. `TargetRead(None, TARGET_READ_OK)`
    constructed fine, and every caller that checks `.outcome` and then reads
    `.target` would have been handed a None it has no branch for."""
    with pytest.raises(ValueError):
        TargetRead(None, TARGET_READ_OK)


def test_a_target_read_that_is_not_ok_must_carry_no_target(fake):
    """The other direction, which matters just as much: a payload on an
    UNAVAILABLE read is a target we are simultaneously claiming not to have
    obtained."""
    _seed_target(fake, id="t-live")
    target = read_target("t-live", user_id="u-1").target
    for outcome in (TARGET_READ_ABSENT, TARGET_READ_UNAVAILABLE):
        with pytest.raises(ValueError):
            TargetRead(target, outcome)


def test_a_target_read_refuses_an_outcome_that_is_not_one_of_the_three():
    """A typo'd outcome is a branch that silently never fires -- `.unavailable`
    answers False and every `== TARGET_READ_*` test answers False, so the read
    reports as OK-shaped without being OK."""
    with pytest.raises(ValueError):
        TargetRead(None, "unavailble")


# ---------------------------------------------------------------------------
# Listing and duplicate lookup
#
# Both of these shipped with ZERO tests: QC removed the owner scope from one
# and the archived filter from the other and nothing failed.
# ---------------------------------------------------------------------------


def _seed_target(fake, **kw):
    row = {
        "id": kw.pop("id", str(uuid.uuid4())),
        "user_id": kw.pop("user_id", "u-1"),
        "kind": "pdb",
        "storage_path": "u-1/target-x/t.pdb",
        "sha256": kw.pop("sha256", "abc123"),
        "archived_at": kw.pop("archived_at", None),
        "created_at": kw.pop("created_at", "2026-07-01T00:00:00Z"),
    }
    row.update(kw)
    fake.store.setdefault("design_targets", []).append(row)
    return row


def test_list_targets_hides_archived_ones(fake):
    _seed_target(fake, id="t-live")
    _seed_target(fake, id="t-gone", archived_at="2026-07-02T00:00:00Z")
    ids = [t.id for t in list_targets_for_user("u-1")]
    assert ids == ["t-live"]


def test_list_targets_is_owner_scoped(fake):
    _seed_target(fake, id="t-mine", user_id="u-1")
    _seed_target(fake, id="t-theirs", user_id="u-2")
    assert [t.id for t in list_targets_for_user("u-1")] == ["t-mine"]


def test_list_targets_clamps_a_limit_past_the_row_cap(fake):
    """A caller that asks for more than one page must not believe it got
    everything — PostgREST would clamp it anyway, silently."""
    for i in range(5):
        _seed_target(fake, id=f"t-{i}")
    assert len(list_targets_for_user("u-1", limit=99999)) == 5


def test_find_by_sha256_is_owner_scoped(fake):
    """Unscoped, this offers user A's target to user B — leaking its id and
    name through the duplicate-upload prompt."""
    _seed_target(fake, id="t-theirs", user_id="u-2", sha256="deadbeef")
    assert find_target_by_sha256("u-1", "deadbeef") is None
    assert find_target_by_sha256("u-2", "deadbeef").id == "t-theirs"


def test_find_by_sha256_ignores_archived_targets(fake):
    """Offering an archived target would hand back one whose structure the
    retention sweeper is free to delete."""
    _seed_target(fake, id="t-gone", sha256="cafe",
                 archived_at="2026-07-02T00:00:00Z")
    assert find_target_by_sha256("u-1", "cafe") is None


def test_find_by_sha256_returns_the_most_recent_match(fake):
    _seed_target(fake, id="t-old", sha256="cafe", created_at="2026-01-01T00:00:00Z")
    _seed_target(fake, id="t-new", sha256="cafe", created_at="2026-07-01T00:00:00Z")
    assert find_target_by_sha256("u-1", "cafe").id == "t-new"


def test_find_by_sha256_ignores_an_empty_hash(fake):
    _seed_target(fake, id="t-1", sha256=None)
    assert find_target_by_sha256("u-1", "") is None


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


def test_campaign_ids_for_target_pages_past_the_row_clamp(fake):
    """2400 runs on one target. A bare select (or .limit()) comes back clamped
    to 1000 and the truncation is invisible at the call site, so the run list
    would silently lose more than half its rows."""
    fake.store["compute_campaigns"] = [
        {"id": f"c-{i:05d}", "target_id": "t-1", "user_id": "u-1"}
        for i in range(2400)
    ]
    ids, complete = campaign_ids_for_target("t-1", user_id="u-1")
    assert len(ids) == 2400
    assert ids[0] == "c-00000"
    assert ids[-1] == "c-02399"
    # Read the whole set, so it says so. The flag is only meaningful if the
    # happy path actually asserts True; a function returning False forever
    # would satisfy the failure test above on its own.
    assert complete is True

    # Proof the assertion above has teeth: against this same fake, the two
    # non-paged reads someone might "simplify" back to both come up short, and
    # neither says so. If this ever passes, the fake stopped modelling
    # PostgREST and the test above became decoration.
    bare = fake.table("compute_campaigns").select("id").eq("target_id", "t-1")
    assert len(bare.execute().data) == _MAX_ROWS
    limited = (
        fake.table("compute_campaigns")
        .select("id").eq("target_id", "t-1").limit(5000)
    )
    assert len(limited.execute().data) == _MAX_ROWS


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


def test_archive_stamps_a_timestamp_and_never_touches_storage(fake):
    """_dispatch_chunk re-mints a presigned URL from the staged input on EVERY
    wave, so deleting the object would break every chunk of every live run."""
    with patch("shared.targets.upload_input", return_value="u-1/target-x/t.pdb"):
        target = create_target(user_id="u-1", upload=_Upload())

    with patch("shared.storage.delete_objects") as deleted:
        assert archive_target(target.id, "u-1") is True
    deleted.assert_not_called()

    row = fake.store["design_targets"][0]
    assert row["archived_at"]


def test_archive_is_owner_scoped(fake):
    with patch("shared.targets.upload_input", return_value="u-1/target-x/t.pdb"):
        target = create_target(user_id="u-1", upload=_Upload())
    assert archive_target(target.id, "u-2") is False
    assert not fake.store["design_targets"][0].get("archived_at")


def test_unarchive_clears_the_timestamp_and_restores_the_target(fake):
    """Archive was one-way from the UI, so a mis-click permanently removed a
    structure the user had paid to run against."""
    _seed_target(fake, id="t-1", archived_at="2026-07-02T00:00:00Z")

    assert unarchive_target("t-1", "u-1") is True

    row = fake.store["design_targets"][0]
    assert row["archived_at"] is None
    assert [t.id for t in list_targets_for_user("u-1")] == ["t-1"]


def test_unarchive_is_owner_scoped(fake):
    _seed_target(fake, id="t-1", archived_at="2026-07-02T00:00:00Z")
    assert unarchive_target("t-1", "u-2") is False
    assert fake.store["design_targets"][0]["archived_at"] == "2026-07-02T00:00:00Z"


def test_archived_only_returns_exactly_the_complement_of_the_live_list(fake):
    """The two reads must partition the user's targets, not overlap or drop
    one: the archived list is the only route to the restore control short of
    pasting a URL. The PREDICATE partitions; the per-section cap is a separate
    limit that :func:`test_each_section_is_capped_independently` covers."""
    _seed_target(fake, id="t-live-1")
    _seed_target(fake, id="t-live-2")
    _seed_target(fake, id="t-gone", archived_at="2026-07-02T00:00:00Z")

    live = {t.id for t in list_targets_for_user("u-1")}
    archived = {t.id for t in list_targets_for_user("u-1", archived_only=True)}

    assert live == {"t-live-1", "t-live-2"}
    assert archived == {"t-gone"}
    assert live & archived == set()
    assert live | archived == {"t-live-1", "t-live-2", "t-gone"}


def test_archived_only_is_owner_scoped(fake):
    _seed_target(fake, id="mine", archived_at="2026-07-02T00:00:00Z")
    _seed_target(fake, id="theirs", user_id="u-2",
                 archived_at="2026-07-02T00:00:00Z")
    assert [t.id for t in list_targets_for_user("u-1", archived_only=True)] == ["mine"]


def test_archived_list_is_ordered_by_when_it_was_archived(fake):
    """Ordering the archived section by created_at buries the target the user
    just archived under ones archived months earlier, and past the cap drops
    it entirely -- which reads as "archiving deleted my target"."""
    _seed_target(fake, id="old-file-just-archived",
                 created_at="2025-01-01T00:00:00Z",
                 archived_at="2026-07-20T00:00:00Z")
    _seed_target(fake, id="new-file-long-archived",
                 created_at="2026-07-01T00:00:00Z",
                 archived_at="2026-02-01T00:00:00Z")

    ids = [t.id for t in list_targets_for_user("u-1", archived_only=True)]

    assert ids == ["old-file-just-archived", "new-file-long-archived"]


def test_each_section_is_capped_independently(fake):
    """The complement is exact as a predicate but neither list is exhaustive.
    A caller that treats a full page as "all of them" is wrong, and on this
    page a dropped archived target is indistinguishable from a deleted one."""
    for i in range(7):
        _seed_target(fake, id=f"arch-{i}",
                     archived_at="2026-07-%02dT00:00:00Z" % (i + 1))
    _seed_target(fake, id="live-1")

    archived = list_targets_for_user("u-1", archived_only=True, limit=5)

    assert len(archived) == 5
    assert [t.id for t in archived] == [
        "arch-6", "arch-5", "arch-4", "arch-3", "arch-2",
    ]
    # arch-1 and arch-0 exist and are archived, but this read cannot see them.
    assert len(list_targets_for_user("u-1", archived_only=True, limit=100)) == 7


def test_unarchive_reports_false_when_the_target_was_never_archived(fake):
    """The bool has to mean "was archived, now live", not "row exists and is
    yours". The route reports a successful restore off the back of it."""
    _seed_target(fake, id="already-live", archived_at=None)

    assert unarchive_target("already-live", "u-1") is False


# ---------------------------------------------------------------------------
# Per-run validation against the persisted inspection
# ---------------------------------------------------------------------------


def _target_with_summary():
    return DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", chain_summary=_Upload().chain_summary,
    )


def test_chain_error_rejects_a_chain_not_in_the_target():
    """A run from a stored target never re-uploads, so validate_target_chain
    never runs and a typo would otherwise reach the GPU."""
    t = _target_with_summary()
    assert t.chain_error("A") is None
    err = t.chain_error("B")
    assert err and "'B'" in err and "A" in err


def test_chain_error_rejects_a_ligand_only_chain():
    t = _target_with_summary()
    err = t.chain_error("L")
    assert err and "no standard protein residues" in err


def test_chain_error_fails_closed_on_a_pdb_target_with_no_summary():
    """"No summary" must not silently mean "no validation". The state is
    unreachable through the create route today, but create_target is a plain
    function and one future caller would otherwise put a structure on the GPU
    with nothing checked."""
    t = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb", chain_summary=None,
    )
    err = t.chain_error("A")
    assert err and "never inspected" in err


def test_chain_error_skips_an_sdf_target():
    """A small molecule has no protein chains to name."""
    t = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", kind="sdf", chain_summary=None,
    )
    assert t.chain_error("A") is None


def test_chain_error_skips_when_no_chain_was_requested():
    t = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb", chain_summary=None,
    )
    assert t.chain_error("") is None


def test_hotspot_error_accepts_a_residue_in_any_named_chain():
    """``target_chain`` may name several ("A B"), which rfdiffusion's
    validator accepts. A residue in the second chain is in range."""
    summary = {
        "chains": [
            {"chain_id": "A", "standard_residue_count": 210,
             "hetatm_resnames": [], "water_count": 0,
             "min_resnum": 1, "max_resnum": 210},
            {"chain_id": "B", "standard_residue_count": 50,
             "hetatm_resnames": [], "water_count": 0,
             "min_resnum": 300, "max_resnum": 350},
        ],
    }
    t = DesignTarget(id=str(uuid.uuid4()), user_id="u-1", chain_summary=summary)
    assert t.hotspot_error("A B", [30, 320]) is None
    err = t.hotspot_error("A B", [30, 9001])
    assert err and "9001" in err and "A 1-210" in err and "B 300-350" in err


def test_hotspot_error_flags_residues_outside_the_chain_range():
    t = _target_with_summary()
    assert t.hotspot_error("A", [12, 200]) is None
    err = t.hotspot_error("A", [12, 9001])
    assert err and "9001" in err and "1-210" in err


# ---------------------------------------------------------------------------
# Multi-chain hotspot validation (A18) — the two paths must agree
# ---------------------------------------------------------------------------


def _multi_chain_pdb(chains=("A", "B", "C"), lo=12, hi=159):
    """A full-backbone multi-chain structure.

    N/CA/C/O per residue, not CA-only: the normalizer runs with
    drop_zero_backbone=True, so a CA-only residue is discarded as having no
    backbone and the whole chain then reads as empty.
    """
    lines, n = [], 1
    for ch in chains:
        for r in range(lo, hi + 1):
            base = r * 3.8
            for atom, elem, dx in (
                ("N", "N", 0.0), ("CA", "C", 1.2), ("C", "C", 2.4), ("O", "O", 3.0),
            ):
                lines.append(
                    "ATOM  %5d  %-3s ALA %s%4d    %8.3f%8.3f%8.3f  1.00  0.00           %s"
                    % (n, atom, ch, r, base + dx, 0.0, 0.0, elem)
                )
                n += 1
    return ("\n".join(lines) + "\n").encode()


def test_validate_hotspots_accepts_a_multi_chain_target():
    """A18. validate_hotspots passed the whole string to report.chain(), got
    None for "A B C", and reported EVERY hotspot out of range — so a valid
    multi-chain hotspot set was refused on the atomic submit and reuse-token
    paths while sailing through the campaign and target-launch routes, which
    use DesignTarget.hotspot_error. The two paths must agree."""
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_multi_chain_pdb())
    in_range, out_of_range = validate_hotspots(report, "A B C", [113, 73])
    assert out_of_range == []
    assert sorted(in_range) == [73, 113]


def test_validate_hotspots_still_flags_a_genuinely_absent_residue():
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_multi_chain_pdb())
    in_range, out_of_range = validate_hotspots(report, "A B C", [113, 9001])
    assert in_range == [113] and out_of_range == [9001]


def test_validate_hotspots_single_chain_behaviour_is_unchanged():
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_multi_chain_pdb())
    assert validate_hotspots(report, "A", [113, 73]) == ([113, 73], [])
    assert validate_hotspots(report, "A", [5]) == ([], [5])


def test_normalizer_keeps_every_named_chain_of_a_multi_chain_target():
    """normalize_for_pipeline compared chain_id against the whole string, so a
    multi-token target_chain dropped every chain and then raised. proteina's
    preflight would have refused every multi-chain target."""
    import tempfile, os
    from shared.pipeline_normalize import normalize_for_proteina

    fd, path = tempfile.mkstemp(suffix=".pdb")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_multi_chain_pdb())
        report = normalize_for_proteina(path, None, target_chain="A B C")
        assert set(report.chains_kept) == {"A", "B", "C"}
        # Numbering must survive: proteina matches hotspots on the ORIGINAL
        # author numbers, silently, so renumbering would void every hotspot.
        assert not report.renumber_map
    finally:
        os.unlink(path)


def test_normalizer_still_rejects_a_chain_that_is_absent():
    import tempfile, os
    import pytest as _pytest
    from shared.pipeline_normalize import normalize_for_proteina

    fd, path = tempfile.mkstemp(suffix=".pdb")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_multi_chain_pdb(chains=("A", "B")))
        with _pytest.raises(ValueError) as exc:
            normalize_for_proteina(path, None, target_chain="A B Z")
        assert "Z" in str(exc.value)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Size gate on the campaign routes.
#
# The per-tool size cap lived only in preflight_for_tool, which only
# /tools/<slug>/submit calls — and that route refuses anything larger than one
# container. So the cap never guarded a campaign, which is the shape that
# spends real money. These cover the counting that makes a size gate possible
# without downloading the structure.
# ---------------------------------------------------------------------------

_FC_SUMMARY = {
    "total_standard_residues": 830,
    "chains": [
        {"chain_id": "A", "standard_residue_count": 208,
         "min_resnum": 236, "max_resnum": 443},
        {"chain_id": "B", "standard_residue_count": 207,
         "min_resnum": 236, "max_resnum": 442},
        {"chain_id": "C", "standard_residue_count": 208,
         "min_resnum": 237, "max_resnum": 444},
        {"chain_id": "D", "standard_residue_count": 207,
         "min_resnum": 238, "max_resnum": 444},
    ],
}


def test_selection_count_uses_the_contig_not_the_whole_file():
    from shared.targets import selection_residue_count
    n = selection_residue_count(
        _FC_SUMMARY, "A B", [("A", 236, 300), ("B", 236, 300)],
    )
    assert n == 130          # the canaried window, not the 830 aa file


def test_selection_count_without_a_contig_is_the_named_chains():
    from shared.targets import selection_residue_count
    assert selection_residue_count(_FC_SUMMARY, "A B", []) == 415
    assert selection_residue_count(_FC_SUMMARY, "A B C D", None) == 830


def test_selection_count_clips_a_range_to_the_chain_it_names():
    """A range running past the end of the chain counts what exists, not what
    was typed — otherwise "A1-9999" would refuse itself."""
    from shared.targets import selection_residue_count
    assert selection_residue_count(_FC_SUMMARY, "A", [("A", 236, 9999)]) == 208


def test_selection_count_never_exceeds_the_chain_it_reads():
    """The summary has no resnum list, so a span is only an upper bound. It
    must still be clamped by the chain's actual residue count: a chain
    numbered 236-443 with gaps holds fewer than 208, and claiming more would
    refuse runs that fit."""
    from shared.targets import selection_residue_count
    sparse = {"chains": [{"chain_id": "A", "standard_residue_count": 50,
                          "min_resnum": 1, "max_resnum": 400}]}
    assert selection_residue_count(sparse, "A", [("A", 1, 400)]) == 50


def test_selection_count_says_it_cannot_tell_rather_than_guessing_zero():
    """A target predating the chain_summary column must not be blocked by a
    check that cannot see it — None means "no verdict", and 0 would mean "too
    small", which is a different and wrong answer."""
    from shared.targets import selection_residue_count
    assert selection_residue_count(None, "A", []) is None
    assert selection_residue_count({}, "A", []) is None


def test_a_malformed_segment_falls_back_to_the_larger_count():
    """An unreadable contig must round UP to the whole chains. Rounding down
    is what would let an oversized campaign through."""
    from shared.targets import selection_residue_count
    assert selection_residue_count(_FC_SUMMARY, "A B", [("A", 236)]) == 415


def test_size_error_refuses_an_over_cap_target_for_proteina():
    from shared.targets import DesignTarget
    t = DesignTarget(
        id="t-1", user_id="u-1", kind="pdb", name="Fc", filename="3s7g.pdb",
        storage_path="u-1/t-1/3s7g.pdb", target_chain="A B",
        chain_summary=_FC_SUMMARY,
    )
    # THE CAP IS PER TOOL, so a discriminating size has to sit BETWEEN two
    # tools' caps: proteina is 500 and boltzgen is 600, and a 559-residue
    # three-chain selection straddles them. This used to be posed at 415 aa,
    # over proteina's 140 cap and under rfdiffusion's 500 — but 415 is now the
    # largest size proteina has been MEASURED at, so that pair discriminates
    # nothing.
    _over_500 = [("A", 236, 443), ("B", 236, 442), ("C", 237, 380)]   # 559 aa
    assert t.size_error("proteina", "A B C", _over_500) is not None
    assert t.size_error("boltzgen", "A B C", _over_500) is None
    # The whole CH2+CH3 pair — 415 aa, the motivating campaign — now fits.
    assert t.size_error("proteina", "A B", []) is None
    # And narrowed to the smallest canaried window it fits with room to spare.
    assert t.size_error(
        "proteina", "A B", [("A", 236, 300), ("B", 236, 300)],
    ) is None


def test_size_error_is_silent_when_it_cannot_see_a_summary():
    from shared.targets import DesignTarget
    t = DesignTarget(
        id="t-2", user_id="u-1", kind="pdb", name="old", filename="old.pdb",
        storage_path="u-1/t-2/old.pdb", target_chain="A", chain_summary=None,
    )
    assert t.size_error("proteina", "A", []) is None
