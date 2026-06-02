"""Loaders for the per-tool example PDBs / FASTAs surfaced by the
"Load example" chips on each tool form (C2 of the growth plan).

Each entry in ``tools/<slug>/meta.py:examples`` is a dict with at least:

  * ``id``           — short slug used in URLs (e.g. ``1ubq``).
  * ``label``        — display string for the chip.
  * ``description``  — one-liner shown as the chip tooltip.
  * ``filename``     — basename under ``tools/<slug>/examples/``.
  * ``params``       — dict of form-field overrides (the equivalent of
                        the existing ``pre_fill`` dict on clone /
                        from_job / handoff prefill paths).
  * ``fasta_field``  — optional; only set on sequence-only tools
                        (AF2, ColabFold, ESMFold). Names the form's
                        textarea field that should be populated with
                        the file contents at GET time.

Usage::

    from shared.examples import load_example, read_example_bytes

    entry = load_example("mpnn", "1ubq")
    pdb_bytes = read_example_bytes("mpnn", "1ubq")
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# All example files live under the repo's ``tools/<slug>/examples/``.
REPO_ROOT = Path(__file__).resolve().parent.parent


def _meta_module(tool_slug: str):
    """Best-effort import of ``tools.<slug>.meta``. Returns ``None`` if
    the slug doesn't exist (e.g. typo from a stale link).
    """
    try:
        return importlib.import_module(f"tools.{tool_slug}.meta")
    except ImportError:
        logger.debug("no meta module for tool %s", tool_slug, exc_info=True)
        return None


def list_examples(tool_slug: str) -> list[dict[str, Any]]:
    """Return the ``examples`` list defined on the tool's meta module.

    Returns an empty list if the tool has no ``examples`` field — every
    template still calls this safely without needing a per-tool guard.
    """
    meta = _meta_module(tool_slug)
    if meta is None:
        return []
    return list(getattr(meta, "examples", []) or [])


def load_example(
    tool_slug: str, example_id: str
) -> Optional[dict[str, Any]]:
    """Return the example dict for ``example_id`` on ``tool_slug``, or
    ``None`` if no match. Lookup is by ``id`` field (case-sensitive).
    """
    for entry in list_examples(tool_slug):
        if entry.get("id") == example_id:
            return entry
    return None


def example_file_path(tool_slug: str, example_id: str) -> Optional[Path]:
    """Resolve the on-disk path for the example's ``filename``. Returns
    ``None`` if the example doesn't exist or its file is missing.
    """
    entry = load_example(tool_slug, example_id)
    if entry is None:
        return None
    filename = entry.get("filename")
    if not filename:
        return None
    candidate = (
        REPO_ROOT / "tools" / tool_slug / "examples" / filename
    )
    if not candidate.is_file():
        logger.warning(
            "example file missing on disk: %s (tool=%s id=%s)",
            candidate, tool_slug, example_id,
        )
        return None
    return candidate


def read_example_bytes(
    tool_slug: str, example_id: str
) -> Optional[bytes]:
    """Read the example's file (PDB or FASTA) as raw bytes."""
    path = example_file_path(tool_slug, example_id)
    if path is None:
        return None
    return path.read_bytes()


def read_example_text(
    tool_slug: str, example_id: str, encoding: str = "ascii"
) -> Optional[str]:
    """Read the example's file as decoded text. Useful for FASTA
    examples that get dropped straight into a textarea pre_fill.

    Normalizes line endings to ``\\n`` because the example files are
    committed from Windows (CRLF), and some downstream pipelines
    (LocalColabFold) are stricter about FASTA whitespace than the spec
    implies.
    """
    raw = read_example_bytes(tool_slug, example_id)
    if raw is None:
        return None
    text = raw.decode(encoding, errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")
