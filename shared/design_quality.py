"""The measured quality figure a results page shows, for the TOP design only.

ONE DESIGN, NOT A COLUMN, and that is a cost decision rather than a design
preference. ``shared/surface_patch.py``'s MEASURED COST block has the numbers:
a few hundred milliseconds of CPU per design, and the results page fetches no
structures per row today, so a figure on every row would add a Storage round
trip per row as well -- about 15 s of CPU plus fifty fetches for a fifty-row
job. One design is one fetch and one pass, which a page can afford. A ranked
column needs the figure precomputed when the result is persisted, and that is
deliberately a separate change.

So NOTHING HERE SUPPORTS COMPARING DESIGNS. The figure describes the top
design and no other, the page must not imply the rest were measured and found
worse, and no copy may read as a pass or fail: these are screening numbers on a
PREDICTED structure, and they inherit that prediction's error (see
surface_patch's module docstring). The repo already holds that line for the
sequence developability pair -- neither of those names appears in
``shared/score_legends.py::GATE_COLUMNS``, pinned by
``tests/test_rfd4_results_and_exports.py::
test_the_developability_columns_are_not_a_pass_fail_bar`` -- and this figure
sits on the same side of it.

WHICH CHAIN IS THE DESIGNED ONE is the hard part, and this module adds no new
rule for it. Both rules below are the ones ``shared/exports.py`` already uses
and tests, reached through its own helpers, so a figure here cannot disagree
with that module about which chain is the binder.
"""
from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# The top design is the FIRST ROW IN PAGE ORDER, not the row whose ``rank``
# field is 1. The "#" column is a position everywhere the shared candidate
# table draws (templates/components/candidate_table.html), and ``rank`` means
# different things per tool, so the caller passes rows already in page order
# (shared/jobs.py::page_ordered_records) and this reads the first of them.
TOP_DESIGN_INDEX = 0


def designed_chains(
    cand: Mapping, index: int, tool: Optional[str], result, inputs,
    chains: Iterable[tuple[str, str]],
) -> Optional[list[str]]:
    """The designed chain ids of one row, or ``None`` when unknown.

    ``chains`` is ``shared/exports.py::structure_chain_sequences`` output for
    that row's own structure.

    Two rules, in order, both borrowed rather than invented:

    1. THE ROW'S OWN SEQUENCE. When the row stores a designed sequence
       (``shared/exports.py::_candidate_sequence``, which reads ``sequence``,
       ``binder_sequence`` or ``scores["sequence"]``), the designed chain is
       the chain of the structure carrying that sequence. This identifies the
       binder without any per-tool knowledge, and it is how the FASTA already
       tells a designed chain from a target.
    2. THE RUN'S TARGET CHAIN. Failing that,
       ``shared/exports.py::target_chain_for_export`` names the chain that is
       the uploaded TARGET, and every other chain is designed. That helper is
       an allow-list of one tool (``rfd4``) on purpose -- its docstring records
       why a ``target_chain`` form field cannot be trusted for the others, iggm
       included, whose delivered antigen chain may have been relabelled.

    ``None``, never a guess, when neither rule fires, when the sequence matches
    no chain or more than one, or when rule 2 leaves more than one designed
    chain. More than one is not an error -- an antibody run designs a heavy and
    a light chain -- but measuring a two-chain surface is a different figure
    from the one this module reports, so it reports nothing instead. A LENGTH
    RULE IS NOT AN ACCEPTABLE FALLBACK: in the committed rung-4 rfd4 run the
    binder is the longest chain in one design of eight and the target in the
    other seven (``tests/test_rfd4_results_and_exports.py::
    test_the_designed_chain_is_not_the_longest_chain``).
    """
    from shared.exports import _candidate_sequence  # noqa: PLC0415
    from shared.exports import target_chain_for_export  # noqa: PLC0415

    pairs = [(str(c), str(s)) for c, s in chains]
    if not pairs:
        return None

    row_seq = _candidate_sequence(cand if isinstance(cand, dict) else {})
    if row_seq:
        wanted = row_seq.strip().upper()
        hits = [c for c, s in pairs if s.strip().upper() == wanted]
        if len(hits) == 1:
            return hits
        logger.info(
            "design quality: the row's sequence matches %d chains of %d; "
            "not naming a designed chain",
            len(hits), len(pairs),
        )
        return None

    target = target_chain_for_export(tool, result, inputs)
    if not target:
        return None
    designed = [c for c, _s in pairs if c.strip() != target.strip()]
    return designed if len(designed) == 1 else None


def top_design_profile(
    candidates, *, tool: Optional[str], result, inputs,
    fetch_bytes=None, job_id=None,
) -> Optional[dict]:
    """Measured surface and liability findings for the top design, or ``None``.

    ``candidates`` must already be in PAGE ORDER; only
    :data:`TOP_DESIGN_INDEX` is read, so this costs at most ONE structure read
    and one SASA pass however many rows the job has.

    Returns ``None`` -- never a zero, never a partial dict, never an
    exception -- whenever anything is missing: no rows, no structure stored for
    that row, bytes that do not parse, a designed chain that cannot be named,
    or a chain the measurement declines. Every one of those renders as no
    figure at all. That is the degradation this was required to have: at least
    one esmfold run (eb94dfd2-a854-44c4-b741-c39fac360c07, 2026-11-12) has no
    retrievable structure, so "the tool stores structures" never meant "every
    run has one".

    On success::

        {"chain": str, "largest_patch_a2": float, "patch_atom_count": int,
         "total_apolar_a2": float, "residues": int, "liabilities": [ ... ]}

    THE SEQUENCE SCANNED FOR LIABILITIES IS READ BACK OFF THE STRUCTURE, not
    taken from the row. ``find_liabilities`` applies ``residue_rsa``
    positionally, so the sequence and the exposure must index the same
    residues; taking both from one parse of one file makes that true by
    construction rather than by assumption. A row whose stored sequence
    disagrees with its own structure would otherwise shift every exposure
    silently -- the failure ``find_liabilities``' length guard exists to catch,
    and this avoids relying on it.
    """
    from shared.developability.dimensions.liabilities import (  # noqa: PLC0415
        find_liabilities,
    )
    from shared.exports import (  # noqa: PLC0415
        _structure_bytes, export_key, structure_chain_sequences,
    )
    from shared.surface_patch import surface_profile  # noqa: PLC0415

    rows = [c for c in (candidates or []) if isinstance(c, dict)]
    if len(rows) <= TOP_DESIGN_INDEX:
        return None
    cand = rows[TOP_DESIGN_INDEX]

    key = export_key(cand, TOP_DESIGN_INDEX)
    data = _structure_bytes(cand, key, fetch_bytes, job_id)
    if not data:
        return None

    chains = structure_chain_sequences(data)
    if not chains:
        return None

    designed = designed_chains(cand, TOP_DESIGN_INDEX, tool, result, inputs, chains)
    if not designed:
        return None

    profile = surface_profile(data, designed)
    if profile is None:
        return None

    chain_id = designed[0]
    sequence = next(
        (s for c, s in chains if str(c) == chain_id), ""
    )
    rsa = (profile.get("residue_rsa") or {}).get(chain_id)
    try:
        liabilities = find_liabilities(sequence, rsa) if sequence else []
    except ValueError:
        # A sequence the scanner refuses -- a non-canonical residue letter the
        # structure extractor mapped through. The surface figure is still a
        # measurement, so it is kept and the flags are simply absent.
        logger.info("design quality: liability scan refused the sequence")
        liabilities = []

    return {
        "chain": chain_id,
        "largest_patch_a2": profile["largest_patch_a2"],
        "patch_atom_count": profile["patch_atom_count"],
        "total_apolar_a2": profile["total_apolar_a2"],
        "residues": len(sequence),
        "liabilities": liabilities,
    }
