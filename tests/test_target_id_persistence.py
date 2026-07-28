"""``target_id`` must actually reach the database row.

QC of this branch deleted ``row["target_id"] = target_id`` from BOTH
``shared/jobs.py::create_job`` and
``shared/compute_campaigns.py::create_campaign`` — so no run and no sub-job
would ever carry its target into Postgres — and every one of the 74 tests
written for the feature still passed. They all asserted that the keyword
argument reached a *mock*, never that it lands in the *insert payload*.

That column is the only thing joining a design to its target, so dropping it
silently produces a merged ranking missing designs the user paid for. These
tests capture the dict handed to ``.insert()`` and assert on it directly, which
is the only assertion a mocked ``create_job`` cannot fake.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")


class _Resp:
    def __init__(self, data):
        self.data = data


class _CapturingTable:
    """Records every insert payload verbatim."""

    def __init__(self, captured: list, name: str):
        self._captured = captured
        self._name = name
        self._row = None

    def insert(self, row):
        self._row = dict(row)
        self._captured.append((self._name, self._row))
        return self

    def execute(self):
        row = dict(self._row or {})
        row.setdefault("id", str(uuid.uuid4()))
        # Columns the dataclasses require but the caller does not supply.
        row.setdefault("job_token", "t" * 64)
        row.setdefault("status", "pending")
        return _Resp([row])


class _CapturingClient:
    def __init__(self):
        self.captured: list = []

    def table(self, name):
        return _CapturingTable(self.captured, name)


def _payload(client, table: str) -> dict:
    for name, row in client.captured:
        if name == table:
            return row
    raise AssertionError(f"no insert captured for {table}: {client.captured}")


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


def test_create_job_persists_target_id_in_the_insert_row():
    from shared.jobs import create_job

    client = _CapturingClient()
    tid = str(uuid.uuid4())
    with patch("shared.jobs.get_service_client", return_value=client):
        job = create_job(
            user_id="u-1", tool="rfdiffusion", preset="pilot",
            inputs={}, target_id=tid,
        )

    assert job is not None
    assert _payload(client, "tool_jobs")["target_id"] == tid
    # And it survives the round-trip back onto the dataclass.
    assert job.target_id == tid


def test_create_job_omits_target_id_entirely_when_there_is_none():
    """PostgREST 400s on a column the schema lacks, so an untargeted insert
    must not carry the key at all — that is what keeps ordinary runs working
    on a database without migration 0039."""
    from shared.jobs import create_job

    client = _CapturingClient()
    with patch("shared.jobs.get_service_client", return_value=client):
        create_job(user_id="u-1", tool="rfdiffusion", preset="pilot", inputs={})

    assert "target_id" not in _payload(client, "tool_jobs")


def test_create_job_does_not_drop_target_id_on_the_schema_gap_retry():
    """The campaign_label retry exists for a cosmetic column. target_id is
    load-bearing: retrying without it would file the design nowhere while
    reporting success."""
    from shared.jobs import create_job

    tid = str(uuid.uuid4())
    attempts: list = []

    class _FailingTable(_CapturingTable):
        def execute(self):
            attempts.append(dict(self._row or {}))
            raise RuntimeError(
                "PGRST204 Could not find the 'campaign_label' column"
            )

    class _FailingClient(_CapturingClient):
        def table(self, name):
            return _FailingTable(self.captured, name)

    client = _FailingClient()
    with patch("shared.jobs.get_service_client", return_value=client):
        create_job(
            user_id="u-1", tool="rfdiffusion", preset="pilot", inputs={},
            campaign_label="grouped", target_id=tid,
        )

    assert len(attempts) == 2, "expected the campaign_label retry"
    assert "campaign_label" not in attempts[1]
    assert attempts[1]["target_id"] == tid


# ---------------------------------------------------------------------------
# create_campaign
# ---------------------------------------------------------------------------


def test_create_campaign_persists_target_id_and_launch_group():
    from shared.compute_campaigns import create_campaign

    client = _CapturingClient()
    tid, gid = str(uuid.uuid4()), str(uuid.uuid4())
    with patch(
        "shared.compute_campaigns.get_service_client", return_value=client
    ):
        campaign = create_campaign(
            user_id="u-1", tool="rfdiffusion", params={},
            requested_designs=8, target_id=tid, launch_group_id=gid,
        )

    row = _payload(client, "compute_campaigns")
    assert row["target_id"] == tid
    assert row["launch_group_id"] == gid
    assert campaign is not None and campaign.target_id == tid


def test_create_campaign_omits_both_columns_when_unset():
    from shared.compute_campaigns import create_campaign

    client = _CapturingClient()
    with patch(
        "shared.compute_campaigns.get_service_client", return_value=client
    ):
        create_campaign(
            user_id="u-1", tool="rfdiffusion", params={}, requested_designs=8,
        )

    row = _payload(client, "compute_campaigns")
    assert "target_id" not in row
    assert "launch_group_id" not in row


def test_create_campaign_honours_a_divided_concurrency_target():
    """Phase 2 splits the global in-flight cap across a multi-tool launch, so
    the override has to reach the row rather than being silently replaced by
    the tool's own launch concurrency."""
    from shared.compute_campaigns import create_campaign, launch_concurrency_for

    client = _CapturingClient()
    with patch(
        "shared.compute_campaigns.get_service_client", return_value=client
    ):
        create_campaign(
            user_id="u-1", tool="rfdiffusion", params={},
            requested_designs=8, concurrency_target=3,
        )

    row = _payload(client, "compute_campaigns")
    assert row["concurrency_target"] == 3
    assert row["concurrency_target"] != launch_concurrency_for("rfdiffusion")
