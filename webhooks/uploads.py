"""Modal pipeline upload-URL minter.

When tools-hub submits a pilot/full-tier job to Modal, it passes an
absolute ``upload_urls_endpoint`` URL that the pipeline calls back to
obtain presigned Storage PUT URLs for each candidate PDB. This avoids
inlining base64-encoded PDBs in the webhook return payload — the
pipeline writes designs directly into ``tool-outputs/`` and tools-hub
resolves them on demand at results-render time.

The endpoint is mounted at::

    POST /api/upload-urls/<job_id>/<job_token>

Authenticated via the same shared-secret token scheme as
``/webhooks/modal/<job_id>/<job_token>`` — mismatch returns 403, no
matching tool_jobs row returns 404. Request body::

    {"filenames": ["design_1.pdb", "design_2.pdb", ...]}

Response body on success::

    {"urls": {"design_1.pdb": "<presigned PUT URL>", ...}}

Each URL is a Supabase Storage signed upload URL scoped to
``tool-outputs/{user_id}/{job_id}/designs/<filename>``. Modal does
HTTP PUT to each URL with the PDB bytes; on success the file is
visible to the browser-facing download route immediately.

Why a peer of /webhooks/modal and not co-located inside it
---------------------------------------------------------
The semantic is the same as a webhook (Modal-initiated callback to
tools-hub) but the auth shape and the response payload are different
enough to keep concerns separate. A bug in upload-URL minting should
not be able to crash the result-handler module.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from flask import Flask, Response, jsonify, request

from shared.jobs import get_job
from shared.storage import StorageError, presigned_output_put_url

logger = logging.getLogger(__name__)


# Hard ceiling on filenames per request — prevents a runaway pipeline
# from spamming the endpoint. Real pilot tier emits 50-200 designs;
# 500 leaves comfortable headroom.
MAX_FILENAMES_PER_REQUEST = 500


def register_upload_urls(flask_app: Flask) -> None:
    """Mount the Modal-facing upload-URL minter on the given app."""

    @flask_app.route(
        "/api/upload-urls/<job_id>/<job_token>", methods=["POST"]
    )
    def upload_urls(job_id: str, job_token: str) -> Any:  # noqa: ANN401
        return _handle(job_id, job_token)


def _handle(job_id: str, job_token: str) -> Any:
    job = get_job(job_id)
    if job is None:
        logger.warning("upload-urls: unknown job id %s", job_id)
        return Response("unknown job", status=404)

    if not hmac.compare_digest(job.job_token, job_token):
        logger.warning("upload-urls: token mismatch for job %s", job_id)
        return Response("forbidden", status=403)

    payload = request.get_json(silent=True) or {}
    filenames = payload.get("filenames")
    if not isinstance(filenames, list) or not filenames:
        return (
            jsonify({"error": "filenames must be a non-empty list"}),
            400,
        )
    if len(filenames) > MAX_FILENAMES_PER_REQUEST:
        return (
            jsonify(
                {
                    "error": (
                        f"too many filenames ({len(filenames)} > "
                        f"{MAX_FILENAMES_PER_REQUEST})"
                    )
                }
            ),
            400,
        )
    for name in filenames:
        if not isinstance(name, str) or not name.strip():
            return (
                jsonify({"error": "filenames must be non-empty strings"}),
                400,
            )

    urls: dict[str, str] = {}
    for name in filenames:
        try:
            urls[name] = presigned_output_put_url(
                user_id=job.user_id,
                job_id=job.id,
                filename=name,
            )
        except StorageError as exc:
            logger.error(
                "upload-urls: failed to mint URL for %s/%s: %s",
                job_id,
                name,
                exc,
            )
            return jsonify({"error": "storage unavailable"}), 502

    return jsonify({"urls": urls})
