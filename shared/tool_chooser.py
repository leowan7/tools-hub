"""Three-question chooser that narrows the catalog to the tools that fit.

A visitor lands on /tools facing fourteen tiles with no way to choose.
This module answers "which of these is for me" from three questions:
what you already have, what shape of binder you want back, and what is
on your target.

WHAT IS TYPED HERE AND WHAT IS DERIVED
--------------------------------------
Typed: ``_FACTS`` — which HAVE bucket and which binder shapes each tool
serves, plus the two target-chemistry capabilities (glycans, a
small-molecule target) that only appear in prose. Each entry cites the
``about["when_to_use"]`` or ``comparison_one_liner`` line it came from.
This is a single table in one module, the same shape as the existing
``shared.tools_catalog._TOOL_CATEGORIES``; no per-tool metadata field
was added.

Derived, never typed — these are the claims a customer acts on, so they
read the enforcer rather than restating it:

* ``needs_hotspots`` reads ``TOOL_RULES[slug].hotspots_required``, the
  flag ``shared/pdb_preflight.py:664`` branches on to refuse a submit.
* ``needs_structure`` reads ``adapter.requires_pdb`` and each
  ``Preset.requires_pdb``, the flags ``blueprints/tools.py:1719`` reads
  to gate a submit before ``create_job``.
* Multi-chain eligibility reads
  ``TOOL_RULES[slug].multi_chain_container_ready``, the flag
  ``shared/pdb_preflight.py::_multi_chain_block`` refuses a run on.

``tests/test_tool_chooser.py`` asserts each of those three against the
enforcing code path, so the chooser and the gate cannot drift apart.

Recommendations are filtered through ``_build_tools_catalog()``, which
already drops flag-disabled adapters via ``shared.feature_flags``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
# Question 3 — what is on the target, asked only for structure-led design.
# ---------------------------------------------------------------------------
CHEMISTRY_CHOICES: tuple[tuple[str, str], ...] = (
    ("plain", "An ordinary protein surface"),
    ("glycan", "It carries sugars or modified residues"),
    ("small-molecule", "My target is a small molecule, not a protein"),
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
    # "Your target is a small molecule rather than a protein"
    # — tools/proteina/meta.py, about["when_to_use"][2]. Its protein_binder preset
    # takes either your own structure or a curated benchmark task
    # ("your own structure ... or a curated benchmark task for any
    # variant" — tools/proteina/meta.py, about["prerequisites"]), so it serves the
    # no-structure bucket too.
    "proteina": _Facts(
        haves=frozenset({"target-structure", "target-sequence"}),
        shapes=frozenset({"mini-protein"}),
        chemistries=frozenset({"small-molecule"}),
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


def _adapter_by_slug() -> dict:
    return {a.slug: a for a in tool_base.all_adapters()}


def needs_structure(slug: str) -> bool:
    """True when a paid run of this tool cannot start without an upload.

    Reads the same two flags ``blueprints/tools.py:1719`` reads to gate
    the submit: the adapter flag, or any preset's. proteina is False on
    both because its target is optional — a curated benchmark task is a
    complete run — and the custom-target case is gated separately at
    ``blueprints/tools.py:1727``.
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
    ``shared/pdb_preflight.py:664`` branches on. A tool with no entry in
    TOOL_RULES is not gated by that path at all, so it is False here.
    """
    rules = TOOL_RULES.get(slug)
    return bool(rules is not None and rules.hotspots_required)


def supports_multi_chain(slug: str) -> bool:
    """True when preflight lets a multi-chain target through.

    Reads ``TOOL_RULES[slug].multi_chain_container_ready``, the flag
    ``shared/pdb_preflight.py::_multi_chain_block`` refuses on. A tool
    absent from TOOL_RULES gets no multi-chain recommendation from the
    chooser: nothing here has established that its container handles one.
    """
    rules = TOOL_RULES.get(slug)
    return bool(rules is not None and rules.multi_chain_container_ready)


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
    path = (
        Path(__file__).resolve().parent.parent
        / "tools" / slug.replace("-", "_") / "example" / "result.json"
    )
    return path.is_file()


def prerequisite_line(slug: str) -> str:
    """One plain sentence naming what the customer must bring.

    Composed from ``needs_structure`` and ``needs_hotspots``, so it
    cannot state a requirement the submit gate does not enforce.
    """
    parts: list[str] = []
    if needs_structure(slug):
        parts.append("a structure of your target (.pdb or .cif)")
    if needs_hotspots(slug):
        parts.append("at least one residue on it for the binder to touch")
    if not parts:
        return ""
    return "You will need " + " and ".join(parts) + "."


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
        if facts is None or have not in facts.haves:
            continue

        is_designer = bool(facts.shapes)
        if have in DESIGN_HAVES and is_designer:
            if shape and shape != "unsure" and shape not in facts.shapes:
                continue
            if chemistry == "glycan" and "glycan" not in facts.chemistries:
                continue
            if (
                chemistry == "small-molecule"
                and "small-molecule" not in facts.chemistries
            ):
                continue
            if chemistry == "multi-chain" and not supports_multi_chain(slug):
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
                "prerequisite": prerequisite_line(slug),
                "is_designer": is_designer,
                "has_example": has_example(slug),
            }
        )
    return out
