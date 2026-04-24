"""LLM-driven literature hotspot retrieval for Epitope Scout.

Makes a single Claude API call per protein to return structured region data.
All API errors degrade gracefully — callers receive an empty list, not an exception.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def fetch_literature_context(protein_name: str) -> list[dict]:
    """Query Claude for published hotspot/epitope regions for protein_name.

    Makes one API call per invocation. Import of the anthropic SDK is deferred
    to the function body (same pattern as freesasa in pipeline.py) so the module
    loads cleanly on systems where anthropic is not yet installed.

    Args:
        protein_name: User-provided protein name (e.g. "EGFR", "IL-6").

    Returns:
        List of region dicts, each with keys:
            name (str), residues (list[int]), summary (str).
        Returns [] if ANTHROPIC_API_KEY is not set, API call fails,
        or response is not valid JSON.
    """
    import anthropic  # deferred import — avoids hard dependency at module load time

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — skipping literature search for %s", protein_name)
        return []

    prompt = (
        f"Return published binding site and hotspot data for the protein {protein_name}. "
        "Respond with a JSON array only. No preamble, no markdown, no text outside JSON.\n\n"
        "Each array element must have exactly these keys:\n"
        '  "name": string — descriptive region label\n'
        '  "residues": list of integers — residue sequence numbers\n'
        '  "summary": string — 1-2 sentences citing specific published evidence '
        "(Ala-scanning, co-crystal structure PDB ID, HDX-MS, or reported binder epitope). "
        "Do not include regions without published experimental evidence.\n\n"
        "Return [] if no published hotspot or binding site data exists."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fence if present — Claude occasionally wraps
        # JSON output in ```json...``` despite JSON-only instructions.
        if raw.startswith("```"):
            parts = raw.split("```")
            # parts[1] is the content between the first ``` and the second ```
            inner = parts[1] if len(parts) > 1 else raw
            # Remove optional "json" language tag at the start of the fence block
            if inner.startswith("json"):
                inner = inner[4:]
            raw = inner.strip()

        regions = json.loads(raw)
        if not isinstance(regions, list):
            logger.warning(
                "Claude returned non-list JSON for %s — ignoring response", protein_name
            )
            return []
        return regions

    except Exception:
        logger.exception("Literature fetch failed for %s", protein_name)
        return []


def map_literature_to_patches(
    patches: list[dict],
    lit_regions: list[dict],
    overlap_threshold: int = 1,
) -> dict[int, dict | None]:
    """Map literature regions to patches by residue number set intersection.

    For each patch, finds the lit_region with the maximum residue number set
    intersection. Returns that region if the overlap meets or exceeds
    overlap_threshold, otherwise returns None for that patch.

    Args:
        patches: List of patch dicts, each with 'epitope_id' (int) and
            'residue_numbers' (list[int]).
        lit_regions: List of region dicts from fetch_literature_context().
        overlap_threshold: Minimum shared residues to count as a match.

    Returns:
        Dict mapping epitope_id -> best-matching lit_region dict, or None
        if no region overlaps this patch at or above the threshold.
    """
    patch_to_region = {}
    for patch in patches:
        patch_set = set(int(residue) for residue in patch["residue_numbers"])
        best_region = None
        best_overlap_count = 0

        for region in lit_regions:
            # Cast region residues to int to handle any string/int type mismatch
            region_set = set(int(r) for r in region.get("residues", []))
            overlap_count = len(patch_set & region_set)
            if overlap_count >= overlap_threshold and overlap_count > best_overlap_count:
                best_region = region
                best_overlap_count = overlap_count

        patch_to_region[patch["epitope_id"]] = best_region

    return patch_to_region
