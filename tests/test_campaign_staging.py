"""Campaign promotion must resolve structures from Storage after slimming.

``_slim_result_for_persist`` drops the inline ``pdb_content_b64`` from
Storage-backed candidates, so ``stage_campaign_candidates`` (the reverse-funnel
"send shortlist to the lab" path) must fall back to the ``tool-outputs`` Storage
object behind ``pdb_key`` — otherwise promoting a BoltzGen pilot stages zero PDBs.
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
