"""Epitope Scout — consolidated into tools-hub as a free-tier tool.

The scout Flask Blueprint lives in :mod:`scout.routes` and is mounted at
``/scout`` by ``app.create_app``. Shared auth (``shared.auth``) gates the
protected routes; the free-tier paywall stays in :mod:`scout.quota`.
"""

from scout.routes import scout_bp  # noqa: F401
