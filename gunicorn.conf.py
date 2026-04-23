"""Gunicorn configuration for the Ranomics tools hub.

Forces preload so import errors surface in logs instead of silent worker
death. Railway dashboard start commands override Procfile, so
--preload lives here too.

Also provisions the Prometheus multiprocess directory so /metrics can
aggregate counters across gunicorn workers. Without this, each worker
holds its own state and scrape results depend on which worker accepts
the /metrics request.
"""

import os
import shutil
from pathlib import Path

# Load app in master before forking workers.
preload_app = True

# Show worker lifecycle events (boot, exit, errors).
loglevel = "info"

# Log to stdout/stderr so Railway captures it.
accesslog = "-"
errorlog = "-"


# ---------------------------------------------------------------------------
# Prometheus multiprocess bookkeeping
# ---------------------------------------------------------------------------

_PROMETHEUS_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom")


def on_starting(_server):  # noqa: ANN001 — gunicorn hook signature
    """Reset the multiprocess dir before workers boot.

    The prometheus_client multiprocess backend appends per-worker db
    files to this directory. Left unswept across deploys, the counter
    history would outlive the process and inflate values. Wipe on boot.
    """
    path = Path(_PROMETHEUS_DIR)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(path)


def child_exit(_server, worker):  # noqa: ANN001 — gunicorn hook signature
    """Clean up a worker's multiprocess files when it exits."""
    try:
        from prometheus_client import multiprocess  # type: ignore[import-untyped]
        multiprocess.mark_process_dead(worker.pid)
    except ImportError:
        pass
