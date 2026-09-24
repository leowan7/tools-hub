"""RCSB keyword search and entry download for the target-prep page."""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from scout.epitope_db import RCSB_SEARCH_URL

logger = logging.getLogger(__name__)

RCSB_GRAPHQL_URL = "https://data.rcsb.org/graphql"
PDB_ID_RE = re.compile(r"^[A-Za-z0-9]{4}$")

_TITLES_QUERY = (
    "query($ids:[String!]!){entries(entry_ids:$ids){rcsb_id struct{title}}}"
)


def search_entries(query: str, rows: int = 10) -> Optional[list[dict]]:
    """Full-text RCSB search. ``[{"id", "title"}]``, or None when RCSB fails."""
    body = {
        "query": {
            "type": "terminal",
            "service": "full_text",
            "parameters": {"value": query},
        },
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": rows}},
    }
    try:
        resp = requests.post(RCSB_SEARCH_URL, json=body, timeout=15)
        # RCSB answers 204 with no body when nothing matches.
        if resp.status_code == 204:
            return []
        resp.raise_for_status()
        ids = [r["identifier"] for r in resp.json().get("result_set", [])]
    except Exception:  # noqa: BLE001
        logger.warning("RCSB search failed for %r", query, exc_info=True)
        return None
    if not ids:
        return []

    titles: dict[str, str] = {}
    try:
        resp = requests.post(
            RCSB_GRAPHQL_URL,
            json={"query": _TITLES_QUERY, "variables": {"ids": ids}},
            timeout=15,
        )
        resp.raise_for_status()
        for e in (resp.json().get("data") or {}).get("entries") or []:
            titles[e["rcsb_id"]] = ((e.get("struct") or {}).get("title")) or ""
    except Exception:  # noqa: BLE001 - ids without titles are still usable
        logger.warning("RCSB title lookup failed", exc_info=True)
    return [{"id": i, "title": titles.get(i, "")} for i in ids]


class EntryTooLarge(Exception):
    pass


def fetch_entry(pdb_id: str, size_cap: int) -> Optional[tuple[bytes, str]]:
    """Download ``pdb_id`` as .pdb, falling back to .cif.

    Returns ``(data, filename)``, or None when RCSB has neither. Raises
    ``EntryTooLarge`` past ``size_cap`` bytes; the body is streamed so an
    oversized entry is never read whole.
    """
    pdb_id = pdb_id.upper()
    for ext in (".pdb", ".cif"):
        url = f"https://files.rcsb.org/download/{pdb_id}{ext}"
        try:
            with requests.get(url, timeout=30, stream=True) as resp:
                if resp.status_code != 200:
                    continue
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(65536):
                    total += len(chunk)
                    if total > size_cap:
                        raise EntryTooLarge(pdb_id)
                    chunks.append(chunk)
        except EntryTooLarge:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("RCSB download failed: %s", url, exc_info=True)
            continue
        if total:
            return b"".join(chunks), f"{pdb_id}{ext}"
    return None
