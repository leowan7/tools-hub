"""Three-question chooser that narrows the catalog to the tools that fit.

A visitor lands on /tools facing fourteen tiles with no way to choose.
This module answers "which of these is for me" from three questions:
what you already have, what shape of binder you want back, and what is
on your target.

WHAT IS TYPED HERE AND WHAT IS DERIVED
--------------------------------------
Typed: ``_FACTS`` — which HAVE bucket and which binder shapes each tool
serves, plus the one target-chemistry capability (glycans) that only
appears in prose — and ``_CROSS_BUCKET_OFFERS`` /
``_CROSS_BUCKET_PREREQUISITE``, the one tool offered outside its bucket
and the limits that offer must state. Each entry cites the
``about["when_to_use"]`` or ``comparison_one_liner`` line it came from.
This is a single table in one module, the same shape as the existing
``shared.tools_catalog._TOOL_CATEGORIES``; no per-tool metadata field
was added.

Derived, never typed — these are the claims a customer acts on, so they
read the enforcer rather than restating it:

* ``needs_hotspots`` reads ``TOOL_RULES[slug].hotspots_required``, the
  flag ``shared/pdb_preflight.py::preflight_for_tool`` branches on to refuse a submit.
* ``needs_structure`` reads ``adapter.requires_pdb`` and each
  ``Preset.requires_pdb``, the flags ``blueprints/tools.py::tool_submit`` reads
  to gate a submit before ``create_job``.
* Multi-chain eligibility ANDs ``TOOL_RULES[slug].multi_chain_supported``
  with ``TOOL_RULES[slug].multi_chain_container_ready``, the two flags
  ``shared/pdb_preflight.py::_multi_chain_block`` ANDs before it lets a
  two-chain target through.

``tests/test_tool_chooser.py`` asserts each of those three against the
enforcing code path, so the chooser and the gate cannot drift apart.

Recommendations are filtered through ``_build_tools_catalog()``, which
already drops flag-disabled adapters via ``shared.feature_flags``.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.pdb_preflight_rules import TOOL_RULES
from shared.tool_meta import meta_for
from shared.tools_catalog import _build_tools_catalog
from tools import base as tool_base

# ---------------------------------------------------------------------------
# Question 1 — what the visitor already has.
# ---------------------------------------------------------------------------
HAVE_CHOICES: tuple[tuple[str, str], ...] = (
    ("target-structure", "A structure of my target (a .pdb or .cif file)"),
    ("target-sequence", "Only the sequence of my target, no structure"),
    ("antibody-and-target", "An antibody or nanobody already, plus its antigen"),
    ("backbone", "A backbone — a 3D shape with no sequence chosen for it yet"),
    ("binder-and-target", "A binder sequence and the target it should hit"),
    ("binder-sequence", "A binder sequence on its own"),
    ("nothing", "Nothing yet — I want to know if my target is worth binding"),
)

# ---------------------------------------------------------------------------
# Question 2 — what shape of binder, asked only for the design buckets.
# ---------------------------------------------------------------------------
SHAPE_CHOICES: tuple[tuple[str, str], ...] = (
    ("mini-protein", "A small de novo protein (roughly 50 to 200 residues)"),
    ("nanobody", "A nanobody — a single-domain antibody"),
    ("scfv", "An scFv — paired heavy and light chains"),
    ("peptide", "A short peptide"),
    ("unsure", "I don't know yet — show me everything that could work"),
)

# ---------------------------------------------------------------------------
# Question 3 — what is on the target, asked for the same design buckets as
# question 2: the template gates both fieldsets on the one `chooser_asks_shape`
# flag (templates/tools/comparison.html), which blueprints/tools.py sets from
# DESIGN_HAVES.
# ---------------------------------------------------------------------------
CHEMISTRY_CHOICES: tuple[tuple[str, str], ...] = (
    ("plain", "An ordinary protein surface"),
    ("glycan", "It carries sugars or modified residues"),
    ("multi-chain", "The patch I care about spans more than one chain"),
)

# Buckets where question 2 (and then question 3) apply at all. Everything
# else is a single-answer bucket resolved by question 1 alone.
DESIGN_HAVES: frozenset[str] = frozenset(
    {"target-structure", "target-sequence"}
)


@dataclass(frozen=True)
class _Facts:
    """What one tool serves. ``why`` cites where each claim came from."""

    haves: frozenset[str]
    shapes: frozenset[str]
    # Target chemistries beyond "plain" this tool is claimed to handle.
    # "multi-chain" is NOT listed here — it is derived from TOOL_RULES.
    chemistries: frozenset[str]


_FACTS: dict[str, _Facts] = {
    # --- de novo binder design, target structure in hand ------------------
    # "You have a target structure and a patch of its surface you want
    # gripped, and you want brand-new binders of whatever shape works"
    # — tools/rfdiffusion/meta.py::comparison_one_liner
    "rfdiffusion": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"mini-protein"}),
        chemistries=frozenset(),
    ),
    # "you want brand-new mini-proteins of 50 to 150 residues built to
    # grip it" — tools/bindcraft/meta.py::comparison_one_liner
    "bindcraft": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"mini-protein"}),
        chemistries=frozenset(),
    ),
    # "You have a target and you want every single candidate to arrive
    # with a real AlphaFold2 confidence score against that target"
    # — tools/pxdesign/meta.py::comparison_one_liner
    "pxdesign": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"mini-protein"}),
        chemistries=frozenset(),
    ),
    # Four shapes at one site: "One model here aims mini-proteins,
    # nanobodies, antibodies or peptides at the same site" — and the
    # glycan claim, "Your target carries sugars, modified residues or
    # other chemistry a protein-only model would silently drop"
    # — tools/boltzgen/meta.py::comparison_one_liner and about["when_to_use"][1]
    "boltzgen": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"mini-protein", "nanobody", "scfv", "peptide"}),
        chemistries=frozenset({"glycan"}),
    ),
    # "You want a nanobody scaffold rather than a de novo mini-protein"
    # — tools/rfantibody/meta.py, about["when_to_use"][0]
    "rfantibody": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"nanobody"}),
        chemistries=frozenset(),
    ),
    # "You want to aim a binder at one specific patch, including a recessed
    # or partly shielded one, by naming residues on it."
    # — tools/proteina/meta.py, about["when_to_use"][0].
    #
    # #353 pointed this at comparison_one_liner instead, quoting "You have a
    # hard target". That quote is stale as of this branch: the one-liner now
    # reads "You have a hard PROTEIN target", because its old opener recruited
    # visitors whose target is a molecule and validate() refuses them. The
    # when_to_use[0] citation above is verbatim; both say the same thing about
    # why proteina sits in the target-structure bucket.
    #
    # NOT in the target-sequence bucket. proteina does run with no upload,
    # but only against "a repo-bundled benchmark task whose target is baked
    # into the config" (tools/proteina/__init__.py module docstring); aiming at
    # the visitor's OWN target is the target_source == "custom" path, which
    # is a .pdb/.cif upload (tools/proteina/meta.py, about["prerequisites"]).
    # A sequence-only visitor recommended proteina would arrive at the form
    # and find their target unreachable, and requires_pdb is False on every
    # preset so prerequisite_line() has nothing to warn them with.
    #
    # A small-molecule target is NOT offered as an answer here at all:
    # the chooser dropped that option (2026-09-24, Leo: "we don't do any
    # small molecules"). proteina was the only near-candidate and
    # ``tools/proteina/__init__.py::validate`` refuses a ligand_binder run
    # against a staged target -- ``_CUSTOM_TARGET_PRESETS`` in that module
    # holds {"protein_binder"} only -- so it designs against a bundled
    # benchmark ligand, never the visitor's molecule. That refusal is pinned
    # by tests/test_proteina_no_custom_ligand_promise.py::
    # test_the_enforcing_set_still_excludes_ligand_binder, which calls
    # validate() rather than only reading the set.
    "proteina": _Facts(
        haves=frozenset({"target-structure"}),
        shapes=frozenset({"mini-protein"}),
        chemistries=frozenset(),
    ),
    # "No PDB required. The gradient loop is sequence-only."
    # — tools/esmfold2_design/meta.py, about["prerequisites"]. Two presets: a
    # paired scFv ("all six binding loops designed jointly against your
    # target. No other tool here does this" — when_to_use[0]) and a de
    # novo minibinder ("generates a free 60 to 200 aa scaffold").
    "esmfold2-design": _Facts(
        haves=frozenset({"target-sequence"}),
        shapes=frozenset({"mini-protein", "scfv"}),
        chemistries=frozenset(),
    ),
    # --- single-answer buckets -------------------------------------------
    # "You already have an antibody or nanobody and want its binding
    # loops, or its framework, rebuilt against a specific antigen and
    # epitope." — tools/iggm/meta.py, about["when_to_use"][0]
    "iggm": _Facts(
        haves=frozenset({"antibody-and-target"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # "You have a backbone — a 3D shape with no sequence decided yet —
    # and need amino-acid sequences that will fold into it."
    # — tools/mpnn/meta.py::comparison_one_liner
    "mpnn": _Facts(
        haves=frozenset({"backbone"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # "You already have a binder sequence and the target it should hit,
    # and you want to know whether they actually stick together before
    # you order DNA." — tools/boltz2/meta.py::comparison_one_liner
    "boltz2": _Facts(
        haves=frozenset({"binder-and-target"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # "Your complex has something in it other than protein: DNA, RNA, or
    # a bound small molecule." — tools/opendde/meta.py, about["when_to_use"][0]
    "opendde": _Facts(
        haves=frozenset({"binder-and-target"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # "Pick Developability Scout when you have a sequence and need a
    # quick liability scan" — shared/tools_catalog.py::_HARDCODED_TOOLS
    "developability": _Facts(
        haves=frozenset({"binder-sequence"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # "Pick Epitope Scout first to identify candidate epitopes and
    # per-dimension feasibility for any target."
    # — shared/tools_catalog.py::_HARDCODED_TOOLS
    "epitope-scout": _Facts(
        haves=frozenset({"nothing"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    # --- fold a sequence so the structure-led tools become reachable ------
    # Offered under "target-sequence" as the route to a structure, not as
    # a binder designer. Each names the others and they split on chain
    # count and runtime (tools/{esmfold,colabfold,af2}/meta.py).
    "esmfold": _Facts(
        haves=frozenset({"target-sequence"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    "colabfold": _Facts(
        haves=frozenset({"target-sequence"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
    "af2": _Facts(
        haves=frozenset({"target-sequence"}),
        shapes=frozenset(),
        chemistries=frozenset(),
    ),
}


# Offers a tool OUTSIDE its own HAVE bucket, for the shapes named here.
#
# esmfold2-design is the only entry. Its card says the paired scFv run is
# something "No other tool here does" (tools/esmfold2_design/meta.py,
# about["when_to_use"][0]), so a visitor who has a target structure and
# wants an scFv was shown BoltzGen alone and never learned it existed.
# It is NOT moved into the target-structure bucket, because it accepts no
# structure at all: ``requires_pdb`` is False on the adapter and on both
# presets, ``tools/esmfold2_design/__init__.py::validate`` reads no file
# field, and its form offers a target preset or a pasted-sequence
# textarea and no upload at all
# (templates/tools/esmfold2_design_form.html), so a visitor's structure
# has nowhere to go on it. The offer therefore carries its own
# prerequisite line below.
#
# Narrowed to "scfv": the other tools in that bucket already answer
# mini-protein, nanobody and peptide from the upload itself, and this
# one would discard it. The two UNnarrowed answers still reach it, as
# they reach every tool: ``recommend`` skips the shape filter for
# "unsure" and for no shape at all (``blueprints/tools.py`` withholds
# the answer until a shape is picked, so the second only arrives from a
# direct call or a hand-edited URL).
_CROSS_BUCKET_OFFERS: dict[str, dict[str, frozenset[str]]] = {
    "esmfold2-design": {"target-structure": frozenset({"scfv"})},
}

# What a tool demands of its input when that demand is neither a
# structure nor a residue list, so needs_structure and needs_hotspots
# both leave prerequisite_line empty. Stated in EVERY bucket the tool
# appears in, because these are facts about the adapter, not about the
# answer that led here: a visitor in its own target-sequence bucket was
# shown no prerequisite at all and met the 800-residue ceiling at the
# form instead.
_ADAPTER_INPUT_LIMITS: dict[str, str] = {
    "esmfold2-design": (
        "You will need your target as a sequence: paste it (30 to 800 "
        "amino acids) or pick one of its five built-in targets. An scFv "
        "is built on one of three built-in humanised frameworks, not one "
        "you supply."
    ),
}

# The prerequisite line shown INSTEAD of ``prerequisite_line`` when a tool
# is reached through _CROSS_BUCKET_OFFERS: the same limits, behind the
# one sentence that answer needs and the others do not. Both limits are
# read off
# tools/esmfold2_design/__init__.py: ``validate`` takes target_mode
# "preset" (TARGET_PRESET_NAMES, five of them) or "paste"
# (_check_protein_sequence, TARGET_SEQ_MIN 30 to TARGET_SEQ_MAX 800), and
# the scfv preset refuses any binder_framework outside FRAMEWORK_NAMES,
# which holds three humanised frameworks. Pinned by
# tests/test_tool_chooser.py::test_the_cross_bucket_line_states_the_limits_
# the_adapter_enforces, which drives that validate.
_CROSS_BUCKET_PREREQUISITE: dict[tuple[str, str], str] = {
    ("esmfold2-design", "target-structure"): (
        "This one takes no structure file. " + _ADAPTER_INPUT_LIMITS[
            "esmfold2-design"
        ]
    ),
}


def _adapter_by_slug() -> dict:
    return {a.slug: a for a in tool_base.all_adapters()}


def needs_structure(slug: str) -> bool:
    """True when a paid run of this tool cannot start without an upload.

    Reads the same two flags ``blueprints/tools.py::tool_submit`` reads to gate
    the submit: the adapter flag, or any preset's. proteina is False on
    both because its target is optional — a curated benchmark task is a
    complete run — and the custom-target case is gated separately in
    that same view, by its ``target_source == "custom"`` branch.
    """
    adapter = _adapter_by_slug().get(slug)
    if adapter is None:
        return False
    if getattr(adapter, "requires_pdb", False):
        return True
    return any(
        getattr(p, "requires_pdb", False) for p in getattr(adapter, "presets", ())
    )


def needs_hotspots(slug: str) -> bool:
    """True when preflight refuses a submit that names no hotspot.

    Reads ``TOOL_RULES[slug].hotspots_required`` — the flag
    ``shared/pdb_preflight.py::preflight_for_tool`` branches on. A tool with no entry in
    TOOL_RULES is not gated by that path at all, so it is False here.
    """
    rules = TOOL_RULES.get(slug)
    return bool(rules is not None and rules.hotspots_required)


def supports_multi_chain(slug: str) -> bool:
    """True when preflight lets a multi-chain target through.

    Reads BOTH flags ``shared/pdb_preflight.py::_multi_chain_block``
    ANDs — it returns None (lets the target through) only when
    ``multi_chain_supported and multi_chain_container_ready``. Reading
    ``container_ready`` alone would recommend a tool whose container is
    ready before its model is, and preflight would then refuse it.

    A tool absent from TOOL_RULES gets no multi-chain recommendation
    from the chooser: nothing here has established it handles one. Its
    callers scope this to tools that take an upload at all — a tool that
    never stages a PDB never reaches ``_multi_chain_block``.
    """
    rules = TOOL_RULES.get(slug)
    return bool(
        rules is not None
        and rules.multi_chain_supported
        and rules.multi_chain_container_ready
    )


def has_example(slug: str) -> bool:
    """True when /tools/<slug> renders a worked example to link to.

    Mirrors the gate at ``blueprints/tools.py::_example_context``, which
    returns None unless BOTH ``meta.EXAMPLE`` and
    ``tools/<slug>/example/result.json`` are present. Derived rather
    than listed so a tool that gains or loses an example does not leave
    the chooser pointing at an anchor that never renders.
    """
    meta = meta_for(slug)
    if meta is None or not getattr(meta, "EXAMPLE", None):
        return False
    # Reuses the renderer's own reader rather than testing for the file:
    # _example_result returns None on unparseable JSON too, so a truncated
    # result.json now withholds the link instead of pointing at an anchor
    # the macro did not render. Imported here, not at module scope,
    # because blueprints/tools.py imports this module. Pinned by
    # test_has_example_agrees_with_the_renderer_for_every_tool.
    from blueprints.tools import _example_result

    if _example_result(slug) is None:
        return False
    # The macro has a third gate: `{% if ex and adapter.results_partial %}`
    # (templates/components/worked_example.html). Every adapter sets
    # results_partial today, so this changes no answer now -- but a tool
    # that shipped an example before a results partial would otherwise be
    # advertised with a link to an anchor the macro never rendered.
    adapter = _adapter_by_slug().get(slug)
    return bool(adapter is not None and getattr(adapter, "results_partial", None))


# A tool with no TOOL_RULES entry is never reached by the preflight
# hotspot gate, so ``needs_hotspots`` is False for it — but an adapter's
# own ``validate`` can still refuse a residue-less submit, and a
# prerequisite line that omits that refusal is looser than the gate.
# iggm is the one such tool: ``tools/iggm/__init__.py::validate`` returns
# "Epitope residues are required." when the epitope field is empty.
# Pinned by tests/test_tool_chooser.py, which drives that validate both
# ways, and by test_every_adapter_residue_refusal_slug_lacks_tool_rules,
# which fails if a slug here ever gains a TOOL_RULES entry instead.
_ADAPTER_RESIDUE_REFUSALS: dict[str, str] = {
    "iggm": "the antigen residues that form the epitope",
}

# Tools whose own validate refuses a two-chain input, which no preflight
# rule records. supports_multi_chain reads TOOL_RULES, and the folding
# tools offered as a "get a structure first" step are absent from it --
# their multi-chain answer lives in the adapter instead:
# ``tools/esmfold/__init__.py::validate`` returns "ESMFold v1 is
# monomer-only but FASTA has {n} records." for a second record, and the
# same refusal for a ':' chain separator. AlphaFold2 and ColabFold accept
# two records. Pinned by
# tests/test_tool_chooser.py::test_only_esmfold_refuses_a_two_chain_fasta,
# which drives all three adapters' real validate.
_ADAPTER_MULTI_CHAIN_REFUSALS: frozenset[str] = frozenset({"esmfold"})

# Designers whose TARGET is a single pasted sequence, so a patch spanning
# two chains cannot be described to them at all. Separate from the set
# above, which is about the FASTA a folding tool is given.
# ``tools/esmfold2_design/__init__.py::_check_protein_sequence`` refuses a
# ':' separator as a non-canonical residue. Pinned by
# tests/test_tool_chooser.py::
# test_a_single_sequence_target_tool_is_withheld_for_a_multi_chain_patch,
# which drives that validate.
_ADAPTER_SINGLE_CHAIN_TARGETS: frozenset[str] = frozenset({"esmfold2-design"})

# Tools that accept a submit with NO upload by falling back to a bundled
# benchmark target. requires_pdb is False on them, so needs_structure is
# False and prerequisite_line would otherwise say nothing -- which, on a
# card offered under "A structure of my target", reads as "no upload
# needed" and bills a GPU run against someone else's target.
# ``tools/proteina/__init__.py::validate`` returns target_source "curated"
# with a default task_name when no custom target is staged, and
# ``blueprints/tools.py::tool_submit`` only demands a PDB once
# target_source is already "custom". Pinned by
# test_a_curated_default_target_tool_tells_you_to_attach_the_file.
_CURATED_DEFAULT_TARGETS: dict[str, str] = {
    "proteina": (
        "You will need a structure of your target (.pdb or .cif), and you "
        "must attach it: submitted with no file, this tool designs against "
        "a bundled benchmark target instead of yours."
    ),
}


def prerequisite_line(slug: str) -> str:
    """One plain sentence naming what the customer must bring.

    Composed from ``needs_structure`` and ``needs_hotspots`` plus the
    adapter-level refusals above, so it cannot state a requirement the
    submit gate does not enforce, nor omit one it does.
    """
    if slug in _CURATED_DEFAULT_TARGETS:
        return _CURATED_DEFAULT_TARGETS[slug]
    if slug in _ADAPTER_INPUT_LIMITS:
        # A sequence-input tool: nothing below would compose a line for
        # it (needs_structure and needs_hotspots are both False), and an
        # empty line on a card is read as "bring nothing".
        return _ADAPTER_INPUT_LIMITS[slug]
    parts: list[str] = []
    if needs_structure(slug):
        # The structure a backbone-only tool wants is the customer's own
        # backbone, not a target: mpnn's answer 1 is "A backbone -- a 3D
        # shape with no sequence chosen for it yet" and it serves no other
        # bucket. Pinned by
        # test_a_backbone_tool_does_not_call_the_structure_a_target.
        facts = _FACTS.get(slug)
        noun = (
            "your backbone"
            if facts is not None and facts.haves == frozenset({"backbone"})
            else "your target"
        )
        parts.append(f"a structure of {noun} (.pdb or .cif)")
    if needs_hotspots(slug):
        parts.append("at least one residue on it for the binder to touch")
    if slug in _ADAPTER_RESIDUE_REFUSALS:
        parts.append(_ADAPTER_RESIDUE_REFUSALS[slug])
    if not parts:
        return ""
    return "You will need " + " and ".join(parts) + "."


def refuses_without_residues(slug: str) -> bool:
    """True when SOMETHING refuses a submit that names no residues.

    ``needs_hotspots`` reads the preflight gate only, and a tool whose own
    ``validate`` does the refusing is absent from TOOL_RULES by design
    (see ``_ADAPTER_RESIDUE_REFUSALS``). iggm is that case: its guided-run
    card read the preflight flag and told the customer the tool "does not
    require them" while tools/iggm/__init__.py answers an empty epitope
    with "Epitope residues are required."
    """
    return needs_hotspots(slug) or slug in _ADAPTER_RESIDUE_REFUSALS


def recommend(
    have: str,
    shape: str | None = None,
    chemistry: str | None = None,
) -> list[dict]:
    """Every enabled tool that fits the answers, in catalog order.

    Returns all qualifying tools rather than a ranked top three: five
    tools genuinely design mini-proteins against a protein target, and
    no metadata in this repo separates them, so picking among them would
    mean inventing a ranking. Each entry carries the tool's own
    ``comparison_one_liner`` as its reason, its runtime band, its route,
    and a derived prerequisite line.
    """
    catalog = _build_tools_catalog()
    out: list[dict] = []

    for entry in catalog:
        slug = entry["slug"]
        facts = _FACTS.get(slug)
        if facts is None:
            continue
        offer_shapes = _CROSS_BUCKET_OFFERS.get(slug, {}).get(have)
        if have not in facts.haves and offer_shapes is None:
            continue
        # esmfold2-design cannot be aimed at a patch spanning two chains
        # in EITHER bucket: it takes one pasted target sequence, and a
        # ':' chain separator is refused by _check_protein_sequence as a
        # non-canonical residue (tools/esmfold2_design/__init__.py,
        # CANONICAL_AA). The generic multi-chain filter below misses it
        # because that one is keyed on TOOL_RULES membership and this
        # tool, taking no upload, has no preflight rules.
        if slug in _ADAPTER_SINGLE_CHAIN_TARGETS and chemistry == "multi-chain":
            continue

        is_designer = bool(facts.shapes)
        # An adapter that refuses a two-chain input is dropped whichever
        # branch below would have offered it. The designer branch asks
        # supports_multi_chain, which reads TOOL_RULES; the prep tools are
        # absent from TOOL_RULES and take no upload, so needs_structure is
        # False for them and that question never reaches them. Without this
        # line ESMFold was listed as a prep step for a site spanning two
        # chains, which its own validate refuses.
        if (
            have in DESIGN_HAVES
            and chemistry == "multi-chain"
            and slug in _ADAPTER_MULTI_CHAIN_REFUSALS
        ):
            continue
        # Every chemistry filter below is scoped to `have in DESIGN_HAVES`,
        # because the chemistry question is only PUT in those two buckets
        # (blueprints/tools.py::tools_comparison sets chooser_asks_shape from
        # DESIGN_HAVES, and the template renders question 3 behind it). The
        # radios all live in one GET form, so a stale chemistry answer can
        # still arrive with a have that never asked for it; unscoped, it
        # emptied every single-answer bucket, whose tools are all
        # non-designers. Pinned by test_a_stale_chemistry_answer_does_not_
        # empty_a_bucket_that_never_asked.
        if have in DESIGN_HAVES and is_designer:
            allowed = offer_shapes if offer_shapes is not None else facts.shapes
            if shape and shape != "unsure" and shape not in allowed:
                continue
            if chemistry == "glycan" and "glycan" not in facts.chemistries:
                continue
            # Multi-chain is a preflight concept: _multi_chain_block runs
            # on a STAGED target. A tool that takes no upload never reaches
            # it, so its absence from TOOL_RULES is not a refusal and must
            # not drop it. esmfold2-design is the tool that showed this:
            # it is the only scFv designer in the target-sequence bucket
            # (boltzgen also designs scFvs, but only from a target
            # structure) and needs no PDB (requires_pdb False on the
            # adapter and both presets), so dropping it for having no
            # rules emptied that answer. It IS dropped for a multi-chain
            # patch now, but by _ADAPTER_SINGLE_CHAIN_TARGETS above,
            # which cites the refusal in its own validate rather than
            # inferring one from a missing preflight rule.
            # Keyed on TOOL_RULES membership, not needs_structure: those
            # are different questions, and proteina is the tool that shows
            # it -- requires_pdb is False on it (so needs_structure is
            # False) yet it HAS rules and does reach _multi_chain_block.
            # Reading needs_structure here would skip the question for it
            # entirely and only happen to be right while both its flags
            # stay True.
            if (
                chemistry == "multi-chain"
                and slug in TOOL_RULES
                and not supports_multi_chain(slug)
            ):
                continue

        reason = entry.get("comparison_one_liner")
        if not reason or reason == "—":
            reason = entry.get("tagline", "")

        out.append(
            {
                "slug": slug,
                "name": entry["name"],
                "reason": reason,
                "runtime_band": entry.get("runtime_band", "—"),
                "route": entry.get("route", "#"),
                "prerequisite": _CROSS_BUCKET_PREREQUISITE.get(
                    (slug, have), prerequisite_line(slug)
                ),
                "is_designer": is_designer,
                "has_example": has_example(slug),
            }
        )
    return out
