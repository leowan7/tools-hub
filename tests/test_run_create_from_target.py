"""Launching a run against a stored target.

The point of a target is that the structure is staged ONCE. These tests pin
that: the run inherits the target's existing storage path, `upload_input` is
never called a second time, and the per-run chain/hotspot overrides are still
validated — against the inspection persisted at upload time, so no download
happens either.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These tests assert ownership and isolation, so they must not consult the
# live database that app.py's load_dotenv() would otherwise hand them.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com",
    )


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


# A genuinely parseable PDB with chain A, so the override path clears
# resolve_target_upload rather than failing before the branch under test.
_PDB = b"""ATOM      1  N   MET A   1      11.104  13.207  10.000  1.00 20.00           N
ATOM      2  CA  MET A   1      12.560  13.207  10.000  1.00 20.00           C
ATOM      3  C   MET A   1      13.100  14.600  10.000  1.00 20.00           C
ATOM      4  O   MET A   1      12.400  15.600  10.000  1.00 20.00           O
ATOM      5  N   ALA A   2      14.400  14.700  10.000  1.00 20.00           N
ATOM      6  CA  ALA A   2      15.100  15.980  10.000  1.00 20.00           C
ATOM      7  C   ALA A   2      16.600  15.800  10.000  1.00 20.00           C
ATOM      8  O   ALA A   2      17.100  14.700  10.000  1.00 20.00           O
END
"""


def _target(**kw):
    base = dict(
        id=str(uuid.uuid4()),
        user_id="u-1",
        name="HER2",
        filename="her2.pdb",
        storage_path="u-1/target-abc/her2.pdb",
        target_chain="A",
        hotspot_residues=[42, 88],
        chain_summary={
            "total_standard_residues": 210,
            "chains": [{
                "chain_id": "A", "standard_residue_count": 210,
                "hetatm_resnames": [], "water_count": 0,
                "min_resnum": 1, "max_resnum": 210,
            }],
        },
    )
    base.update(kw)
    return DesignTarget(**base)


def _form(**kw):
    data = {
        "tool": "rfdiffusion",
        "requested_designs": "8",
        "target_chain": "A",
        "hotspot_residues": "42,88",
        "binder_length_min": "55",
        "binder_length_max": "65",
    }
    data.update(kw)
    return data


def _preauth_ok():
    return SimpleNamespace(
        ok=True, reason=None, balance_usd=Decimal("1000"),
        required_usd=Decimal("1"),
    )


def test_run_form_swaps_the_upload_for_a_target_chip(client):
    _login(client)
    t = _target()
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t):
        resp = client.get(f"/campaigns/new?target_id={t.id}")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert f'name="target_id" value="{t.id}"' in body
    assert "HER2" in body
    # The file input must not be required (and must not submit) when the
    # structure is already staged.
    assert 'name="target_pdb" accept=".pdb,.cif,.mmcif" disabled' in body
    # Stored chain and hotspots prefill the form as per-run defaults.
    assert 'id="target_chain" name="target_chain" maxlength="4" value="A"' in body
    assert 'value="42,88"' in body


def test_run_form_falls_back_to_upload_for_an_unowned_target(client):
    """Do not confirm that someone else's id exists — just show the normal
    upload form."""
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=None):
        resp = client.get(f"/campaigns/new?target_id={uuid.uuid4()}")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="target_id"' not in body
    assert 'name="target_pdb" accept=".pdb,.cif,.mmcif" required' in body


def test_launching_from_a_target_never_re_stages_the_structure(client):
    _login(client)
    t = _target()
    created = SimpleNamespace(id="c-1")
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t), \
            patch("shared.targets.touch_target"), \
            patch("shared.compute_campaigns.campaign_preauth",
                  return_value=_preauth_ok()), \
            patch("blueprints.campaigns.upload_input") as staged, \
            patch("shared.compute_campaigns.create_campaign",
                  return_value=created) as mk, \
            patch("shared.compute_campaigns.fund_campaign"), \
            patch("shared.compute_campaigns.drive_campaign_async"):
        resp = client.post("/campaigns", data=_form(target_id=t.id))

    assert resp.status_code == 302
    # The whole point: one upload, many runs.
    staged.assert_not_called()
    kwargs = mk.call_args.kwargs
    # The path is DENORMALIZED onto the run, which is what keeps the driver
    # unchanged: _dispatch_chunk re-mints its presigned URL from this column
    # every wave and never learns design_targets exist.
    assert kwargs["target_storage_path"] == t.storage_path
    assert kwargs["target_id"] == t.id


def test_launching_from_a_target_stamps_it_on_every_sub_job(client):
    """A sub-job carries target_id so a design is attributable without joining
    back through compute_campaigns — the fan-in reads tool_jobs directly."""
    from shared.compute_campaigns import ComputeCampaign

    campaign = ComputeCampaign(
        id="c-1", user_id="u-1", tool="rfdiffusion", preset="pilot",
        status="running", requested_designs=8, chunk_size=8, total_subjobs=1,
        concurrency_target=1, max_attempts=2,
        budget_usd=Decimal("10"), reserved_usd=Decimal("0"),
        spent_usd=Decimal("0"), refunded_usd=Decimal("0"),
        params={"target_chain": "A"},
        target_storage_path="u-1/target-abc/her2.pdb",
        target_id="t-42",
    )
    # _dispatch_chunk imports these inside the function body, so they have to
    # be patched at their source modules (same as the driver test fixture).
    with patch("shared.storage.presigned_input_url",
               return_value="https://signed"), \
            patch("shared.wallet.reserve_hold", return_value=7), \
            patch("shared.wallet.release_hold"), \
            patch("shared.jobs.create_job", return_value=None) as mk:
        from shared.compute_campaigns import _dispatch_chunk
        _dispatch_chunk(campaign, 0)

    assert mk.call_args.kwargs["target_id"] == "t-42"


def test_an_attached_file_overrides_the_target_and_drops_the_link(client):
    """Previously the target won and the posted file was discarded in silence,
    so the user paid for a campaign against a structure they did not send. Now
    the upload wins — matching the atomic form's documented override — and the
    target link is dropped with it, so a design from structure Y cannot appear
    in target X's merged ranking."""
    import io
    _login(client)
    t = _target()
    created = SimpleNamespace(id="c-1")
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.campaign_preauth",
                  return_value=_preauth_ok()), \
            patch("blueprints.campaigns.upload_input",
                  return_value="u-1/campaign-x/mine.pdb") as staged, \
            patch("shared.compute_campaigns.create_campaign",
                  return_value=created) as mk, \
            patch("shared.compute_campaigns.fund_campaign"), \
            patch("shared.compute_campaigns.drive_campaign_async"):
        resp = client.post("/campaigns", data=dict(
            _form(target_id=t.id),
            target_pdb=(io.BytesIO(_PDB), "mine.pdb"),
        ), content_type="multipart/form-data")

    assert resp.status_code == 302
    staged.assert_called_once()
    kwargs = mk.call_args.kwargs
    assert kwargs["target_storage_path"] == "u-1/campaign-x/mine.pdb"
    assert kwargs["target_id"] is None


def test_a_run_cannot_be_launched_against_someone_elses_target(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=None) as fetch, \
            patch("shared.compute_campaigns.create_campaign") as mk:
        resp = client.post("/campaigns", data=_form(target_id=str(uuid.uuid4())))

    assert resp.status_code == 400
    assert "could not be found" in resp.get_data(as_text=True)
    mk.assert_not_called()
    # Owner scope is enforced in the query, not after the fetch.
    assert fetch.call_args.kwargs["user_id"] == "u-1"


def test_an_archived_target_cannot_be_launched_against(client):
    _login(client)
    t = _target(archived_at="2026-07-01T00:00:00Z")
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.create_campaign") as mk:
        resp = client.post("/campaigns", data=_form(target_id=t.id))

    assert resp.status_code == 400
    assert "archived" in resp.get_data(as_text=True)
    mk.assert_not_called()


def test_a_per_run_chain_override_is_still_validated(client):
    """The structure is never re-uploaded, so resolve_target_upload (and with
    it validate_target_chain) never runs. Without this check a typo'd chain
    would reach the GPU and burn the whole run."""
    _login(client)
    t = _target()
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t), \
            patch("shared.storage.download_input") as downloaded, \
            patch("shared.compute_campaigns.create_campaign") as mk:
        resp = client.post("/campaigns", data=_form(target_id=t.id, target_chain="Z"))

    assert resp.status_code == 400
    assert "is not in this target" in resp.get_data(as_text=True)
    mk.assert_not_called()


def test_a_per_run_hotspot_override_is_range_checked(client):
    _login(client)
    t = _target()
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.create_campaign") as mk:
        resp = client.post(
            "/campaigns", data=_form(target_id=t.id, hotspot_residues="42,9001"),
        )

    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "9001" in body and "1-210" in body
    mk.assert_not_called()
