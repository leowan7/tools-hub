"""Modal-side GPU metadata sync for the preflight panel.

The preflight panel shows each tool's GPU class (e.g. "A100-80GB") in
the size envelope row. The value lives in ``shared.pdb_preflight_rules
.TOOL_RULES[slug].gpu`` as a hardcoded string. If llm-proteinDesigner
ever redeploys a Modal app to a different GPU SKU, the hardcoded value
goes stale silently and the panel displays the wrong hardware.

This module is the extension point for keeping that label in sync with
what Modal actually allocates. The current implementation is a stub
that always returns None (fallback to the hardcoded value). The hook
exists so a future enhancement can plug in:

  * A direct Modal API query if/when the Modal SDK exposes the GPU
    resource spec from the client side (today it does not — the
    Function object returned by ``modal.Function.from_name`` is a
    lazy stub and does not surface the deployed GPU type).
  * A vendored ``gpu_manifest.json`` written by llm-proteinDesigner
    at deploy time and pinned via the contracts SHA256 lock the
    repos already share. tools-hub would read the manifest at boot
    and override TOOL_RULES.gpu in-memory.
  * A periodic ops sync that queries the Modal CLI and pokes the
    values into a Supabase config table.

Until one of the above is wired, the hardcoded GPU values in
TOOL_RULES are the source of truth. They are currently right --
rfantibody, rfdiffusion and boltzgen on A100-40GB, bindcraft and
pxdesign on A100-80GB, each matching _GPU in that repo's Modal app --
but NOT because Week 2 measured them. This docstring used to say Week 2
had "verified all four against actual Modal logs"; it had not. One of
the five jobs succeeded, two failed in under 90 s, two were cancelled,
and the single GPU reading taken is called a misattribution by the
2026-06-10 correction in docs/CALIBRATION-WEEK2.md, which derives the
right answer from source. The labels that sentence listed were also
wrong for rfantibody and rfdiffusion.

tests/test_gpu_class_drift.py now pins these labels against
shared.wallet_estimates.TOOL_SPECS, and against tools/<dir>/modal_app.py
:_GPU wherever the container lives in this repo.

Risk: any Modal-side GPU change without a parallel TOOL_RULES update
will surface as a wrong label in the preflight panel. The blast radius
is NOT informational only, which this docstring asserted until
2026-09-01: shared/wallet_estimates.py holds a second copy of the same
GPU class as ToolSpec.gpu_class, and that one prices the job. Four of
those had drifted and were mispricing real runs by -30% to +44%. The
two copies are now cross-checked, so a wrong label here fails a test
rather than merely looking odd on a panel. The deploy checklist
(DEPLOY.md) covers the Modal side in the meantime.

Note for whoever wires the planned OBSERVED_GPU overlay: the drift test
reads the static tree, so an overlay that mutates TOOL_RULES at runtime
would move it away from TOOL_SPECS without the test noticing.
"""
from __future__ import annotations

import logging
from typing import Optional

from shared.pdb_preflight_rules import TOOL_RULES

logger = logging.getLogger(__name__)


def fetch_modal_gpu_for_tool(slug: str) -> Optional[str]:
    """Best-effort query for the GPU class Modal allocates to this tool.

    Returns the GPU class string (e.g. "A100-80GB") when a live source
    of truth is available, or ``None`` to signal "no override; use the
    hardcoded TOOL_RULES value".

    Current implementation: always returns None. See module docstring
    for the planned extension paths.

    Args:
        slug: The tool slug as used in TOOL_RULES (e.g. "rfantibody").

    Returns:
        The runtime GPU class string, or None when no live source is
        configured.
    """
    # Intentionally returns None. Wire one of the planned extension
    # paths from the module docstring when label drift becomes a
    # real ops cost (no incidents to date).
    _ = slug
    return None


def sync_tool_rules_gpu_labels() -> dict[str, Optional[str]]:
    """Refresh TOOL_RULES[*].gpu from Modal-side metadata where available.

    Called once at app startup. Iterates TOOL_RULES, calls
    ``fetch_modal_gpu_for_tool`` for each, and returns a map of
    slug -> (live value | None) for observability. When the live value
    differs from the hardcoded TOOL_RULES.gpu, the in-memory rule is
    NOT mutated today -- ToolRules is a frozen dataclass and mutating
    via dataclasses.replace would fragment the source of truth between
    the import-time dict and a per-startup overlay.

    The sync IS structured so the call site is in place; a future
    enhancement can either:

      * Replace ToolRules entries with frozen-replace copies that
        carry the live GPU.
      * Maintain a separate ``OBSERVED_GPU`` overlay dict that the
        preflight panel checks before falling back to TOOL_RULES.

    Returns:
        Map of slug -> observed GPU string (or None if no live source).
        Logged at INFO level so ops can spot drift without grepping.
    """
    observed: dict[str, Optional[str]] = {}
    for slug, rules in TOOL_RULES.items():
        live = fetch_modal_gpu_for_tool(slug)
        observed[slug] = live
        if live is None:
            logger.debug(
                "modal_gpu_sync: %s -> no live source, using hardcoded %s",
                slug, rules.gpu,
            )
        elif live != rules.gpu:
            logger.warning(
                "modal_gpu_sync: %s drift detected -- TOOL_RULES says %s "
                "but Modal allocates %s. Update shared/pdb_preflight_rules.py "
                "or wire the in-memory override.",
                slug, rules.gpu, live,
            )
        else:
            logger.info(
                "modal_gpu_sync: %s -> %s (matches TOOL_RULES)",
                slug, live,
            )
    return observed
