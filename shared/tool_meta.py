"""Load ``tools/<slug>/meta.py`` by ADAPTER SLUG, not by package name.

Package directories use underscores; two adapter slugs do not
(``esmfold2-design``, and any future hyphenated tool). Four call sites
built the module path by interpolating the slug raw, got an
ImportError for those, swallowed it, and rendered the tool's page with
NO metadata at all — no FAQ, no positioning line, no references —
which looks exactly like a tool that simply has none.
Found because a PILOT card silently did not render on esmfold2-design.

Lives in ``shared/`` rather than ``tools/base.py`` on purpose. It is
web-tier plumbing that never reaches a GPU container, and anything
under ``tools/`` outside the ``meta.py`` / ``example/`` negations in
.github/workflows/deploy-modal.yml redeploys all nine Modal images on
merge.
"""

from __future__ import annotations

import importlib
from types import ModuleType


# Stored preset slug -> the short name a customer sees. Every slug in
# tools/*/__init__.py Preset tuples is listed; the Preset.label strings
# themselves are form sentences, too long for a table cell or a badge.
# An unlisted slug falls
# back to its own words, so a new preset reads as prose, not as code.
_PRESET_LABELS: dict[str, str] = {
    "pilot": "trial run",
    "standalone": "standalone",
    "batch": "batch",
    "msa_server": "with MSA",
    "minibinder": "de novo minibinder",
    "scfv": "scFv",
    "complex_prediction": "complex prediction",
    "cdr_design": "CDR design",
    "fr_design": "framework redesign",
    "affinity_maturation": "affinity maturation",
    "inverse_design": "inverse design",
    "general": "general co-folding",
    "abag": "antibody-antigen",
    "protein_binder": "protein binder",
    "ligand_binder": "ligand binder",
    "motif_ame": "motif scaffolding",
    "validate": "validate (free dry run)",
}


def preset_label(slug) -> str:  # noqa: ANN001
    """The name a customer sees for a stored preset slug."""
    if not slug:
        return ""
    return _PRESET_LABELS.get(slug, str(slug).replace("_", " "))


def meta_for(slug: str) -> ModuleType | None:
    """Return ``tools.<slug>.meta`` or None if the tool ships none."""
    try:
        return importlib.import_module(f"tools.{slug.replace('-', '_')}.meta")
    except ImportError:
        return None
