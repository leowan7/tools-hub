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


@dataclass(frozen=True)
class Preset:
    """One selectable preset on a tool form."""

    slug: str                    # ``smoke`` / ``mini_pilot`` / ``pilot`` / ``full``
    label: str                   # e.g. "Smoke (2 credits, ~3 min)"
    credits_cost: int
    description: str             # subtitle shown under the option


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
