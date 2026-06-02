"""IndexNow ping for tools.ranomics.com.

Submits the high-value public URLs to IndexNow so Bing and Yandex
re-crawl them quickly after a content change. Mirrors the iteration
shape used by the ``/sitemap.xml`` route in ``app.py`` so the two
stay in lockstep when a tool is added or feature-flagged.

CLI entry point::

    flask indexnow:ping
    python -m cron.indexnow_ping

No-ops if ``INDEXNOW_KEY`` is unset.
"""
from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)


def _build_url_list(base: str) -> List[str]:
    """Return the absolute URLs to submit.

    Mirrors the static + per-tool iteration in ``app.py:sitemap_xml`` so
    a tool added to the catalog automatically shows up in both feeds.
    """
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    base = base.rstrip("/")

    static_paths = [
        "/",
        "/tools",
        "/help",
        "/pricing",
        "/scout",
    ]
    urls: List[str] = [f"{base}{path}" for path in static_paths]

    try:
        for adapter in tool_base.all_adapters():
            if not tool_enabled(adapter.slug):
                continue
            urls.append(f"{base}/tools/{adapter.slug}")
            urls.append(f"{base}/help/tools/{adapter.slug}")
    except Exception:
        logger.warning(
            "indexnow: failed to enumerate tool adapters", exc_info=True
        )

    return urls


def ping_high_value_urls() -> dict:
    """Submit the hub's high-value URLs to IndexNow.

    Returns the ``submit_urls`` result dict so callers can log or
    inspect the outcome. Never raises.
    """
    from shared.indexnow import DEFAULT_HOST, submit_urls  # noqa: PLC0415

    base = os.environ.get(
        "PUBLIC_BASE_URL", f"https://{DEFAULT_HOST}"
    ).rstrip("/")
    urls = _build_url_list(base)
    return submit_urls(urls, host=DEFAULT_HOST)


if __name__ == "__main__":
    # Allow ``python -m cron.indexnow_ping`` for ad-hoc runs without
    # depending on the Flask app context. The submit helper is pure.
    logging.basicConfig(level=logging.INFO)
    result = ping_high_value_urls()
    print(
        f"indexnow:ping status={result['status']} "
        f"submitted={result['submitted']} message={result['message']}",
        flush=True,
    )
