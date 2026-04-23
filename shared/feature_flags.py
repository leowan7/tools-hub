"""Feature-flag helper for tool visibility.

Stream C (Wave-2 launch prep). Every GPU tool lands behind a
``FLAG_TOOL_<NAME>=off`` env var so the route + form + submission plumbing
can ship in one commit and the operator flips the flag AFTER running the
first real end-to-end job in production. This prevents users from
encountering a half-wired tool between "merge" and "validated in prod".

Usage
-----
    from shared.feature_flags import tool_enabled

    if not tool_enabled("bindcraft"):
        abort(404)

Naming convention
-----------------
    tool="bindcraft"        → reads FLAG_TOOL_BINDCRAFT
    tool="rfantibody"       → reads FLAG_TOOL_RFANTIBODY
    tool="proteinmpnn"      → reads FLAG_TOOL_PROTEINMPNN
    tool="af2-ig"           → reads FLAG_TOOL_AF2_IG  (hyphen becomes underscore)

Acceptable "on" values: ``on``, ``true``, ``1``, ``yes`` (case-insensitive).
Anything else — including missing — means off. Fail-closed by design:
a typo in the var value defaults the tool to hidden, which is the safe
direction for pre-validation state.
"""

from __future__ import annotations

import os

_ON_VALUES = frozenset({"on", "true", "1", "yes"})


def flag_name(tool: str) -> str:
    """Return the env var name for a given tool slug."""
    return "FLAG_TOOL_" + tool.upper().replace("-", "_")


def tool_enabled(tool: str) -> bool:
    """Return True if the tool's flag env var is set to an "on" value."""
    value = os.environ.get(flag_name(tool), "").strip().lower()
    return value in _ON_VALUES
