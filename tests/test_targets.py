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
    DesignTarget,
    _target_storage_key,
    archive_target,
    campaign_ids_for_target,
    create_target,
    find_target_by_sha256,
    get_target,
    list_targets_for_user,
)

# Mirrors supabase/config.toml. PostgREST caps every response at this many
# rows and clamps .limit() to it; .range() paging is the only way past.
_MAX_ROWS = 1000


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._op = "select"
        self._filters = []
        self._payload = None
        self._range = None
        self._limit = None
        self._single = False
        self._projection = None
        self._order = None

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
    assert campaign_ids_for_target("t-1", user_id="u-1") == ["c-1"]


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
    ids = campaign_ids_for_target("t-1", user_id="u-1")
    assert len(ids) == 2400
    assert ids[0] == "c-00000"
    assert ids[-1] == "c-02399"

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
