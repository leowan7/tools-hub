"""Campaign promotion must resolve structures from Storage after slimming.

``_slim_result_for_persist`` drops the inline ``pdb_content_b64`` from
Storage-backed candidates, so ``stage_campaign_candidates`` (the reverse-funnel
"send shortlist to the lab" path) must fall back to the ``tool-outputs`` Storage
object behind ``pdb_key`` — otherwise promoting a BoltzGen pilot stages zero PDBs.

The second half is the B-factor scale. #202 put every customer-facing
download on 0-100; this bucket kept the stored 0-1, so a Ranomics
scientist opening the shortlist in PyMOL and the customer who downloaded
the same design were colouring different scales. Nothing in the app reads
this bucket back — ``presigned_campaign_url`` signs only the operator
results envelope — so there is no read-side seam and the conversion
happens here, on the way in.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

from shared import storage as storage_mod


def _patched_bucket():
    """Patch get_service_client so bucket.upload is a no-op MagicMock."""
    client = MagicMock()
    return patch.object(storage_mod, "get_service_client", lambda: client), client


def test_stages_from_storage_when_inline_slimmed():
    cand = {"rank": 1, "pdb_key": "designs/design_001.cif", "scores": {}}
    ctx, _client = _patched_bucket()
    with ctx, patch.object(
        storage_mod, "download_output", return_value=b"ATOM\n"
    ) as dl:
        written = storage_mod.stage_campaign_candidates(
            campaign_id="camp-1",
            candidates=[cand],
            indices=[0],
            user_id="u1",
            job_id="job-1",
        )
    assert len(written) == 1
    dl.assert_called_once_with(
        user_id="u1", job_id="job-1", filename="designs/design_001.cif"
    )


def test_prefers_inline_when_present():
    cand = {
        "rank": 1,
        "pdb_key": "designs/design_001.cif",
        "pdb_content_b64": base64.b64encode(b"ATOM\n").decode(),
        "scores": {},
    }
    ctx, _client = _patched_bucket()
    with ctx, patch.object(storage_mod, "download_output") as dl:
        written = storage_mod.stage_campaign_candidates(
            campaign_id="camp-1",
            candidates=[cand],
            indices=[0],
            user_id="u1",
            job_id="job-1",
        )
    assert len(written) == 1
    dl.assert_not_called()


# --------------------------------------------------------------------------
# B-factor scale (item C behind #202)
# --------------------------------------------------------------------------

def _atom(serial: int, bfactor: float) -> str:
    """One ATOM record with ``bfactor`` in columns 61-66."""
    return (
        f"ATOM  {serial:5d}  CA  LEU A{serial:4d}    "
        f"   0.000   0.000   0.000  1.00{bfactor:6.2f}           C\n"
    )


def _bfactors(data: bytes) -> list[float]:
    """Read the B-factor column out of the raw bytes.

    Deliberately does NOT go through ``shared.pdb_bfactors``: that module
    is what rewrote these bytes, so an assertion parsing them with it
    would be checking the module against itself.
    """
    return [
        float(line[60:66])
        for line in data.decode("utf-8").splitlines()
        if line.startswith("ATOM") and len(line) >= 66
    ]


def _uploaded(client) -> bytes:
    """The bytes handed to ``bucket.upload``."""
    call = client.storage.from_.return_value.upload.call_args
    assert call is not None, "nothing was uploaded"
    return call.kwargs["file"]


def _stage(cand, *, download=None):
    ctx, client = _patched_bucket()
    dl = patch.object(storage_mod, "download_output", return_value=download)
    with ctx, dl:
        written = storage_mod.stage_campaign_candidates(
            campaign_id="camp-1",
            candidates=[cand],
            indices=[0],
            user_id="u1",
            job_id="job-1",
        )
    assert len(written) == 1
    return _uploaded(client)


def test_an_inline_fractional_payload_is_staged_on_0_100():
    """The headline. An ESMFold/proteina/boltz2 design stores pLDDT as a
    fraction; the staff copy must carry it the way PyMOL's usual
    ``spectrum b, ..., minimum=50, maximum=90`` reads it."""
    pdb = _atom(1, 0.21) + _atom(2, 0.66)
    cand = {
        "rank": 1,
        "pdb_key": "designs/design_001.pdb",
        "pdb_content_b64": base64.b64encode(pdb.encode()).decode(),
    }
    assert _bfactors(_stage(cand)) == [21.0, 66.0]


def test_the_exact_column_layout_survives():
    """Not just the value — the field is fixed-width and every viewer
    parses it by offset. A converted record has to still be a legal ATOM
    record, terminator included."""
    cand = {
        "rank": 1,
        "pdb_key": "designs/d.pdb",
        "pdb_content_b64": base64.b64encode(_atom(1, 0.21).encode()).decode(),
    }
    assert _stage(cand).decode("utf-8") == (
        "ATOM      1  CA  LEU A   1       0.000   0.000   0.000"
        "  1.00 21.00           C\n"
    )


def test_a_storage_resolved_payload_is_converted_too():
    """The path every modern job takes: the inline copy was slimmed off
    the row, so the bytes arrive from ``tool-outputs``. Converting only
    the inline branch would have missed almost every real campaign."""
    cand = {"rank": 1, "pdb_key": "designs/design_001.pdb", "scores": {}}
    pdb = (_atom(1, 0.21) + _atom(2, 0.66)).encode()
    assert _bfactors(_stage(cand, download=pdb)) == [21.0, 66.0]


def test_a_payload_already_on_0_100_is_uploaded_byte_for_byte():
    """af2, colabfold and pxdesign already store 0-100. Re-encoding what
    the gate declined is how an earlier draft of the bytes helper
    destroyed non-UTF-8 content, so identity is the assertion here, not
    equality."""
    pdb = (_atom(1, 88.50) + _atom(2, 42.00)).encode()
    cand = {"rank": 1, "pdb_key": "designs/d.pdb", "scores": {}}
    staged = _stage(cand, download=pdb)
    assert staged is pdb
    assert _bfactors(staged) == [88.50, 42.00]


def test_a_crystal_target_keeps_its_real_b_factors():
    """The whole-file gate, on the case that motivated it.
    ``static/example/1HEW.pdb`` runs 0.01 to 150.80 — a per-atom rule
    would scale the 0.01 atom to 1.00 and leave the 150.80, corrupting a
    deposition it has no business touching."""
    pdb = (_atom(1, 0.01) + _atom(2, 150.80)).encode()
    cand = {"rank": 1, "pdb_key": "designs/target.pdb", "scores": {}}
    assert _bfactors(_stage(cand, download=pdb)) == [0.01, 150.80]


def test_an_unreadable_coordinate_record_declines_the_whole_file():
    """Fails closed, the same way the download routes do. A stitched
    binder+target complex whose target chain carries a blank occupancy
    must not arrive with one chain converted and the other not."""
    blank_occupancy = (
        "ATOM      2  CA  LEU A   2    "
        "   0.000   0.000   0.000      49.00           C\n"
    )
    pdb = (_atom(1, 0.11) + blank_occupancy).encode()
    cand = {"rank": 1, "pdb_key": "designs/complex.pdb", "scores": {}}
    staged = _stage(cand, download=pdb)
    assert staged is pdb
    assert _bfactors(staged) == [0.11, 49.00]
