"""Job directory management for Epitope Scout.

Each analysis request creates an isolated temporary directory under tmp/<job_id>/
using a UUID4 identifier. This module provides helpers to create those directories
and to clean up old ones after a configurable retention window.

Designed to be imported by the Flask application (app.py) and any background
cleanup tasks. Uses pathlib throughout — no os.path usage.
"""

import time
import uuid
from pathlib import Path


def create_job_dir(base_dir: Path = Path("tmp")) -> tuple[str, Path]:
    """Create a unique job directory under base_dir and return its ID and path.

    Args:
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
