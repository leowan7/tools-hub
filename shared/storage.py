"""Supabase Storage helper for GPU-tool input files.

Stream C (Wave-2 launch prep). Users upload a PDB / CIF / FASTA through
a tools-hub form; we stage it in the ``tool-inputs`` bucket under
``{user_id}/{job_id}/{filename}`` and generate a short-lived presigned
download URL that the Modal pipeline uses as ``input_pdb_url``.

Why not a Flask-served tempfile
-------------------------------
Modal containers can reach our Railway URL, but (a) tools-hub would
need a tokenised download route that outlives the Flask request handler
that accepted the upload, (b) one worker cannot read what another
worker wrote without a shared filesystem, and (c) Railway recycles
ephemeral storage on every restart. Supabase Storage solves all three
for free.

Usage
-----
    from shared.storage import upload_input, presigned_input_url

    # In the submit handler, after credits debit but before modal.submit():
    path = upload_input(
        user_id=ctx.user_id,
        job_id=job.id,
        filename="target.pdb",
        data=uploaded_file.read(),
        content_type="chemical/x-pdb",
    )
    url = presigned_input_url(path, expires_seconds=7200)
    # pass url to modal_client as inputs["_input_pdb_url"]

Environment
-----------
Uses the same ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY`` as the
rest of the app. No extra configuration.
"""

from __future__ import annotations

import logging
from typing import Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

BUCKET = "tool-inputs"

# Application-layer size cap. Should match migration 0006's bucket
# file_size_limit; keep them in sync if one changes.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


class StorageError(RuntimeError):
    """Raised when a Storage upload or URL generation fails."""


def upload_input(
    *,
    user_id: str,
    job_id: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload ``data`` under ``{user_id}/{job_id}/{filename}`` and return the object path.

    The object path is what ``presigned_input_url`` consumes. Callers
    should treat it as opaque.

    Raises:
        StorageError: when the bucket is unreachable, the payload is
            oversized, or the Storage API returns an error.
    """
    if len(data) == 0:
        raise StorageError("Refusing to upload empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise StorageError(
            f"File exceeds {MAX_UPLOAD_BYTES} byte cap ({len(data)} bytes)."
        )

    safe_filename = _safe_filename(filename)
    path = f"{user_id}/{job_id}/{safe_filename}"

    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(BUCKET)
        # supabase-py's upload signature varies slightly between versions
        # (``file`` vs. ``data``); the bytes path is the common one.
        bucket.upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:
        logger.error("Storage upload failed for %s", path, exc_info=True)
        raise StorageError(f"upload failed: {exc}") from exc
    return path


def presigned_input_url(object_path: str, *, expires_seconds: int = 7200) -> str:
    """Return a presigned download URL valid for ``expires_seconds`` seconds.

    Default 2 hours — long enough for Modal to start a container and
    download the file before a pipeline kicks off. Caller can extend
    for longer pilot/full runs.
    """
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(BUCKET)
        result = bucket.create_signed_url(object_path, expires_seconds)
    except Exception as exc:
        logger.error("Signed URL request failed for %s", object_path, exc_info=True)
        raise StorageError(f"signed URL failed: {exc}") from exc

    # supabase-py returns ``{"signedURL": "..."}`` on v2.x and
    # ``{"signedUrl": "..."}`` on older versions. Be defensive.
    if isinstance(result, dict):
        for key in ("signedURL", "signedUrl", "signed_url"):
            if result.get(key):
                return str(result[key])
    raise StorageError(f"unexpected signed URL response: {result!r}")


def download_input(object_path: str) -> bytes:
    """Download the object at ``object_path`` via the service client.

    Used by Wave 3 clone + Scout-handoff flows to stage a PDB that was
    already uploaded (by the original job or by Scout) into a new job's
    storage prefix without making the user re-upload.
    """
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(BUCKET)
        data = bucket.download(object_path)
    except Exception as exc:
        logger.error("Storage download failed for %s", object_path, exc_info=True)
        raise StorageError(f"download failed: {exc}") from exc
    if not data:
        raise StorageError(f"empty object at {object_path}")
    return data


def copy_input(
    *,
    source_path: str,
    dest_user_id: str,
    dest_job_id: str,
    filename: str,
    content_type: str = "chemical/x-pdb",
) -> str:
    """Copy an existing object to ``{dest_user_id}/{dest_job_id}/{filename}``.

    Download-then-upload since supabase-py has no server-side copy op.
    Used by clone + Scout handoff to reuse a previously staged PDB
    under the new job's path so the RLS owner-prefix still holds.
    """
    data = download_input(source_path)
    return upload_input(
        user_id=dest_user_id,
        job_id=dest_job_id,
        filename=filename,
        data=data,
        content_type=content_type,
    )


def delete_input(object_path: str) -> bool:
    """Remove a previously uploaded object. Used for cleanup on failure."""
    client = get_service_client()
    if client is None:
        return False
    try:
        client.storage.from_(BUCKET).remove([object_path])
        return True
    except Exception:
        logger.warning("Storage delete failed for %s", object_path, exc_info=True)
        return False


def _safe_filename(name: str) -> str:
    """Strip path components and dangerous characters from a filename.

    Matches the Werkzeug secure_filename approach but with one tweak —
    we keep the original extension (Werkzeug sometimes normalises to
    lowercase which is fine but explicit).
    """
    from werkzeug.utils import secure_filename  # noqa: PLC0415
    safe = secure_filename(name) or "upload"
    return safe
