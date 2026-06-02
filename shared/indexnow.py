"""Minimal IndexNow client.

IndexNow is a simple HTTP protocol that lets a site notify participating
search engines (Bing, Yandex, Seznam, Naver) about new or updated URLs
so they can be crawled and indexed near-instantly. Google does not
consume IndexNow today, but pinging is cheap and the Bing/Yandex
coverage is worth a one-line POST per content change.

Two pieces are required at the site root for IndexNow to trust us:

  * A verification file at ``/<key>.txt`` whose body is the key itself.
    The Flask route is registered in ``app.py`` gated on the same env
    var (``INDEXNOW_KEY``).
  * Every submission must carry the same key and a ``keyLocation`` URL
    pointing to that verification file.

Usage::

    from shared.indexnow import submit_urls

    result = submit_urls([
        "https://tools.ranomics.com/",
        "https://tools.ranomics.com/tools",
    ])
    # {"status": 200, "message": "ok", "submitted": 2}

Designed to be safe to call from request handlers and cron jobs: any
network error is caught and returned as a structured dict so the
caller's flow is never interrupted.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


DEFAULT_HOST: str = "tools.ranomics.com"
INDEXNOW_KEY_ENV: str = "INDEXNOW_KEY"  # 8-64 hex chars
INDEXNOW_ENDPOINT: str = "https://api.indexnow.org/IndexNow"

# IndexNow caps a single submission at 10000 URLs. The hub's high-value
# URL list is well under 100 today; chunking is defensive only so the
# helper stays correct if a future caller passes a larger list.
_MAX_URLS_PER_REQUEST: int = 10000


def _resolve_key(key: Optional[str]) -> Optional[str]:
    """Return the explicit key if passed, else fall back to env."""
    if key:
        return key
    env_key = os.environ.get(INDEXNOW_KEY_ENV, "").strip()
    return env_key or None


def _post_chunk(
    urls: list[str], host: str, key: str, timeout: float
) -> dict:
    """POST a single chunk of URLs and return a result dict."""
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"https://{host}/{key}.txt",
        "urlList": urls,
    }
    try:
        resp = requests.post(
            INDEXNOW_ENDPOINT,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return {"status": -1, "message": str(exc), "submitted": 0}

    # IndexNow uses HTTP status only; the body is empty on success.
    return {
        "status": resp.status_code,
        "message": "ok" if resp.ok else resp.reason or "error",
        "submitted": len(urls) if resp.ok else 0,
    }


def submit_urls(
    urls: list[str],
    host: str = DEFAULT_HOST,
    key: Optional[str] = None,
    timeout: float = 5.0,
) -> dict:
    """Submit URLs to IndexNow.

    Returns a summary dict with keys ``status`` (int), ``message`` (str),
    and ``submitted`` (int). Status values follow this convention:

      *  200..299: IndexNow acknowledged the submission.
      *    0:      ``INDEXNOW_KEY`` not set; no submission attempted.
      *   -1:      Network or transport error.
      *  4xx/5xx:  IndexNow rejected the submission.

    Never raises: callers can fire-and-forget without try/except. The
    function logs one structured line per outcome.
    """
    resolved = _resolve_key(key)
    if not resolved:
        logger.info("indexnow: INDEXNOW_KEY not set; skip (urls=%d)", len(urls))
        return {
            "status": 0,
            "message": "INDEXNOW_KEY not set; skip",
            "submitted": 0,
        }

    if not urls:
        return {"status": 0, "message": "no urls", "submitted": 0}

    # Chunk defensively. Our typical batch is <100 so this loop runs once.
    total_submitted = 0
    last_status = 0
    last_message = "ok"
    for start in range(0, len(urls), _MAX_URLS_PER_REQUEST):
        chunk = urls[start : start + _MAX_URLS_PER_REQUEST]
        result = _post_chunk(chunk, host=host, key=resolved, timeout=timeout)
        last_status = result["status"]
        last_message = result["message"]
        total_submitted += result["submitted"]
        # Stop early on a non-2xx; later chunks are unlikely to succeed
        # and the caller already has a useful status to report.
        if not (200 <= last_status < 300):
            break

    logger.info(
        "indexnow: status=%s submitted=%d host=%s",
        last_status,
        total_submitted,
        host,
    )
    return {
        "status": last_status,
        "message": last_message,
        "submitted": total_submitted,
    }
