"""Epitope Scout — consolidated into tools-hub as a free-tier tool.

The scout Flask Blueprint lives in :mod:`scout.routes` and is mounted at
``/scout`` by ``app.create_app``. Shared auth (``shared.auth``) gates the
protected routes. Scout itself is free and uncapped for everyone; the meter
is :mod:`scout.ratelimit` and :mod:`scout.quota` only writes the run ledger.
"""

from scout.routes import scout_bp  # noqa: F401
