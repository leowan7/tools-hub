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

from shared import pdb_bfactors as _pdb_bfactors
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
    """Remove a previously uploaded ``tool-inputs`` object.

    Thin, backwards-compatible wrapper over the bucket-generic
    :func:`delete_objects`. Kept so callers that only need to drop a
    single input (e.g. cleanup on a failed submit) have a named entry
    point; the retention sweeper and per-user erasure use
    :func:`delete_objects` directly across all three buckets.
    """
    return delete_objects(BUCKET, [object_path]) > 0


CAMPAIGN_BUCKET = "lab-campaigns"


def stage_campaign_candidates(
    *,
    campaign_id: str,
    candidates: list[dict],
    indices: list[int],
    user_id: str,
    job_id: str,
    prefix: str = "",
) -> list[str]:
    """Copy the shortlisted candidates' PDB payloads into the
    ``lab-campaigns/{campaign_id}/`` folder so Ranomics staff can read
    them independently of the source job's payload.

    ``candidates`` is ``job.result["candidates"]``; each entry resolves to
    bytes the same two ways the download routes use — inline
    ``pdb_content_b64`` if present, otherwise the ``tool-outputs`` Storage
    object behind ``pdb_key`` (``user_id``/``job_id`` locate it). ``indices``
    is the 0-based shortlist selected on the results page.

    Each payload goes through ``pdb_bfactors.bfactors_on_100_bytes`` on
    the way in, so the staff copy carries pLDDT on the same 0-100 scale
    the customer's own download has used since #202. Whole-file gated:
    anything already on 0-100, any crystal target, any mmCIF and anything
    unparseable is uploaded byte-for-byte unchanged.

    Returns the list of storage object paths written. Silently skips a
    candidate that resolves via neither path rather than failing the submit.
    """
    import base64  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    bucket = client.storage.from_(CAMPAIGN_BUCKET)
    written: list[str] = []
    for idx in indices:
        if idx < 0 or idx >= len(candidates):
            continue
        cand = candidates[idx] or {}
        raw_key = cand.get("pdb_key") or f"candidate_{idx}.pdb"
        encoded = cand.get("pdb_content_b64")
        data = None
        if encoded:
            try:
                data = base64.b64decode(encoded)
            except Exception:
                logger.warning("Candidate %s has un-decodable pdb_content_b64.", idx)
                continue
        elif cand.get("pdb_key"):
            # Inline copy was slimmed off the row — fetch from Storage.
            try:
                data = download_output(
                    user_id=user_id, job_id=job_id, filename=cand["pdb_key"],
                )
            except StorageError:
                logger.warning(
                    "Campaign stage: storage miss for %s/%s",
                    job_id, raw_key, exc_info=True,
                )
                continue
        if data is None:
            continue
        # ONE SCALE FOR THE STAFF COPY. Every customer-facing download has
        # served pLDDT B-factors on 0-100 since #202, but this bucket kept
        # the stored 0-1 -- so the Ranomics scientist opening the shortlist
        # in PyMOL and the customer who downloaded the same design from
        # /jobs were colouring two different scales and could not compare
        # notes. Staff are the readers MOST likely to reach for
        # `spectrum b, ..., minimum=50, maximum=90`.
        #
        # AT WRITE TIME, WHICH THE #202 NOTE SAID TO AVOID. It said to
        # convert at whatever READS the bucket; there is nothing to hook.
        # `presigned_campaign_url` signs only the operator-uploaded results
        # envelope (tools/platform_api/routes.py:806), never these objects,
        # and staff open them through the Supabase console. So the choice is
        # here or nowhere. It is also less of a departure than it sounds:
        # this bucket is a derived CRO deliverable keyed by campaign, not a
        # source of truth. tool-outputs still holds the untouched original,
        # and that is the copy every guarantee is written against.
        #
        # The gate does the discriminating, so no tool slug is consulted:
        # af2/colabfold/pxdesign already store 0-100 and decline on their
        # first ATOM record, a staged crystal target declines (1HEW runs
        # 0.01-150.80), a .cif declines twice over, and anything this module
        # cannot parse declines rather than being half-converted. Declined
        # files come back as the SAME object and upload byte-for-byte.
        data = _pdb_bfactors.bfactors_on_100_bytes(data)
        filename = _safe_filename(raw_key)
        # ``prefix`` namespaces a multi-sub-job campaign shortlist by source
        # job so identically-named designs from different sub-jobs don't
        # collide in the lab-campaigns bucket. Empty for the single-job path.
        path = f"{campaign_id}/{prefix}{filename}"
        try:
            bucket.upload(
                path=path,
                file=data,
                file_options={"content-type": "chemical/x-pdb", "upsert": "true"},
            )
        except Exception as exc:
            logger.error("Campaign PDB upload failed for %s", path, exc_info=True)
            raise StorageError(f"campaign upload failed: {exc}") from exc
        written.append(path)
    return written


# Campaign results per-file cap. The whole admin upload (all file slots plus
# the JSON box) is ALSO bounded by the app's global MAX_CONTENT_LENGTH (20 MB,
# enforced by Werkzeug during multipart parsing). This per-file guard is set
# to the same ceiling so it never promises more than a single request can
# carry. Uploads are additive across saves, and large raw FASTQ should be
# linked externally via the results "downloads" map rather than uploaded.
MAX_CAMPAIGN_RESULT_BYTES = 20 * 1024 * 1024  # 20 MB (matches MAX_CONTENT_LENGTH)


def upload_campaign_result(
    *,
    campaign_id: str,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload an operator-attached result file under
    ``lab-campaigns/{campaign_id}/results/{filename}`` and return the object
    path.

    The path (not a URL) is what the campaign's results envelope stores in
    ``download_paths``; GET /experiments/{id}/results mints a fresh signed
    URL from it at read time so the link never expires in storage. Raises
    StorageError on an empty, oversized, or failed upload.
    """
    if len(data) == 0:
        raise StorageError("Refusing to upload empty result file.")
    if len(data) > MAX_CAMPAIGN_RESULT_BYTES:
        raise StorageError(
            f"Result file exceeds {MAX_CAMPAIGN_RESULT_BYTES} byte cap "
            f"({len(data)} bytes). Link large raw reads externally instead."
        )
    safe = _safe_filename(filename)
    path = f"{campaign_id}/results/{safe}"
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(CAMPAIGN_BUCKET)
        bucket.upload(
            path=path,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:
        logger.error("Campaign result upload failed for %s", path, exc_info=True)
        raise StorageError(f"campaign result upload failed: {exc}") from exc
    return path


def presigned_campaign_url(object_path: str, *, expires_seconds: int = 3600) -> str:
    """Mint a short-lived signed download URL for a ``lab-campaigns`` object.

    Used by GET /experiments/{id}/results to resolve stored result-file
    paths into fresh links on every read (default 1 hour), so a customer
    never receives a URL that has already expired in storage.
    """
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(CAMPAIGN_BUCKET)
        result = bucket.create_signed_url(object_path, expires_seconds)
    except Exception as exc:
        logger.error("Campaign signed URL failed for %s", object_path, exc_info=True)
        raise StorageError(f"campaign signed URL failed: {exc}") from exc
    url = _extract_signed_url(result)
    if url:
        return url
    raise StorageError(f"unexpected campaign signed URL response: {result!r}")


OUTPUT_BUCKET = "tool-outputs"


def _output_object_path(user_id: str, job_id: str, filename: str) -> str:
    """Return the canonical storage path for a candidate PDB.

    Centralised so the upload-URL endpoint and the download path agree
    on layout. Filename is reduced to its basename — different pipelines
    emit ``pdb_key`` as either ``"design_0.pdb"`` (basename only) or
    ``"designs/design_0.pdb"`` (with subfolder prefix). Both must land
    at the same storage path so the round-trip works either way. After
    basename extraction, ``_safe_filename`` strips any remaining
    dangerous characters.
    """
    import posixpath  # noqa: PLC0415
    basename = posixpath.basename(filename) or filename
    return f"{user_id}/{job_id}/designs/{_safe_filename(basename)}"


def _extract_signed_url(result: object) -> Optional[str]:
    """Pull a signed URL out of supabase-py's response dict.

    storage3 has shifted key casing between releases (signedURL /
    signedUrl / signed_url / url). Be defensive across all four.
    """
    if isinstance(result, dict):
        for key in ("signedURL", "signedUrl", "signed_url", "url"):
            value = result.get(key)
            if value:
                return str(value)
    return None


def presigned_output_put_url(
    *,
    user_id: str,
    job_id: str,
    filename: str,
) -> str:
    """Mint a presigned PUT URL for a candidate PDB.

    The Modal pipeline calls ``/api/upload-urls/<job_id>`` to obtain
    these and then HTTP-PUTs each design's PDB bytes to the returned
    URL. The URL itself carries the upload auth — no Supabase session
    is needed on the Modal side.
    """
    path = _output_object_path(user_id, job_id, filename)
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(OUTPUT_BUCKET)
        result = bucket.create_signed_upload_url(path)
    except Exception as exc:
        logger.error("Signed upload URL failed for %s", path, exc_info=True)
        raise StorageError(f"signed upload URL failed: {exc}") from exc
    url = _extract_signed_url(result)
    if url:
        return url
    raise StorageError(f"unexpected signed upload URL response: {result!r}")


def download_output(
    *,
    user_id: str,
    job_id: str,
    filename: str,
) -> bytes:
    """Server-side download of a candidate PDB. Used by ZIP export."""
    path = _output_object_path(user_id, job_id, filename)
    client = get_service_client()
    if client is None:
        raise StorageError("Supabase service client unavailable.")
    try:
        bucket = client.storage.from_(OUTPUT_BUCKET)
        data = bucket.download(path)
    except Exception as exc:
        logger.error("Storage download failed for %s", path, exc_info=True)
        raise StorageError(f"download failed: {exc}") from exc
    if not data:
        raise StorageError(f"empty object at {path}")
    return data


def output_exists(
    *,
    user_id: str,
    job_id: str,
    filename: str,
) -> bool:
    """Cheap existence check used by the resolver to decide whether to
    serve a Storage object or fall back to inline ``pdb_content_b64``.

    Returns False on any failure rather than raising — the fallback
    path is correctness-equivalent for jobs that still emit inline b64.
    """
    import posixpath  # noqa: PLC0415
    safe = _safe_filename(posixpath.basename(filename) or filename)
    prefix = f"{user_id}/{job_id}/designs"
    client = get_service_client()
    if client is None:
        return False
    try:
        bucket = client.storage.from_(OUTPUT_BUCKET)
        listing = bucket.list(path=prefix, options={"search": safe})
    except Exception:
        return False
    if not isinstance(listing, list):
        return False
    return any(
        isinstance(item, dict) and item.get("name") == safe
        for item in listing
    )


def _safe_filename(name: str) -> str:
    """Strip path components and dangerous characters from a filename.

    Matches the Werkzeug secure_filename approach but with one tweak —
    we keep the original extension (Werkzeug sometimes normalises to
    lowercase which is fine but explicit).
    """
    from werkzeug.utils import secure_filename  # noqa: PLC0415
    safe = secure_filename(name) or "upload"
    return safe


# ---------------------------------------------------------------------------
# Data-retention primitives
# ---------------------------------------------------------------------------
#
# The tools-hub keeps three Storage buckets of customer data that otherwise
# accumulate forever:
#   - tool-inputs   (0006) — uploaded PDB / CIF / FASTA, keyed {user}/{job}/...
#   - tool-outputs  (0021) — pipeline result PDBs, keyed {user}/{job}/designs/...
#   - lab-campaigns (0011) — shortlisted PDBs for CRO handoff, keyed {campaign}/...
#
# RETENTION_DAYS is the single source of truth for the retention window across
# the sweeper (cron.purge_old_storage) and the legal copy. Do NOT hard-code 30
# elsewhere — import this constant.
RETENTION_DAYS = 30

# Full set of customer-data buckets. Used by the per-user (GDPR-style) erasure
# path, which must reach ALL of a user's data — including lab-campaigns — when
# an account is deleted or a data-deletion request comes in.
DATA_BUCKETS = (BUCKET, OUTPUT_BUCKET, CAMPAIGN_BUCKET)

# Buckets the AGE sweeper is allowed to time-delete on the RETENTION_DAYS clock.
# Deliberately EXCLUDES lab-campaigns: that bucket holds CRO wet-lab handoff
# shortlists — client deliverables whose lifecycle runs for MONTHS (a wet-lab
# cycle far exceeds 30 days), so aging them out on the same clock as ephemeral
# tool inputs/outputs would destroy live deliverables. lab-campaigns objects are
# removed ONLY via per-user erasure (purge_user_objects) on account deletion or
# an explicit deletion request — never by the scheduled age sweep.
AGE_SWEEP_BUCKETS = (BUCKET, OUTPUT_BUCKET)

# Storage list() default page size (storage3 DEFAULT_SEARCH_OPTIONS["limit"]).
# We page explicitly so a bucket with >100 objects is fully enumerated.
_LIST_PAGE = 100
# Supabase caps a single DELETE prefixes[] payload; batch removes well under it.
_DELETE_BATCH = 100
# Backstop against a pathological listing that never terminates (e.g. a folder
# that always reports a full page). 100k pages * 100 = 10M entries per level.
_MAX_LIST_PAGES = 100_000
# Placeholder object Supabase writes to keep an "empty" folder visible.
_EMPTY_FOLDER_PLACEHOLDER = ".emptyFolderPlaceholder"


def _resolve_client(client: Optional[object]):
    """Return the passed client or fetch the service client lazily.

    Lazy import (not the module-level binding) so tests can monkeypatch
    ``shared.credits.get_service_client``; callers may also inject a client
    directly to avoid a second fetch.
    """
    if client is not None:
        return client
    from shared.credits import get_service_client  # noqa: PLC0415
    return get_service_client()


def list_objects_recursive(
    bucket_name: str,
    prefix: str = "",
    *,
    client: Optional[object] = None,
) -> list[dict]:
    """Enumerate every leaf object under ``prefix`` in ``bucket_name``.

    Supabase Storage ``list()`` is NOT recursive — folders are virtual and
    each call returns only one level's immediate children (folder entries
    carry ``id is None``; real objects carry a non-null ``id`` plus
    ``created_at`` / ``updated_at``). This walks the tree depth-first,
    paging each level, and returns a flat list of::

        {"path": "<full object path>", "created_at": ..., "updated_at": ...}

    for every real object. ``prefix`` scopes the walk (e.g. a user id, a
    job id, or "" for the whole bucket). Raises ``StorageError`` if the
    service client is unavailable.
    """
    resolved = _resolve_client(client)
    if resolved is None:
        raise StorageError("Supabase service client unavailable.")
    bucket = resolved.storage.from_(bucket_name)
    results: list[dict] = []
    _walk_prefix(bucket, prefix, results)
    return results


def _walk_prefix(bucket, prefix: str, results: list[dict]) -> None:
    """Depth-first page-walk of one virtual folder, appending leaf objects."""
    offset = 0
    for _ in range(_MAX_LIST_PAGES):
        try:
            listing = bucket.list(
                path=prefix or None,
                options={"limit": _LIST_PAGE, "offset": offset},
            )
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"list failed for {bucket_name_of(bucket)}:{prefix}: {exc}") from exc
        if not isinstance(listing, list) or not listing:
            return
        for item in listing:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name or name == _EMPTY_FOLDER_PLACEHOLDER:
                continue
            full = f"{prefix}/{name}" if prefix else name
            if item.get("id") is None:
                # Virtual sub-folder — recurse.
                _walk_prefix(bucket, full, results)
            else:
                results.append(
                    {
                        "path": full,
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                    }
                )
        if len(listing) < _LIST_PAGE:
            return
        offset += _LIST_PAGE


def bucket_name_of(bucket) -> str:
    """Best-effort bucket id for error messages (storage3 stores it on ``id``)."""
    return str(getattr(bucket, "id", None) or getattr(bucket, "bucket_id", "?"))


def delete_objects(
    bucket_name: str,
    paths: list[str],
    *,
    client: Optional[object] = None,
) -> int:
    """Delete ``paths`` from ``bucket_name`` in batches; return the count removed.

    Bucket-generic replacement for the old inline ``.remove([...])`` calls.
    Idempotent — removing an already-gone object is a no-op on Supabase's
    side, so re-running the sweeper is safe. Returns 0 (and logs) rather
    than raising when the client is unavailable, so a cleanup path never
    fails a request over a missing client.
    """
    if not paths:
        return 0
    resolved = _resolve_client(client)
    if resolved is None:
        logger.warning("delete_objects: no service client; skipped %d paths", len(paths))
        return 0
    bucket = resolved.storage.from_(bucket_name)
    removed = 0
    for start in range(0, len(paths), _DELETE_BATCH):
        batch = paths[start:start + _DELETE_BATCH]
        try:
            bucket.remove(batch)
            removed += len(batch)
        except Exception:  # noqa: BLE001
            logger.warning(
                "delete_objects: batch remove failed for %s (%d paths)",
                bucket_name, len(batch), exc_info=True,
            )
    return removed
