"""RFD4-Proteina tool adapter.

Modal app: ``ranomics-rfd4-prod`` (``tools/rfd4/modal_app.py``). GPU:
A100-80GB. The container lives in this repo -- ``tools/rfd4/run_pipeline.py``
is the contract this module writes to. Every bound below is RESTATED as a
literal with the container's symbol named in a trailing comment -- they are
two files, not one import -- and
tests/test_rfd4_adapter.py::test_adapter_bounds_are_the_containers_bounds
is what fails if the two ever drift apart.

Single modality in release 1: hotspot-conditioned protein binders against
one target chain.

NOT REGISTERED YET, AND BEHIND ``FLAG_TOOL_RFD4``. This module builds the
adapter and exports it as ``adapter``; the ``tools.base.register`` call
that puts it in the registry belongs to step 7A of
docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md section 8. The two templates
named at the bottom of this file now exist, and so does the copy a guard
reads off the slug without the registry (tools/rfd4/meta.py, the two lede
maps in blueprints/tools.py, the ``_tool_label`` entry in shared/email.py).
Three registry-coupled edits were measured 2026-11-05 on a throwaway
worktree as having to land in ONE commit with the ``register`` call,
because each was gated by an exact-identity guard that reads the live
registry. Two of the three have since landed AHEAD of registration, with
their guards taught the difference between an unregistered slug and a typo:
the ``_TOOL_CATEGORIES`` band in shared/tools_catalog.py (the size identity
in test_no_tool_lands_in_other now subtracts
tests/test_tool_categories.py::UNREGISTERED_SLUGS, and
test_the_unregistered_exception_list_is_still_true fails the moment rfd4
registers) and the ``_FACTS`` row in shared/tool_chooser.py
(test_every_fact_slug_is_a_real_catalog_slug now accepts the NAMED
tests/test_tool_categories.py::UNREGISTERED_SLUGS set -- deliberately
not any slug that merely carries a band, which would have retired the
typo shape that test exists for). The GPU-table row in README.md is
the one still waiting:
test_readme_tool_table_matches_the_live_registry asserts set equality
against ``tools.base.all_adapters()``, so that row turns the suite red
while the slug is unregistered and green only once it is.
rfd4 has no shared/result_columns.py entry and no score-legend row by
design: nothing in a run of it measures how well a binder holds.
The
scope document's step table records 66 failing ids, measured on step 3's
branch; re-running the same experiment on this base on 2026-11-05 gave 49.
THE 49 IS NOT RECORDED ANYWHERE IN THIS REPOSITORY: it was measured in a
throwaway worktree and no file here holds the run, so a reader cannot check
it. The 66 is checkable (that scope document, the step table).
NO CAUSE IS OFFERED FOR THE DROP, because the obvious one does not add up:
the scope document attributes 47 of the 66 to the two missing templates
(docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md, the per-banner tally -- 24
TemplateNotFound on rfd4_form.html and 23 on rfd4_results.html), and only
rfd4_results.html had landed at this base, which predicts 43 rather than 49.
Both figures are recorded as measurements and neither is a bound on 7A's own
work: both were taken with nothing but the registration applied, and
reconciling them would need the experiment re-run twice, which no decision
here depends on.

So the slug is unreachable for two independent reasons:
``blueprints/tools.py::_require_tool`` 404s a slug ``tools.base.get`` does
not know (its first arm, before the flag is read), and
``shared/feature_flags.py::tool_enabled`` is fail-closed on the unset env
var. Both are pinned by tests/test_rfd4_adapter.py::
test_the_adapter_is_built_and_the_flag_is_off.

Customer-visible copy below is provisional and Leo's to approve; the scope
doc's steps 7-8 own it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import (
    Preset,
    ToolAdapter,
    parse_hotspot_residues,
    parse_target_chains,
)

# The citation lives in ``tools/rfd4/meta.py``, not here, which is the house
# split -- see tools/rfantibody/meta.py for the shape. That file is reached
# without the registry: tests/test_citations_name_the_right_model.py::
# test_every_tool_meta_is_covered_here globs ``tools/*/meta.py`` ON DISK, so
# its three map entries (FIRST_AUTHOR, PAPER_YEAR, REQUIRED_URL_TOKENS) had
# to land in the same commit as the file. Leo settled all three on
# 2026-11-05: Corley, 2026, and the preprint PDF the README carries at the
# SHA we pin (ARG RFD4_SHA in tools/rfd4/Dockerfile.modal). The URL-token
# entry is the first in that map that is a path rather than a persistent
# identifier; its own comment in the test file carries the caveat.

# Mirrors of run_pipeline.py's own constants. Duplicated rather than
# imported: run_pipeline.py imports ``gemmi`` and ``requests`` at module
# scope and only ever runs inside the GPU image, so importing it from the
# web app would break app startup. The guard that keeps the two in step is
# tests/test_rfd4_adapter.py::test_adapter_bounds_are_the_containers_bounds,
# which reads the constants out of run_pipeline.py's source.
_BINDER_LEN_DEFAULT = (60, 120)       # run_pipeline.DEFAULT_BINDER_LENGTH
_BINDER_LEN_MIN = 20                  # run_pipeline.BINDER_LENGTH_FLOOR
_BINDER_LEN_MAX = 300                 # run_pipeline.BINDER_LENGTH_CEILING
_DEFAULT_NUM_DESIGNS = 8              # run_pipeline.DEFAULT_NUM_DESIGNS
_MAX_HOTSPOTS = 64                    # run_pipeline.MAX_HOTSPOTS

# Sanity ceiling only -- the wallet's per-tool hard cap is what bounds spend.
# 200 is where the container's own wall-clock runs out, not a policy pick:
# tools/rfd4/modal_app.py::run_tool kills the subprocess at
# ``max(60, _MAX_SESSION_S - 30)`` = 2670 s, and scope section 4.3's measured
# fit ``65 + 12.3 x N`` seconds reaches that at N = 211 on the 115 aa target
# it was measured on. A larger target reaches it sooner, which is what the
# preflight runtime-ceiling gate is for (shared/pdb_preflight.py::
# _check_size_envelope, via _RFD4.size.runtime_ceiling_s).
#
# CHECKED AGAINST THE OTHER FIT TOO, because N=211 comes from the eager
# rung-3 run. Scope section 5.8's fit over the production configuration,
# ``128 + 4.3 x (N-1) + 20 x (B-1) + 21``, puts N=200 near 1005 s for a
# single padded length bucket -- so rung 3 is the tighter of the two bases
# and 200 sits under both. shared/pdb_preflight_rules.py's _RFD4 envelope
# carries the same note where the runtime terms are set.
_MAX_NUM_DESIGNS = 200


def _parse_binder_length(
    form: Mapping[str, Any]
) -> tuple[Optional[tuple[int, int]], Optional[str]]:
    """Coerce binder_length_min / binder_length_max into a (lo, hi) pair.

    Returns a TUPLE; validate() ships ``list(...)`` of it because
    run_pipeline.parse_binder_length reads a two-element sequence out of
    JSON. Bounds are the container's own floor and ceiling.
    """
    raw_min = str(form.get("binder_length_min") or _BINDER_LEN_DEFAULT[0]).strip()
    raw_max = str(form.get("binder_length_max") or _BINDER_LEN_DEFAULT[1]).strip()
    try:
        lo = int(raw_min)
        hi = int(raw_max)
    except (TypeError, ValueError):
        return None, "Binder length must be integers."
    if lo > hi:
        return None, "Binder length min must be <= max."
    if lo < _BINDER_LEN_MIN or hi > _BINDER_LEN_MAX:
        return None, (
            f"Binder length must be between {_BINDER_LEN_MIN} and "
            f"{_BINDER_LEN_MAX} residues."
        )
    return (lo, hi), None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce the RFD4 form into the job_spec ``run_pipeline.main`` reads.

    Every refusal here mirrors one the container would make anyway. The
    converse does NOT hold -- a run this function passes can still be one
    the container refuses, and such a run IS funded: a PDB holding a second
    polymer chain while the form names one gets through every hub-side gate
    and dies in ``run_pipeline.resolve_target_chain``. That gap is pinned by
    tests/test_rfd4_adapter.py::
    test_an_extra_polymer_chain_passes_the_gates_the_container_refuses and
    recorded as scope section 9 item 11, a blocker on registration.

    * ONE target chain. ``run_pipeline.resolve_target_chain`` ``_fail``s on
      a multi-chain target, and its docstring records why: no committed
      artefact shows upstream accepting one.
    * AT LEAST ONE HOTSPOT. ``run_pipeline.build_hotspot_selections``
      ``_fail``s on an empty token list -- this tool runs upstream's
      hotspot-conditioned configuration and has nothing to condition on
      without them. ``parse_hotspot_residues`` already refuses an empty
      field, so no extra check is needed here; scope section 4.2's
      "Hotspots | optional" is wrong about this tool, and the envelope in
      shared/pdb_preflight_rules.py records the requirement as
      ``hotspots_required=True``.
    * At most 64 hotspots, the container's ``MAX_HOTSPOTS``.
    """
    preset = (form.get("preset") or "binder_design").strip() or "binder_design"
    if preset != "binder_design":
        return None, "Pick a preset."

    target_chains = parse_target_chains(form.get("target_chain") or "A")
    if not target_chains:
        return None, "Target chain is required."
    if len(target_chains) > 1:
        return None, (
            "This tool designs against one target chain. Pick the chain "
            "carrying the epitope, or use Proteina for a multi-chain target."
        )
    if len(target_chains[0]) > 4:
        return None, (
            f"Chain id {target_chains[0]!r} is too long (max 4 characters)."
        )

    hotspot_residues, err = parse_hotspot_residues(
        form.get("hotspot_residues") or "", target_chains
    )
    if err:
        return None, err
    if len(hotspot_residues) > _MAX_HOTSPOTS:
        return None, (
            f"At most {_MAX_HOTSPOTS} hotspot residues "
            f"(you gave {len(hotspot_residues)})."
        )

    binder_length, err = _parse_binder_length(form)
    if err:
        return None, err

    raw_num_designs = str(form.get("num_designs") or _DEFAULT_NUM_DESIGNS).strip()
    try:
        num_designs = int(raw_num_designs)
    except (TypeError, ValueError):
        return None, "Number of designs must be an integer."
    if num_designs < 1 or num_designs > _MAX_NUM_DESIGNS:
        return None, (
            f"Number of designs must be between 1 and {_MAX_NUM_DESIGNS}."
        )

    return (
        {
            "preset": preset,
            "target_chain": target_chains[0],
            # ONE representation, and it is the one that ships.
            # ``shared.pdb_preflight.shipped_hotspots`` -- what every paid
            # gate reads -- prefers ``hotspot_spec`` and falls back to this
            # key. A single-chain tool has nothing to lose by stripping the
            # chain letter, so there is no second authority to keep in
            # step, unlike proteina (see _parse_hotspots there).
            "hotspot_residues": hotspot_residues,
            "binder_length": list(binder_length),
            # The container's own key for the design count
            # (run_pipeline.parse_num_designs reads ``nsamples``).
            "nsamples": num_designs,
            "target": f"Your uploaded structure (chain {target_chains[0]})",
            # THE WALLET SCALES ON THIS KEY BY THIS NAME.
            # ``TOOL_SPECS["rfd4"].scaling_param`` is "n_designs_total" and
            # ``shared.wallet_estimates._effective_scaling_value`` falls back
            # to ``designs_per_run_baseline`` (8) in silence when it is
            # absent, so a missing stamp prices every run of every size as
            # eight designs. Pinned by tests/test_rfd4_pipeline.py::
            # test_the_adapter_stamps_the_key_the_wallet_scales_on.
            "parameters": {"n_designs_total": num_designs},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the job_spec ``tools/rfd4/run_pipeline.py::main`` reads.

    Four keys at the top level because that is where ``main`` looks for
    them: ``preset``, ``binder_length``, ``nsamples``, ``target_chain``,
    plus hotspots via ``hotspot_tokens``. ``parameters`` carries the wallet
    stamp and is not read by the container.

    ``presigned_url`` is unused: the generic submit route puts the same URL
    on the top-level payload as ``_input_presigned_url``
    (blueprints/tools.py::tool_submit), which
    ``gpu/modal_client.py::_build_payload`` forwards as
    ``input_presigned_url`` -- the key ``main`` downloads the target from.
    Same arrangement as rfdiffusion. Both cited by symbol rather than by
    line: step 3's own edits to those two files moved the line numbers an
    earlier draft of this docstring named.
    """
    return {
        "preset": inputs["preset"],
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "binder_length": inputs["binder_length"],
        "nsamples": inputs["nsamples"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="rfd4",
    label="RFD4-Proteina",
    blurb=(
        "Upload your target structure, mark the residues you want gripped, "
        "and get back binders whose sequence and all-atom structure come "
        "out of one model, with no separate sequence-design step."
    ),
    presets=(
        Preset(
            slug="binder_design",
            label="Protein binder (de novo, vs your target chain)",
            description=(
                "Hotspot-conditioned binder design against one chain of "
                "your uploaded structure. Sequence and all-atom structure "
                "are generated together, so a design arrives complete "
                "rather than as a backbone still needing a sequence step. "
                "Runs on our GPUs."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    # Both templates exist now -- templates/tools/rfd4_results.html came with
    # step 5 and templates/tools/rfd4_form.html with step 7 -- but nothing
    # renders either while the adapter is out of the registry, so neither is
    # exercised by a request yet. The form is covered unregistered by the
    # template-source guards that read the templates tree off disk, named in
    # the comment above rfd4's entry in tests/test_hotspot_picker.py's
    # PICKER_FORM_TEMPLATES; the rendered-page guards arm at 7A.
    form_template="tools/rfd4_form.html",
    results_partial="tools/rfd4_results.html",
)

# NOT REGISTERED. ``register(adapter)`` is one line of step 7A's commit --
# not the whole of it. See the module docstring for the three registry-coupled
# edits that have to land in the same commit, and the step table in
# docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md section 8 for the step.
