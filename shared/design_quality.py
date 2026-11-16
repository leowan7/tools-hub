"""The measured surface and liability screen for ONE design, ON REQUEST.

NOT A COLUMN, AND NOT A PAGE-LOAD FIGURE EITHER. Both of those were measured
and rejected, and the numbers are in ``shared/surface_patch.py``'s MEASURED
COST block: a few hundred milliseconds of CPU per design, and the results page
fetches no structures per row today, so a figure on every row would add a
Storage round trip per row as well -- about 15 s of CPU plus fifty fetches for
a fifty-row job.

Nor is the expandable detail block a cheaper home for it: the ``viewer-row``
``<tr>`` sits INSIDE the per-row ``{% for %}`` loop in
``templates/components/candidate_table.html`` and is merely hidden with
``display:none``, so anything Jinja renders there is computed for every row at
page load, exactly like a column. The 3D viewer escapes that only because its
bytes are fetched in the BROWSER (``data-pdb-url``,
``static/js/candidate_table.js``), which means the server never holds the
structure and cannot ride along on it for free.

So the cost lands on the user who asks for it: one request, one design, one
structure read, one SASA pass. ``blueprints/jobs.py::job_candidate_surface``
is the endpoint; a page that nobody expands pays nothing at all. This shape
needs no migration, no backfill, and nothing written into a finished job's
result, so it works on every historical run from the day it ships.

A KNOWN HAZARD FOR WHOEVER BUILDS THE RANKED COLUMN, recorded here because
this module is where they will look. The obvious route is to precompute the
figure when a result is persisted -- but ``job_status`` has NO terminal guard,
so a persist-time write can land on a run that has already been refunded or
cancelled (``memory``: streamed designs were delivered on a refunded run for
exactly this reason). A precompute must check terminal state itself; it cannot
assume the pipeline that calls it only ever runs for a live job.

NOTHING HERE SUPPORTS COMPARING DESIGNS. The figure describes the one design
asked for and no other, the page must not imply the rest were measured and
found worse, and no copy may read as a pass or fail: these are screening
numbers on a PREDICTED structure, and they inherit that prediction's error
(see surface_patch's module docstring). The repo already holds that line for
the sequence developability pair -- neither of those names appears in
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
import posixpath
from typing import Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# Design tools that are NOT binder-design-with-a-target-upload, so are absent
# from BINDER_DESIGN_TOOLS (which is derived from the PDB preflight rules,
# shared/pdb_preflight_rules.py:1146, and so lists only tools that take a
# target file). esmfold2-design designs a binder from a target SEQUENCE, so it
# has a designed chain and no preflight entry.
#
# HYPHEN, NOT UNDERSCORE: the registered slug is "esmfold2-design" while the
# package directory is esmfold2_design (the same trap
# shared/score_legends.py:770 records).
_EXTRA_DESIGN_TOOLS = frozenset({"esmfold2-design"})


def designs_its_own_chain(tool: Optional[str]) -> bool:
    """Whether a row of ``tool`` can have a DESIGNED chain at all.

    THIS GATE IS WHAT STOPS A FOLD BEING SCREENED AS A DESIGN, and without it
    the page made a false claim about the user's own input. ``shared/jobs.py::
    _single_fold_record`` gives an ESMFold fold a one-row candidate list
    carrying ``sequence`` and inline ``pdb_content_b64`` -- exactly the shape
    rule 1 of :func:`designed_chains` matches -- so an ordinary single-chain
    fold resolved to "the designed chain" and the panel reported liability
    motifs on a protein the customer supplied, labelled as a design. On the
    committed ``tools/esmfold/example/result.json`` that produced a full
    profile (chain A, 304 residues, 23 motifs) for a prediction whose mean
    pLDDT is 0.39.

    A fold tool predicts a structure for a sequence its user chose. There is
    no designed chain on that row to measure, whatever the row's fields look
    like, so the question is answered by the TOOL and not by the row's shape.
    Pinned by tests/test_design_quality.py::
    test_a_fold_is_never_screened_as_a_design.
    """
    from shared.pdb_preflight_rules import BINDER_DESIGN_TOOLS  # noqa: PLC0415

    return str(tool or "").strip() in (BINDER_DESIGN_TOOLS | _EXTRA_DESIGN_TOOLS)


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

    if not designs_its_own_chain(tool):
        return None

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

    # THE NAMED TARGET MUST ACTUALLY BE IN THE FILE. Without this, a structure
    # that does not contain the target chain has every chain it DOES contain
    # declared designed -- so an uploaded target delivered under a chain id
    # other than the one the form named would be measured and shown as the
    # customer's design. ``shared/exports.py::_chain_records`` carries exactly
    # this guard (``present = any(cid.strip() == target_chain ...)``, :1170)
    # and its docstring records the reason: whether upstream re-emits the
    # target under a different letter is not settled by any committed
    # artefact, so this guards an unproven input rather than a measured bug.
    # Omitting it was how an earlier version of this module could disagree
    # with the exports about which chain is the binder, which the claim above
    # says cannot happen. Pinned by tests/test_design_quality.py::
    # test_rule_two_needs_the_target_chain_to_be_in_the_file.
    if not any(c.strip() == target.strip() for c, _s in pairs):
        logger.info(
            "design quality: target chain %r is absent from the structure; "
            "not naming a designed chain", target,
        )
        return None

    designed = [c for c, _s in pairs if c.strip() != target.strip()]
    return designed if len(designed) == 1 else None


def design_profile(
    cand: Mapping, index: int, *, tool: Optional[str], result, inputs,
    fetch_bytes=None, job_id=None,
) -> Optional[dict]:
    """Measured surface and liability findings for ONE design, or ``None``.

    Costs exactly one structure read and one SASA pass. ``index`` is used only
    to build the row's storage key (``shared/exports.py::export_key``), the
    same way the CSV and FASTA exporters build it.

    Returns ``None`` -- never a zero and never a partial dict -- whenever
    anything is missing: no structure stored for that row, bytes that do not
    parse, a designed chain that cannot be named, two designed chains, or a
    chain the measurement declines. Every one of those renders as no figure at
    all. That is the degradation this was required to have: at least one
    esmfold run (eb94dfd2-a854-44c4-b741-c39fac360c07, 2026-11-12) has no
    retrievable structure, so "the tool stores structures" never meant "every
    run has one".

    IT IS NOT EXCEPTION-PROOF, and an earlier version of this docstring
    claimed it was. Nothing here catches an arbitrary error out of
    ``fetch_bytes`` (``shared/exports.py::_structure_bytes`` handles a
    ``StorageError``, not everything a caller's fetcher might raise), and only
    ``ValueError`` is caught around the liability scan. The catch-all that
    keeps a failure off the page lives at the route,
    ``blueprints/jobs.py::job_candidate_surface``, which logs and renders the
    empty state -- pinned by tests/test_candidate_surface_endpoint.py::
    test_a_raising_measurement_still_returns_the_empty_state.

    On success::

        {"chain": str, "largest_patch_a2": float, "patch_atom_count": int,
         "total_apolar_a2": float, "residues": int, "liabilities": [ ... ]}

    THE SEQUENCE SCANNED FOR LIABILITIES IS READ BACK OFF THE STRUCTURE, not
    taken from the row. ``find_liabilities`` applies ``residue_rsa``
    positionally, so the sequence and the exposure must index the same
    residues, and a row whose stored sequence disagreed with its own structure
    would shift every exposure silently.

    WHAT MAKES THAT ALIGN IS A SHARED RESIDUE TABLE, NOT A SINGLE PARSE, and
    the difference matters to anyone changing either side. These are two
    independent parses of the same bytes: the sequence comes from
    ``shared/exports.py::structure_chain_sequences`` (a line scanner,
    ``_pdb_residue_names``, for PDB; ``MMCIFParser`` for mmCIF) and the
    exposure from ``shared/surface_patch.py``'s own ``PDBParser`` pass. They
    agree on which residues count because both decide it with the SAME
    predicate -- membership of Biopython's ``protein_letters_3to1_extended``
    (``surface_patch.py::_is_protein_residue``, whose docstring records that
    this is why) -- so the two lists hold the same residues in file order.
    They are not guaranteed to agree by construction, and
    ``find_liabilities``' length guard remains the backstop that discards a
    mismatched exposure list rather than applying it at the wrong offsets.
    """
    from shared.developability.dimensions.liabilities import (  # noqa: PLC0415
        find_liabilities,
    )
    from shared.exports import (  # noqa: PLC0415
        _candidate_sequence, _structure_bytes, export_key,
        structure_chain_sequences, target_chain_for_export,
    )
    from shared.surface_patch import surface_profile  # noqa: PLC0415

    if not isinstance(cand, dict):
        return None

    # EVERY REASON TO SAY NO THAT DOES NOT NEED THE FILE IS CHECKED BEFORE THE
    # FILE IS FETCHED. Both are cheap and neither reads Storage: the tool
    # decides whether a designed chain can exist at all, and if the row stores
    # no sequence then rule 1 cannot fire, so rule 2's target name must be
    # there or no rule can. Checking after the fetch meant a tool that can
    # never show this figure still paid a blocking download per request and,
    # for a missing object, logged an ERROR with a traceback
    # (shared/storage.py) plus a WARNING from the route's fetcher.
    if not designs_its_own_chain(tool):
        return None
    if not _candidate_sequence(cand) and not target_chain_for_export(
        tool, result, inputs
    ):
        return None

    key = export_key(cand, index)
    data = _structure_bytes(cand, key, fetch_bytes, job_id)
    if not data:
        return None

    chains = structure_chain_sequences(data)
    if not chains:
        return None

    designed = designed_chains(cand, index, tool, result, inputs, chains)
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


def profile_for_structure_name(
    candidates, filename: str, *, tool: Optional[str], result, inputs,
    fetch_bytes=None, job_id=None,
) -> Optional[dict]:
    """:func:`design_profile` for the row whose stored structure is ``filename``.

    ADDRESSED BY STRUCTURE NAME, NOT BY ROW NUMBER, and that is a correctness
    requirement rather than a preference. The candidate table is also rendered
    for campaigns and targets, where a row's position in the table it is drawn
    in has nothing to do with its position in the job that produced it (the
    rows carry ``_source_job_id`` and come from many jobs,
    ``templates/components/candidate_table.html:878``). An index taken off the
    page would therefore measure a different design than the one the reader
    expanded. The structure name is the row's own identity and is what the 3D
    viewer and the download link already address it by
    (``blueprints/jobs.py::job_candidate_pdb``).

    Matching is on BASENAME, the same rule and for the same reason as that
    route: a ``pdb_key`` of ``designs/design_0.pdb`` must match a request for
    either ``designs/design_0.pdb`` or ``design_0.pdb``.

    ``index`` is still needed, because ``export_key`` falls back to a
    positional name for a row that stores no key of its own, so the row's
    position WITHIN ITS OWN JOB's candidate list is what gets passed on -- the
    enumerate below, over the candidates of the job named in the URL.

    ``None`` when no row matches, or for any reason
    :func:`design_profile` returns ``None``.
    """
    wanted = posixpath.basename(filename or "") or (filename or "")
    if not wanted:
        return None

    for index, cand in enumerate(candidates or []):
        if not isinstance(cand, dict):
            continue
        raw_key = cand.get("pdb_key")
        if posixpath.basename(str(raw_key) if raw_key else "") != wanted:
            continue
        return design_profile(
            cand, index, tool=tool, result=result, inputs=inputs,
            fetch_bytes=fetch_bytes, job_id=job_id,
        )
    return None
