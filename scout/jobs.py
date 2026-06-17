"""Job directory management for Epitope Scout.

Each analysis request creates an isolated temporary directory under tmp/<job_id>/
using a UUID4 identifier. This module provides helpers to create those directories
and to clean up old ones after a configurable retention window.

Designed to be imported by the Flask application (app.py) and any background
cleanup tasks. Uses pathlib throughout — no os.path usage.
"""

import re
import time
import uuid
from pathlib import Path

from werkzeug.utils import safe_join

# A scout job id is always a UUID4 string (see create_job_dir). Validate
# user-supplied ids against a strict UUID pattern so they cannot smuggle
# path separators or traversal sequences into a filesystem path.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Plain-text marker file recording the session key that owns a job dir.
OWNER_FILENAME = ".owner"


def is_valid_job_id(job_id) -> bool:
    """Return True only for a strict UUID string (the shape create_job_dir mints)."""
    return isinstance(job_id, str) and bool(_UUID_RE.match(job_id))


def safe_job_dir(job_id, base_dir: Path = Path("tmp")) -> "Path | None":
    """Return the confined job directory for ``job_id``, or None.

    Returns None unless ``job_id`` is a strict UUID *and* resolves under
    ``base_dir`` via ``werkzeug.utils.safe_join``. This is defence in depth:
    the UUID check already forbids separators and ``..``; safe_join blocks
    anything that might slip past. No existence or ownership check is done
    here — see ``resolve_owned_job_dir``.
    """
    if not is_valid_job_id(job_id):
        return None
    joined = safe_join(str(base_dir), job_id)
    if joined is None:
        return None
    return Path(joined)


def write_owner(job_dir: Path, owner: str) -> None:
    """Record the owning session key for a job directory.

    Stored as a plain-text ``.owner`` file inside the job dir so a later
    request can confirm the caller owns the job before serving its files.
    Best-effort: a write failure must not break job creation — the read
    side fails closed (access is denied when the marker is absent).
    """
    if not owner:
        return
    try:
        (job_dir / OWNER_FILENAME).write_text(owner, encoding="utf-8")
    except OSError:
        pass


def read_owner(job_dir: Path) -> "str | None":
    """Return the recorded owner of a job dir, or None if absent/unreadable."""
    try:
        return (job_dir / OWNER_FILENAME).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve_owned_job_dir(
    job_id, owner: str, base_dir: Path = Path("tmp")
) -> "Path | None":
    """Validate, confine, and ownership-check ``job_id`` in one call.

    Returns the confined job directory only when ALL of the following hold:
      * ``job_id`` is a strict UUID,
      * the path resolves under ``base_dir`` (``safe_join``),
      * the directory exists, and
      * its recorded ``.owner`` equals ``owner`` (which must be non-empty).

    Any failure returns None so every caller can answer a uniform 404 —
    cross-user reads and missing/legacy owner markers both fail closed.
    """
    if not owner:
        return None
    job_dir = safe_job_dir(job_id, base_dir)
    if job_dir is None or not job_dir.is_dir():
        return None
    recorded = read_owner(job_dir)
    if not recorded or recorded != owner:
        return None
    return job_dir


def create_job_dir(
    owner: "str | None" = None, base_dir: Path = Path("tmp")
) -> tuple[str, Path]:
    """Create a unique job directory under base_dir and return its ID and path.

    Args:
        owner: Session key (Supabase uid or email) that owns this job. When
            provided, it is recorded in a ``.owner`` marker so later reads can
            enforce per-user access. Omitting it leaves the dir unowned, which
            ``resolve_owned_job_dir`` treats as inaccessible (fail closed).
        base_dir: Root directory under which the per-job subdirectory is created.
            Defaults to Path("tmp") relative to the working directory.

    Returns:
        A tuple of (job_id, job_dir_path) where:
            - job_id is a UUID4 string (e.g. "3f2d1a0e-...").
            - job_dir_path is the resolved Path to the newly created directory.

    Raises:
        OSError: If the directory cannot be created due to filesystem permissions
            or other I/O errors.
    """
    job_id = str(uuid.uuid4())
    job_dir_path = Path(base_dir) / job_id
    job_dir_path.mkdir(parents=True, exist_ok=False)
    if owner:
        write_owner(job_dir_path, owner)
    return job_id, job_dir_path


def cleanup_old_jobs(base_dir: Path = Path("tmp"), max_age_seconds: int = 3600) -> int:
    """Delete job directories under base_dir that are older than max_age_seconds.

    Iterates over immediate subdirectories of base_dir and removes any whose
    last-modification time is older than the specified age threshold. Only
    directories are removed; loose files directly under base_dir (e.g. .gitkeep)
    are not touched.

    Args:
        base_dir: Root directory containing per-job subdirectories.
            Defaults to Path("tmp") relative to the working directory.
        max_age_seconds: Age threshold in seconds. Directories with an mtime
            older than (now - max_age_seconds) are deleted. Defaults to 3600
            (one hour).

    Returns:
        The number of job directories successfully deleted.

    Raises:
        No exceptions are raised for individual deletion failures — errors are
        silently skipped to avoid aborting a cleanup run partway through. If
        base_dir does not exist, returns 0 immediately.
    """
    base_dir = Path(base_dir)

    if not base_dir.exists():
        return 0

    deleted_count = 0
    cutoff_time = time.time() - max_age_seconds

    for entry in base_dir.iterdir():
        # Only clean up subdirectories — skip files like .gitkeep
        if not entry.is_dir():
            continue

        try:
            dir_mtime = entry.stat().st_mtime
        except OSError:
            # Cannot stat the directory — skip it rather than raising
            continue

        if dir_mtime < cutoff_time:
            try:
                # Remove the directory and all its contents
                for child in entry.rglob("*"):
                    if child.is_file():
                        child.unlink()
                entry.rmdir()
                deleted_count += 1
            except OSError:
                # Deletion failed (e.g. permissions) — skip, don't abort
                continue

    return deleted_count
