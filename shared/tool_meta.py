"""Load ``tools/<slug>/meta.py`` by ADAPTER SLUG, not by package name.

Package directories use underscores; two adapter slugs do not
(``esmfold2-design``, and any future hyphenated tool). Four call sites
built the module path by interpolating the slug raw, got an
ImportError for those, swallowed it, and rendered the tool's page with
NO metadata at all — no FAQ, no positioning line, no references, no
runtime band — which looks exactly like a tool that simply has none.
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


def meta_for(slug: str) -> ModuleType | None:
    """Return ``tools.<slug>.meta`` or None if the tool ships none."""
    try:
        return importlib.import_module(f"tools.{slug.replace('-', '_')}.meta")
    except ImportError:
        return None
