"""Shared adapter interface for GPU tools.

Stream C (Wave-2 launch prep). Every GPU tool module under ``tools/``
registers a :class:`ToolAdapter` via :func:`register`. The generic route
handlers in ``app.py`` look up the adapter by slug and dispatch form
rendering, validation, Modal payload assembly, and results rendering
to it — so adding a new GPU tool is a matter of writing one module,
not editing routes.

Adapter contract
----------------
    slug             — URL slug, matches Kendrew Modal app (``bindcraft``,
                       ``rfantibody``, ``boltzgen``, ``pxdesign``, ...).
                       Also used to derive the FLAG_TOOL_<NAME> env var.
    label            — human-readable name shown in UI.
    blurb            — one-line subtitle on the form page.
    presets          — tuple of :class:`Preset` values offered on the form.
    requires_pdb     — if True the form includes a PDB upload field; the
                       generic submit route stages the upload to Supabase
                       Storage and passes a presigned URL to the adapter.
    form_template    — path to the form template under ``templates/``.
    results_partial  — path to the results template rendered inside
                       ``templates/job_detail.html`` on success.
    validate         — callable (form, files) → (inputs_dict, error_msg).
                       Returns inputs_dict=None on validation error.
    build_payload    — callable (inputs, presigned_url) → Kendrew job_spec
                       dict. The generic route forwards this to
                       ``gpu.modal_client.submit``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


# ---------------------------------------------------------------------------
# Multi-chain target helpers
# ---------------------------------------------------------------------------
# The binder generators (bindcraft, pxdesign, rfdiffusion) take a target that
# may be an oligomer — an IgG1 Fc homodimer binder grips both protomers, and
# designing against one chain aims at half the epitope. The pipeline-side
# contract in llm-proteinDesigner is:
#
#     target_chain:     "A,B"              comma string; "A" behaves as before
#     hotspot_residues: ["A296", "B264"]   chain-prefixed; bare ints still
#                                          accepted and attributed to the
#                                          FIRST target chain
#
# These helpers keep the three adapters from each growing their own parser.


def parse_target_chains(raw: str) -> list:
    """Split a target-chain field into an ordered, de-duplicated list.

    ``"A"`` -> ``["A"]``; ``"A,B"`` -> ``["A", "B"]``. Order is preserved
    because it drives contig and FASTA concatenation downstream.
    """
    ordered: list = []
    for tok in str(raw or "").split(","):
        tok = tok.strip()
        if tok and tok not in ordered:
            ordered.append(tok)
    return ordered


def parse_hotspot_residues(
    raw: str, target_chains: list
) -> tuple[Optional[list], Optional[str]]:
    """Parse the hotspot field into the pipeline's ``hotspot_residues`` shape.

    Accepts bare integers (``54,56,115``) and chain-prefixed tokens
    (``A296,B264``), mixed freely. Returns ``(residues, None)`` or
    ``(None, error_message)`` to match the adapters' validate() convention.

    Output shape is chosen to keep existing single-chain jobs byte-identical:
    a single target chain with only bare integers still emits plain ints, the
    exact payload submitted before multi-chain existed. Anything else emits
    normalized chain-prefixed strings, which is what the pipelines need to
    tell apart residue 264 on protomer A from residue 264 on protomer B.

    A token naming a chain that is not a target is an error rather than a
    silent drop — a hotspot that quietly disappears yields an untargeted
    design that still completes and still scores.
    """
    if not target_chains:
        return None, "Target chain is required."

    tokens = [tok.strip() for tok in str(raw or "").split(",") if tok.strip()]
    if not tokens:
        return None, "At least one hotspot residue is required."

    example = f"{target_chains[0]}296"
    parsed: list = []
    all_bare = True
    for token in tokens:
        try:
            parsed.append((target_chains[0], int(token)))
            continue
        except ValueError:
            pass
        all_bare = False
        for chain in sorted(target_chains, key=len, reverse=True):
            if token.startswith(chain):
                remainder = token[len(chain):].strip()
                try:
                    parsed.append((chain, int(remainder)))
                except ValueError:
                    return None, (
                        f"Hotspot {token!r} must be a chain letter followed by "
                        f"an integer residue number (e.g. {example})."
                    )
                break
        else:
            return None, (
                f"Hotspot {token!r} does not name one of your target chains "
                f"({', '.join(target_chains)}). Use a bare integer residue "
                f"number (e.g. 296, read as chain {target_chains[0]}) or "
                f"prefix it with the chain (e.g. {example})."
            )

    if len(target_chains) == 1 and all_bare:
        return [res for _, res in parsed], None
    return [f"{chain}{res}" for chain, res in parsed], None


@dataclass(frozen=True)
class Preset:
    """One selectable preset on a tool form."""

    slug: str                    # ``standalone`` / ``pilot`` / ``full``
    label: str                   # e.g. "Pilot — your target, ~30 min"
    description: str             # subtitle shown under the option
    requires_pdb: bool = False   # if True, this preset needs a PDB upload + hotspots
    long_running: bool = False   # if True, render "we'll email you" UX (>5 min jobs)


ValidateFn = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    tuple[Optional[dict], Optional[str]],
]
BuildPayloadFn = Callable[[dict, str], dict]


@dataclass(frozen=True)
class ToolAdapter:
    """Per-tool interface consumed by the generic routes in ``app.py``."""

    slug: str
    label: str
    blurb: str
    presets: tuple[Preset, ...]
    validate: ValidateFn
    build_payload: BuildPayloadFn
    requires_pdb: bool = False
    form_template: str = ""
    results_partial: str = ""

    def preset_for(self, preset_slug: str) -> Optional[Preset]:
        for p in self.presets:
            if p.slug == preset_slug:
                return p
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, ToolAdapter] = {}


def register(adapter: ToolAdapter) -> None:
    """Add ``adapter`` to the registry. Re-registering the same slug replaces."""
    _REGISTRY[adapter.slug] = adapter


def get(slug: str) -> Optional[ToolAdapter]:
    return _REGISTRY.get(slug)


def all_adapters() -> list[ToolAdapter]:
    """Return adapters in insertion order."""
    return list(_REGISTRY.values())
