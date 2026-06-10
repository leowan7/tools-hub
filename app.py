"""Flask application for the Ranomics tools hub.

Hosts Ranomics' free scientific tools as lead magnets under
``tools.ranomics.com``. Today:

    /                     — hub index with tool cards
    /login, /signup,
    /forgot-password,
    /logout               — Supabase auth (shares Scout's project)
    /account              — simple logged-in user dashboard
    /health               — unauthenticated health check
    /developability       — Binder Developability Scout (form)
    /developability/score — Binder Developability Scout (results)
    /library-planner      — Yeast Display Library Planner (form)
    /library-planner/plan — Yeast Display Library Planner (results)

Auth helpers live in ``shared.auth``. Tool modules live under
``tools/<name>/`` — each one exposes a small stable API that the hub
imports lazily (scoring/analysis only, no Flask coupling inside tools).

Runs with:
    gunicorn app:app
or:
    flask --app app run
"""

import logging
import os

# Load .env for local dev. In production (Railway) env vars come from the
# platform, so load_dotenv is a silent no-op when no .env file is present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from decimal import Decimal, InvalidOperation
from functools import wraps
import json

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix

from gpu.modal_client import ModalClient
from shared.credits import (
    load_user_context,
    recent_ledger,
)
from shared.wallet import (
    MIN_TOPUP_USD,
    REASON_DAILY_CAP,
    REASON_INSUFFICIENT,
    REASON_OK,
    REASON_PER_TOOL_CAP,
    REASON_SELF_SERVE_CEILING,
    REASON_WALLET_FROZEN,
    SELF_SERVE_CEILING_USD,
    get_or_create_wallet,
    release_hold as wallet_release_hold,
    reserve_hold as wallet_reserve_hold,
    wallet_preflight,
)
from shared.wallet_estimates import (
    compute_hard_cap,
    estimated_cost_for_tool,
)
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared.handoffs import get_handoff, mark_consumed
from shared.jobs import (
    cancel_job,
    complete_job,
    create_job,
    get_job,
    list_campaign_labels_for_user,
    list_jobs_for_user,
    list_jobs_paginated,
    mark_failed,
    mark_running,
    set_modal_call,
    update_inputs,
)
from shared.metrics import register_metrics
from typing import Optional

from shared.pdb_inspect import (
    CifConversionError,
    convert_cif_to_pdb_bytes,
    hotspot_range_message,
    inspect_pdb_bytes,
    summarize_for_log,
    validate_hotspots,
    validate_target_chain,
)
from shared.pdb_preflight import (
    PREFLIGHT_TOOLS,
    PreflightVerdict,
    VerdictKind,
    preflight_for_tool,
)

# Tools whose form template renders the rich preflight panel (the JS
# verdict UI). For the others (pxdesign, boltz2) a hard-gate rejection is
# surfaced as a plain actionable ``error`` string in the form instead, so
# the message is never silently swallowed.
_PREFLIGHT_PANEL_FORMS: frozenset = frozenset(
    {"rfantibody", "rfdiffusion", "bindcraft", "boltzgen"}
)
from shared.uniprot_lookup import alphafold_api_url


# ---------------------------------------------------------------------------
# AlphaFold fallback helpers (wired into /tools/<tool>/preflight and the
# ``alphafold:<accession>`` reuse_pdb_token path in tool_submit).
# ---------------------------------------------------------------------------

# Match the UniProt accession format that uniprot_lookup uses. Kept local
# to app.py so the helper stays a one-liner and we don't have to expose
# yet another private regex from the vendored module.
import re as _re  # noqa: PLC0415 — alias to dodge any later "import re" rename

_AF_ACCESSION_RE = _re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
    r"(?:-\d+)?$"
)


def _fetch_alphafold_bytes(accession: str) -> Optional[bytes]:
    """Fetch the latest AlphaFold-DB PDB for a UniProt accession.

    Two-hop: first hit the prediction API to pin the current model URL
    (v4/v5/v6 vary across entries), then GET the PDB file. Returns the
    bytes on success, ``None`` on any failure (the caller surfaces a
    "couldn't fetch" message to the user).
    """
    if not _AF_ACCESSION_RE.match(accession or ""):
        return None
    import requests  # noqa: PLC0415
    try:
        api = requests.get(
            alphafold_api_url(accession),
            timeout=8,
            headers={"User-Agent": "ranomics-tools-hub/preflight"},
        )
    except Exception as exc:  # noqa: BLE001 - any network failure
        logger.warning("alphafold fetch metadata failed for %s: %s",
                       accession, exc)
        return None
    if api.status_code != 200:
        logger.info("alphafold metadata %s returned HTTP %d",
                    accession, api.status_code)
        return None
    try:
        meta_list = api.json()
        pdb_url = meta_list[0]["pdbUrl"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("alphafold metadata %s shape unexpected: %s",
                       accession, exc)
        return None
    try:
        pdb = requests.get(
            pdb_url, timeout=20,
            headers={"User-Agent": "ranomics-tools-hub/preflight"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("alphafold fetch pdb failed for %s: %s",
                       accession, exc)
        return None
    if pdb.status_code != 200:
        logger.info("alphafold pdb fetch %s returned HTTP %d",
                    accession, pdb.status_code)
        return None
    return pdb.content


def _verdict_to_json(verdict: PreflightVerdict, source_label: str) -> dict:
    """Project a PreflightVerdict into the JSON shape the panel JS expects."""
    af = None
    if verdict.alphafold is not None:
        af = {
            "accession": verdict.alphafold.uniprot_accession,
            "display_id": verdict.alphafold.display_id,
            "reuse_token": f"alphafold:{verdict.alphafold.uniprot_accession}",
        }
    gap_block = None
    if verdict.gap_analysis is not None and (
        verdict.gap_analysis.gaps
        or verdict.gap_analysis.warn_message
        or verdict.gap_analysis.hard_fail_message
    ):
        import math as _math
        gap_block = {
            "longest_gap": verdict.gap_analysis.longest_gap,
            "causes_hard_fail": verdict.gap_analysis.causes_hard_fail,
            "warn_message": verdict.gap_analysis.warn_message,
            "hard_fail_message": verdict.gap_analysis.hard_fail_message,
            "gaps": [
                {
                    "start": g.start,
                    "end": g.end,
                    "length": g.length,
                    "nearest_hotspot_distance": (
                        None
                        if g.nearest_hotspot_distance == _math.inf
                        else g.nearest_hotspot_distance
                    ),
                }
                for g in verdict.gap_analysis.gaps
            ],
        }
    size_block = None
    if verdict.size_envelope is not None:
        size_block = {
            "residue_count": verdict.size_envelope.residue_count,
            "hard_cap_target_aa": verdict.size_envelope.hard_cap_target_aa,
            "soft_warn_target_aa": verdict.size_envelope.soft_warn_target_aa,
            "hard_cap_combined_aa": verdict.size_envelope.hard_cap_combined_aa,
            "binder_max_aa": verdict.size_envelope.binder_max_aa,
            "combined_aa": verdict.size_envelope.combined_aa,
            "over_soft_warn": verdict.size_envelope.over_soft_warn,
            "over_hard_cap": verdict.size_envelope.over_hard_cap,
            "over_combined_cap": verdict.size_envelope.over_combined_cap,
            "runtime_estimate_min": (
                None
                if verdict.size_envelope.runtime_estimate_min is None
                else round(verdict.size_envelope.runtime_estimate_min, 1)
            ),
            "runtime_basis": verdict.size_envelope.runtime_basis,
            "gpu": verdict.size_envelope.gpu,
            "warn_message": verdict.size_envelope.warn_message,
            "hard_fail_message": verdict.size_envelope.hard_fail_message,
        }
    return {
        "kind": verdict.kind.value,
        "ok": verdict.ok,
        "tool_slug": verdict.tool_slug,
        "target_chain": verdict.target_chain,
        "source_label": source_label,
        "cleanup_items": list(verdict.cleanup.items),
        "residues_kept_on_target_chain":
            verdict.cleanup.residues_kept_on_target_chain,
        "hotspots": {
            "surviving": list(verdict.hotspot_status.get("surviving", [])),
            "dropped": list(verdict.hotspot_status.get("dropped", [])),
        },
        "reason": verdict.reason,
        "suggested_fix": verdict.suggested_fix,
        "alphafold": af,
        "nearest_clean_residues": list(verdict.nearest_clean_residues),
        "gap_analysis": gap_block,
        "size_envelope": size_block,
    }


def _parse_preflight_size_params(source) -> tuple[Optional[int], Optional[int]]:
    """Extract (binder_max_aa, num_designs) from a request.form-like mapping.

    Both are optional — when absent or unparseable, return (None, None) so
    preflight_for_tool skips the runtime estimate + combined-budget cap
    rather than firing on garbage. Used by both /preflight (request.form)
    and tool_submit (the validated ``inputs`` dict; .get works for both).
    """
    def _maybe_int(v) -> Optional[int]:
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    # Binder size for the combined-complex cap. Tools name the field
    # differently: the binder-design forms use ``binder_length_max``;
    # pxdesign uses ``binder_length``; boltz2 carries ``binder_sequences``
    # (a list of {name, sequence}) and we take the longest.
    binder_max = _maybe_int(source.get("binder_length_max"))
    if binder_max is None:
        seqs = source.get("binder_sequences")
        if isinstance(seqs, list) and seqs:
            lengths = [
                len(s.get("sequence", ""))
                for s in seqs
                if isinstance(s, dict) and s.get("sequence")
            ]
            if lengths:
                binder_max = max(lengths)
    if binder_max is None:
        binder_max = _maybe_int(source.get("binder_length"))

    return (binder_max, _maybe_int(source.get("num_designs")))


def _verify_reuse_pdb_bytes(
    adapter,
    pdb_bytes: bytes,
    *,
    target_chain: str,
    hotspots: list,
    filename: str,
    binder_max_aa: Optional[int] = None,
    num_designs: Optional[int] = None,
) -> Optional[str]:
    """Re-run the upload gate on resolved reuse/handoff/resample bytes.

    Fresh uploads are inspected + gated at the upload boundary, but the
    reuse-token paths (job:/handoff:/example:/resample:) stage bytes that
    skipped both. This mirrors that gate (inspect + chain/hotspot
    validation + per-tool hard-gate preflight) so a mismatch is caught
    upfront instead of crashing on the GPU. Reuse bytes are already PDB,
    so no CIF conversion is needed. Returns None when fit to ship, else an
    actionable error string. Never raises.
    """
    inspection = inspect_pdb_bytes(pdb_bytes, filename=filename)
    if not inspection.ok:
        return inspection.error or "The reused structure could not be read as PDB."
    tc = (target_chain or "").strip()
    if tc:
        chain_err = validate_target_chain(inspection, tc)
        if chain_err:
            return chain_err
        # boltz2 hotspots are 1-indexed sequence positions, range-checked
        # by position in its own preflight, not original PDB numbering.
        if hotspots and adapter.slug != "boltz2":
            _, out_of_range = validate_hotspots(inspection, tc, hotspots)
            if out_of_range:
                return hotspot_range_message(inspection, tc, out_of_range)
    if adapter.slug in PREFLIGHT_TOOLS:
        try:
            verdict = preflight_for_tool(
                adapter.slug, pdb_bytes,
                target_chain=tc, hotspots=hotspots or [],
                binder_max_aa=binder_max_aa, num_designs=num_designs,
            )
        except Exception:
            logger.exception(
                "reuse preflight unexpected error tool=%s", adapter.slug,
            )
            verdict = None
        if verdict is not None and not verdict.ok:
            msg = verdict.reason or "This reused target can't run as-is."
            if verdict.suggested_fix:
                msg = f"{msg} {verdict.suggested_fix}"
            return msg
    return None
from shared.storage import (
    StorageError,
    copy_input,
    download_input,
    download_output,
    output_exists,
    presigned_input_url,
    stage_campaign_candidates,
    upload_input,
)
from shared import category_glyphs as _category_glyphs
from shared import metric_glossary as _metric_glossary
from shared import resample as _resample
from shared import score_legends as _score_legends
from tools import base as tool_base
import tools.af2         # noqa: F401 — import to register adapter (D2 atomic)
import tools.bindcraft   # noqa: F401 — import to register adapter
import tools.boltz2      # noqa: F401 — import to register adapter (Boltz-2 cofold)
import tools.boltzgen    # noqa: F401 — import to register adapter
import tools.colabfold   # noqa: F401 — import to register adapter (D3 atomic)
import tools.esmfold     # noqa: F401 — import to register adapter (D4 atomic)
import tools.esmfold2_design  # noqa: F401 — import to register adapter (ESMFold2-design)
import tools.mpnn        # noqa: F401 — import to register adapter (D1 atomic)
import tools.pxdesign    # noqa: F401 — import to register adapter
import tools.rfantibody  # noqa: F401 — import to register adapter
import tools.rfdiffusion # noqa: F401 — import to register adapter
from scout import scout_bp
from webhooks.modal import register_modal_webhooks
from webhooks.stripe import register_stripe_webhook
from webhooks.uploads import register_upload_urls

logger = logging.getLogger(__name__)


# Static taglines for the hardcoded (non-adapter) tools. These three tools
# are not part of the GPU tool_base registry, so they are added to the
# catalog directly.
_HARDCODED_TOOLS: tuple[dict, ...] = (
    {
        "slug": "epitope-scout",
        "name": "Epitope Scout",
        "tagline": (
            "Score a target's surface for binder-design feasibility "
            "before committing GPU time."
        ),
        "comparison_one_liner": (
            "Pick Epitope Scout first to identify candidate epitopes "
            "and per-dimension feasibility for any target."
        ),
        "category": "Scope the target",
        "smoke_runtime": "~30 s",
        "pilot_runtime": "—",
        "runtime_band": "~30 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "scout.index",
        "external": False,
        "status": "live",
    },
    {
        "slug": "developability",
        "name": "Binder Developability Scout",
        "tagline": (
            "Flag developability liabilities in antibody and nanobody "
            "sequences before you order them."
        ),
        "comparison_one_liner": (
            "Pick Developability Scout when you have a sequence and "
            "need a quick liability scan (CDR length, hydrophobic "
            "patches, charge balance, isoelectric point)."
        ),
        "category": "Check developability",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "runtime_band": "<5 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "developability",
        "external": False,
        "status": "live",
    },
    {
        "slug": "library-planner",
        "name": "Yeast Display Library Planner",
        "tagline": (
            "Plan yeast display libraries with realistic diversity and "
            "screen-size estimates for your scaffold and Kd target."
        ),
        "comparison_one_liner": (
            "Pick the Library Planner when you have a binder design "
            "shortlist and need to scope library size, diversification "
            "scheme, and screening throughput before ordering DNA."
        ),
        "category": "Scope the target",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "runtime_band": "<5 s",
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "library_planner",
        "external": False,
        "status": "live",
    },
)


# Maps each GPU tool slug to a workflow-stage category. The buckets
# describe what each tool actually designs so a scientist scanning the
# catalog can find the right scaffold class at a glance. The earlier
# single "Design binders" bucket lumped six tools doing different jobs
# (de novo minibinders vs antibody scaffolds vs sequence-on-backbone)
# and forced readers to open each card to disambiguate.
_TOOL_CATEGORIES: dict[str, str] = {
    "rfdiffusion": "De novo minibinders",
    "bindcraft": "De novo minibinders",
    "pxdesign": "De novo minibinders",
    "rfantibody": "Antibodies (VHH)",
    "esmfold2-design": "Antibodies (scFv) + minibinders",
    "boltzgen": "Dual capabilities (minibinder + antibody scaffolds)",
    "mpnn": "Sequence on a backbone",
    "af2": "Structure prediction",
    "colabfold": "Structure prediction",
    "esmfold": "Structure prediction",
    "boltz2": "Structure prediction",
}


def _build_tools_catalog() -> list[dict]:
    """Return the unified tool catalog used by the homepage tile grid
    and the ``/tools`` discovery page.

    Each entry includes display name, tagline, category, route, and
    runtime bands so a single template can render the tile layout, the
    comparison matrix, and the homepage cards from one source of truth. Hardcoded tools (Epitope Scout, Developability,
    Library Planner) are included regardless of feature flags;
    GPU adapters are filtered through ``tool_enabled`` so a flag-off
    tool stays invisible to the catalog.
    """
    import importlib  # noqa: PLC0415

    catalog: list[dict] = []

    # Hardcoded tools (not part of tool_base registry). External Scout
    # link is resolved at request time so the URL adapts to the current
    # host (covers local dev vs production).
    for entry in _HARDCODED_TOOLS:
        item = dict(entry)
        endpoint = item.pop("endpoint", None)
        try:
            item["route"] = url_for(endpoint) if endpoint else "#"
        except Exception:  # noqa: BLE001 — outside request context
            item["route"] = "#"
        catalog.append(item)

    # GPU adapters (flag-gated).
    for adapter in tool_base.all_adapters():
        if not tool_enabled(adapter.slug):
            continue

        meta = None
        try:
            meta = importlib.import_module(f"tools.{adapter.slug}.meta")
        except ImportError:
            pass

        # Build the runtime band from whatever presets the adapter exposes
        # (smoke + mini_pilot tiers were removed 2026-05-29; atomic tools
        # now have a single standalone preset, composites have pilot
        # only). Show the fastest preset's runtime through the slowest
        # as a band.
        runtime_band = "—"
        if meta is not None:
            runtime_map = getattr(meta, "PRESET_RUNTIME", None) or {}
            legacy_rows = getattr(meta, "preset_runtime_rows", None) or ()
            legacy_by_slug = {
                r.get("slug"): r.get("runtime")
                for r in legacy_rows
                if r.get("slug") and r.get("runtime")
            }
            runtimes: list[str] = []
            for preset in adapter.presets:
                entry = runtime_map.get(preset.slug) or {}
                if entry.get("typical_minutes"):
                    rt = f"{entry['typical_minutes']} min"
                else:
                    rt = legacy_by_slug.get(preset.slug)
                if rt and rt not in runtimes:
                    runtimes.append(rt)
            if len(runtimes) >= 2:
                runtime_band = f"{runtimes[0]} to {runtimes[-1]}"
            elif len(runtimes) == 1:
                runtime_band = runtimes[0]

        display_name = adapter.label.split("—")[0].strip() or adapter.label
        try:
            route = url_for("tool_form", tool=adapter.slug)
        except Exception:  # noqa: BLE001
            route = f"/tools/{adapter.slug}"

        catalog.append(
            {
                "slug": adapter.slug,
                "name": display_name,
                "tagline": adapter.blurb,
                "comparison_one_liner": getattr(
                    meta, "comparison_one_liner", "—"
                ) if meta is not None else "—",
                "category": _TOOL_CATEGORIES.get(adapter.slug, "Other"),
                "runtime_band": runtime_band,
                "paper_citation": getattr(
                    meta, "paper_citation", "—"
                ) if meta is not None else "—",
                "paper_url": getattr(
                    meta, "paper_url", ""
                ) if meta is not None else "",
                "github_url": getattr(
                    meta, "github_url", ""
                ) if meta is not None else "",
                "route": route,
                "external": False,
                "status": "live",
            }
        )

    return catalog


def _wallet_params_from_form(form) -> dict:  # noqa: ANN001
    """Return a dict of params relevant to the wallet estimator.

    The wallet estimate only looks at scaling parameters (num_designs,
    iters, target_length, etc.). We strip the form down to a flat dict
    and coerce numerics where possible so the estimator can read them.
    """
    params: dict[str, object] = {}
    for key, value in form.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        # Cheap numeric coercion; the estimator falls back to defaults
        # on unparseable inputs.
        if isinstance(value, (int, float, Decimal)):
            params[key] = value
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                params[key] = int(stripped)
                continue
            except ValueError:
                pass
            try:
                params[key] = float(stripped)
                continue
            except ValueError:
                pass
            params[key] = stripped
    return params


def _share_allowed(user_metadata) -> bool:  # noqa: ANN001
    """Return True when ``user_metadata.allow_share`` is explicitly True.

    Default is False — the D4 share endpoint never emits a URL for an
    account that has not actively turned share-out on. Used by
    ``/jobs/<id>/share`` to gate the JSON response.
    """
    if not isinstance(user_metadata, dict):
        return False
    value = user_metadata.get("allow_share")
    return isinstance(value, bool) and value is True


def _top_score_for_share(job) -> str | None:  # noqa: ANN001
    """Pull a formatted top-candidate score for the share og_title.

    Returns None when the job has no candidate scores to surface (a
    failed run, a sequence-design tool, a job without a result yet).
    The caller composes ``og_title`` without the trailing score clause
    when this returns None.
    """
    if getattr(job, "status", None) != "succeeded":
        return None
    result = getattr(job, "result", None) or {}
    if not isinstance(result, dict):
        return None
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    top = candidates[0]
    if not isinstance(top, dict):
        return None
    scores = top.get("scores")
    if not isinstance(scores, dict):
        # Some adapters inline the score at the candidate root.
        flat = {
            k: top.get(k) for k in ("iptm", "ipTM", "plddt", "pLDDT")
            if isinstance(top.get(k), (int, float))
        }
        scores = flat or {}
    for col in scores:
        val = scores.get(col)
        if isinstance(val, (int, float)):
            return f"{col} {val:.3f}" if isinstance(val, float) else f"{col} {val}"
    return None


def _round_up_topup_amount(deficit: Decimal) -> Decimal:
    """Round the deficit up to the nearest $5, with a floor of MIN_TOPUP_USD.

    Mirrors the formula in the plan's Moment 2 spec:
    ``ceil((estimate - balance) / 5) * 5`` with a $20 minimum.
    """
    if deficit <= 0:
        return MIN_TOPUP_USD
    five = Decimal("5")
    bumped = (deficit / five).to_integral_value(rounding="ROUND_CEILING") * five
    return max(bumped, MIN_TOPUP_USD)


def _render_topup_gate(
    *,
    tool_slug: str,
    estimate: Decimal,
    balance: Decimal,
    deficit: Decimal,
    reason: str,
    hard_cap: Decimal,
    form_snapshot: dict,
):
    """Render the 'Top up and run' gate.

    Reuses ``templates/wallet/topup.html`` which already supports the
    gate flow via ``next_url`` and ``deficit_usd`` (Agent H may swap
    in a dedicated topup-and-run template later; the context here is
    forward compatible with that swap).
    """
    suggested = _round_up_topup_amount(deficit)
    # Stash the original form on the session so /account/topup-complete
    # can return the user back to the form with values intact. The form
    # snapshot is JSON serializable text only.
    try:
        session["wallet_gate_form"] = {
            "tool": tool_slug,
            "form": {
                k: v for k, v in form_snapshot.items()
                if isinstance(v, (str, int, float, bool))
            },
            "reason": reason,
        }
    except Exception:  # session writes are best effort
        pass

    next_url = url_for("tool_form", tool=tool_slug)
    wallet = get_or_create_wallet(session.get("user_id") or "") or {}
    return render_template(
        "wallet/topup.html",
        wallet=wallet,
        deficit_usd=deficit,
        estimate_usd=estimate,
        balance_usd=balance,
        hard_cap_usd=hard_cap,
        suggested_amount=suggested,
        min_topup_usd=MIN_TOPUP_USD,
        next_url=next_url,
        gate_reason=reason,
        tool_slug=tool_slug,
        self_serve_ceiling_usd=SELF_SERVE_CEILING_USD,
    )


def requires_wallet(view_func=None, *, tool_slug=None):
    """Flask decorator that gates a tool submit POST on the wallet.

    Two call shapes are supported:

    * Bare decorator: ``@requires_wallet`` on a handler whose Flask
      URL converter binds ``<tool>`` (the slug is read from ``kwargs``).
    * Factory: ``@requires_wallet(tool_slug='mpnn')`` on a route
      whose URL is hardcoded to one tool.

    Three phase contract:

    1. Compute the estimate via ``estimated_cost_for_tool`` based on
       the form params. Resolve the parameter scaled hard cap and the
       user's current balance.
    2. Block flow on any of these reasons by rendering the 'Top up and
       run' gate (Moment 2 of the plan) or the per tool cap message
       (Moment 3): wallet frozen, insufficient balance, per tool cap
       exceeded, self serve ceiling exceeded, daily cap reached.
    3. On allow, atomically reserve the hold via ``reserve_hold`` and
       stash the hold_tx_id plus estimate on ``flask.g`` for the
       handler. If the SQL hold returns null (lost a concurrent race),
       render the gate too.

    The wrapped handler is expected to read ``g.wallet_hold_tx_id`` and
    persist it on the job row so the settle path in
    :func:`shared.jobs.complete_job` can close out the hold later.
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003
            # Resolve the tool slug from the URL kwarg, the factory
            # argument, or fall through if neither is present.
            resolved_slug = (
                tool_slug
                or kwargs.get("tool")
                or kwargs.get("tool_slug")
                or ""
            )
            if not resolved_slug:
                return f(*args, **kwargs)

            # Resolve user_id from session first; fall back to
            # load_user_context which reads the email and looks up
            # auth.users for the id. Tests that only set user_email
            # in the session take this branch.
            user_id = session.get("user_id")
            if not user_id:
                try:
                    ctx = load_user_context()
                except Exception:
                    ctx = None
                user_id = ctx.user_id if ctx else None
            if not user_id:
                # No identifiable user. Fall through so the
                # @login_required path handles the redirect. The wallet
                # decorator never preempts auth.
                g.wallet_estimate_usd = Decimal("0")
                g.wallet_hold_tx_id = None
                g.wallet_params = {}
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            params = _wallet_params_from_form(request.form)
            try:
                estimate = estimated_cost_for_tool(
                    user_id, resolved_slug, params
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "requires_wallet: estimate failed for user=%s tool=%s",
                    user_id, resolved_slug, exc_info=True,
                )
                estimate = Decimal("0")

            # Smoke runs with a zero estimate skip the gate entirely.
            # No hold row is placed; the handler simply proceeds.
            if estimate <= Decimal("0"):
                g.wallet_estimate_usd = Decimal("0")
                g.wallet_hold_tx_id = None
                g.wallet_params = params
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            # Detect a missing service client (tests, dev with no
            # Supabase). When the wallet layer can not even resolve
            # the user_wallets row, fall through to the legacy path
            # instead of locking out every request behind the gate.
            try:
                wallet_row = get_or_create_wallet(user_id)
            except Exception:  # noqa: BLE001
                wallet_row = None
            if wallet_row is None:
                g.wallet_estimate_usd = estimate
                g.wallet_hold_tx_id = None
                g.wallet_params = params
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            pre = wallet_preflight(
                user_id, resolved_slug, estimate, params
            )
            if not pre.allow:
                return _render_topup_gate(
                    tool_slug=resolved_slug,
                    estimate=pre.estimated_cost_usd,
                    balance=pre.balance_usd,
                    deficit=pre.deficit_usd,
                    reason=pre.reason,
                    hard_cap=pre.hard_cap_usd,
                    form_snapshot=request.form.to_dict() or {},
                )

            hold_tx_id = wallet_reserve_hold(
                user_id, resolved_slug, None, estimate, params
            )
            if not hold_tx_id:
                # Lost a concurrent race or fell foul of a SQL guard.
                fresh = wallet_preflight(
                    user_id, resolved_slug, estimate, params
                )
                return _render_topup_gate(
                    tool_slug=resolved_slug,
                    estimate=fresh.estimated_cost_usd,
                    balance=fresh.balance_usd,
                    deficit=fresh.deficit_usd,
                    reason=fresh.reason or REASON_INSUFFICIENT,
                    hard_cap=fresh.hard_cap_usd,
                    form_snapshot=request.form.to_dict() or {},
                )

            g.wallet_estimate_usd = estimate
            g.wallet_hold_tx_id = hold_tx_id
            g.wallet_params = params
            g.wallet_tool_slug = resolved_slug
            # The wrapped view sets this to True once create_job has run
            # and stashed the hold_tx_id on the job inputs. Any early
            # return before that (form validation, PDB validation,
            # workspace gate, etc.) leaves the flag False and triggers
            # an automatic release in the finally block below. Without
            # this guard, a user who submits with a missing PDB has the
            # estimate deducted from their wallet with no job to settle
            # it, and the only recovery is a manual SQL release.
            g.wallet_hold_consumed = False

            try:
                response = f(*args, **kwargs)
            except Exception:
                # Handler raised. Release the hold so the user is not
                # left with a stranded reservation.
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="handler_exception"
                    )
                except Exception:
                    logger.warning(
                        "requires_wallet: release_hold on exception "
                        "failed for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
                raise

            # Early-return path (no exception, but the view returned
            # without writing a tool_jobs row, e.g. a form-with-error
            # render). Release so the hold does not leak.
            if not getattr(g, "wallet_hold_consumed", False):
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="view_early_return"
                    )
                except Exception:
                    logger.warning(
                        "requires_wallet: release_hold on early return "
                        "failed for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return response

        return wrapper

    # Bare-decorator usage: @requires_wallet
    if callable(view_func) and tool_slug is None:
        return decorator(view_func)
    # Factory usage: @requires_wallet(tool_slug='mpnn')
    return decorator


def create_app() -> Flask:
    """Create and configure the tools-hub Flask application.

    Returns:
        Flask: Configured Flask application instance.
    """
    flask_app = Flask(__name__)

    # Trust the X-Forwarded-Proto/X-Forwarded-Host headers Railway sets.
    # Without this, Flask sees the internal http:// hop and url_for(_external=True)
    # generates http:// URLs — which Railway 405s when the Modal pipeline tries
    # to POST a webhook back to /webhooks/modal/<job_id>/<token>. PREFERRED_URL_SCHEME
    # is the belt-and-suspenders fallback if the header is ever stripped upstream.
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)
    flask_app.config["PREFERRED_URL_SCHEME"] = os.environ.get(
        "PREFERRED_URL_SCHEME", "https"
    )

    # Enable gzip/brotli compression on text responses (HTML, CSS, JS, JSON).
    # Reduces transfer size 70-90% on repeat-heavy pages and speeds up first paint.
    Compress(flask_app)

    # Secret key for signing Flask session cookies. Set SESSION_SECRET_KEY
    # in the deployment environment. Random fallback means sessions do not
    # survive restarts, which is acceptable for an internal tool.
    flask_app.config["SECRET_KEY"] = os.environ.get(
        "SESSION_SECRET_KEY", os.urandom(32)
    )

    # GPU label sync: best-effort refresh of TOOL_RULES.gpu from Modal-side
    # metadata. Today this is a stub that always falls back to the hardcoded
    # values; the hook exists so a future Modal API query or vendored
    # gpu_manifest.json can plug in without touching the create_app flow.
    # Wrapped in try/except so a Modal outage cannot stop tools-hub from
    # booting. See shared/modal_gpu_metadata.py for the extension paths.
    try:
        from shared.modal_gpu_metadata import (  # noqa: PLC0415
            sync_tool_rules_gpu_labels,
        )
        sync_tool_rules_gpu_labels()
    except Exception:
        logger.warning(
            "modal_gpu_sync raised at startup; continuing with hardcoded "
            "TOOL_RULES.gpu values.",
            exc_info=True,
        )

    # Metric glossary available in all templates (candidate_table macro reads it).
    flask_app.jinja_env.globals["metric_glossary"] = _metric_glossary.GLOSSARY

    # Per-tool score legends. The candidate_table macro calls
    # ``score_legends_for(tool_slug)`` to render per-column "what counts
    # as good?" tooltips. Returns a {column_key: legend} dict.
    flask_app.jinja_env.globals["score_legends_for"] = (
        _score_legends.score_legends_for
    )

    # Map workflow-stage category labels to SVG glyph slugs. The
    # homepage tile grid and ``/tools`` discovery page render the
    # returned slug into ``static/img/categories/<slug>.svg`` so each
    # category section gets a scannable visual marker.
    flask_app.jinja_env.globals["category_glyph"] = (
        _category_glyphs.category_glyph_slug
    )
    flask_app.jinja_env.globals["inline_category_glyph"] = (
        _category_glyphs.inline_category_glyph
    )

    # ``tool_about(adapter)`` returns the structured About-panel dict
    # from ``tools/<slug>/meta.py``. Lets refactored form templates
    # render the shared about_panel macro without every render_template
    # call site needing to pass an explicit ``about=`` kwarg.
    def _tool_about(adapter):
        import importlib  # noqa: PLC0415
        if adapter is None:
            return {}
        try:
            meta = importlib.import_module(f"tools.{adapter.slug}.meta")
        except ImportError:
            return {}
        return getattr(meta, "about", {}) or {}
    flask_app.jinja_env.globals["tool_about"] = _tool_about

    # Inject Workspace context into every template so the shared header
    # can render the "Active Workspaces (N)" badge. Replaces the legacy
    # ranomics_tier / ranomics_credits injection from the subscription
    # model. ``now`` is also injected so workspace templates can render
    # "N days remaining" without each view recomputing it.
    @flask_app.context_processor
    def inject_workspace_context():
        from datetime import datetime, timezone  # noqa: PLC0415
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415
        from shared.workspaces import active_workspaces_count  # noqa: PLC0415

        email = session.get("user_email") or ""
        base = {
            "now": datetime.now(timezone.utc),
            "active_workspaces_count": 0,
            "ranomics_user_id": None,
            "is_staff": email in STAFF_EMAILS,
            "nav_wallet_usd": None,
            "support_email": (
                os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip()
                or "info@ranomics.com"
            ),
            # Analytics keys for templates/base.html. Empty in dev/staging
            # so the snippets render no-ops unless the env vars are set.
            "posthog_key": os.environ.get("POSTHOG_KEY", "").strip(),
            "posthog_host": os.environ.get(
                "POSTHOG_HOST", "https://us.i.posthog.com"
            ).strip(),
            "ga4_measurement_id": os.environ.get(
                "GA4_MEASUREMENT_ID", ""
            ).strip(),
            # Per-page canonical URL override. Routes that need a non-default
            # canonical (e.g. a paginated index canonicalized to page 1) can
            # pass canonical_url=... in render_template kwargs.
            "canonical_url": None,
        }
        if not email:
            return base
        ctx = load_user_context()
        if ctx is None:
            return base
        base["active_workspaces_count"] = active_workspaces_count(ctx.user_id)
        base["ranomics_user_id"] = ctx.user_id

        # Wallet balance for the navbar chip. Best-effort: a Supabase
        # hiccup must not break header rendering, so we swallow failures
        # and leave the chip absent.
        try:
            client = get_service_client()
            if client is not None:
                resp = (
                    client.table("user_wallets")
                    .select("balance_usd")
                    .eq("user_id", ctx.user_id)
                    .maybe_single()
                    .execute()
                )
                row = getattr(resp, "data", None) or {}
                if row.get("balance_usd") is not None:
                    base["nav_wallet_usd"] = float(row["balance_usd"])
        except Exception:
            logger.debug(
                "nav wallet read failed for %s", ctx.user_id, exc_info=True
            )

        # Onboarding ribbon (C9): show the welcome strip to first-run users.
        # Conditions: signed in + wallet credit > 0 + zero tool_jobs rows.
        # Cheap check, runs once for fresh users and stops as soon as they
        # submit their first job (the ribbon hides server-side).
        base["show_onboarding_ribbon"] = False
        try:
            if (
                base.get("nav_wallet_usd") is not None
                and base["nav_wallet_usd"] > 0
            ):
                client = get_service_client()
                if client is not None:
                    resp = (
                        client.table("tool_jobs")
                        .select("id")
                        .eq("user_id", ctx.user_id)
                        .limit(1)
                        .execute()
                    )
                    rows = getattr(resp, "data", None) or []
                    if not rows:
                        base["show_onboarding_ribbon"] = True
        except Exception:
            logger.debug(
                "onboarding ribbon check failed for %s",
                ctx.user_id,
                exc_info=True,
            )

        return base

    # Stripe webhook — mounted at /webhooks/stripe. Signature verification
    # + event_id idempotency live inside webhooks/stripe.py.
    register_stripe_webhook(flask_app)

    # Prometheus /metrics (IP-allowlisted) + /healthz readiness probe.
    # The existing /health liveness probe below stays as a dumb 200.
    register_metrics(flask_app)

    # Modal pipeline callbacks — /webhooks/modal/<job_id>/<token> + /webhooks/heartbeat.
    register_modal_webhooks(flask_app)

    # Modal-facing upload-URL minter — /api/upload-urls/<job_id>/<token>.
    # Pipelines call this to obtain presigned PUT URLs for candidate PDBs,
    # which they then write into the tool-outputs Storage bucket directly.
    register_upload_urls(flask_app)

    # Scout (free tier) blueprint — everything under /scout.
    from pathlib import Path as _Path  # noqa: PLC0415
    _Path("tmp").mkdir(exist_ok=True)
    flask_app.config.setdefault("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)
    flask_app.register_blueprint(scout_bp)

    # ------------------------------------------------------------------
    # Platform API — wet-lab as an API for binder-design agents.
    #
    # Gated behind ENABLE_PLATFORM_API=1 so the entire /api/v1/* surface
    # plus /account/api-keys plus /.well-known/ai-plugin.json return 404
    # in environments where the alpha is not yet live. Toggle on the
    # Railway env var to flip live; remove and restart to remove cleanly.
    # ------------------------------------------------------------------
    if os.environ.get("ENABLE_PLATFORM_API", "").strip() == "1":
        # FIX #7 (validation finding) — refuse to boot if SESSION_SECRET_KEY
        # is unset when the API is enabled. The plaintext-key flow in
        # /account/api-keys/create can't rely on auto-rotating per-process
        # keys: a Railway redeploy would invalidate every in-flight session
        # cookie, silently losing the one-shot reveal banner. WEBHOOK_SIGNING_
        # SECRET is verified inside shared.webhooks, but the operator should
        # set it at the same time as ENABLE_PLATFORM_API or transitions will
        # log noisy errors on first webhook dispatch.
        if not (os.environ.get("SESSION_SECRET_KEY") or "").strip():
            raise RuntimeError(
                "ENABLE_PLATFORM_API=1 requires SESSION_SECRET_KEY to be set "
                "in the process env (the plaintext API-key reveal depends on "
                "stable session signing across redeploys)."
            )
        if not (os.environ.get("WEBHOOK_SIGNING_SECRET") or "").strip():
            logger.warning(
                "ENABLE_PLATFORM_API=1 but WEBHOOK_SIGNING_SECRET is unset; "
                "webhook delivery will fail closed until it is configured."
            )

        # FIX HI-03 (fresh-review): harden session cookies before any
        # API-key surface goes live. Flask's default
        # SESSION_COOKIE_SAMESITE is None (== browser default "Lax"),
        # which still permits top-level POST navigations to send the
        # cookie — so a malicious page can submit a hidden form to
        # /account/api-keys/create on the user's behalf. Strict blocks
        # cross-site requests entirely. Secure flag is harmless here
        # because the app runs behind HTTPS in prod (Railway provides
        # TLS). Direct assignment — Flask's default_config pre-populates
        # these keys with None/False so .setdefault() is a no-op.
        flask_app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
        flask_app.config["SESSION_COOKIE_SECURE"] = True
        flask_app.config["SESSION_COOKIE_HTTPONLY"] = True

        from tools.platform_api import platform_api_bp  # noqa: PLC0415

        flask_app.register_blueprint(platform_api_bp)

        # Surface the flag to Jinja so templates (e.g. account.html) can
        # conditionally show the "Platform API → API Keys" entry point.
        # Without this, the /account/api-keys page exists but is
        # invisible — users had no way to discover it from the in-app nav.
        flask_app.jinja_env.globals["platform_api_enabled"] = True

        # CR-02 (fresh-review): start the webhook-retry sweep.
        # The in-thread sleep model used to lose every retry on a Railway
        # redeploy (the worker dies mid-sleep). The sweep replaces that:
        # ``next_retry_at`` becomes the source of truth, and a 60s tick
        # picks up any due rows. Gated behind WEBHOOK_SWEEP_ENABLED in
        # case an operator needs to disable it (the kill switch on top
        # of the ENABLE_PLATFORM_API kill switch).
        if os.environ.get("WEBHOOK_SWEEP_ENABLED", "1").strip() == "1":
            try:
                from datetime import (  # noqa: PLC0415
                    datetime,
                    timedelta,
                    timezone,
                )

                from apscheduler.schedulers.background import (  # noqa: PLC0415
                    BackgroundScheduler,
                )
                from shared.webhooks import sweep_due_deliveries  # noqa: PLC0415

                sweep_interval = int(
                    os.environ.get("WEBHOOK_SWEEP_INTERVAL_SECONDS", "60")
                )
                if sweep_interval < 10:
                    sweep_interval = 10  # floor; below this we DoS the DB
                _webhook_scheduler = BackgroundScheduler(
                    timezone="UTC",
                    daemon=True,
                    job_defaults={
                        "coalesce": True,  # drop missed ticks, don't pile up
                        "max_instances": 1,  # one sweep at a time per replica
                    },
                )
                _webhook_scheduler.add_job(
                    sweep_due_deliveries,
                    trigger="interval",
                    seconds=sweep_interval,
                    id="webhook-sweep",
                    next_run_time=datetime.now(timezone.utc)
                    + timedelta(seconds=sweep_interval),
                )
                _webhook_scheduler.start()
                logger.info(
                    "Webhook sweep started (interval=%ds)",
                    sweep_interval,
                )
            except Exception:
                logger.error(
                    "Webhook sweep failed to start; "
                    "deliveries that backpressure or fail their first attempt "
                    "will not be retried until a manual sweep runs.",
                    exc_info=True,
                )

        @flask_app.route("/.well-known/ai-plugin.json", methods=["GET"])
        def ai_plugin_manifest():
            from flask import jsonify  # noqa: PLC0415

            payload = {
                "schema_version": "v1",
                "name_for_human": "Ranomics Platform API",
                "name_for_model": "ranomics_platform",
                "description_for_human": (
                    "Submit binder candidates for yeast-display triage and "
                    "retrieve enrichment results."
                ),
                "description_for_model": (
                    "Use this API to triage AI-designed binder libraries via "
                    "wet-lab yeast display, mammalian display, or DMS at "
                    "Ranomics. POST /api/v1/experiments with a sequences dict "
                    "and target spec; poll GET /api/v1/experiments/{id} for "
                    "status; fetch results via GET /api/v1/experiments/{id}/"
                    "results once results_status != 'none'. Convention-"
                    "compatible with Adaptyv Foundry shapes."
                ),
                "auth": {"type": "user_http", "authorization_type": "bearer"},
                "api": {
                    "type": "openapi",
                    "url": "https://tools.ranomics.com/api/v1/openapi.json",
                },
                "logo_url": "https://ranomics.com/favicon.svg",
                "contact_email": "info@ranomics.com",
                "legal_info_url": "https://ranomics.com/platform",
            }
            resp = jsonify(payload)
            resp.headers["Cache-Control"] = "public, max-age=300"
            return resp

        # --- /account/api-keys management page ---
        from shared.api_keys import (  # noqa: PLC0415
            VALID_ROLES,
            list_keys,
            mint_token,
            revoke_key,
        )
        from shared.auth import login_required  # noqa: PLC0415

        # FIX HI-03 (fresh-review): per-session CSRF token for the
        # /account/api-keys/* POST handlers. SameSite=Strict blocks the
        # cross-site case browser-side; this guards against same-site
        # XSS-leveraged forgeries and any pre-Strict legacy browser.
        # Stored in session as a hex string; rotated when the cookie
        # rotates (login/logout/secret change).
        import hmac as _hmac  # noqa: PLC0415
        import secrets as _secrets  # noqa: PLC0415

        _CSRF_SESSION_KEY = "_platform_api_csrf"

        def _ensure_csrf_token() -> str:
            """Return the session's CSRF token, minting one if absent."""
            token = session.get(_CSRF_SESSION_KEY)
            if not token or not isinstance(token, str):
                token = _secrets.token_urlsafe(32)
                session[_CSRF_SESSION_KEY] = token
            return token

        def _csrf_ok() -> bool:
            """Constant-time compare submitted ``_csrf`` against session value."""
            expected = session.get(_CSRF_SESSION_KEY) or ""
            submitted = (request.form.get("_csrf") or "").strip()
            if not expected or not submitted:
                return False
            return _hmac.compare_digest(expected, submitted)

        def _format_dt(value):
            if not value:
                return None
            # Supabase returns ISO 8601 strings; trim subseconds + tz for display.
            return str(value)[:19].replace("T", " ") + " UTC"

        def _render_api_keys_page(
            user_id: str,
            *,
            just_minted_plaintext: Optional[str] = None,
            just_minted_webhook_secret: Optional[str] = None,
            create_error: Optional[str] = None,
            rotate_notice: Optional[str] = None,
        ):
            """Shared renderer for the GET and POST handlers.

            Pulled out so the POST path can render the template directly
            with the one-shot plaintext token instead of round-tripping it
            through the session cookie (FIX #3 in the validation review:
            Flask sessions are signed-but-not-encrypted, so storing
            ``rk_live_...`` there leaked the plaintext into the browser
            cookie jar and any proxy log capturing cookies). The
            ``just_minted_webhook_secret`` parameter follows the same
            never-in-session rule for the per-tenant HMAC key (CR-01).
            """
            from shared.api_keys import get_webhook_secret_display

            raw_keys = list_keys(user_id)
            keys = [
                {
                    "key_id": k.key_id,
                    "prefix": k.prefix,
                    "label": k.label,
                    "role": k.role,
                    "revoked_at": k.revoked_at,
                    "created_at_display": _format_dt(k.created_at),
                    "last_used_display": _format_dt(k.last_used_at),
                }
                for k in raw_keys
            ]
            webhook_secret_display = get_webhook_secret_display(user_id=user_id)
            return render_template(
                "account_api_keys.html",
                keys=keys,
                just_minted_plaintext=just_minted_plaintext,
                just_minted_webhook_secret=just_minted_webhook_secret,
                webhook_secret_display=webhook_secret_display,
                create_error=create_error,
                rotate_notice=rotate_notice,
                csrf_token=_ensure_csrf_token(),
            )

        @flask_app.route("/account/api-keys", methods=["GET"])
        @login_required
        def account_api_keys():
            user_ctx = load_user_context()
            if user_ctx is None:
                return redirect(url_for("login"))
            return _render_api_keys_page(user_ctx.user_id)

        @flask_app.route("/account/api-keys/create", methods=["POST"])
        @login_required
        def account_api_keys_create():
            user_ctx = load_user_context()
            if user_ctx is None:
                return redirect(url_for("login"))
            if not _csrf_ok():
                # FIX HI-03: defense-in-depth over SameSite=Strict. A 400
                # is fine here — legitimate users hitting this path always
                # POST through the rendered form, which carries the token.
                return _render_api_keys_page(
                    user_ctx.user_id,
                    create_error=(
                        "Form submission failed CSRF check. Refresh this "
                        "page and try again."
                    ),
                ), 400
            label = (request.form.get("label") or "").strip()[:120] or None
            role = (request.form.get("role") or "member").strip().lower()
            if role not in VALID_ROLES:
                role = "member"
            minted = mint_token(
                user_id=user_ctx.user_id, role=role, label=label
            )
            if minted is None:
                return _render_api_keys_page(
                    user_ctx.user_id,
                    create_error=(
                        "Could not mint a new key. Either you've hit the active-"
                        "key cap or the database is temporarily unreachable. "
                        "Revoke an unused key and try again, or contact support."
                    ),
                )
            plaintext, _prefix, webhook_secret = minted
            # Plaintext is rendered ONCE in the response body. It never
            # touches session, cookies, or storage. Same rule for the
            # per-tenant webhook secret (CR-01) — non-None only on the
            # first mint per user.
            return _render_api_keys_page(
                user_ctx.user_id,
                just_minted_plaintext=plaintext,
                just_minted_webhook_secret=webhook_secret,
            )

        @flask_app.route(
            "/account/api-keys/rotate-webhook-secret", methods=["POST"]
        )
        @login_required
        def account_api_keys_rotate_webhook_secret():
            """Rotate the per-tenant webhook signing secret (CR-01).

            Surfaces the new plaintext exactly once. The old secret stops
            being valid for HMAC verification immediately — receivers
            must be updated before this is clicked.
            """
            from shared.api_keys import rotate_webhook_secret

            user_ctx = load_user_context()
            if user_ctx is None:
                return redirect(url_for("login"))
            if not _csrf_ok():
                return _render_api_keys_page(
                    user_ctx.user_id,
                    create_error=(
                        "Rotate request failed CSRF check. Refresh and "
                        "try again."
                    ),
                ), 400
            new_secret = rotate_webhook_secret(user_id=user_ctx.user_id)
            if not new_secret:
                return _render_api_keys_page(
                    user_ctx.user_id,
                    create_error=(
                        "Could not rotate the webhook secret. The database "
                        "is temporarily unreachable; try again in a moment."
                    ),
                )
            return _render_api_keys_page(
                user_ctx.user_id,
                just_minted_webhook_secret=new_secret,
                rotate_notice=(
                    "Webhook secret rotated. The old secret stopped "
                    "verifying as of now. Update your receivers."
                ),
            )

        @flask_app.route(
            "/account/api-keys/<key_id>/revoke", methods=["POST"]
        )
        @login_required
        def account_api_keys_revoke(key_id):
            user_ctx = load_user_context()
            if user_ctx is None:
                return redirect(url_for("login"))
            if not _csrf_ok():
                # FIX HI-03: revoke is destructive; refuse without CSRF.
                return _render_api_keys_page(
                    user_ctx.user_id,
                    create_error=(
                        "Revoke request failed CSRF check. Refresh and "
                        "try again."
                    ),
                ), 400
            revoke_key(key_id=key_id, user_id=user_ctx.user_id)
            return redirect(url_for("account_api_keys"))

        logger.info(
            "Platform API enabled (/api/v1/*, /.well-known/ai-plugin.json, "
            "/account/api-keys)"
        )

    # Single Modal client shared across stub tool routes.
    modal_client = ModalClient()

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------

    @flask_app.route("/login", methods=["GET", "POST"])
    def login():
        """Render the login form (GET) or handle credential submission (POST)."""
        from shared.auth import verify_login  # noqa: PLC0415

        if request.method == "GET":
            next_url = request.args.get("next", "/")
            return render_template(
                "login.html",
                mode="signin",
                error=None,
                email=None,
                next=next_url,
            )

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "/")

        success, error_msg, user_id = verify_login(email, password)
        if success:
            session["user_email"] = email
            if user_id:
                session["user_id"] = user_id
            # Restrict redirect to same-origin paths to prevent open redirect.
            if not next_url.startswith("/"):
                next_url = "/"
            try:
                from shared.events import log_event  # noqa: PLC0415
                log_event(
                    event_type="login",
                    user_id=user_id,
                    session_id=session.get("anon_session_id"),
                    path="/login",
                    ip=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                        or request.remote_addr),
                    user_agent=request.headers.get("User-Agent"),
                )
            except Exception:
                logger.warning("login event log failed", exc_info=True)
            return redirect(next_url)

        return render_template(
            "login.html",
            mode="signin",
            error=error_msg,
            email=email,
            next=next_url,
        )

    @flask_app.route("/signup", methods=["GET", "POST"])
    def signup():
        """Render the sign-up form (GET) or handle new account creation (POST).

        On POST, four guards run before Supabase Auth is touched:
        honeypot, signed-timestamp timing, email-domain classification,
        and (for personal domains) the "what are you working on" note.
        Failures are logged to public.signup_rejections so the daily
        digest can flag false positives.
        """
        from shared.auth import (  # noqa: PLC0415
            SignupContext,
            issue_signup_token,
            register_user,
        )
        from shared.events import log_event, log_signup_rejection  # noqa: PLC0415

        def _log_signup_failed(reason: str, email_value: str) -> None:
            """Fire a signup_failed user_event so every failure mode is funnel-visible.

            Coexists with signup_rejections: that table stays the
            bot-filter audit log; this event is the UX funnel feed.
            """
            domain = email_value.rsplit("@", 1)[1].lower() if "@" in email_value else ""
            log_event(
                event_type="signup_failed",
                session_id=session.get("anon_session_id"),
                path="/signup",
                props={
                    "reason": reason,
                    "email": email_value.strip().lower()[:320] if email_value else None,
                    "email_domain": domain or None,
                },
                ip=client_ip,
                user_agent=user_agent,
            )

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="signup",
                error=None,
                signup_email=None,
                signup_purpose=None,
                signup_terms=False,
                signup_token=issue_signup_token(),
                next="/",
            )

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        purpose = request.form.get("purpose", "").strip()
        honeypot = request.form.get("website", "").strip()
        token = request.form.get("signup_token", "")
        terms_accepted = request.form.get("terms_accepted") == "on"
        client_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                     or request.remote_addr)
        user_agent = request.headers.get("User-Agent")

        # Password-pair check runs before register_user so the user sees
        # this error without re-typing their email + purpose. The
        # honeypot / timing checks still happen below — confirmation
        # mismatch is a UX problem, not a junk-filter problem.
        if password and password2 and password != password2:
            _log_signup_failed("password_mismatch", email)
            return render_template(
                "login.html",
                mode="signup",
                error="Passwords do not match.",
                signup_email=email,
                signup_purpose=purpose,
                signup_terms=terms_accepted,
                signup_token=issue_signup_token(),
                next="/",
            )
        if password and len(password) < 8:
            _log_signup_failed("password_short", email)
            return render_template(
                "login.html",
                mode="signup",
                error="Password must be at least 8 characters.",
                signup_email=email,
                signup_purpose=purpose,
                signup_terms=terms_accepted,
                signup_token=issue_signup_token(),
                next="/",
            )
        if not terms_accepted:
            _log_signup_failed("terms_not_accepted", email)
            return render_template(
                "login.html",
                mode="signup",
                error="You must accept the Terms of Service and Privacy Policy to create an account.",
                signup_email=email,
                signup_purpose=purpose,
                signup_terms=False,
                signup_token=issue_signup_token(),
                next="/",
            )

        # Send the confirmation email's "click here" link back to tools-hub
        # explicitly. Otherwise Supabase falls back to the project Site URL,
        # which on the shared Scout/tools-hub project points at scout.
        public_base = os.environ.get(
            "PUBLIC_BASE_URL", "https://tools.ranomics.com"
        ).rstrip("/")

        ctx = SignupContext(
            email=email,
            password=password,
            purpose=purpose,
            honeypot=honeypot,
            token=token,
            ip=client_ip,
            user_agent=user_agent,
        )
        result = register_user(ctx, email_redirect_to=f"{public_base}/login")

        if not result.success:
            # Honeypot hits never see an error — the bot got the same
            # generic page as a real user re-fetching after a failure.
            # Everyone else sees the per-reason message.
            if result.rejection_reason:
                log_signup_rejection(
                    email=email,
                    reason=result.rejection_reason,
                    ip=client_ip,
                    user_agent=user_agent,
                )
            # Always emit the user-event so the funnel sees every miss,
            # including the silent register_user paths (existing_account,
            # weak_password, auth_error, etc.) that don't land in
            # signup_rejections.
            _log_signup_failed(result.failure_code or "unknown", email)
            return render_template(
                "login.html",
                mode="signup",
                error=result.error_message,
                signup_email=email,
                signup_purpose=purpose,
                signup_terms=terms_accepted,
                signup_token=issue_signup_token(),
                next="/",
            )

        # Wallet signup credit ($5 by default) is granted lazily when the
        # user_wallets row is first created on sign-in
        # (shared.wallet._create_wallet_with_signup_credit). No legacy
        # credits-ledger grant on this path.

        log_event(
            event_type="signup_completed",
            user_id=result.user_id,
            session_id=session.get("anon_session_id"),
            path="/signup",
            props={
                "domain_class": result.classification,
                "signup_quality": result.signup_quality,
            },
            ip=client_ip,
            user_agent=user_agent,
        )

        # D3 funnel fire. The Supabase audit row above is the source of
        # truth; this is the PostHog mirror that drives the funnel
        # dashboard. emit() is a no-op when PUBLIC_POSTHOG_KEY is unset.
        from shared.events import EVENTS, emit  # noqa: PLC0415
        emit(
            EVENTS.SIGNUP_COMPLETE,
            user_id=result.user_id,
            properties={
                "domain_class": result.classification,
                "signup_quality": result.signup_quality,
            },
        )

        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=email,
            next="/",
            success_msg=(
                "Account created with $5 of compute credit. "
                "Sign in to get started."
            ),
        )

    @flask_app.route("/api/track", methods=["POST"])
    def api_track():
        """Append a behavioural event to ``public.user_events``.

        Body: JSON ``{event_type, path?, props?, session_id?}``.
        Returns 204 always (best-effort).
        """
        from shared.events import log_event  # noqa: PLC0415

        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        event_type = str(payload.get("event_type") or "").strip()[:64]
        if not event_type:
            return ("", 204)
        path = payload.get("path")
        props_raw = payload.get("props") or {}
        props = props_raw if isinstance(props_raw, dict) else {}
        session_id = (payload.get("session_id") or "").strip() or None
        if session_id:
            session["anon_session_id"] = session_id[:64]
        elif session.get("anon_session_id"):
            session_id = session["anon_session_id"]

        log_event(
            event_type=event_type,
            user_id=session.get("user_id"),
            session_id=session_id,
            path=path if isinstance(path, str) else None,
            props=props,
            ip=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr),
            user_agent=request.headers.get("User-Agent"),
        )
        return ("", 204)

    @flask_app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        """Handle password reset requests.

        On POST, the same anti-bot gauntlet that gates /signup runs
        before any Supabase recovery email is sent: honeypot, signed
        timing token, email-domain classification, and an existence
        check against auth.users. Every failure path renders the same
        generic success copy a legit user sees, so a bot cannot
        enumerate valid recovery emails by probing here.
        """
        import time as _time  # noqa: PLC0415

        from shared.auth import (  # noqa: PLC0415
            ResetContext,
            issue_reset_token,
            process_reset_request,
            reset_password,
        )
        from shared.credits import get_service_client  # noqa: PLC0415

        # Single success-copy used by every outcome — bots and humans
        # see identical text.
        SUCCESS_COPY = (
            "If an account exists for that email, a reset link has "
            "been sent."
        )

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="reset",
                error=None,
                email=None,
                next="/",
                reset_success=None,
                reset_token=issue_reset_token(),
            )

        email = request.form.get("email", "").strip()
        honeypot = request.form.get("website", "").strip()
        token = request.form.get("reset_token", "")

        ctx = ResetContext(
            email=email,
            reset_token=token,
            honeypot_value=honeypot,
            now_unix=int(_time.time()),
        )
        result = process_reset_request(ctx, get_service_client())

        if result.should_send_email:
            # Send the recovery email's "click here" link to tools-hub's
            # /reset-password route. Otherwise Supabase falls back to
            # the project Site URL, which on the shared Scout/tools-hub
            # project points at scout.
            public_base = os.environ.get(
                "PUBLIC_BASE_URL", "https://tools.ranomics.com"
            ).rstrip("/")
            reset_password(
                email, redirect_to=f"{public_base}/reset-password"
            )

        # Always render the same success copy — gauntlet drops and real
        # sends are indistinguishable to the caller.
        return render_template(
            "login.html",
            mode="reset",
            error=None,
            email=email,
            next="/",
            reset_success=SUCCESS_COPY,
            reset_token=issue_reset_token(),
        )

    @flask_app.route("/reset-password", methods=["GET", "POST"])
    def reset_password_update():
        """Land Supabase recovery clicks and apply the new password.

        The recovery URL hash fragment (#access_token=...&refresh_token=...
        &type=recovery) is read client-side by JS in login.html and
        round-tripped via hidden form fields on POST.
        """
        from shared.auth import update_password  # noqa: PLC0415

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="update_password",
                error=None,
                next="/",
            )

        access_token = request.form.get("access_token", "").strip()
        refresh_token = request.form.get("refresh_token", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not access_token or not refresh_token:
            return render_template(
                "login.html",
                mode="update_password",
                error=(
                    "Reset link is invalid or has expired. "
                    "Request a new password reset email."
                ),
                next="/",
            )

        # Validation errors re-render with the tokens preserved so the user
        # can fix and resubmit without going back to their email.
        if not password:
            return render_template(
                "login.html",
                mode="update_password",
                error="Password is required.",
                access_token=access_token,
                refresh_token=refresh_token,
                next="/",
            )

        if len(password) < 8:
            return render_template(
                "login.html",
                mode="update_password",
                error="Password must be at least 8 characters.",
                access_token=access_token,
                refresh_token=refresh_token,
                next="/",
            )

        if password != password2:
            return render_template(
                "login.html",
                mode="update_password",
                error="Passwords do not match.",
                access_token=access_token,
                refresh_token=refresh_token,
                next="/",
            )

        success, error_msg = update_password(
            access_token, refresh_token, password
        )

        if success:
            return render_template(
                "login.html",
                mode="signin",
                error=None,
                email=None,
                next="/",
                success_msg=(
                    "Password updated. Sign in with your new password."
                ),
            )

        # Supabase rejected the update (e.g. weak password). The recovery
        # session was consumed by set_session, so a retry needs a fresh
        # email link — don't preserve tokens here.
        return render_template(
            "login.html",
            mode="update_password",
            error=error_msg,
            next="/",
        )

    @flask_app.route("/logout", methods=["POST"])
    def logout():
        """Clear the session and redirect to the login page."""
        session.clear()
        return redirect(url_for("login"))

    @flask_app.route("/health", methods=["GET"])
    def health():
        """Unauthenticated health check for Railway port scanner."""
        return jsonify({"status": "ok"}), 200

    @flask_app.route("/readyz", methods=["GET"])
    def readyz():
        """Deep readiness probe (catches incident 2026-06-10 Mode B).

        /health is static and DB-free, so it stays green even when the
        Supabase client fails to build and the entire authenticated surface
        (login, wallet, credits, Platform API) is down. /readyz does ONE
        cheap, bounded Supabase read so an external uptime monitor catches
        that mode directly. Bounded by SUPABASE_CLIENT_TIMEOUT_S (30s) and
        >1 gunicorn worker, so the probe itself can never wedge the site.

        Uses the service-role client because user_events is service-role-only
        under RLS. A None client (construction failed) or any read error
        returns 503 so the monitor's keyword check ("ready") and status code
        both fail. Unauthenticated by design: an external prober cannot log
        in, and it is placed above the login_required routes for that reason.
        """
        from shared.credits import get_service_client  # noqa: PLC0415

        try:
            client = get_service_client()
            if client is None:
                return (
                    jsonify({"status": "degraded", "reason": "no_client"}),
                    503,
                )
            client.table("user_events").select("id").limit(1).execute()
            return jsonify({"status": "ready"}), 200
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            logger.warning("readyz degraded: %s", exc)
            return jsonify({"status": "degraded", "reason": "db_error"}), 503

    # ------------------------------------------------------------------
    # Protected routes
    # ------------------------------------------------------------------

    from shared.auth import login_required  # noqa: PLC0415

    @flask_app.route("/", methods=["GET"])
    def index():
        """Landing page.

        For anonymous visitors: marketing hero + tool catalog tiles
        with sign-in CTAs, so first-time visitors can see what runs on
        the platform without signing up.

        For authenticated users: a "Recent runs" dashboard strip on top
        (top 3 jobs with clone shortcuts), then the tool catalog tiles
        for new runs.
        """
        catalog = _build_tools_catalog()

        # Match the grouped layout used by /tools — same categories,
        # same order, just rendered as wide tile sections instead of a
        # comparison matrix. Ordering walks the iteration loop:
        # scope → design (4 scaffold-class buckets) → predict → QC.
        category_order = (
            "Scope the target",
            "De novo minibinders",
            "Antibodies (VHH)",
            "Dual capabilities (minibinder + antibody scaffolds)",
            "Sequence on a backbone",
            "Structure prediction",
            "Check developability",
            "Other",
        )
        grouped: list[tuple[str, list[dict]]] = []
        for category in category_order:
            members = [t for t in catalog if t.get("category") == category]
            if members:
                grouped.append((category, members))

        recent_jobs: list = []
        if session.get("user_email"):
            ctx = load_user_context()
            if ctx is not None:
                try:
                    recent_jobs = list(
                        list_jobs_for_user(ctx.user_id, limit=3)
                    )
                except Exception:  # noqa: BLE001 — never block the homepage
                    logger.exception("Failed to load recent jobs for homepage")
                    recent_jobs = []

        return render_template(
            "index.html",
            tools=catalog,
            grouped=grouped,
            recent_jobs=recent_jobs,
            authenticated=bool(session.get("user_email")),
        )

    @flask_app.route("/pricing", methods=["GET"])
    def pricing():
        """Public pricing page — logged-out visitors can reach it."""
        return render_template("pricing.html")

    @flask_app.route("/terms", methods=["GET"])
    def terms():
        return render_template("legal/terms.html")

    @flask_app.route("/privacy", methods=["GET"])
    def privacy():
        return render_template("legal/privacy.html")

    @flask_app.route("/robots.txt", methods=["GET"])
    def robots_txt():
        """Serve the static robots.txt from /static/ at the URL root.

        Search engines fetch /robots.txt, not /static/robots.txt, so we
        need an explicit route that maps one to the other.
        """
        return send_from_directory(
            flask_app.static_folder, "robots.txt", mimetype="text/plain"
        )

    @flask_app.route("/talk/<campaign>", methods=["GET"])
    def talk_redirect(campaign: str):
        """Conference short-link redirector (D5 of the growth plan).

        Looks up ``campaign`` in ``CONFERENCE_LINKS`` and 302-redirects
        to the configured destination with UTM params appended so the
        click attributes back to the originating conference. Unknown
        slugs fall back to the homepage but still carry a UTM tag so we
        capture the click as ``conference-unknown``.
        """
        from urllib.parse import urlencode, urlsplit, urlunsplit  # noqa: PLC0415
        from shared.conference_links import CONFERENCE_LINKS  # noqa: PLC0415

        if campaign in CONFERENCE_LINKS and campaign != "default":
            destination = CONFERENCE_LINKS[campaign]
            utm_campaign = campaign
            utm_source = f"conference-{campaign}"
        else:
            destination = CONFERENCE_LINKS.get(
                "default", "https://tools.ranomics.com/"
            )
            utm_campaign = "unknown"
            utm_source = "conference-unknown"

        parts = urlsplit(destination)
        existing = parts.query
        utm = urlencode({
            "utm_source": utm_source,
            "utm_medium": "outbound",
            "utm_campaign": utm_campaign,
        })
        new_query = f"{existing}&{utm}" if existing else utm
        target = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )
        return redirect(target, code=302)

    # Register the IndexNow verification file route only when the env
    # var is set. IndexNow requires a key.txt file at the site root
    # whose body is the same key sent in the submission payload.
    _indexnow_key = os.environ.get("INDEXNOW_KEY", "").strip()
    if _indexnow_key:
        @flask_app.route(f"/{_indexnow_key}.txt", methods=["GET"])
        def indexnow_key_file():
            """Serve the IndexNow ownership-verification key as plain text."""
            return Response(_indexnow_key, mimetype="text/plain")

    @flask_app.route("/sitemap.xml", methods=["GET"])
    def sitemap_xml():
        """Emit a sitemap listing every public, crawlable URL.

        Sources of truth:
          * Static URLs are enumerated below in ``_static_paths``.
          * Per-tool help pages are pulled from ``tool_base.all_adapters()``
            so newly enabled tools appear automatically.
        Tool run forms (``/tools/<slug>``) are NOT listed because they
        currently require login and serve a redirect to crawlers.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        base = request.url_root.rstrip("/")
        today = datetime.now(timezone.utc).date().isoformat()

        _static_paths = [
            "/",
            "/tools",
            "/pricing",
            "/help",
            "/help/getting-started",
            "/help/faq",
            "/help/troubleshooting",
            "/scout",
            "/showcase",
            "/terms",
            "/privacy",
        ]

        urls: list[tuple[str, str, str]] = []
        # (loc, changefreq, priority)
        for path in _static_paths:
            priority = "1.0" if path == "/" else "0.7"
            urls.append((f"{base}{path}", "weekly", priority))

        # Per-tool help guides + public preview pages (B2). The preview
        # page at /tools/<slug> serves logged-out crawlers a real HTML
        # response; the run form (same URL, logged-in) is not crawled.
        try:
            for adapter in tool_base.all_adapters():
                if not tool_enabled(adapter.slug):
                    continue
                urls.append(
                    (f"{base}/help/tools/{adapter.slug}", "monthly", "0.6")
                )
                urls.append(
                    (f"{base}/tools/{adapter.slug}", "weekly", "0.7")
                )
        except Exception:
            logger.warning("sitemap: failed to enumerate tool adapters", exc_info=True)

        # Render manually — Flask's jsonify + render_template are overkill
        # for a fixed XML shape and the templating cost isn't worth it.
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for loc, freq, priority in urls:
            lines.append("  <url>")
            lines.append(f"    <loc>{loc}</loc>")
            lines.append(f"    <lastmod>{today}</lastmod>")
            lines.append(f"    <changefreq>{freq}</changefreq>")
            lines.append(f"    <priority>{priority}</priority>")
            lines.append("  </url>")
        lines.append("</urlset>")

        return Response("\n".join(lines), mimetype="application/xml")

    @flask_app.route("/billing/checkout", methods=["GET"])
    @login_required
    def billing_checkout():
        """Create a Stripe Checkout Session for a Workspace SKU.

        Accepts ``?sku=workspace_standard|workspace_xl`` plus a
        ``target_pdb_id`` query param (set by /workspaces/new POST
        after the user has uploaded their target PDB to storage).
        """
        from billing.checkout import create_checkout_session  # noqa: PLC0415
        from billing.tiers import SKU_NAMES  # noqa: PLC0415

        sku = request.args.get("sku", "").strip()
        target_pdb_id = request.args.get("target_pdb_id", "").strip()
        target_label = request.args.get("target_label", "").strip() or None
        if sku not in SKU_NAMES:
            return redirect(url_for("pricing"))
        if not target_pdb_id:
            # Bounce back to the new-Workspace form so the user can
            # upload a target before paying.
            return redirect(f"/workspaces/new?sku={sku}")

        base = request.url_root.rstrip("/")
        success_url = base + "/workspaces?success=1"
        cancel_url = base + url_for("pricing") + "?cancelled=1"

        url, error = create_checkout_session(
            sku,
            target_pdb_id=target_pdb_id,
            target_label=target_label,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if error or not url:
            logger.warning("Checkout creation failed: %s", error)
            return redirect(url_for("pricing") + "?checkout_error=1")
        return redirect(url, code=303)

    # ------------------------------------------------------------------
    # Workspace routes (new — replaces the legacy subscription tier
    # gating). See shared/workspaces.py for the lifecycle module.
    # ------------------------------------------------------------------

    @flask_app.route("/workspaces", methods=["GET"])
    @login_required
    def workspaces_list():
        """List active + past Workspaces for the signed-in user."""
        from shared.workspaces import (  # noqa: PLC0415
            list_active_workspaces,
            list_workspace_history,
        )

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        active = list_active_workspaces(ctx.user_id)
        history = list_workspace_history(ctx.user_id, limit=20)
        # Hide currently-active rows from the history view (avoid dupes).
        active_ids = {ws.id for ws in active}
        expired_or_refunded = [
            ws for ws in history if ws.id not in active_ids
        ]
        return render_template(
            "workspaces/list.html",
            active_workspaces=active,
            expired_workspaces=expired_or_refunded,
        )

    @flask_app.route("/workspaces/new", methods=["GET"])
    @login_required
    def workspaces_new():
        """Render the new-Workspace form (upload PDB + confirm SKU)."""
        from billing.tiers import SKU_NAMES  # noqa: PLC0415

        sku = request.args.get("sku", "workspace_standard").strip()
        if sku not in SKU_NAMES:
            sku = "workspace_standard"
        return render_template("workspaces/new.html", sku=sku)

    @flask_app.route("/workspaces/new", methods=["POST"])
    @login_required
    def workspaces_new_submit():
        """Handle PDB upload, stage it in storage, redirect to Stripe.

        On success: redirects to /billing/checkout with the resolved
        target_pdb_id baked into the URL so the Stripe checkout session
        carries it through to the activation webhook.
        """
        from billing.tiers import SKU_NAMES  # noqa: PLC0415
        from shared.storage import upload_input  # noqa: PLC0415
        from shared.pdb_inspect import inspect_pdb  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        sku = (request.form.get("sku") or "workspace_standard").strip()
        if sku not in SKU_NAMES:
            sku = "workspace_standard"
        target_label = (request.form.get("target_label") or "").strip()

        uploaded = request.files.get("target_pdb_file")
        if not uploaded or not uploaded.filename:
            return redirect(f"/workspaces/new?sku={sku}&error=missing_pdb")

        data = uploaded.read()
        if not data:
            return redirect(f"/workspaces/new?sku={sku}&error=empty_pdb")

        # Stash under workspace-targets/{user_id}/{timestamp}-{filename}.
        # We use a synthetic "job_id" slot to namespace the upload; the
        # actual Workspace id doesn't exist yet (created by the Stripe
        # webhook after payment).
        import secrets  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        upload_token = (
            datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            + "-" + secrets.token_hex(4)
        )
        try:
            object_path = upload_input(
                user_id=ctx.user_id,
                job_id=f"workspace-target-{upload_token}",
                filename=uploaded.filename,
                data=data,
                content_type="chemical/x-pdb",
            )
        except Exception:
            logger.exception(
                "Failed to stage target PDB for user=%s sku=%s",
                ctx.user_id, sku,
            )
            return redirect(f"/workspaces/new?sku={sku}&error=upload_failed")

        # Optional: sanity-check the PDB structure (chains, residues).
        # Failure here is non-fatal — we'd rather let a quirky PDB
        # through than block a paying customer.
        try:
            inspect_pdb(data)
        except Exception:
            logger.info(
                "PDB inspection failed for upload by %s; continuing.",
                ctx.user_id, exc_info=True,
            )

        # The "target_pdb_id" we pass through to Stripe metadata is the
        # storage object path itself — it's globally unique, durable, and
        # the Modal pipeline can fetch the PDB via presigned URL when
        # actually running a tool inside this Workspace.
        target_pdb_id = object_path
        from urllib.parse import quote_plus  # noqa: PLC0415
        return redirect(
            "/billing/checkout"
            f"?sku={sku}"
            f"&target_pdb_id={quote_plus(target_pdb_id)}"
            f"&target_label={quote_plus(target_label)}"
        )

    @flask_app.route("/workspaces/<workspace_id>", methods=["GET"])
    @login_required
    def workspace_detail(workspace_id: str):
        """Show a single Workspace dashboard with cap meter + tool buttons."""
        from shared.workspaces import get_workspace  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        ws = get_workspace(workspace_id)
        if ws is None or ws.user_id != ctx.user_id:
            return redirect(url_for("workspaces_list"))

        # Pull recent jobs scoped to this Workspace's target. (The
        # tool_jobs table doesn't yet have a workspace_id column — we
        # match on target via job metadata when the route handlers add
        # it; for now show all the user's recent jobs as a fallback.)
        workspace_jobs: list = []
        try:
            from shared.jobs import list_jobs_for_user  # noqa: PLC0415
            workspace_jobs = list_jobs_for_user(ctx.user_id, limit=10)
        except Exception:
            workspace_jobs = []

        return render_template(
            "workspaces/detail.html",
            workspace=ws,
            workspace_jobs=workspace_jobs,
        )

    @flask_app.route("/billing/portal", methods=["GET"])
    @login_required
    def billing_portal():
        """Redirect the user to their Stripe Billing Portal session."""
        from billing.checkout import create_portal_session  # noqa: PLC0415

        base = request.url_root.rstrip("/")
        return_url = base + url_for("account")

        url, error = create_portal_session(return_url=return_url)
        if error or not url:
            logger.warning("Portal creation failed: %s", error)
            return redirect(url_for("account") + "?portal_error=1")
        return redirect(url, code=303)

    @flask_app.route("/account", methods=["GET"])
    @login_required
    def account():
        """Account dashboard: active Workspaces + Workspace history."""
        from shared.workspaces import (  # noqa: PLC0415
            list_active_workspaces,
            list_workspace_history,
        )

        ctx = load_user_context()
        active: list = []
        history: list = []
        if ctx is not None:
            active = list_active_workspaces(ctx.user_id)
            history = list_workspace_history(ctx.user_id, limit=20)
        return render_template(
            "account.html",
            user_email=session.get("user_email", ""),
            active_workspaces=active,
            workspace_history=history,
        )

    # ------------------------------------------------------------------
    # Wallet endpoints
    # ------------------------------------------------------------------

    @flask_app.route("/api/wallet/estimate", methods=["GET"])
    def api_wallet_estimate():
        """Return the wallet estimate, hard cap, and current balance.

        Used by every tool form for the inline Moment 1 display (live
        update of "Estimated cost / Balance / Balance after"). The
        endpoint is read only and idempotent; it never places a hold or
        modifies the wallet.

        Query parameters:

        * ``tool`` (or ``tool_slug``): the tool slug.
        * ``params``: optional JSON object of param values. Falls back
          to flat query params (``num_designs=100``) when omitted.

        Response shape::

            {"estimate_usd": "0.0500",
             "hard_cap_usd": "150.00",
             "balance_usd": "5.0000",
             "ok": true}

        All money values are returned as JSON strings to preserve
        Decimal precision through JSON's float coercion.
        """
        tool_slug = (
            request.args.get("tool")
            or request.args.get("tool_slug")
            or ""
        ).strip()
        if not tool_slug:
            return jsonify({"error": "missing_tool_slug"}), 400

        user_id = session.get("user_id")
        # Resolve params: prefer a JSON ``params`` blob, fall back to
        # flat query args minus the meta keys.
        params: dict[str, object] = {}
        raw_params = request.args.get("params")
        if raw_params:
            try:
                parsed = json.loads(raw_params)
                if isinstance(parsed, dict):
                    params = parsed
            except (ValueError, TypeError):
                pass
        if not params:
            for key, value in request.args.items():
                if key in {"tool", "tool_slug", "params"}:
                    continue
                if not value:
                    continue
                # Coerce numerics so the estimator's scaling math works.
                try:
                    params[key] = int(value)
                    continue
                except ValueError:
                    pass
                try:
                    params[key] = float(value)
                    continue
                except ValueError:
                    pass
                params[key] = value

        try:
            estimate = estimated_cost_for_tool(user_id, tool_slug, params)
        except Exception:  # noqa: BLE001
            logger.warning(
                "api_wallet_estimate: estimate failed for tool=%s",
                tool_slug, exc_info=True,
            )
            return jsonify({"error": "estimate_failed"}), 500

        try:
            hard_cap = compute_hard_cap(tool_slug, params)
        except Exception:  # noqa: BLE001
            logger.warning(
                "api_wallet_estimate: hard cap failed for tool=%s",
                tool_slug, exc_info=True,
            )
            hard_cap = Decimal("0")

        balance = Decimal("0")
        wallet = None
        if user_id:
            wallet = get_or_create_wallet(user_id)
            balance = Decimal(str((wallet or {}).get("balance_usd") or 0))

        # Derived contract values consumed by templates/wallet/_partials.html.
        # The Moment 1 estimate panel and the inline Moment 2 gate both
        # read these flag fields to flip visibility.
        deficit = estimate - balance
        if deficit < 0:
            deficit = Decimal("0")
        rounded_topup = _round_up_topup_amount(deficit)

        exceeds_self_serve = estimate > SELF_SERVE_CEILING_USD
        exceeds_hard_cap = estimate > hard_cap
        # Soft warning band: estimate has eaten 80% of the current
        # balance without going under, so the user is close to a top up
        # gate on the next click. Suppressed when a harder block trips.
        soft_block = False
        if balance > 0 and not exceeds_hard_cap and not exceeds_self_serve:
            soft_block = estimate >= (balance * Decimal("0.8")) and (
                estimate < balance
            )
        # Hard block: balance cannot cover the estimate at all. The
        # gate inside the partial owns the visual; this flag is what
        # the JS reads.
        hard_block = balance < estimate
        wallet_frozen = bool((wallet or {}).get("wallet_frozen"))

        return jsonify({
            "ok": True,
            "tool_slug": tool_slug,
            "estimate_usd": str(estimate),
            "hard_cap_usd": str(hard_cap),
            "balance_usd": str(balance),
            "balance_after_usd": str(balance - estimate),
            "self_serve_ceiling_usd": str(SELF_SERVE_CEILING_USD),
            "exceeds_hard_cap": exceeds_hard_cap,
            "exceeds_self_serve_ceiling": exceeds_self_serve,
            # Wave 3 contract keys consumed by wallet/_partials.html JS.
            # Names align with the partial's documented schema.
            "deficit_usd": str(deficit),
            "rounded_topup_usd": str(rounded_topup),
            "scaled_hard_cap_usd": str(hard_cap),
            "soft_block": soft_block,
            "hard_block": hard_block,
            "self_serve_block": exceeds_self_serve,
            "confirm_band": exceeds_hard_cap,
            "wallet_frozen": wallet_frozen,
        })

    @flask_app.route("/api/wallet/balance", methods=["GET"])
    def api_wallet_balance():
        """Return the current wallet balance for the logged-in user.

        Used by the nav-chip JS to refresh after a top-up redirect or
        window-focus without forcing a full page reload. Read-only and
        idempotent. Returns 401 if no session.

        Response shape::

            {"balance_usd": "5.0000", "wallet_frozen": false}
        """
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "unauthorized"}), 401
        wallet = get_or_create_wallet(user_id) or {}
        return jsonify({
            "balance_usd": str(Decimal(str(wallet.get("balance_usd") or 0))),
            "wallet_frozen": bool(wallet.get("wallet_frozen")),
        })

    @flask_app.route("/account/topup-complete", methods=["GET"])
    @login_required
    def topup_complete():
        """Stripe Checkout success_url landing page.

        Validates the ``session_id`` query parameter against Stripe and
        renders a confirmation. The webhook handler in
        ``webhooks/stripe.py`` actually credits the wallet on the
        ``checkout.session.completed`` event; this route is the user
        visible confirmation while that webhook flies.

        When the gate flow stashed an original tool form, the user can
        click 'Return to <tool>' from the confirmation to land back on
        the tool form with values preserved.
        """
        from billing.checkout import retrieve_topup_session  # noqa: PLC0415

        ctx = load_user_context()
        session_id = (request.args.get("session_id") or "").strip()
        gate_payload = session.pop("wallet_gate_form", None) or {}
        return_tool = (gate_payload or {}).get("tool")

        if not session_id:
            return render_template(
                "wallet/topup.html",
                topup_error=(
                    "No Stripe session was provided. If you just paid, "
                    "your wallet will update shortly. Refresh the "
                    "Account page to see the balance."
                ),
                wallet=get_or_create_wallet(ctx.user_id) if ctx else None,
                return_tool=return_tool,
            )

        stripe_session, err = retrieve_topup_session(session_id)
        if err or not stripe_session:
            logger.warning(
                "topup_complete: could not retrieve session=%s err=%s",
                session_id, err,
            )
            return render_template(
                "wallet/topup.html",
                topup_error=(
                    "Could not validate the Stripe session. The webhook "
                    "still credits the wallet when payment clears."
                ),
                wallet=get_or_create_wallet(ctx.user_id) if ctx else None,
                return_tool=return_tool,
            )

        # Owner check: the session metadata.user_id must match the
        # signed in user so a leaked session_id link does not expose
        # another user's amount or status.
        metadata = stripe_session.get("metadata") or {}
        if ctx and metadata.get("user_id") and metadata["user_id"] != ctx.user_id:
            logger.warning(
                "topup_complete: session=%s user mismatch (session=%s viewer=%s)",
                session_id, metadata.get("user_id"), ctx.user_id,
            )
            return render_template(
                "wallet/topup.html",
                topup_error=(
                    "This Checkout session belongs to a different account."
                ),
                wallet=get_or_create_wallet(ctx.user_id),
                return_tool=return_tool,
            )

        wallet = get_or_create_wallet(ctx.user_id) if ctx else None
        # Pass ?topup=success on the return URL so wallet-nav.js polls the
        # balance while the Stripe webhook lands — the user can otherwise
        # see a stale chip for a few seconds after redirect.
        return render_template(
            "wallet/topup.html",
            topup_success=True,
            stripe_session=stripe_session,
            wallet=wallet,
            return_tool=return_tool,
            return_tool_url=(
                url_for("tool_form", tool=return_tool) + "?topup=success"
                if return_tool else None
            ),
        )

    # ------------------------------------------------------------------
    # Wallet UI routes
    # ------------------------------------------------------------------
    #
    # Five routes back the wallet self serve surface:
    #
    #   GET  /account/wallet                -> overview dashboard
    #   GET  /account/wallet/topup          -> top up form (standalone)
    #   POST /account/wallet/checkout       -> create Stripe Checkout, 302
    #   GET  /account/wallet/transactions   -> paginated ledger
    #   POST /account/wallet/auto-reload    -> save auto reload settings
    #
    # The gate flow lives on the same /account/wallet/topup template via
    # the requires_wallet decorator (see _render_topup_gate above); the
    # standalone topup form below renders the same template with no
    # deficit_usd context.

    @flask_app.route("/account/wallet", methods=["GET"])
    @login_required
    def wallet_overview():
        """Render the wallet overview dashboard.

        Shows current balance, today plus 30 day spend, auto reload
        status, the 10 most recent ledger rows, and a Binder Pilot
        callout for users averaging >$1000 / 30d.
        """
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        wallet = get_or_create_wallet(ctx.user_id) or {}

        # Decorate the wallet with derived fields the template reads.
        # _spent_today_usd is a private helper but the values it returns
        # are stable; we shape the dict here rather than pushing the
        # query into the template.
        from shared.wallet import _spent_today_usd  # noqa: PLC0415

        spent_today = _spent_today_usd(ctx.user_id)
        try:
            wallet["spent_today_usd"] = float(spent_today)
        except Exception:  # pragma: no cover (defensive)
            wallet["spent_today_usd"] = 0.0

        # 30-day spend: net of holds, releases, and charges over a
        # rolling 30-day window. Same canonical formula as the daily
        # figure (shared.wallet._net_spend_usd), just a wider cutoff.
        from datetime import datetime, timezone, timedelta  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415
        from shared.wallet import _net_spend_usd  # noqa: PLC0415

        cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
        spent_30d = _net_spend_usd(ctx.user_id, cutoff_30d)

        client = get_service_client()
        recent_transactions: list = []
        if client is not None:
            try:
                tx_response = (
                    client.table("wallet_transactions")
                    .select("*")
                    .eq("user_id", ctx.user_id)
                    .order("created_at", desc=True)
                    .limit(10)
                    .execute()
                )
                recent_transactions = list(
                    getattr(tx_response, "data", None) or []
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "wallet_overview: recent ledger lookup failed for %s",
                    ctx.user_id, exc_info=True,
                )

        try:
            wallet["spent_30d_usd"] = float(spent_30d)
        except Exception:  # pragma: no cover
            wallet["spent_30d_usd"] = 0.0

        return render_template(
            "wallet/overview.html",
            wallet=wallet,
            recent_transactions=recent_transactions,
            user_email=session.get("user_email", ""),
        )

    @flask_app.route("/account/wallet/topup", methods=["GET"])
    @login_required
    def wallet_topup():
        """Render the standalone wallet top up form.

        The gate flow renders the same template with a ``deficit_usd``
        and ``next_url`` set; this route renders it bare so the user can
        top up manually without coming from a tool gate. ``topup_error``
        is read from the query string so the POST handler can redirect
        here with an inline error.
        """
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        wallet = get_or_create_wallet(ctx.user_id) or {}
        if wallet.get("wallet_frozen"):
            return redirect(url_for("wallet_overview") + "?wallet_frozen=1")
        topup_error = (request.args.get("topup_error") or "").strip() or None
        return render_template(
            "wallet/topup.html",
            wallet=wallet,
            min_topup_usd=MIN_TOPUP_USD,
            next_url=None,
            topup_action_url="/account/wallet/checkout",
            topup_error=topup_error,
        )

    @flask_app.route("/account/wallet/checkout", methods=["POST"])
    @login_required
    def wallet_checkout():
        """Create a Stripe Checkout Session for a wallet top up.

        Reads ``amount_usd`` from the form, hands off to
        :func:`billing.checkout.create_topup_session`, and redirects to
        the returned Stripe URL. Any error redirects back to
        ``/account/wallet/topup?topup_error=<msg>`` so the user lands on
        a form they can retry.
        """
        from billing.checkout import create_topup_session  # noqa: PLC0415
        from urllib.parse import quote  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        wallet = get_or_create_wallet(ctx.user_id) or {}
        if wallet.get("wallet_frozen"):
            return redirect(url_for("wallet_overview") + "?wallet_frozen=1")

        amount_raw = (request.form.get("amount_usd") or "").strip()
        if not amount_raw:
            return redirect(
                "/account/wallet/topup?topup_error="
                + quote("Pick an amount to top up.")
            )

        try:
            amount = Decimal(amount_raw)
        except (InvalidOperation, ValueError):
            return redirect(
                "/account/wallet/topup?topup_error="
                + quote("Top up amount must be a number.")
            )

        # If the gate flow passed a return path through the form, preserve
        # it on the session so /account/topup-complete can route back to
        # the original tool form. The decorator already stashes
        # wallet_gate_form, so we only set return_url if the form sent one
        # and the session has not already captured it.
        next_url = (request.form.get("next") or "").strip()
        if next_url and not session.get("wallet_gate_form"):
            session["wallet_gate_form"] = {"return_url": next_url}

        save_pm_raw = (
            request.form.get("save_payment_method") or ""
        ).strip().lower()
        save_payment_method = save_pm_raw in {"on", "true", "1", "yes"}

        result, err = create_topup_session(
            ctx.user_id,
            ctx.email,
            amount,
            save_payment_method=save_payment_method,
        )
        if err or not result:
            return redirect(
                "/account/wallet/topup?topup_error=" + quote(err or "Checkout failed.")
            )

        return redirect(result.get("url"))

    @flask_app.route("/account/wallet/transactions", methods=["GET"])
    @login_required
    def wallet_transactions():
        """Render the paginated ledger view.

        ``page`` query param drives offset; 25 rows per page. ``kind``
        query param optionally filters by ledger kind (signup_credit,
        topup, charge, etc.).
        """
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        wallet = get_or_create_wallet(ctx.user_id) or {}

        try:
            page = int((request.args.get("page") or "1").strip())
        except ValueError:
            page = 1
        if page < 1:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        filter_kind = (request.args.get("kind") or "").strip() or None

        transactions: list = []
        total_count = None
        has_next = False
        from shared.credits import get_service_client  # noqa: PLC0415

        client = get_service_client()
        if client is not None:
            try:
                query = (
                    client.table("wallet_transactions")
                    .select("*", count="exact")
                    .eq("user_id", ctx.user_id)
                )
                if filter_kind:
                    query = query.eq("kind", filter_kind)
                # Pull one extra row so we can tell whether a next page
                # exists without a second count query.
                response = (
                    query.order("created_at", desc=True)
                    .range(offset, offset + page_size)
                    .execute()
                )
                rows = list(getattr(response, "data", None) or [])
                if len(rows) > page_size:
                    has_next = True
                    rows = rows[:page_size]
                transactions = rows
                total_count = getattr(response, "count", None)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "wallet_transactions: ledger lookup failed for %s",
                    ctx.user_id, exc_info=True,
                )

        return render_template(
            "wallet/transactions.html",
            wallet=wallet,
            transactions=transactions,
            filter_kind=filter_kind,
            page=page,
            page_size=page_size,
            has_next=has_next,
            has_prev=page > 1,
            total_count=total_count,
        )

    @flask_app.route("/account/wallet/auto-reload", methods=["POST"])
    @login_required
    def wallet_auto_reload():
        """Persist auto reload settings on the ``user_wallets`` row.

        Reads ``auto_reload_enabled`` (on / off), ``threshold_usd``,
        ``amount_usd``, and ``monthly_cap_usd`` from the form. Coerces
        numeric fields and clamps them to safe ranges so a runaway form
        post cannot set a 1 cent threshold or a million dollar cap.
        """
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        # Form fields land under the input names defined in topup.html
        # (auto_reload_enabled / _threshold_usd / _amount_usd /
        # _monthly_cap_usd). Accept both the bare and the suffixed names
        # so a future template rename does not silently break the route.
        enabled_raw = (
            request.form.get("auto_reload_enabled")
            or request.form.get("enabled")
            or ""
        ).strip().lower()
        enabled = enabled_raw in {"on", "true", "1", "yes"}

        def _coerce(name_a: str, name_b: str, default: Decimal) -> Decimal:
            raw = (
                request.form.get(name_a)
                or request.form.get(name_b)
                or ""
            ).strip()
            if not raw:
                return default
            try:
                return Decimal(raw)
            except (InvalidOperation, ValueError):
                return default

        threshold = _coerce(
            "auto_reload_threshold_usd", "threshold_usd", Decimal("10")
        )
        amount = _coerce(
            "auto_reload_amount_usd", "amount_usd", Decimal("50")
        )
        monthly_cap = _coerce(
            "auto_reload_monthly_cap_usd", "monthly_cap_usd", Decimal("1000")
        )

        # Clamp to plan documented safe bounds. Threshold must be at
        # least $5 (below that auto reload runs constantly); amount must
        # be at least the minimum top up; monthly cap must be at least
        # $100 so a typo cannot disable the safety net entirely.
        if threshold < Decimal("5"):
            threshold = Decimal("5")
        if amount < MIN_TOPUP_USD:
            amount = MIN_TOPUP_USD
        if monthly_cap < Decimal("100"):
            monthly_cap = Decimal("100")

        from shared.credits import get_service_client  # noqa: PLC0415

        client = get_service_client()
        if client is not None:
            try:
                client.table("user_wallets").update(
                    {
                        "auto_reload_enabled": enabled,
                        "auto_reload_threshold_usd": float(threshold),
                        "auto_reload_amount_usd": float(amount),
                        "auto_reload_monthly_cap_usd": float(monthly_cap),
                    }
                ).eq("user_id", ctx.user_id).execute()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "wallet_auto_reload: update failed for %s",
                    ctx.user_id, exc_info=True,
                )

        return redirect("/account/wallet/topup#auto-reload")

    @flask_app.route("/developability", methods=["GET"])
    @login_required
    def developability():
        """Render the Binder Developability Scout input form."""
        return render_template(
            "developability_form.html",
            error=None,
            sequence="",
            chain_type="VH",
        )

    @flask_app.route("/developability/score", methods=["POST"])
    @login_required
    @idempotent()
    def developability_score():
        """Validate input and render the developability results page."""
        from tools.developability import score_developability  # noqa: PLC0415

        raw_sequence = request.form.get("sequence", "")
        chain_type = request.form.get("chain_type", "VH").strip() or "VH"

        # Strip FASTA headers (lines starting with '>') and whitespace.
        lines = [
            line.strip()
            for line in raw_sequence.splitlines()
            if line and not line.lstrip().startswith(">")
        ]
        cleaned_sequence = "".join(lines).replace(" ", "").upper()

        # Allowed chain types for the UI select; scoring accepts broader set.
        allowed_chains = {"VH", "VL", "VK", "SCFV", "VHH", "OTHER"}
        if chain_type.upper() not in allowed_chains:
            chain_type = "VH"
        chain_type = chain_type.upper()

        # Sequence validation.
        valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
        error = None
        if not cleaned_sequence:
            error = "Paste a sequence before submitting."
        elif not (10 <= len(cleaned_sequence) <= 2000):
            error = (
                f"Sequence length must be between 10 and 2000 residues "
                f"(got {len(cleaned_sequence)})."
            )
        else:
            bad = sorted(set(cleaned_sequence) - valid_aa)
            if bad:
                error = (
                    "Sequence contains non-canonical residues: "
                    + ", ".join(bad)
                    + ". Only the 20 standard amino acids are accepted."
                )

        if error:
            return render_template(
                "developability_form.html",
                error=error,
                sequence=raw_sequence,
                chain_type=chain_type,
            )

        try:
            result = score_developability(
                cleaned_sequence,
                chain_type=chain_type,
            )
        except ValueError as exc:
            return render_template(
                "developability_form.html",
                error=str(exc),
                sequence=raw_sequence,
                chain_type=chain_type,
            )

        return render_template(
            "developability_results.html",
            result=result,
        )

    @flask_app.route("/library-planner", methods=["GET"])
    @login_required
    def library_planner():
        """Render the Yeast Display Library Planner input form."""
        return render_template(
            "library_planner_form.html",
            error=None,
            form_values=None,
        )

    @flask_app.route("/library-planner/plan", methods=["POST"])
    @login_required
    @idempotent()
    def library_planner_plan():
        """Validate inputs and render the library planner results page."""
        from tools.library_planner import plan_library  # noqa: PLC0415

        raw = {
            "scaffold": request.form.get("scaffold", "").strip(),
            "positions": request.form.get("positions", "").strip(),
            "scheme": request.form.get("scheme", "").strip(),
            "kd_nm": request.form.get("kd_nm", "").strip(),
            "starting_material": request.form.get(
                "starting_material", ""
            ).strip(),
            "coverage_pct": request.form.get("coverage_pct", "90").strip(),
        }

        error = None
        try:
            positions = int(raw["positions"])
        except ValueError:
            positions = None
            error = "Diversified positions must be a whole number."
        try:
            kd_nm = float(raw["kd_nm"])
        except ValueError:
            kd_nm = None
            if error is None:
                error = "Target KD must be a number in nanomolar."
        try:
            coverage_pct = float(raw["coverage_pct"])
        except ValueError:
            coverage_pct = 90.0

        if coverage_pct <= 0 or coverage_pct >= 100:
            coverage_pct = 90.0

        if error is None and (positions is None or positions < 1):
            error = "Diversified positions must be at least 1."
        if error is None and positions is not None and positions > 40:
            error = (
                "Diversified positions capped at 40 for this tool. "
                "For combinatorial libraries beyond 40 positions, please "
                "reach out to the Ranomics team."
            )
        if error is None and (kd_nm is None or kd_nm <= 0):
            error = "Target KD must be greater than zero."

        if error:
            return render_template(
                "library_planner_form.html",
                error=error,
                form_values=raw,
            )

        try:
            plan = plan_library(
                scaffold=raw["scaffold"],
                diversification_positions=positions,
                diversification_scheme=raw["scheme"],
                target_kd_nm=kd_nm,
                starting_material=raw["starting_material"],
                target_coverage=coverage_pct / 100.0,
            )
        except ValueError as exc:
            return render_template(
                "library_planner_form.html",
                error=str(exc),
                form_values=raw,
            )

        return render_template(
            "library_planner_results.html",
            plan=plan,
        )

    # ------------------------------------------------------------------
    # GPU tool routes — one form/submit pair per registered adapter,
    # plus shared jobs routes. FLAG_TOOL_<NAME>=off hides a tool at the
    # route level so the UI can ship in one commit and the operator
    # flips the flag after verifying an end-to-end production run.
    # ------------------------------------------------------------------

    def _require_tool(tool_slug: str):
        """Return (adapter, error_response). ``error_response`` is non-None on fail."""
        adapter = tool_base.get(tool_slug)
        if adapter is None:
            return None, (render_template("404.html"), 404)
        if not tool_enabled(tool_slug):
            return None, (render_template("coming_soon.html"), 404)
        return adapter, None

    # ------------------------------------------------------------------
    # B2 — public preview page helpers. Used by the logged-out branch
    # of /tools/<slug> and only there.
    # ------------------------------------------------------------------

    # SEO phrase pairs per tool slug. ``seo_phrase`` is a short natural
    # phrase reused in the page title and lede; ``seo_long`` is a longer
    # phrase used once in body copy. Pulled into one map so the shared
    # preview shell stays free of per-tool branching.
    _PREVIEW_SEO_PHRASES: dict[str, tuple[str, str]] = {
        "mpnn": (
            "free online ProteinMPNN tool",
            "Run ProteinMPNN sequence design on a backbone PDB with no "
            "install and no local GPU"
        ),
        "af2": (
            "AlphaFold2 multimer without a local GPU",
            "Fold complexes through your browser with full MSA and "
            "templates, results land at /jobs"
        ),
        "colabfold": (
            "ColabFold online without Colab",
            "Fast no-MSA folds in 1 to 2 minutes per run, no MMseqs2 "
            "round-trip on your laptop"
        ),
        "esmfold": (
            "ESMFold online single-sequence fold",
            "Fastest monomer fold from the ESM-2 language model with no "
            "MSA, no multimer, no install"
        ),
        "bindcraft": (
            "BindCraft de novo binder design no install",
            "Hallucinate 60 to 150 residue protein binders against a "
            "target PDB on a dedicated GPU"
        ),
        "rfantibody": (
            "RFantibody nanobody design online",
            "Generate VHH scaffolds against a target PDB without setting "
            "up RoseTTAFold or Rosetta locally"
        ),
        "rfdiffusion": (
            "RFdiffusion de novo binder design online",
            "Run RFdiffusion plus AF2 multimer scoring through your "
            "browser without an A100"
        ),
        "boltzgen": (
            "BoltzGen multi-modality binder design online",
            "Design mini-proteins, nanobodies, antibodies, or peptides "
            "against the same target with glycan and PTM support"
        ),
        "boltz2": (
            "Boltz-2 cofold validation online",
            "Validate a designed binder against your antigen with "
            "single-sequence cofold and interface confidence"
        ),
        "pxdesign": (
            "PXDesign AF2-IG binder design online",
            "AF2-initial-guess binder generation with real ipTM, pLDDT, "
            "and pAE on every candidate"
        ),
    }

    def _preview_seo_phrases(slug: str) -> tuple[str, str]:
        """Return (short, long) SEO phrase pair for a tool slug.

        Falls back to a generic pair so newly registered tools still get
        sensible copy without an explicit entry in the map.
        """
        return _PREVIEW_SEO_PHRASES.get(
            slug,
            (
                f"free {slug} tool online",
                "Run it through your browser on a dedicated GPU with no "
                "install"
            ),
        )

    # Title-only phrases. Kept separate from ``_PREVIEW_SEO_PHRASES`` so the
    # body lede stays grammatical ("X is a <seo_phrase> you can run") while
    # the <title> stays under the 65-char SERP cap.
    _PREVIEW_TITLE_PHRASES: dict[str, str] = {
        "mpnn": "Free Sequence Design",
        "af2": "AF2 Multimer No-Install",
        "colabfold": "No Colab Required",
        "esmfold": "Single-Sequence Folding",
        "bindcraft": "De Novo Binder Design",
        "rfantibody": "Nanobody Design",
        "rfdiffusion": "De Novo Binder Design",
        "boltzgen": "Multi-Modal Binder Design",
        "boltz2": "Cofold Validation",
        "pxdesign": "AF2-IG Binder Design",
        "esmfold2-design": "scFv CDR Design",
    }

    def _preview_title_phrase(slug: str) -> str:
        return _PREVIEW_TITLE_PHRASES.get(slug, "GPU-Backed Protein Design")

    # Map tools-hub slug -> ranomics.com /technology/<slug> page slug.
    # Used to emit a cross-site "Learn how X works" link on each public
    # preview so the two co-owned sites reinforce each other for
    # algorithm-name search intent.
    _RANOMICS_TECHNOLOGY_SLUGS: dict[str, str] = {
        "mpnn": "proteinmpnn",
        "af2": "alphafold2",
        "colabfold": "colabfold",
        "esmfold": "esmfold",
        "rfdiffusion": "rfdiffusion",
        "rfantibody": "rfantibody",
        "bindcraft": "bindcraft",
        "boltzgen": "boltzgen",
        "pxdesign": "pxdesign",
    }

    # 2-3 related tools per slug, ordered by closest sibling first.
    # Powers an internal-linking "Related tools" block on each preview
    # page so a searcher comparing algorithms gets surfaced the next
    # logical option from the same workflow stage.
    _RELATED_TOOLS: dict[str, tuple[str, ...]] = {
        "rfdiffusion": ("bindcraft", "pxdesign", "boltzgen"),
        "bindcraft":   ("rfdiffusion", "boltzgen", "pxdesign"),
        "pxdesign":    ("rfdiffusion", "bindcraft", "boltzgen"),
        "boltzgen":    ("rfdiffusion", "rfantibody", "bindcraft"),
        "rfantibody":  ("boltzgen", "rfdiffusion", "bindcraft"),
        "mpnn":        ("af2", "colabfold", "esmfold"),
        "af2":         ("colabfold", "esmfold", "mpnn"),
        "colabfold":   ("af2", "esmfold", "mpnn"),
        "esmfold":     ("af2", "colabfold", "mpnn"),
        "boltz2":      ("af2", "colabfold", "boltzgen"),
    }

    def _short_name_for_label(label: str) -> str:
        """Return the algorithm name only — strip any 'X — descriptor' tail.

        Accepts em-dash ('—'), double-dash ('--'), or bare label. Used
        in SEO titles and h1s so the page leads with the searchable
        algorithm name rather than the full marketing label.
        """
        for sep in (" — ", " -- ", " – "):
            if sep in label:
                return label.split(sep, 1)[0].strip()
        return label.strip()

    def _related_tool_cards(slug: str) -> list[dict]:
        """Build the related-tools card list for the preview page.

        Each card carries slug, short_name, one-line description, and
        the tool_form URL so the template stays declarative.
        """
        import importlib  # noqa: PLC0415
        out: list[dict] = []
        for related_slug in _RELATED_TOOLS.get(slug, ()):
            related_adapter = tool_base.get(related_slug)
            if related_adapter is None or not tool_enabled(related_slug):
                continue
            blurb = related_adapter.blurb or ""
            try:
                rmeta = importlib.import_module(f"tools.{related_slug}.meta")
                one_liner = getattr(rmeta, "comparison_one_liner", None)
                if one_liner:
                    blurb = one_liner
            except ImportError:
                pass
            out.append({
                "slug": related_slug,
                "short_name": _short_name_for_label(related_adapter.label),
                "blurb": blurb,
                "url": url_for("tool_form", tool=related_slug),
            })
        return out

    def _runtime_band_for_adapter(adapter, meta) -> str:
        """Compute the same runtime band string used on the homepage cards.

        Mirrors the inline logic in :func:`_build_tools_catalog` so the
        preview page reports the same band as the homepage. Falls back
        to '—' when the adapter has no PRESET_RUNTIME entries.
        """
        if meta is None:
            return "—"
        runtime_map = getattr(meta, "PRESET_RUNTIME", None) or {}
        legacy_rows = getattr(meta, "preset_runtime_rows", None) or ()
        legacy_by_slug = {
            r.get("slug"): r.get("runtime")
            for r in legacy_rows
            if r.get("slug") and r.get("runtime")
        }
        runtimes: list[str] = []
        for preset in adapter.presets:
            entry = runtime_map.get(preset.slug) or {}
            if entry.get("typical_minutes"):
                rt = f"{entry['typical_minutes']} min"
            else:
                rt = legacy_by_slug.get(preset.slug)
            if rt and rt not in runtimes:
                runtimes.append(rt)
        if len(runtimes) >= 2:
            return f"{runtimes[0]} to {runtimes[-1]}"
        if len(runtimes) == 1:
            return runtimes[0]
        return "—"

    def _template_exists(template_name: str) -> bool:
        """True if Jinja can resolve ``template_name`` via the loader."""
        try:
            flask_app.jinja_env.get_template(template_name)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # B7 — showcase loader. Reads content/showcase/*.md, parses a simple
    # ``---``-delimited frontmatter block, and returns a list of
    # ``{meta, body, slug, tool_url, guide_url}`` dicts for the template.
    # ------------------------------------------------------------------

    _SHOWCASE_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "content", "showcase"
    )

    def _parse_showcase_frontmatter(text: str) -> tuple[dict, str]:
        """Parse a minimal YAML-ish frontmatter block from ``text``.

        Accepts ``key: value`` lines between two ``---`` separators.
        ``true``/``false`` (case-insensitive) coerce to bool; bare
        numbers coerce to int or float. Everything else stays a string.
        Returns ``(meta, body)``. If the frontmatter block is missing,
        ``meta`` is empty and the whole input is the body.
        """
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        meta: dict = {}
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            line = lines[i]
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if value.lower() in ("true", "false"):
                    meta[key] = value.lower() == "true"
                else:
                    try:
                        if "." in value:
                            meta[key] = float(value)
                        else:
                            meta[key] = int(value)
                    except ValueError:
                        meta[key] = value
            i += 1
        body = "\n".join(lines[i + 1:]).strip("\n")
        return meta, body

    def _load_showcase_entries() -> list[dict]:
        """Read every .md file under content/showcase/, sorted by filename.

        Filename order is the curated display order (entries are named
        ``01-...``, ``02-...`` etc). Each entry's ``tool`` frontmatter
        is matched to a registered tool adapter so the entry can link
        into the matching /tools/<slug> preview page from B2. The
        hardcoded Epitope Scout slug ``scout`` resolves to the Scout
        index route instead.
        """
        if not os.path.isdir(_SHOWCASE_DIR):
            return []
        entries: list[dict] = []
        for filename in sorted(os.listdir(_SHOWCASE_DIR)):
            if not filename.endswith(".md"):
                continue
            full = os.path.join(_SHOWCASE_DIR, filename)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                logger.warning("showcase: failed to read %s", filename, exc_info=True)
                continue
            meta, body = _parse_showcase_frontmatter(raw)
            meta.setdefault("internal_benchmark", True)
            tool_slug = (meta.get("tool") or "").strip()
            tool_url: str | None = None
            guide_url: str | None = None
            if tool_slug == "scout":
                try:
                    tool_url = url_for("scout.index")
                except Exception:
                    tool_url = "/scout"
            elif tool_slug:
                adapter = tool_base.get(tool_slug)
                if adapter is not None and tool_enabled(tool_slug):
                    try:
                        tool_url = url_for("tool_form", tool=tool_slug)
                    except Exception:
                        tool_url = f"/tools/{tool_slug}"
                    try:
                        guide_url = url_for(
                            "help_tool_guide", tool=tool_slug
                        )
                    except Exception:
                        guide_url = f"/help/tools/{tool_slug}"
            entries.append({
                "meta": meta,
                "body": body,
                "slug": filename[:-3],
                "tool_url": tool_url,
                "guide_url": guide_url,
            })
        return entries

    # ------------------------------------------------------------------
    # B7 — public /showcase: curated anonymized runs with deep links into
    # the matching /tools/<slug> preview pages from B2. Indexable.
    # ------------------------------------------------------------------

    @flask_app.route("/showcase", methods=["GET"])
    def showcase():
        """Render the curated showcase index.

        Loads every ``.md`` file under ``content/showcase/``, parses a
        simple YAML-ish frontmatter block, and renders the body as
        plaintext inside the template's ``<pre>`` block. Per-entry
        Dataset JSON-LD is emitted from the template so each entry is
        indexable as its own dataset.

        Frontmatter shape:
            ---
            title: str
            tool: <slug matching tools.<slug>>
            target_kind: str
            top_score: number
            date: YYYY-MM-DD
            internal_benchmark: bool (default True)
            ---
        """
        entries = _load_showcase_entries()
        return render_template("showcase.html", entries=entries)

    # ------------------------------------------------------------------
    # Help / docs hub — public (no login required).
    # ------------------------------------------------------------------

    @flask_app.route("/help", methods=["GET"])
    def help_index():
        """Docs hub: getting started, per-tool guides, FAQ, troubleshooting."""
        breadcrumbs = [
            {"name": "Home", "url": url_for("index", _external=True)},
            {"name": "Help", "url": url_for("help_index", _external=True)},
        ]
        return render_template(
            "help/index.html",
            adapters=tool_base.all_adapters(),
            breadcrumbs=breadcrumbs,
        )

    @flask_app.route("/help/getting-started", methods=["GET"])
    def help_getting_started():
        return render_template("help/getting_started.html")

    @flask_app.route("/help/tools/<tool>", methods=["GET"])
    def help_tool_guide(tool: str):
        adapter = tool_base.get(tool)
        if adapter is None:
            return render_template("404.html"), 404
        import importlib  # noqa: PLC0415
        try:
            meta = importlib.import_module(f"tools.{tool}.meta")
        except ImportError:
            meta = None
        short_name = _short_name_for_label(adapter.label)
        breadcrumbs = [
            {"name": "Home", "url": url_for("index", _external=True)},
            {"name": "Help", "url": url_for("help_index", _external=True)},
            {"name": "Tools", "url": url_for(
                "tools_comparison", _external=True
            )},
            {"name": short_name, "url": url_for(
                "help_tool_guide", tool=tool, _external=True
            )},
        ]
        return render_template(
            "help/tool_guide.html",
            tool=tool,
            adapter=adapter,
            meta=meta,
            short_name=short_name,
            breadcrumbs=breadcrumbs,
        )

    @flask_app.route("/help/faq", methods=["GET"])
    def help_faq():
        return render_template("help/faq.html")

    @flask_app.route("/help/troubleshooting", methods=["GET"])
    def help_troubleshooting():
        return render_template("help/troubleshooting.html")

    @flask_app.route("/tools/<tool>", methods=["GET"])
    def tool_form(tool: str):
        """Render a GPU tool's submission form, or a public preview if logged out.

        Logged-out branch (B2): render ``tools/<slug>_preview.html`` if
        present, falling back to the shared ``tools/_preview.html`` shell.
        The preview is indexable; the underlying run form is not. The
        POST handler at /tools/<slug>/submit stays @login_required so
        logged-out visitors cannot spawn jobs.

        Logged-in pre-fill sources (query params, owner-scoped):
          * ``clone_from=<job_id>`` — reuse all inputs of an earlier job.
            Same-tool only (exact parameter fidelity).
          * ``from_job=<job_id>`` — Phase 4 cross-tool handoff. Copies
            only the target fields (target PDB reuse token, target_chain,
            hotspot_residues) and defaults preset='pilot'. Works across
            tools so a user can refine RFantibody output with BindCraft,
            validate BoltzGen output with PXDesign, etc.
          * ``handoff=<handoff_id>`` — target PDB + chain + hotspots from
            Epitope Scout via ``public.scout_handoffs``.
          * ``workspace_id=<ws_id>&target_pdb_id=<storage_path>`` —
            Workspace-funded run. The detail page at
            /workspaces/<id> emits these together so the POST gate
            (``workspace_preflight``) can verify the run is funded by
            an active Workspace and bill the actual Modal cost back.
        """
        adapter, err = _require_tool(tool)
        if err:
            return err

        # Logged-out: render the public preview shell. Per-tool override
        # at templates/tools/<slug>_preview.html wins if present;
        # otherwise fall through to the shared shell. The shared shell
        # extends base.html and renders About + score legend + paper +
        # "Sign in to run" CTA.
        if not session.get("user_email"):
            import importlib  # noqa: PLC0415
            preview_meta = None
            try:
                preview_meta = importlib.import_module(
                    f"tools.{adapter.slug}.meta"
                )
            except ImportError:
                pass
            runtime_band = _runtime_band_for_adapter(adapter, preview_meta)
            seo_phrase, seo_long = _preview_seo_phrases(adapter.slug)
            title_phrase = _preview_title_phrase(adapter.slug)
            login_next = url_for("tool_form", tool=adapter.slug)
            per_tool_template = f"tools/{adapter.slug}_preview.html"
            template_name = per_tool_template if _template_exists(
                per_tool_template
            ) else "tools/_preview.html"
            short_name = _short_name_for_label(adapter.label)
            tech_slug = _RANOMICS_TECHNOLOGY_SLUGS.get(adapter.slug)
            learn_more_url = (
                f"https://www.ranomics.com/technology/{tech_slug}"
                if tech_slug else None
            )
            breadcrumbs = [
                {"name": "Home", "url": url_for("index", _external=True)},
                {"name": "Tools", "url": url_for(
                    "tools_comparison", _external=True
                )},
                {"name": short_name, "url": url_for(
                    "tool_form", tool=adapter.slug, _external=True
                )},
            ]
            return render_template(
                template_name,
                adapter=adapter,
                meta=preview_meta,
                runtime_band=runtime_band,
                login_next=login_next,
                seo_phrase=seo_phrase,
                seo_long=seo_long,
                title_phrase=title_phrase,
                short_name=short_name,
                learn_more_url=learn_more_url,
                related_tools=_related_tool_cards(adapter.slug),
                breadcrumbs=breadcrumbs,
            )

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        # Workspace context (Wave-2 launch). Forwarded as hidden form
        # inputs by templates/tools/_prefill.html::workspace_hidden_inputs
        # so the POST handler can re-read and gate.
        workspace_ctx: dict | None = None
        ws_id_q = (request.args.get("workspace_id") or "").strip()
        ws_target_q = (request.args.get("target_pdb_id") or "").strip()
        if ws_id_q and ws_target_q:
            workspace_ctx = {
                "workspace_id": ws_id_q,
                "target_pdb_id": ws_target_q,
            }

        pre_fill: dict = {}
        pdb_source = None  # dict describing a reusable PDB, or None

        clone_from = request.args.get("clone_from", "").strip()
        if clone_from:
            prior = get_job(clone_from, user_id=ctx.user_id)
            if prior is not None and prior.tool == adapter.slug:
                pre_fill = {
                    k: v for k, v in (prior.inputs or {}).items()
                    if not k.startswith("_")
                }
                # Normalize list-typed inputs back to form-friendly strings.
                hs = pre_fill.get("hotspot_residues")
                if isinstance(hs, list):
                    pre_fill["hotspot_residues"] = ",".join(str(x) for x in hs)
                stored_path = (prior.inputs or {}).get("_pdb_storage_path")
                stored_name = (prior.inputs or {}).get("_pdb_filename")
                if stored_path and stored_name:
                    pdb_source = {
                        "label": f"PDB from job {prior.id[:8]} ({stored_name})",
                        "filename": stored_name,
                        "token": f"job:{prior.id}",
                    }

        from_job = request.args.get("from_job", "").strip()
        if from_job and not pre_fill:
            # Cross-tool handoff: copy only target fields, default to pilot.
            # Unlike clone_from this works across tools — the binder /
            # parameter shape differs, but target_pdb + target_chain +
            # hotspots are shared across BindCraft / RFantibody /
            # BoltzGen / PXDesign.
            src = get_job(from_job, user_id=ctx.user_id)
            if src is not None:
                src_inputs = src.inputs or {}
                for key in ("target_chain", "hotspot_residues"):
                    val = src_inputs.get(key)
                    if val is None:
                        continue
                    if isinstance(val, list):
                        val = ",".join(str(x) for x in val)
                    pre_fill[key] = val
                pre_fill["preset"] = "pilot"
                stored_path = src_inputs.get("_pdb_storage_path")
                stored_name = src_inputs.get("_pdb_filename")
                if stored_path and stored_name:
                    pdb_source = {
                        "label": (
                            f"Target PDB from {src.tool} job {src.id[:8]} "
                            f"({stored_name})"
                        ),
                        "filename": stored_name,
                        "token": f"job:{src.id}",
                    }

        handoff_id = request.args.get("handoff", "").strip()
        if handoff_id:
            ho = get_handoff(handoff_id, user_id=ctx.user_id)
            if ho is not None:
                pre_fill.setdefault("target_chain", ho.target_chain)
                pre_fill.setdefault(
                    "hotspot_residues",
                    ",".join(str(r) for r in ho.hotspot_residues),
                )
                pre_fill["preset"] = "pilot"
                pdb_source = {
                    "label": f"Target PDB from Epitope Scout ({ho.pdb_filename})",
                    "filename": ho.pdb_filename,
                    "token": f"handoff:{ho.id}",
                }

        # C2 — "Load example" chip: a first-time user can populate the
        # form with a known-good PDB / FASTA in one click. The example
        # registry lives at tools/<slug>/meta.py:examples; the actual
        # files live at tools/<slug>/examples/<filename>. Param overrides
        # in the example dict are applied to pre_fill the same way
        # clone_from does, then the form is decorated with either an
        # "example:<tool>/<id>" pdb_source token (binder / sequence-
        # design tools) or pre-populated FASTA text (AF2 / ColabFold /
        # ESMFold). The submit-side resolver below stages the PDB
        # exactly like a fresh upload.
        example_id = request.args.get("example", "").strip()
        if example_id and not pre_fill:
            from shared.examples import (  # noqa: PLC0415
                load_example, read_example_text,
            )
            entry = load_example(adapter.slug, example_id)
            if entry is not None:
                for k, v in (entry.get("params") or {}).items():
                    pre_fill[k] = v
                fasta_field = entry.get("fasta_field")
                if fasta_field:
                    fasta_content = read_example_text(
                        adapter.slug, example_id
                    )
                    if fasta_content is not None:
                        pre_fill[fasta_field] = fasta_content
                else:
                    pre_fill.setdefault("preset", "pilot")
                    pdb_source = {
                        "label": (
                            f"Example: {entry.get('label', example_id)}"
                        ),
                        "filename": entry.get(
                            "filename", f"{example_id}.pdb"
                        ),
                        "token": (
                            f"example:{adapter.slug}/{example_id}"
                        ),
                    }

                # D3 funnel fire. Only count entries that resolved to a
                # real example dict; a typo'd ?example=foo silently
                # falls through to the empty form and should not pollute
                # the dashboard.
                from shared.events import EVENTS, emit  # noqa: PLC0415
                emit(
                    EVENTS.EXAMPLE_LOADED,
                    user_id=ctx.user_id,
                    properties={
                        "tool": adapter.slug,
                        "example_id": example_id,
                    },
                )

        # AF2-resample chain: when the user lands on the MPNN form via a
        # "Resample with MPNN" button on an AF2 / ColabFold / ESMFold
        # result page, prefill the MPNN form with the source job's
        # predicted PDB and sensible diversification defaults
        # (sampling_temp=0.5, num_seq_per_target=16). The PDB itself is
        # not staged here — that happens at submit time when the
        # ``resample:<job_id>`` token is resolved (the submit-side
        # branch decodes pdb_b64 from the source job's result and
        # uploads it like a fresh PDB).
        resample_from = request.args.get("resample_from", "").strip()
        if (
            resample_from
            and adapter.slug == _resample.RESAMPLE_DESTINATION
            and not pre_fill
        ):
            src = get_job(resample_from, user_id=ctx.user_id)
            if (
                src is not None
                and _resample.can_resample(src.tool)
                and src.status == "succeeded"
                and ((src.result or {}).get("pdb_b64") or "").strip()
            ):
                for k, v in _resample.RESAMPLE_MPNN_DEFAULTS.items():
                    pre_fill[k] = v
                pdb_source = {
                    "label": (
                        f"Predicted PDB from {src.tool} job {src.id[:8]}"
                    ),
                    "filename": (
                        f"predicted-{src.tool}-{src.id[:8]}.pdb"
                    ),
                    "token": f"resample:{src.id}",
                }
                from shared.events import EVENTS, emit  # noqa: PLC0415
                emit(
                    EVENTS.RESAMPLE_LOADED,
                    user_id=ctx.user_id,
                    properties={
                        "source_tool": src.tool,
                        "source_job_id": src.id,
                    },
                )

        # The wallet estimate partial reads balance_usd for first paint
        # so the form lights up with the user's real balance even before
        # the /api/wallet/estimate call returns. Falls back to 0 if the
        # service client is misconfigured.
        wallet_for_form = get_or_create_wallet(ctx.user_id) or {}

        from shared.examples import list_examples  # noqa: PLC0415
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=None,
            pre_fill=pre_fill,
            pdb_source=pdb_source,
            workspace_ctx=workspace_ctx,
            wallet=wallet_for_form,
            examples=list_examples(adapter.slug),
            active_example_id=example_id or None,
        )

    @flask_app.route("/tools/<tool>/preflight", methods=["POST"])
    @login_required
    def tool_preflight(tool: str):
        """Run the per-tool PDB preflight and return a JSON verdict.

        Fired by ``static/js/preflight.js`` when the user attaches a PDB
        (or clicks "Use AlphaFold model instead"). No wallet hold, no
        job row, no Modal call — this is purely a "would this work?"
        check. The same logic re-runs at submit time as the actual gate.

        Accepts EITHER:
          - ``target_pdb`` file upload (multipart) + form fields, OR
          - ``alphafold_accession`` form field with a UniProt id like
            ``P25779`` (we fetch the AF model and run preflight on it).

        Returns JSON; see ``_verdict_to_json`` for the shape.
        """
        adapter, err = _require_tool(tool)
        if err:
            return ({"error": "Unknown tool"}, 404)
        if adapter.slug not in PREFLIGHT_TOOLS:
            return ({
                "kind": "ready", "ok": True,
                "tool_slug": adapter.slug,
                "cleanup_items": [], "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        target_chain = (request.form.get("target_chain") or "A").strip()
        raw_hotspots = (request.form.get("hotspot_residues") or "").strip()
        hotspots: list = []
        if raw_hotspots:
            for tok in raw_hotspots.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    hotspots.append(int(tok))
                except ValueError:
                    # Non-integer hotspot entries are surfaced through the
                    # form validator on submit; for preflight purposes we
                    # ignore them so the panel renders something useful.
                    pass

        # Source the bytes: file upload OR AlphaFold fetch.
        af_accession = (request.form.get("alphafold_accession") or "").strip()
        uploaded = request.files.get("target_pdb")
        pdb_bytes: Optional[bytes] = None
        source_label: str = ""

        if af_accession:
            fetched = _fetch_alphafold_bytes(af_accession)
            if fetched is None:
                return ({
                    "kind": "needs_fix", "ok": False,
                    "tool_slug": adapter.slug,
                    "reason": (
                        f"Couldn't fetch AlphaFold model for {af_accession}. "
                        f"The AlphaFold-DB may not have this UniProt entry."
                    ),
                    "suggested_fix": (
                        "Pick a different target or upload a cleaned PDB manually."
                    ),
                    "cleanup_items": [],
                    "hotspots": {"surviving": [], "dropped": []},
                    "alphafold": None,
                }, 200)
            pdb_bytes = fetched
            source_label = f"AF-{af_accession}"
        elif uploaded and uploaded.filename:
            pdb_bytes = uploaded.read()
            # If the upload is CIF, convert before preflight (the
            # downstream pipeline_normalize assumes PDB-or-CIF, but the
            # normalizer's extension routing keys off filename; safer to
            # convert here once so the preflight matches the submit-side
            # cleanup pass exactly).
            fname_lower = (uploaded.filename or "").lower()
            if fname_lower.endswith((".cif", ".mmcif")):
                try:
                    pdb_bytes = convert_cif_to_pdb_bytes(pdb_bytes, uploaded.filename)
                except CifConversionError as exc:
                    return ({
                        "kind": "needs_fix", "ok": False,
                        "tool_slug": adapter.slug,
                        "reason": str(exc),
                        "suggested_fix": (
                            "Save the structure as PDB format and re-upload."
                        ),
                        "cleanup_items": [],
                        "hotspots": {"surviving": [], "dropped": []},
                        "alphafold": None,
                    }, 200)
            source_label = uploaded.filename
        else:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": "No PDB uploaded.",
                "suggested_fix": "Attach a target PDB above.",
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        # Cheap inspection first — catches "this isn't a PDB at all".
        inspection = inspect_pdb_bytes(pdb_bytes, filename=source_label)
        if not inspection.ok:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": inspection.error or "Couldn't parse upload as PDB.",
                "suggested_fix": (
                    "Confirm the file is a PDB or mmCIF protein structure."
                ),
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        binder_max_aa, num_designs = _parse_preflight_size_params(request.form)
        verdict = preflight_for_tool(
            adapter.slug, pdb_bytes,
            target_chain=target_chain, hotspots=hotspots,
            binder_max_aa=binder_max_aa, num_designs=num_designs,
        )
        return (_verdict_to_json(verdict, source_label), 200)

    @flask_app.route("/tools/<tool>/submit", methods=["POST"])
    @login_required
    @idempotent()
    @requires_wallet
    def tool_submit(tool: str):
        """Validate, place a wallet hold, upload PDB, spawn Modal, redirect to job detail."""
        adapter, err = _require_tool(tool)
        if err:
            return err

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        # Workspace context (Wave-2). The /workspaces/<id> detail page
        # links to /tools/<slug>?workspace_id=...&target_pdb_id=... and
        # the form template forwards both as hidden inputs (see
        # ``workspace_hidden_inputs`` macro in
        # ``templates/tools/_prefill.html``). When present, the
        # workspace_preflight gate below rejects expired or
        # cap-exhausted workspaces BEFORE we create the job row, and the
        # IDs flow through to ``create_job`` so the completion-side
        # ``charge_for_job`` wiring (item #6) can bill the right cap.
        ws_id_form = (request.form.get("workspace_id") or "").strip()
        ws_target_form = (request.form.get("target_pdb_id") or "").strip()
        workspace_ctx: dict | None = None
        if ws_id_form and ws_target_form:
            workspace_ctx = {
                "workspace_id": ws_id_form,
                "target_pdb_id": ws_target_form,
            }

        inputs, error_msg = adapter.validate(request.form, request.files)
        if inputs is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=error_msg,
                pre_fill=dict(request.form.items()),
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        preset = adapter.preset_for(inputs["preset"])
        if preset is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error="Unknown preset.",
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # Workspace gate (when context present). Rejects expired,
        # refunded, or cap-exhausted workspaces BEFORE the job row is
        # written, BEFORE PDB upload, BEFORE the Modal call. Submissions
        # without workspace context are gated by the wallet alone — the
        # requires_wallet decorator placed a hold before this handler ran.
        if workspace_ctx is not None:
            from shared.workspaces import workspace_preflight  # noqa: PLC0415
            preflight = workspace_preflight(
                ctx.user_id, workspace_ctx["target_pdb_id"]
            )
            if not preflight.allow:
                if preflight.reason == "no_workspace":
                    return redirect(url_for("workspaces_new"))
                # cap_exceeded / expired: send the user to the workspace
                # detail so the cap meter + upgrade CTA explain why.
                return redirect(
                    url_for(
                        "workspace_detail",
                        workspace_id=workspace_ctx["workspace_id"],
                    )
                )
            # Sanity-check: the user's active workspace for this target
            # may differ from the one the form claims (e.g. if they
            # bought a second workspace mid-session). Trust the form ID
            # for charge attribution; preflight already confirmed an
            # active workspace exists for this user+target.
            workspace_ctx["workspace_id"] = preflight.workspace.id

        # Per-preset PDB requirement: paid presets need an upload, smoke
        # and preview do not. Falls back to the adapter-level flag for
        # tools that require a PDB on every paid run (e.g. BindCraft).
        needs_pdb = bool(getattr(preset, "requires_pdb", False)) or adapter.requires_pdb
        uploaded = request.files.get("target_pdb")
        reuse_token = (request.form.get("reuse_pdb_token") or "").strip()

        # Gate "no PDB attached" BEFORE create_job. Otherwise the row gets
        # written, the upload check fails further down, and the row sits
        # in 'pending' forever as an orphan with no Modal call and no
        # spend ledger entry. Production incident 2026-04-30: a pxdesign
        # pilot submit with no file attached created job d2d421ad which
        # showed PENDING for 2.5 hours until manually cancelled.
        if needs_pdb and not (
            (uploaded is not None and uploaded.filename)
            or reuse_token.startswith("job:")
            or reuse_token.startswith("handoff:")
            or reuse_token.startswith("example:")
            or reuse_token.startswith("resample:")
            or reuse_token.startswith("alphafold:")
        ):
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error="Upload a target PDB file.",
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # ---- PDB pre-flight inspection (Bug 9 follow-on) ----
        # Run a fast Biopython inspection on freshly-uploaded files so we
        # can reject obvious garbage (no protein, no ATOM records, malformed
        # parse) and validate user-typed target_chain + hotspots BEFORE
        # spinning up Modal. Reuse-token paths are not
        # re-inspected (the source job's PDB has already passed this gate).
        # Bytes are read here ONCE; we pass them through to upload_input
        # below so we don't need to seek the file pointer back.
        pdb_bytes: bytes | None = None
        inspection = None
        converted_filename: str | None = None
        if needs_pdb and uploaded is not None and uploaded.filename:
            pdb_bytes = uploaded.read()
            inspection = inspect_pdb_bytes(pdb_bytes, filename=uploaded.filename)
            logger.info(
                "pdb_inspect %s/%s: %s",
                adapter.slug, preset.slug, summarize_for_log(inspection),
            )
            if not inspection.ok:
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=inspection.error,
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            target_chain = (inputs.get("target_chain") or "").strip()
            if target_chain:
                chain_err = validate_target_chain(inspection, target_chain)
                if chain_err:
                    return render_template(
                        adapter.form_template, adapter=adapter,
                        error=chain_err, pre_fill=inputs, pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                hotspots = inputs.get("hotspot_residues") or []
                # boltz2 hotspots are 1-indexed SEQUENCE positions, not
                # original PDB numbering; they are range-checked against the
                # antigen length in boltz2's own preflight, so skip the
                # original-numbering check here (it would false-reject an
                # antigen whose numbering does not start at 1).
                if hotspots and adapter.slug != "boltz2":
                    in_range, out_of_range = validate_hotspots(
                        inspection, target_chain, hotspots,
                    )
                    if out_of_range:
                        return render_template(
                            adapter.form_template, adapter=adapter,
                            error=hotspot_range_message(
                                inspection, target_chain, out_of_range,
                            ),
                            pre_fill=inputs, pdb_source=None,
                            workspace_ctx=workspace_ctx,
                        )

            # ---- CIF -> PDB conversion (fleet-wide fix for
            # MPNN/RFdiff/BindCraft/RFantibody) ----
            # ProteinMPNN's parser and the rfdiffusion / bindcraft /
            # rfantibody docker pipelines are PDB-column-strict and
            # crash on CIF text (ValueError on byte-slice float
            # conversion). Convert here, before storage upload, so
            # Modal workers always see real PDB content. Pxdesign and
            # Boltzgen accept PDB just as well as CIF, so this is
            # universally safe across the tool set.
            fname_lower = uploaded.filename.lower()
            if fname_lower.endswith(".cif") or fname_lower.endswith(".mmcif"):
                try:
                    pdb_bytes = convert_cif_to_pdb_bytes(
                        pdb_bytes, uploaded.filename,
                    )
                except CifConversionError as exc:
                    return render_template(
                        adapter.form_template, adapter=adapter,
                        error=str(exc), pre_fill=inputs, pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                converted_filename = (
                    uploaded.filename.rsplit(".", 1)[0] + ".pdb"
                )
                logger.info(
                    "cif_convert %s/%s: %s -> %s (%d bytes)",
                    adapter.slug, preset.slug,
                    uploaded.filename, converted_filename, len(pdb_bytes),
                )
            else:
                converted_filename = uploaded.filename

        # ---- AlphaFold reuse_token: fetch the AF model + use as PDB ----
        # When the user clicked "Use AlphaFold model instead" in the
        # preflight panel, the form replaces the file upload with
        # reuse_pdb_token="alphafold:<accession>". Fetch the model now,
        # treat the bytes as the upload for the rest of the submit path,
        # and let the hard-gate preflight below decide if the hotspots
        # still resolve on the AF model.
        af_accession_for_reuse: str | None = None
        if reuse_token.startswith("alphafold:"):
            af_accession_for_reuse = reuse_token.split(":", 1)[1].strip()
            af_bytes = _fetch_alphafold_bytes(af_accession_for_reuse)
            if af_bytes is None:
                if hold_tx_id_from_g := getattr(g, "wallet_hold_tx_id", None):
                    try:
                        wallet_release_hold(
                            hold_tx_id_from_g, reason="alphafold_fetch_failed",
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold after AF fetch fail "
                            "raised for hold=%s", hold_tx_id_from_g,
                            exc_info=True,
                        )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=(
                        f"Couldn't fetch AlphaFold model AF-{af_accession_for_reuse}. "
                        f"Try uploading a target PDB directly."
                    ),
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            pdb_bytes = af_bytes
            converted_filename = f"AF-{af_accession_for_reuse}.pdb"

        # ---- Hard-gate preflight (the rfantibody / hcruz fix) ----
        # For binder design tools, re-run the per-tool normalizer in
        # dry-run mode against the bytes we're about to ship to Modal
        # and BLOCK the submit on NEEDS_FIX. The exact same logic powers
        # the /tools/<tool>/preflight AJAX endpoint that drives the panel
        # above the Run button, so the user has already seen this verdict
        # before clicking. The gate here is the safety net for direct-POST
        # / curl / form-resubmit-without-JS paths.
        if (
            adapter.slug in PREFLIGHT_TOOLS
            and pdb_bytes is not None
        ):
            preflight_target_chain = (inputs.get("target_chain") or "").strip()
            preflight_hotspots = inputs.get("hotspot_residues") or []
            preflight_binder_max, preflight_num_designs = (
                _parse_preflight_size_params(inputs)
            )
            try:
                preflight_verdict = preflight_for_tool(
                    adapter.slug, pdb_bytes,
                    target_chain=preflight_target_chain,
                    hotspots=preflight_hotspots,
                    binder_max_aa=preflight_binder_max,
                    num_designs=preflight_num_designs,
                )
            except Exception:
                # Defensive: a preflight crash must not block submit on
                # otherwise-valid uploads. Log and let the existing
                # server-side normalizer in the Modal pipeline handle it.
                logger.exception("preflight unexpected error tool=%s",
                                 adapter.slug)
                preflight_verdict = None
            if preflight_verdict is not None and not preflight_verdict.ok:
                if hold_for_release := getattr(g, "wallet_hold_tx_id", None):
                    try:
                        wallet_release_hold(
                            hold_for_release, reason="preflight_failed",
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold on preflight "
                            "failure raised for hold=%s",
                            hold_for_release, exc_info=True,
                        )
                source_label = converted_filename or (
                    uploaded.filename if uploaded is not None else None
                ) or ""
                if adapter.slug not in _PREFLIGHT_PANEL_FORMS:
                    # pxdesign / boltz2 forms have no rich panel — surface a
                    # plain actionable message so the rejection is visible.
                    plain = (
                        preflight_verdict.reason
                        or "This target can't run as-is."
                    )
                    if preflight_verdict.suggested_fix:
                        plain = f"{plain} {preflight_verdict.suggested_fix}"
                    return render_template(
                        adapter.form_template,
                        adapter=adapter,
                        error=plain,
                        pre_fill=inputs,
                        pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=None,
                    preflight_verdict=_verdict_to_json(
                        preflight_verdict, source_label,
                    ),
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            # Verdict is OK — stash the JSON shape on inputs._preflight so
            # the /jobs/<id> page can replay the same panel ("we cleaned X,
            # Y, Z"; "swapped in AlphaFold AF-Pxxxxxx"). Persists with the
            # job row so the panel survives a page refresh and shows up on
            # completed jobs too — useful for "what did the cleanup change
            # before this design pool was generated?" provenance later.
            if preflight_verdict is not None and preflight_verdict.ok:
                ok_source_label = converted_filename or (
                    uploaded.filename if uploaded is not None else None
                ) or (
                    f"AF-{af_accession_for_reuse}.pdb"
                    if af_accession_for_reuse else ""
                )
                inputs = dict(inputs)
                inputs["_preflight"] = _verdict_to_json(
                    preflight_verdict, ok_source_label,
                )
                # Record explicitly when the user actually accepted the AF
                # swap (vs the verdict merely surfacing the suggestion).
                if af_accession_for_reuse:
                    inputs["_preflight"]["used_alphafold"] = True
                    inputs["_preflight"]["alphafold_accession_used"] = \
                        af_accession_for_reuse

        # Create the tool_jobs row so we have job_id + job_token for the
        # Modal payload and a persistent handle even if Modal submit
        # raises. Workspace IDs (when present) are stashed in inputs._workspace
        # so the completion-side ``charge_for_job`` (item #6) bills the
        # right cap.
        ws_target = workspace_ctx["target_pdb_id"] if workspace_ctx else None
        ws_id = workspace_ctx["workspace_id"] if workspace_ctx else None
        # Stash the wallet hold id (from the requires_wallet decorator)
        # on the job's inputs so the settle path in shared.jobs can
        # close it out on completion. None when the estimate was zero
        # (smoke runs); the settle hook short circuits in that case.
        hold_tx_id = getattr(g, "wallet_hold_tx_id", None)
        wallet_estimate = getattr(g, "wallet_estimate_usd", None)
        if hold_tx_id or wallet_estimate is not None:
            inputs = dict(inputs)
            wallet_ctx = dict(inputs.get("_wallet") or {})
            if hold_tx_id:
                wallet_ctx["hold_tx_id"] = hold_tx_id
            if wallet_estimate is not None:
                wallet_ctx["estimate_usd"] = str(wallet_estimate)
            wallet_ctx["tool_slug"] = adapter.slug
            inputs["_wallet"] = wallet_ctx

        # C4 — free-form campaign label. Trimmed + length-capped in
        # create_job so power users running 50 variations of one target
        # see them grouped on /jobs instead of 50 flat rows.
        form_campaign_label = (request.form.get("campaign_label") or "").strip()

        job = create_job(
            user_id=ctx.user_id,
            tool=adapter.slug,
            preset=preset.slug,
            inputs=inputs,
            target_pdb_id=ws_target,
            workspace_id=ws_id,
            campaign_label=form_campaign_label or None,
        )
        if job is None:
            # Release the hold so we don't leave a stranded reservation.
            if hold_tx_id:
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="job_create_failed"
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold after create_job "
                        "failure raised for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not create job record. Supabase is unreachable. "
                    "Try again in a moment."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # create_job succeeded and the hold_tx_id is now stashed on
        # inputs._wallet. shared.jobs._settle_wallet_hold_for_completed_job
        # owns the hold lifecycle from here on. Tell the requires_wallet
        # decorator not to fire its auto-release: if the storage upload
        # or the Modal submit fails below, those paths release the hold
        # explicitly (release_hold is idempotent, so a follow-up
        # auto-release would no-op, but mark it consumed anyway for
        # clarity).
        g.wallet_hold_consumed = True

        presigned_url = ""
        staged_path = ""
        staged_filename = ""
        # Bytes resolved in-memory by a reuse token (example: / resample:),
        # captured so the reuse verification below need not re-download them.
        reuse_resolved_bytes: bytes | None = None
        if needs_pdb:
            try:
                if uploaded is not None and uploaded.filename:
                    # converted_filename is the original name with a .pdb
                    # extension after CIF conversion (set above), or the
                    # original .pdb filename unchanged. Storage + Modal
                    # always see .pdb because pdb_bytes is always PDB by
                    # the time we get here.
                    staged_filename = converted_filename or uploaded.filename
                    # pdb_bytes was read (and possibly converted) during
                    # pre-flight; reuse instead of double-reading.
                    file_data = pdb_bytes if pdb_bytes is not None else uploaded.read()
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=file_data,
                        content_type="chemical/x-pdb",
                    )
                elif reuse_token.startswith("job:"):
                    # Wave 3A clone: copy PDB from the original job's prefix.
                    prior_job_id = reuse_token.split(":", 1)[1]
                    prior = get_job(prior_job_id, user_id=ctx.user_id)
                    if prior is None:
                        raise StorageError("source job not found")
                    src_path = (prior.inputs or {}).get("_pdb_storage_path")
                    src_name = (prior.inputs or {}).get("_pdb_filename")
                    if not src_path or not src_name:
                        raise StorageError("source job has no stored PDB")
                    staged_filename = src_name
                    staged_path = copy_input(
                        source_path=src_path,
                        dest_user_id=ctx.user_id,
                        dest_job_id=job.id,
                        filename=src_name,
                    )
                elif reuse_token.startswith("handoff:"):
                    # Wave 3C Scout handoff: copy PDB staged by Scout.
                    ho_id = reuse_token.split(":", 1)[1]
                    ho = get_handoff(ho_id, user_id=ctx.user_id)
                    if ho is None:
                        raise StorageError(
                            "handoff not found or already consumed"
                        )
                    staged_filename = ho.pdb_filename
                    staged_path = copy_input(
                        source_path=ho.pdb_storage_path,
                        dest_user_id=ctx.user_id,
                        dest_job_id=job.id,
                        filename=ho.pdb_filename,
                    )
                    mark_consumed(ho.id)
                elif reuse_token.startswith("example:"):
                    # C2 example: read local PDB bytes and stage as if
                    # uploaded. Idempotent — examples on disk are immutable
                    # so re-running the same example_id is safe.
                    spec = reuse_token.split(":", 1)[1]  # "<tool>/<id>"
                    if "/" not in spec:
                        raise StorageError("malformed example token")
                    ex_tool, ex_id = spec.split("/", 1)
                    from shared.examples import (  # noqa: PLC0415
                        load_example, read_example_bytes,
                    )
                    entry = load_example(ex_tool, ex_id)
                    if entry is None:
                        raise StorageError("example not found")
                    example_bytes = read_example_bytes(ex_tool, ex_id)
                    if example_bytes is None:
                        raise StorageError("example file missing")
                    reuse_resolved_bytes = example_bytes
                    staged_filename = entry.get(
                        "filename", f"{ex_id}.pdb"
                    )
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=example_bytes,
                        content_type="chemical/x-pdb",
                    )
                elif reuse_token.startswith("alphafold:"):
                    # AlphaFold fallback: pdb_bytes was already populated by
                    # the AF fetch above the preflight gate (so the gate
                    # could vote on the actual model). Stage those bytes as
                    # if the user had uploaded them.
                    if pdb_bytes is None:
                        raise StorageError("alphafold fetch produced no bytes")
                    staged_filename = (
                        converted_filename or f"AF-{af_accession_for_reuse}.pdb"
                    )
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=pdb_bytes,
                        content_type="chemical/x-pdb",
                    )
                elif reuse_token.startswith("resample:"):
                    # AF2-resample chain: decode the source fold job's
                    # predicted PDB (stored as base64 in
                    # ``result.pdb_b64`` across AF2/ColabFold/ESMFold)
                    # and stage it as a fresh MPNN input PDB. The
                    # source-tool gate prevents stuffing a non-fold
                    # job id into the token to read its result blob.
                    import base64  # noqa: PLC0415
                    src_job_id = reuse_token.split(":", 1)[1]
                    src = get_job(src_job_id, user_id=ctx.user_id)
                    if src is None:
                        raise StorageError("source fold job not found")
                    if not _resample.can_resample(src.tool):
                        raise StorageError(
                            "source job is not a fold predictor"
                        )
                    src_pdb_b64 = (
                        (src.result or {}).get("pdb_b64") or ""
                    ).strip()
                    if not src_pdb_b64:
                        raise StorageError(
                            "source job has no predicted PDB"
                        )
                    try:
                        src_pdb_bytes = base64.b64decode(
                            src_pdb_b64, validate=True
                        )
                    except Exception as exc:
                        raise StorageError(
                            f"predicted PDB decode failed: {exc}"
                        )
                    reuse_resolved_bytes = src_pdb_bytes
                    staged_filename = (
                        f"predicted-{src.tool}-{src.id[:8]}.pdb"
                    )
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=src_pdb_bytes,
                        content_type="chemical/x-pdb",
                    )

                presigned_url = presigned_input_url(
                    staged_path, expires_seconds=7200
                )
                # Persist the storage path + filename on the job row so a
                # future clone can re-use the file without re-uploading.
                update_inputs(
                    job.id,
                    {
                        **inputs,
                        "_pdb_storage_path": staged_path,
                        "_pdb_filename": staged_filename,
                    },
                )
            except StorageError as exc:
                mark_failed(
                    job.id,
                    error={"bucket": "storage", "detail": str(exc)},
                )
                if hold_tx_id:
                    try:
                        wallet_release_hold(
                            hold_tx_id, reason="storage_failure"
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold on storage error "
                            "raised for hold=%s",
                            hold_tx_id, exc_info=True,
                        )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=f"Upload failed: {exc}",
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )

        # ---- Reuse-token inspection + hard-gate (gap 2) ----
        # Fresh uploads are inspected + gated at the boundary above, but the
        # reuse tokens (job:/handoff:/example:/resample:) stage bytes that
        # skipped both. Re-check the RESOLVED bytes here before any Modal
        # call so a mismatch (wrong chain, oversized, corrupt predicted PDB
        # piped into MPNN) is flagged upfront. alphafold: already populated
        # pdb_bytes and ran the hard-gate above, so it is excluded.
        if (
            needs_pdb
            and pdb_bytes is None
            and reuse_token
            and not reuse_token.startswith("alphafold:")
            and staged_path
        ):
            check_bytes = reuse_resolved_bytes
            if check_bytes is None:
                # job: / handoff: copied storage-to-storage; read the staged
                # object back to verify it. Best-effort: a verification-only
                # download hiccup must not block an already-staged reuse.
                try:
                    check_bytes = download_input(staged_path)
                except StorageError:
                    logger.warning(
                        "tool_submit: could not download staged reuse PDB "
                        "for verification job=%s path=%s",
                        job.id, staged_path, exc_info=True,
                    )
                    check_bytes = None
            if check_bytes is not None:
                reuse_binder_max, reuse_num_designs = (
                    _parse_preflight_size_params(inputs)
                )
                reuse_err = _verify_reuse_pdb_bytes(
                    adapter, check_bytes,
                    target_chain=(inputs.get("target_chain") or "").strip(),
                    hotspots=inputs.get("hotspot_residues") or [],
                    filename=staged_filename or "input.pdb",
                    binder_max_aa=reuse_binder_max,
                    num_designs=reuse_num_designs,
                )
                if reuse_err:
                    mark_failed(
                        job.id,
                        error={"bucket": "preflight", "detail": reuse_err},
                    )
                    if hold_tx_id:
                        try:
                            wallet_release_hold(
                                hold_tx_id, reason="reuse_preflight_failed",
                            )
                        except Exception:
                            logger.warning(
                                "tool_submit: release_hold on reuse preflight "
                                "failure raised for hold=%s",
                                hold_tx_id, exc_info=True,
                            )
                    return render_template(
                        adapter.form_template,
                        adapter=adapter,
                        error=reuse_err,
                        pre_fill=inputs,
                        pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )

        job_spec = adapter.build_payload(inputs, presigned_url)
        webhook_url = url_for(
            "modal_result",
            job_id=job.id,
            job_token=job.job_token,
            _external=True,
        )
        upload_urls_endpoint = url_for(
            "upload_urls",
            job_id=job.id,
            job_token=job.job_token,
            _external=True,
        )

        try:
            submit_result = modal_client.submit(
                adapter.slug,
                preset.slug,
                inputs={
                    **job_spec,
                    "_input_pdb_url": presigned_url,
                    "_input_presigned_url": presigned_url,
                    "_upload_urls_endpoint": upload_urls_endpoint,
                },
                job_id=job.id,
                job_token=job.job_token,
                webhook_url=webhook_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Modal submit failed for job %s", job.id)
            mark_failed(
                job.id,
                error={"bucket": "modal-submit", "detail": str(exc)},
            )
            if hold_tx_id:
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="modal_submit_failure"
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold on modal submit "
                        "failure raised for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not submit to the GPU pool. Your wallet was "
                    "not charged. Try again or contact support at "
                    + (
                        os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip()
                        or "info@ranomics.com"
                    )
                    + "."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        set_modal_call(job.id, submit_result["function_call_id"])

        # D3 funnel fire. Distinguish the user's first-ever submission
        # from their nth so the dashboard can read activation rate
        # directly. list_jobs_paginated returns a count that already
        # includes the row we just created, so total == 1 -> first.
        # Best-effort: a count failure must not stall the redirect.
        try:
            _, total_jobs = list_jobs_paginated(
                ctx.user_id, page=1, page_size=1,
            )
            is_first = total_jobs == 1
        except Exception:
            is_first = False
        from shared.events import EVENTS, emit  # noqa: PLC0415
        emit(
            EVENTS.FIRST_JOB_SUBMITTED if is_first
            else EVENTS.NTH_JOB_SUBMITTED,
            user_id=ctx.user_id,
            properties={
                "tool": adapter.slug,
                "preset": preset.slug,
                "is_pilot": preset.slug == "pilot",
                "job_id": job.id,
            },
        )

        return redirect(url_for("job_detail", job_id=job.id))

    @flask_app.route("/jobs", methods=["GET"])
    @login_required
    def jobs_list():
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        page_size = 25
        # C4 — campaign filter. ``?campaign=<label>`` narrows the list to
        # a single campaign; ``?campaign=__uncategorized__`` selects the
        # rows where campaign_label IS NULL. Missing means "all rows".
        raw_campaign = request.args.get("campaign")
        campaign_filter: str | None
        if raw_campaign is None:
            campaign_filter = None
        elif raw_campaign == "__uncategorized__":
            campaign_filter = ""
        else:
            campaign_filter = raw_campaign.strip() or None
        jobs, total = list_jobs_paginated(
            ctx.user_id,
            page=page,
            page_size=page_size,
            campaign_label=campaign_filter,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            redirect_args = {"page": total_pages}
            if raw_campaign is not None:
                redirect_args["campaign"] = raw_campaign
            return redirect(url_for("jobs_list", **redirect_args))
        # Group rows by campaign_label for the per-campaign h3 headers.
        # Uncategorized rows sort last. Preserves the newest-first order
        # within each group since list_jobs_paginated already ordered by
        # created_at DESC.
        groups: dict[str, list] = {}
        for j in jobs:
            key = j.campaign_label or "__uncategorized__"
            groups.setdefault(key, []).append(j)
        campaign_groups = []
        for key in sorted(
            (k for k in groups if k != "__uncategorized__"),
            key=str.lower,
        ):
            campaign_groups.append({"label": key, "jobs": groups[key]})
        if "__uncategorized__" in groups:
            campaign_groups.append({
                "label": None, "jobs": groups["__uncategorized__"]
            })
        all_campaign_labels = list_campaign_labels_for_user(ctx.user_id)
        return render_template(
            "jobs_list.html",
            jobs=jobs,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            campaign_groups=campaign_groups,
            all_campaign_labels=all_campaign_labels,
            selected_campaign=raw_campaign,
        )

    @flask_app.route("/jobs/compare", methods=["GET"])
    @login_required
    def jobs_compare():
        """Wave 3B cross-run compare: render selected jobs side-by-side.

        Accepts ``ids=a,b,c`` or repeated ``ids=a&ids=b``. Owner-scoped.
        """
        from shared.jobs import list_jobs_by_ids  # local import avoids cycle
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        raw = request.args.getlist("ids")
        if len(raw) == 1 and "," in raw[0]:
            raw = [x.strip() for x in raw[0].split(",") if x.strip()]
        raw = [x for x in raw if x]
        if len(raw) < 2:
            return redirect(url_for("jobs_list"))
        # Bumped from 6 to 10 so a C3 "Re-fold top 10" lands cleanly in
        # a single comparison view.
        jobs = list_jobs_by_ids(ctx.user_id, raw[:10])
        columns = []
        for j in jobs:
            adapter = tool_base.get(j.tool)
            columns.append({
                "job": j,
                "tool_label": adapter.label if adapter else j.tool,
            })
        return render_template("jobs_compare.html", columns=columns)

    @flask_app.route("/jobs/<job_id>", methods=["GET"])
    @login_required
    def job_detail(job_id: str):
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        adapter = tool_base.get(job.tool)
        preset_obj = adapter.preset_for(job.preset) if adapter else None

        # Phase 4 cross-tool handoff: only offer the buttons when the
        # source job staged a reusable PDB (has _pdb_storage_path) and
        # finished successfully. Skip the current tool — "send to self"
        # is what Clone is for. Skip any adapter whose input contract is
        # not PDB-based — e.g. D2 AF2, which takes FASTA. The generic
        # ``from_job`` flow only ports a PDB reuse token + chain +
        # hotspots; offering AF2 as a handoff target would drop the user
        # on a form that cannot consume the handoff (Codex P2).
        NON_PDB_INPUT_TOOLS = frozenset({"af2"})
        send_target_tools: list[dict] = []
        if (
            job.status == "succeeded"
            and (job.inputs or {}).get("_pdb_storage_path")
        ):
            for other in tool_base.all_adapters():
                if other.slug == job.tool:
                    continue
                if other.slug in NON_PDB_INPUT_TOOLS:
                    continue
                if not tool_enabled(other.slug):
                    continue
                send_target_tools.append({
                    "slug": other.slug,
                    "label": other.label,
                    "url": url_for(
                        "tool_form", tool=other.slug, from_job=job.id
                    ),
                })

        # D4 — share button gating. Only resolve user_metadata for the
        # terminal-success branch where the share button could render;
        # the admin.list_users round-trip is wasted on pending/running.
        share_allowed = False
        if job.status == "succeeded":
            from shared.jobs import resolve_user_email_and_meta  # noqa: PLC0415
            _email, user_meta = resolve_user_email_and_meta(ctx.user_id)
            share_allowed = _share_allowed(user_meta)

        return render_template(
            "job_detail.html",
            job=job,
            tool_label=adapter.label if adapter else job.tool,
            tool_results_partial=(
                adapter.results_partial
                if adapter and adapter.results_partial
                else "tools/_default_results.html"
            ),
            is_long_running=bool(preset_obj and preset_obj.long_running),
            user_email=session.get("user_email") or "",
            send_target_tools=send_target_tools,
            share_allowed=share_allowed,
        )

    @flask_app.route("/jobs/<job_id>/status.json", methods=["GET"])
    @login_required
    def job_status(job_id: str):
        ctx = load_user_context()
        if ctx is None:
            return jsonify({"error": "unauthenticated"}), 401
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return jsonify({"error": "not_found"}), 404

        # If the job still thinks it is pending/running, poll Modal once
        # so terminal transitions are detected even when the webhook
        # callback has not fired (e.g. inline smoke-tier returns).
        if job.status in ("pending", "running") and job.modal_function_call_id:
            poll = modal_client.poll(job.modal_function_call_id)
            if poll["status"] == "succeeded":
                complete_job(
                    job.id,
                    terminal_status="succeeded",
                    result=poll["result"] or {},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                job = get_job(job_id, user_id=ctx.user_id)
            elif poll["status"] == "failed":
                complete_job(
                    job.id,
                    terminal_status="failed",
                    error={"bucket": "pipeline", "detail": poll.get("error") or ""},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                job = get_job(job_id, user_id=ctx.user_id)
            elif poll["status"] == "running" and job.status == "pending":
                mark_running(job.id)
                job = get_job(job_id, user_id=ctx.user_id)

        inputs = job.inputs or {}
        partials = inputs.get("_partial_candidates") or []
        if not isinstance(partials, list):
            partials = []
        passed = 0
        for cand in partials:
            if not isinstance(cand, dict):
                continue
            fs = str(cand.get("filter_status") or "").strip().lower()
            if fs == "pass":
                passed += 1
        return jsonify(
            {
                "id": job.id,
                "status": job.status,
                "tool": job.tool,
                "preset": job.preset,
                "progress": inputs.get("_progress") or {},
                "partial_candidates": partials,
                "passed_count": passed,
                "gpu_seconds_used": job.gpu_seconds_used,
                "started_at": getattr(job, "started_at", None),
            }
        )

    @flask_app.route("/jobs/<job_id>/refold", methods=["POST"])
    @login_required
    @idempotent()
    def job_refold(job_id: str):
        """C3 — spawn N orthogonal second-opinion folds on the top N
        designs from a binder-design job and redirect the user to
        /jobs/compare with the new IDs.

        Form fields:
          dest_tool  — "colabfold" or "esmfold"
          n          — number of top designs to refold (clamped to
                       refold.MAX_REFOLD_N, default refold.DEFAULT_REFOLD_N).

        Each spawned job runs in the destination tool's "standalone"
        preset with the candidate's binder sequence as a single-monomer
        FASTA. Per-job wallet billing happens on the existing
        completion-side path (charge_for_job); a fresh signup credit
        covers a top-5 refold many times over.
        """
        from shared.refold import (  # noqa: PLC0415
            DEFAULT_REFOLD_N, MAX_REFOLD_N, can_refold,
            extract_top_n_sequences,
        )
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        src = get_job(job_id, user_id=ctx.user_id)
        if src is None:
            return render_template("404.html"), 404
        if src.status != "succeeded":
            return redirect(url_for("job_detail", job_id=job_id))

        dest_tool = (request.form.get("dest_tool") or "").strip()
        try:
            n_raw = int(request.form.get("n") or DEFAULT_REFOLD_N)
        except ValueError:
            n_raw = DEFAULT_REFOLD_N
        n = max(1, min(n_raw, MAX_REFOLD_N))

        if not can_refold(src.tool, dest_tool):
            return redirect(url_for("job_detail", job_id=job_id))
        if not tool_enabled(dest_tool):
            return redirect(url_for("job_detail", job_id=job_id))
        dest_adapter = tool_base.get(dest_tool)
        if dest_adapter is None:
            return redirect(url_for("job_detail", job_id=job_id))

        seqs = extract_top_n_sequences(src.result or {}, n)
        if not seqs:
            # Source job has no extractable sequences. Bail back to the
            # source detail page; the calling button is only rendered
            # when the candidate table is non-empty, so this should be
            # rare (e.g. partially failed runs that completed early).
            return redirect(url_for("job_detail", job_id=job_id))

        # Spawn one job per sequence. The shared campaign_label lets
        # /jobs/compare and future C4 campaign work group them later.
        campaign_label = f"validation-of-{src.id[:8]}"
        spawned: list[str] = []
        for seq in seqs:
            # Build the inputs in the shape the destination adapter's
            # validate() would produce after parsing the form. This
            # bypasses validate() since we control the FASTA content
            # entirely — every sequence here came out of a previously
            # validated job's result candidates.
            if dest_tool == "colabfold":
                inputs = {
                    "preset": "standalone",
                    "fasta_text": (
                        f">{seq.fasta_header}\n{seq.sequence}"
                    ),
                    "num_recycles": 1,
                    "use_templates": False,
                    "target": (
                        f"Refold of {src.tool} job {src.id[:8]}, "
                        f"rank {seq.rank}"
                    ),
                    "_refold_of_job_id": src.id,
                    "_campaign_label": campaign_label,
                }
            elif dest_tool == "esmfold":
                inputs = {
                    "preset": "standalone",
                    "fasta_text": (
                        f">{seq.fasta_header}\n{seq.sequence}"
                    ),
                    "target": (
                        f"Refold of {src.tool} job {src.id[:8]}, "
                        f"rank {seq.rank}"
                    ),
                    "_refold_of_job_id": src.id,
                    "_campaign_label": campaign_label,
                }
            elif dest_tool == "boltz2":
                # Boltz-2 cofold against the SOURCE job's original antigen.
                # Reuses the source's already-staged target PDB rather than
                # re-uploading. The source job must have requires_pdb=True
                # (all SOURCE_TOOLS do), so _pdb_storage_path is guaranteed.
                src_inputs = src.inputs or {}
                staged_path = (src_inputs.get("_pdb_storage_path") or "").strip()
                if not staged_path:
                    logger.warning(
                        "refold->boltz2: source job %s has no _pdb_storage_path",
                        src.id,
                    )
                    continue
                try:
                    src_presigned = presigned_input_url(
                        staged_path, expires_seconds=7200,
                    )
                except Exception:
                    logger.exception(
                        "refold->boltz2: presigned_input_url failed for %s",
                        staged_path,
                    )
                    continue
                src_chain = str(src_inputs.get("target_chain") or "A").strip() or "A"
                # SOURCE_TOOLS all persist hotspot_residues as list[int] in
                # their validate() output; tolerate a string from any
                # future adapter that drops the parsing.
                raw_hotspots = src_inputs.get("hotspot_residues") or []
                if isinstance(raw_hotspots, str):
                    parsed: list[int] = []
                    for tok in raw_hotspots.replace(";", ",").split(","):
                        tok = tok.strip()
                        if tok:
                            try:
                                parsed.append(int(tok))
                            except ValueError:
                                pass
                    raw_hotspots = parsed
                hotspot_list = [int(x) for x in raw_hotspots if str(x).strip()]
                inputs = {
                    "preset": "standalone",
                    "target_chain": src_chain,
                    "hotspot_residues": hotspot_list,
                    "binder_sequences": [
                        {"name": seq.fasta_header, "sequence": seq.sequence},
                    ],
                    "parameters": {"n_designs_total": 1},
                    "target": (
                        f"Refold of {src.tool} job {src.id[:8]}, "
                        f"rank {seq.rank}"
                    ),
                    "_refold_of_job_id": src.id,
                    "_campaign_label": campaign_label,
                    "_pdb_storage_path": staged_path,
                    "_input_pdb_url": src_presigned,
                    "_input_presigned_url": src_presigned,
                }
            else:
                # can_refold gate above should make this unreachable.
                continue

            # C4 — promote the per-batch label to the first-class column so
            # /jobs can group the refold batch without sniffing inputs JSON.
            # The legacy inputs._campaign_label key is kept for backward
            # compatibility with rows older than the C4 migration.
            job = create_job(
                user_id=ctx.user_id,
                tool=dest_adapter.slug,
                preset="standalone",
                inputs=inputs,
                campaign_label=campaign_label,
            )
            if job is None:
                logger.warning(
                    "refold: create_job failed for rank %s (%s -> %s)",
                    seq.rank, src.tool, dest_tool,
                )
                continue

            try:
                job_spec = dest_adapter.build_payload(inputs, "")
                webhook_url = url_for(
                    "modal_result",
                    job_id=job.id,
                    job_token=job.job_token,
                    _external=True,
                )
                # Boltz-2 needs the antigen presigned URL and the
                # per-design upload endpoint (partial-results streaming).
                # ColabFold/ESMFold ignore both because their FASTA
                # travels inline in job_spec.
                submit_inputs: dict = dict(job_spec)
                if dest_tool == "boltz2":
                    submit_inputs["_input_pdb_url"] = inputs.get(
                        "_input_presigned_url", ""
                    )
                    submit_inputs["_input_presigned_url"] = inputs.get(
                        "_input_presigned_url", ""
                    )
                    submit_inputs["_upload_urls_endpoint"] = url_for(
                        "upload_urls",
                        job_id=job.id,
                        job_token=job.job_token,
                        _external=True,
                    )
                modal_client.submit(
                    dest_adapter.slug,
                    "standalone",
                    inputs=submit_inputs,
                    job_id=job.id,
                    job_token=job.job_token,
                    webhook_url=webhook_url,
                )
                spawned.append(job.id)
            except Exception:
                logger.exception(
                    "refold: modal submit failed for job %s", job.id,
                )
                mark_failed(
                    job.id,
                    error={
                        "bucket": "modal-submit",
                        "detail": "refold spawn failed",
                    },
                )

        if not spawned:
            return redirect(url_for("job_detail", job_id=job_id))

        # D3 funnel fire. The refold is a per-batch handoff from the
        # source tool to the destination predictor; ``n`` is the actual
        # number of jobs that landed in Supabase, not the form-requested
        # count (a few candidates may have lacked extractable sequences).
        from shared.events import EVENTS, emit  # noqa: PLC0415
        emit(
            EVENTS.REFOLD_SPAWNED,
            user_id=ctx.user_id,
            properties={
                "source_tool": src.tool,
                "dest_tool": dest_tool,
                "n": len(spawned),
                "source_job_id": src.id,
            },
        )

        return redirect(
            url_for("jobs_compare", ids=",".join(spawned))
        )

    @flask_app.route("/jobs/<job_id>/cancel", methods=["POST"])
    @login_required
    @idempotent()
    def job_cancel(job_id: str):
        """User-initiated cancel of a pending/running job.

        Best-effort Modal cancel, wallet hold released, row transitions
        to status='cancelled'. Safe to call repeatedly — terminal jobs
        return an error_code without mutating state.
        """
        ctx = load_user_context()
        if ctx is None:
            return jsonify({"error": "unauthenticated"}), 401
        job, err = cancel_job(
            job_id, user_id=ctx.user_id, modal_client=modal_client
        )
        if job is None:
            code = 404 if err == "not_found" else 409
            return jsonify({"error": err or "cancel_failed"}), code
        return jsonify(
            {
                "id": job.id,
                "status": job.status,
            }
        )

    @flask_app.route("/jobs/<job_id>/share", methods=["POST"])
    @login_required
    def job_share(job_id: str):
        """D4 share payload for a finished job.

        Returns ``{url, og_title, og_description, og_image}`` for the
        caller to drop into a LinkedIn / X compose box. The URL points
        back at ``/jobs/<id>`` with a ``utm_source=share`` trio so the
        cross-domain analytics report can attribute the inbound click.

        Gated on the per-user opt-in
        ``auth.users.user_metadata.allow_share`` (default False) so a
        share link is never generated for an account that has not
        explicitly enabled the feature.
        """
        from shared.jobs import resolve_user_email_and_meta  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return jsonify({"error": "unauthenticated"}), 401
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return jsonify({"error": "not_found"}), 404
        _email, user_meta = resolve_user_email_and_meta(ctx.user_id)
        if not _share_allowed(user_meta):
            return jsonify({"error": "share_not_enabled"}), 403

        base_url = os.environ.get(
            "PUBLIC_BASE_URL", "https://tools.ranomics.com"
        ).rstrip("/")
        tool_slug = job.tool or ""
        share_url = (
            f"{base_url}/jobs/{job.id}"
            f"?utm_source=share&utm_medium=user-share"
            f"&utm_campaign={tool_slug}"
        )
        adapter = tool_base.get(tool_slug)
        tool_label = adapter.label if adapter else (tool_slug or "tool")
        top_score = _top_score_for_share(job)
        if top_score is None:
            og_title = (
                f"I designed a binder with {tool_label} on "
                f"tools.ranomics.com"
            )
        else:
            og_title = (
                f"I designed a binder with {tool_label} on "
                f"tools.ranomics.com. Top score {top_score}."
            )
        og_description = (
            "Ranomics tools-hub runs the same GPU pipelines used in "
            "production protein design."
        )
        og_image = url_for(
            "static", filename="og-image.png", _external=True,
        )
        return jsonify({
            "url":            share_url,
            "og_title":       og_title,
            "og_description": og_description,
            "og_image":       og_image,
        })

    # ------------------------------------------------------------------
    # Public tool comparison matrix + campaign intake stub
    # ------------------------------------------------------------------

    @flask_app.route("/tools", methods=["GET"])
    def tools_comparison():
        """Public discovery hub for the full tool catalog.

        Renders the iteration-loop framing, a category-grouped tile
        grid, and the comparison matrix at the bottom for power users.
        Catalog includes both hardcoded tools (Epitope Scout, Binder
        Developability Scout, Library Planner) and flag-enabled GPU
        adapters.
        """
        catalog = _build_tools_catalog()

        # Group catalog into workflow-stage sections in a stable order.
        # The order mirrors the iteration loop a scientist walks through:
        # scope → design (4 scaffold-class buckets) → predict → QC.
        category_order = (
            "Scope the target",
            "De novo minibinders",
            "Antibodies (VHH)",
            "Dual capabilities (minibinder + antibody scaffolds)",
            "Sequence on a backbone",
            "Structure prediction",
            "Check developability",
            "Other",
        )
        grouped: list[tuple[str, list[dict]]] = []
        for category in category_order:
            members = [t for t in catalog if t.get("category") == category]
            if members:
                grouped.append((category, members))

        breadcrumbs = [
            {"name": "Home", "url": url_for("index", _external=True)},
            {"name": "All tools", "url": url_for(
                "tools_comparison", _external=True
            )},
        ]
        return render_template(
            "tools/comparison.html",
            tools=catalog,
            grouped=grouped,
            authenticated=bool(session.get("user_email")),
            breadcrumbs=breadcrumbs,
        )

    # ------------------------------------------------------------------
    # Export routes — /jobs/<id>/export.{csv,fasta,zip}
    # ------------------------------------------------------------------

    @flask_app.route("/jobs/<job_id>/export.csv", methods=["GET"])
    @login_required
    def export_csv(job_id: str):
        import csv  # noqa: PLC0415
        import io   # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        candidates = (job.result or {}).get("candidates", [])
        buf = io.StringIO()
        all_score_keys: list[str] = []
        for cand in candidates:
            for k in (cand.get("scores") or {}):
                if k not in all_score_keys:
                    all_score_keys.append(k)
        writer = csv.DictWriter(buf, fieldnames=["rank", "pdb_key"] + all_score_keys,
                                extrasaction="ignore")
        writer.writeheader()
        for i, cand in enumerate(candidates):
            scores = cand.get("scores") or {}
            row = {"rank": cand.get("rank", i + 1), "pdb_key": cand.get("pdb_key", "")}
            row.update(scores)
            writer.writerow(row)
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_scores.csv"},
        )

    @flask_app.route("/jobs/<job_id>/export.fasta", methods=["GET"])
    @login_required
    def export_fasta(job_id: str):
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        result = job.result or {}
        candidates = result.get("candidates", [])
        mpnn_sequences = result.get("sequences", [])
        lines: list[str] = []
        # Binder-design tools (rfantibody/bindcraft/boltzgen/pxdesign)
        # return ``candidates`` (PDB + docked pose + scores). MPNN is a
        # sequence-design primitive and returns ``sequences`` (seq +
        # score + recovery), so the header+body shape has to differ.
        for i, cand in enumerate(candidates):
            seq = cand.get("sequence") or cand.get("binder_sequence") or ""
            if not seq:
                continue
            pdb_key = cand.get("pdb_key", f"candidate_{i + 1}")
            rank = cand.get("rank", i + 1)
            lines.append(f">rank{rank}_{pdb_key}")
            # wrap at 80 chars
            for start in range(0, len(seq), 80):
                lines.append(seq[start:start + 80])
        for i, seq_obj in enumerate(mpnn_sequences):
            seq = seq_obj.get("seq") or ""
            if not seq:
                continue
            header_parts = [f">mpnn_rank{i + 1}"]
            score = seq_obj.get("score")
            recovery = seq_obj.get("recovery")
            if score is not None:
                header_parts.append(f"score={score}")
            if recovery is not None:
                header_parts.append(f"recovery={recovery}")
            lines.append(" ".join(header_parts))
            for start in range(0, len(seq), 80):
                lines.append(seq[start:start + 80])
        if not lines:
            return Response(
                "# No sequences found in this job's output.\n",
                mimetype="text/plain",
                headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
            )
        return Response(
            "\n".join(lines) + "\n",
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
        )

    @flask_app.route("/api/jobs/<job_id>/pdb/<path:filename>", methods=["GET"])
    @login_required
    def job_candidate_pdb(job_id: str, filename: str):
        """Serve a candidate PDB by filename — Storage first, inline b64 fallback.

        Two resolution paths, owner-scoped via ``get_job(user_id=...)``:

        1. ``tool-outputs/{user_id}/{job_id}/designs/<filename>`` — bytes
           served from Storage (server-side proxy, not 302, to keep the
           3D viewer's JS fetch on a same-origin URL).
        2. Inline ``tool_jobs.result.candidates[?].pdb_content_b64`` — scan
           candidates for one whose ``pdb_key`` matches and return the
           decoded bytes.

        Returns 404 with a plain-text body when neither resolves. The
        ``Content-Disposition`` header lets ``<a download="...">`` render
        the right filename on save.
        """
        import base64  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404

        # Path 1: tool-outputs Storage.
        try:
            if output_exists(
                user_id=ctx.user_id, job_id=job_id, filename=filename
            ):
                data = download_output(
                    user_id=ctx.user_id,
                    job_id=job_id,
                    filename=filename,
                )
                return Response(
                    data,
                    mimetype="chemical/x-pdb",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{filename}"'
                        )
                    },
                )
        except StorageError:
            logger.warning(
                "Storage resolve failed for %s/%s; falling back to inline.",
                job_id, filename, exc_info=True,
            )

        # Path 2: inline pdb_content_b64 fallback (legacy / boltzgen path).
        # Compare on basename so a pdb_key of "designs/design_0.pdb" matches
        # a request URL of either "designs/design_0.pdb" or "design_0.pdb".
        import posixpath  # noqa: PLC0415
        target_basename = posixpath.basename(filename) or filename
        candidates = (job.result or {}).get("candidates", []) or []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            cand_basename = posixpath.basename(cand.get("pdb_key") or "")
            if cand_basename != target_basename:
                continue
            encoded = cand.get("pdb_content_b64")
            if not encoded:
                break
            try:
                data = base64.b64decode(encoded, validate=True)
            except Exception:
                return Response(
                    "# Malformed PDB payload.\n",
                    mimetype="text/plain",
                    status=500,
                )
            return Response(
                data,
                mimetype="chemical/x-pdb",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{filename}"'
                    )
                },
            )

        return Response(
            "# Candidate PDB not found.\n",
            mimetype="text/plain",
            status=404,
        )

    @flask_app.route("/jobs/<job_id>/af2.pdb", methods=["GET"])
    @login_required
    def af2_download_pdb(job_id: str):
        """Stream the AF2 predicted structure as a .pdb download.

        D2 atomic tool. Result payload carries ``pdb_b64`` (base64-encoded
        PDB text); decode and return as text/plain for browser-friendly
        Save As. Owner-scoped via the get_job RLS wrapper.
        """
        import base64  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None or job.tool != "af2":
            return render_template("404.html"), 404
        pdb_b64 = (job.result or {}).get("pdb_b64") or ""
        if not pdb_b64:
            return Response(
                "# No PDB in this job's result.\n",
                mimetype="text/plain",
                status=404,
            )
        try:
            pdb_bytes = base64.b64decode(pdb_b64, validate=True)
        except Exception:
            return Response(
                "# Malformed PDB payload.\n",
                mimetype="text/plain",
                status=500,
            )
        return Response(
            pdb_bytes,
            mimetype="chemical/x-pdb",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=af2_{job_id[:8]}.pdb"
                )
            },
        )

    @flask_app.route("/jobs/<job_id>/af2_pae.npy", methods=["GET"])
    @login_required
    def af2_download_pae(job_id: str):
        """Stream the AF2 PAE matrix as a .npy download.

        D2 atomic tool. Result payload carries ``pae_matrix_b64`` which
        is a base64-encoded numpy .npy file (written by run_pipeline.py
        via ``numpy.save``). We hand it back as-is — the client can
        ``numpy.load`` it directly.
        """
        import base64  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None or job.tool != "af2":
            return render_template("404.html"), 404
        pae_b64 = (job.result or {}).get("pae_matrix_b64") or ""
        if not pae_b64:
            return Response(
                "# No PAE matrix in this job's result.\n",
                mimetype="text/plain",
                status=404,
            )
        try:
            pae_bytes = base64.b64decode(pae_b64, validate=True)
        except Exception:
            return Response(
                "# Malformed PAE payload.\n",
                mimetype="text/plain",
                status=500,
            )
        return Response(
            pae_bytes,
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=af2_{job_id[:8]}_pae.npy"
                )
            },
        )

    @flask_app.route("/jobs/<job_id>/export.zip", methods=["GET"])
    @login_required
    def export_zip(job_id: str):
        """Bundle every candidate PDB into a ZIP.

        Two resolution paths per candidate, mirroring the per-design
        endpoint:

        1. Inline ``pdb_content_b64`` — decoded and written directly.
        2. ``tool-outputs`` Storage — bytes fetched server-side and
           written. Used when the pipeline POSTed to the upload-URLs
           endpoint rather than emitting b64 in the result row.

        Candidates that resolve via neither path are silently skipped
        (rather than failing the whole archive).
        """
        import base64   # noqa: PLC0415
        import io       # noqa: PLC0415
        import zipfile  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        candidates = (job.result or {}).get("candidates", []) or []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, cand in enumerate(candidates):
                if not isinstance(cand, dict):
                    continue
                filename = cand.get("pdb_key") or f"candidate_{i + 1}.pdb"
                data = None

                # Path 1: inline b64.
                encoded = cand.get("pdb_content_b64")
                if encoded:
                    try:
                        data = base64.b64decode(encoded)
                    except Exception:
                        data = None

                # Path 2: tool-outputs Storage.
                if data is None and cand.get("pdb_key"):
                    try:
                        data = download_output(
                            user_id=ctx.user_id,
                            job_id=job_id,
                            filename=cand["pdb_key"],
                        )
                    except StorageError:
                        logger.warning(
                            "export_zip: storage miss for %s/%s",
                            job_id, filename, exc_info=True,
                        )
                        data = None

                if data is None:
                    continue
                zf.writestr(filename, data)
        buf.seek(0)
        return Response(
            buf.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_pdbs.zip"},
        )

    # ------------------------------------------------------------------
    # Campaign routes — /campaigns/*
    # ------------------------------------------------------------------

    @flask_app.route("/campaigns/submit", methods=["POST"])
    @login_required
    def campaigns_submit():
        import json  # noqa: PLC0415
        from shared.campaigns import create_campaign  # noqa: PLC0415
        from shared.email import send_campaign_submitted_emails  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        source_job_id = request.form.get("source_job_id", "").strip()
        target_name   = request.form.get("target_name", "").strip()
        assay_type    = request.form.get("assay_type", "yeast_display").strip()
        budget_band   = request.form.get("budget_band", "pilot").strip()
        target_context = request.form.get("target_context", "").strip()

        raw_kd = request.form.get("affinity_goal_kd_nm", "").strip()
        affinity_goal_kd_nm = float(raw_kd) if raw_kd else None

        raw_weeks = request.form.get("timeline_weeks", "").strip()
        timeline_weeks = int(raw_weeks) if raw_weeks else None

        raw_indices = request.form.get("candidate_indices", "[]").strip()
        try:
            candidate_indices = [int(i) for i in json.loads(raw_indices)]
        except Exception:
            candidate_indices = []

        if not source_job_id or not target_name or not candidate_indices:
            return redirect(url_for("jobs_list"))

        job = get_job(source_job_id, user_id=ctx.user_id)
        if job is None:
            return redirect(url_for("jobs_list"))

        try:
            campaign = create_campaign(
                user_id=ctx.user_id,
                source_job_id=source_job_id,
                candidate_indices=candidate_indices,
                target_name=target_name,
                target_context=target_context,
                assay_type=assay_type,
                budget_band=budget_band,
                affinity_goal_kd_nm=affinity_goal_kd_nm,
                timeline_weeks=timeline_weeks,
            )
        except ValueError:
            return redirect(url_for("jobs_list"))

        if campaign is None:
            return redirect(url_for("jobs_list"))

        # Copy candidate PDBs into durable campaign bucket.
        candidates = (job.result or {}).get("candidates", [])
        try:
            stage_campaign_candidates(
                campaign_id=campaign.id,
                candidates=candidates,
                indices=candidate_indices,
            )
        except StorageError:
            logger.warning("stage_campaign_candidates failed for %s", campaign.id)

        # Emails — best-effort.
        try:
            send_campaign_submitted_emails(
                campaign=campaign,
                user_email=session.get("user_email", ""),
            )
        except Exception:
            logger.warning("campaign submit emails failed", exc_info=True)

        return redirect(url_for("campaign_detail", campaign_id=campaign.id) + "?submitted=1")

    @flask_app.route("/campaigns", methods=["GET"])
    @login_required
    def campaigns_dashboard():
        from shared.campaigns import list_user_campaigns  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        campaigns = list_user_campaigns(ctx.user_id)
        return render_template("campaigns/dashboard.html", campaigns=campaigns)

    @flask_app.route("/campaigns/<campaign_id>", methods=["GET"])
    @login_required
    def campaign_detail(campaign_id: str):
        from shared.campaigns import get_campaign  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        campaign = get_campaign(campaign_id, user_id=ctx.user_id)
        if campaign is None:
            return render_template("404.html"), 404
        submitted_flash = request.args.get("submitted") == "1"
        return render_template(
            "campaigns/detail.html",
            campaign=campaign,
            submitted_flash=submitted_flash,
        )

    # Legacy stub redirect — old results pages linked here.
    @flask_app.route("/campaigns/new", methods=["GET"])
    @login_required
    def campaigns_new_stub():
        from_job = request.args.get("from_job", "")
        if from_job:
            return redirect(url_for("job_detail", job_id=from_job))
        return redirect(url_for("campaigns_dashboard"))

    # ------------------------------------------------------------------
    # Admin routes — /admin/campaigns/*
    # ------------------------------------------------------------------

    @flask_app.route("/admin/campaigns", methods=["GET"])
    def admin_campaigns_list():
        from shared.auth import require_staff, STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import list_all_campaigns, STATUSES  # noqa: PLC0415
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404
        status_filter = request.args.get("status") or None
        campaigns = list_all_campaigns(status=status_filter)
        return render_template(
            "admin/campaigns_list.html",
            campaigns=campaigns,
            statuses=list(STATUSES),
            current_status=status_filter,
        )

    @flask_app.route("/admin/campaigns/<campaign_id>", methods=["GET"])
    def admin_campaign_detail(campaign_id: str):
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import (  # noqa: PLC0415
            get_campaign,
            STATUSES,
            API_STATUSES,
        )
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404
        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404
        flash_msg = None
        flash_kind = "success"
        if request.args.get("updated") == "1":
            flash_msg = "Status updated."
        elif request.args.get("quoted") == "1":
            flash_msg = "Quote saved."
        elif request.args.get("quote_error") == "1":
            flash_msg = "Quote could not be saved. Check the values and retry."
            flash_kind = "error"
        elif request.args.get("results_saved") == "1":
            flash_msg = "Results saved."
        elif request.args.get("results_error") == "1":
            flash_msg = "Results could not be saved. Check the files and JSON, then retry."
            flash_kind = "error"
        # API-direct (MCP/REST) campaigns live on the longer Adaptyv-style FSM;
        # web-funnel campaigns on the short one. Offer the right status set so
        # the admin form posts a value the backend will accept.
        statuses = (
            list(API_STATUSES)
            if campaign.submission_source == "api"
            else list(STATUSES)
        )
        return render_template(
            "admin/campaign_detail.html",
            campaign=campaign,
            statuses=statuses,
            flash_msg=flash_msg or None,
            flash_kind=flash_kind,
        )

    @flask_app.route("/admin/campaigns/<campaign_id>/status", methods=["POST"])
    def admin_campaign_update_status(campaign_id: str):
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import (  # noqa: PLC0415
            get_campaign,
            update_status,
            transition_api_status,
            set_campaign_admin_fields,
        )
        from shared.email import send_campaign_status_email  # noqa: PLC0415
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login"))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404

        prev_status     = campaign.status
        new_status      = request.form.get("status", "").strip()
        contact         = request.form.get("ranomics_contact", "").strip() or None
        notes_internal  = request.form.get("notes_internal", "").strip() or None

        # API-direct (MCP/REST) campaigns run on the Adaptyv-style FSM and must
        # transition through the atomic RPC (update_status only accepts the web
        # enum and would reject an API status). Per product decision the admin
        # status change does NOT fire the customer webhook or a status email —
        # the customer's agent observes it on its next status poll. Contact /
        # internal notes are persisted separately since the RPC ignores them.
        if campaign.submission_source == "api":
            if new_status and new_status != prev_status:
                try:
                    transition_api_status(
                        campaign_id, new_status=new_status, by="admin"
                    )
                except ValueError:
                    # Invalid API status — ignore and fall through to redirect.
                    pass
            set_campaign_admin_fields(
                campaign_id,
                ranomics_contact=contact,
                notes_internal=notes_internal,
            )
            return redirect(
                url_for("admin_campaign_detail", campaign_id=campaign_id)
                + "?updated=1"
            )

        try:
            updated = update_status(
                campaign_id,
                status=new_status,
                ranomics_contact=contact,
                notes_internal=notes_internal,
            )
        except ValueError:
            return redirect(url_for("admin_campaign_detail", campaign_id=campaign_id))

        if updated and updated.status != prev_status:
            # Look up submitter email via service client.
            from shared.credits import get_service_client  # noqa: PLC0415
            client = get_service_client()
            user_email_for_notify = None
            if client:
                try:
                    resp = client.auth.admin.get_user_by_id(updated.user_id)
                    user_email_for_notify = getattr(resp.user, "email", None)
                except Exception:
                    pass
            if user_email_for_notify:
                try:
                    send_campaign_status_email(
                        campaign=updated,
                        user_email=user_email_for_notify,
                        prev_status=prev_status,
                    )
                except Exception:
                    logger.warning("campaign status email failed", exc_info=True)

        return redirect(
            url_for("admin_campaign_detail", campaign_id=campaign_id) + "?updated=1"
        )

    @flask_app.route("/admin/campaigns/<campaign_id>/quote", methods=["POST"])
    def admin_campaign_save_quote(campaign_id: str):
        """Persist an operator-entered quote on an API-FSM campaign.

        Quotes apply only to API-direct (MCP/REST) campaigns; web-funnel
        rows have no quote concept and run on the short status enum, so a
        non-API row 404s. set_campaign_quote writes all quote columns (the
        form is the full source of truth). If "Move status to QuoteSent on
        save" is checked and the row is still pre-quote, we advance the FSM
        via the atomic RPC — silently, like every other admin transition
        (no webhook/email; the customer's agent observes it on its next
        poll). Phase 3 adds opt-in customer notification.
        """
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import (  # noqa: PLC0415
            get_campaign,
            set_campaign_quote,
            transition_api_status,
        )
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login"))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404
        if campaign.submission_source != "api":
            return render_template("404.html"), 404

        # Authoritative total. Blank -> None (and possibly summed below).
        raw_total = request.form.get("quote_total_usd", "").strip()
        total_usd = None
        if raw_total:
            try:
                parsed = float(raw_total)
            except ValueError:
                parsed = None
            if parsed is not None and parsed >= 0:
                total_usd = parsed

        # Line items come in as three parallel arrays (one row each). Each
        # rendered row always emits all three inputs, so the lists stay
        # index-aligned; drop rows that are entirely blank.
        names = request.form.getlist("line_name")
        amounts = request.form.getlist("line_amount")
        line_notes = request.form.getlist("line_notes")
        line_items: list[dict] = []
        for i, raw_name in enumerate(names):
            name = (raw_name or "").strip()
            raw_amt = (amounts[i] if i < len(amounts) else "").strip()
            note = (line_notes[i] if i < len(line_notes) else "").strip()
            if not name and not raw_amt and not note:
                continue
            item: dict = {"name": name}
            if raw_amt:
                try:
                    amt = float(raw_amt)
                except ValueError:
                    amt = None
                if amt is not None and amt >= 0:
                    item["amount_usd"] = amt
            if note:
                item["notes"] = note
            line_items.append(item)

        # Convenience: no explicit total but the line items carry amounts ->
        # use their sum so the customer still sees a total.
        if total_usd is None and line_items:
            summed = sum(it["amount_usd"] for it in line_items if "amount_usd" in it)
            if summed > 0:
                total_usd = float(summed)

        # A bare date input means "valid through end of that day" (UTC).
        # Validate it here: an invalid/forged value must NOT reach the
        # timestamptz column, where a cast error would be swallowed by
        # set_campaign_quote and look like a silent no-save.
        from datetime import date as _date  # noqa: PLC0415
        raw_valid = request.form.get("quote_valid_until", "").strip()
        valid_until = None
        if raw_valid:
            try:
                parsed_valid = _date.fromisoformat(raw_valid)
            except ValueError:
                return redirect(
                    url_for("admin_campaign_detail", campaign_id=campaign_id)
                    + "?quote_error=1"
                )
            valid_until = f"{parsed_valid.isoformat()}T23:59:59+00:00"

        quote_notes = request.form.get("quote_notes", "").strip() or None

        saved = set_campaign_quote(
            campaign_id,
            total_usd=total_usd,
            currency="USD",
            line_items=line_items,
            valid_until=valid_until,
            notes=quote_notes,
        )
        if saved is None:
            # Write failed (service client down, RLS, CHECK violation, …).
            # Do NOT advance the FSM or claim success — surface an error.
            return redirect(
                url_for("admin_campaign_detail", campaign_id=campaign_id)
                + "?quote_error=1"
            )

        # Quote is persisted, so a customer fetching /quote the instant the
        # status flips already sees real numbers. Only now advance the FSM.
        # transition_api_status is forward-only and a no-op past QuoteSent.
        if request.form.get("set_quote_sent") == "1" and campaign.status in (
            "Draft",
            "WaitingForConfirmation",
        ):
            try:
                transition_api_status(
                    campaign_id, new_status="QuoteSent", by="admin"
                )
            except ValueError:
                pass

        return redirect(
            url_for("admin_campaign_detail", campaign_id=campaign_id) + "?quoted=1"
        )

    @flask_app.route("/admin/campaigns/<campaign_id>/results", methods=["POST"])
    def admin_campaign_save_results(campaign_id: str):
        """Attach results to an API-FSM campaign (gap G2).

        Accepts up to three result files (enrichment CSV, hits FASTA, raw
        reads FASTQ) uploaded to Supabase Storage under
        lab-campaigns/{id}/results/, and/or a pasted YDS results JSON
        (rounds + sequences, optional external downloads). File uploads are
        additive: each save merges newly uploaded paths onto any previously
        stored download_paths, and a blank JSON box leaves prior rounds /
        sequences intact. The results_status picker gates whether the API
        serves the envelope. When results_status first leaves 'none' (or
        changes among partial/all) and the row has a webhook_url, a
        results-ready webhook fires; otherwise this is silent like every
        other admin change.
        """
        import json as _json  # noqa: PLC0415
        import posixpath as _posixpath  # noqa: PLC0415
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import (  # noqa: PLC0415
            RESULTS_STATUSES,
            get_campaign,
            set_campaign_results,
        )
        from shared.storage import StorageError, upload_campaign_result  # noqa: PLC0415

        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login"))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404
        if campaign.submission_source != "api":
            return render_template("404.html"), 404

        prev_results_status = campaign.results_status

        results_status = request.form.get("results_status", "").strip() or "none"
        if results_status not in RESULTS_STATUSES:
            return redirect(
                url_for("admin_campaign_detail", campaign_id=campaign_id)
                + "?results_error=1"
            )

        # Start from the existing envelope so uploads are additive and a
        # blank JSON box preserves prior rounds/sequences.
        envelope: dict = dict(campaign.results or {})
        download_paths: dict = dict(envelope.get("download_paths") or {})

        # Optional pasted YDS JSON: validate FIRST (before any upload) so a
        # typo can't orphan a freshly stored file. Present -> replaces
        # rounds/sequences (and external downloads); blank keeps prior values.
        raw_json = request.form.get("results_json", "").strip()
        if raw_json:
            try:
                parsed = _json.loads(raw_json)
            except ValueError:
                return redirect(
                    url_for("admin_campaign_detail", campaign_id=campaign_id)
                    + "?results_error=1"
                )
            if not isinstance(parsed, dict):
                return redirect(
                    url_for("admin_campaign_detail", campaign_id=campaign_id)
                    + "?results_error=1"
                )
            if isinstance(parsed.get("rounds"), list):
                envelope["rounds"] = parsed["rounds"]
            if isinstance(parsed.get("sequences"), list):
                envelope["sequences"] = parsed["sequences"]
            if isinstance(parsed.get("downloads"), dict):
                envelope["downloads"] = parsed["downloads"]

        # The three documented download slots; each optional. Uploaded only
        # after JSON validation so a bad paste never leaves an orphaned object.
        slot_content_types = {
            "enrichment_table_csv": "text/csv",
            "hits_fasta": "text/plain",
            "raw_reads_fastq": "application/octet-stream",
        }
        for slot, default_ct in slot_content_types.items():
            uploaded = request.files.get(slot)
            if uploaded is None or not uploaded.filename:
                continue
            data = uploaded.read()
            if not data:
                continue
            # Name the stored object by the slot so re-uploads overwrite the
            # same path; keep the original extension.
            ext = _posixpath.splitext(uploaded.filename)[1]
            stored_name = f"{slot}{ext}" if ext else slot
            try:
                path = upload_campaign_result(
                    campaign_id=campaign_id,
                    filename=stored_name,
                    data=data,
                    content_type=uploaded.mimetype or default_ct,
                )
            except StorageError:
                logger.warning(
                    "results upload failed for %s slot %s",
                    campaign_id,
                    slot,
                    exc_info=True,
                )
                return redirect(
                    url_for("admin_campaign_detail", campaign_id=campaign_id)
                    + "?results_error=1"
                )
            download_paths[slot] = path

        if download_paths:
            envelope["download_paths"] = download_paths

        saved = set_campaign_results(
            campaign_id,
            results=envelope,
            results_status=results_status,
        )
        if saved is None:
            return redirect(
                url_for("admin_campaign_detail", campaign_id=campaign_id)
                + "?results_error=1"
            )

        # Notify the agent only when results genuinely became available.
        if (
            saved.results_status != "none"
            and saved.results_status != prev_results_status
            and saved.webhook_url
        ):
            try:
                from shared.webhooks import dispatch_webhook  # noqa: PLC0415

                dispatch_webhook(
                    campaign_id=saved.id,
                    event_type="experiment.results_ready",
                    target_url=saved.webhook_url,
                    owner_user_id=saved.user_id,
                    payload={
                        "event_type": "experiment.results_ready",
                        "experiment_id": saved.id,
                        "prev_status": saved.status,
                        "new_status": saved.status,
                        "results_status": saved.results_status,
                        "timestamp": saved.last_transition_at,
                    },
                )
            except Exception:
                logger.warning(
                    "results-ready webhook dispatch raised for %s",
                    saved.id,
                    exc_info=True,
                )

        return redirect(
            url_for("admin_campaign_detail", campaign_id=campaign_id)
            + "?results_saved=1"
        )

    # ------------------------------------------------------------------
    # Admin routes — /admin/users/* and /admin/signups/rejected
    # ------------------------------------------------------------------

    @flask_app.route("/admin/users", methods=["GET"])
    def admin_users_list():
        """Per-user activity dashboard: signup quality, runs, last seen.

        Pulls auth.users via service role (50-row first page), joins
        ``public.user_profiles``, ``credits_balance``, and the trailing
        30-day count from ``public.user_events`` + ``public.tool_jobs``.
        Sorts by last-activity DESC so the most engaged users surface
        first.
        """
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        client = get_service_client()
        users: list[dict] = []
        if client is not None:
            try:
                from datetime import datetime, timedelta, timezone  # noqa: PLC0415
                window_start = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat()

                page = client.auth.admin.list_users()
                auth_users = getattr(page, "users", None) or page

                profile_rows = (
                    client.table("user_profiles").select("*").execute().data or []
                )
                profiles_by_id = {r["user_id"]: r for r in profile_rows}

                balance_rows = (
                    client.table("user_wallets")
                    .select("user_id,balance_usd")
                    .execute()
                    .data
                    or []
                )
                balance_by_id = {
                    r["user_id"]: float(r.get("balance_usd") or 0)
                    for r in balance_rows
                }

                event_rows = (
                    client.table("user_events")
                    .select("user_id,event_type,created_at")
                    .gte("created_at", window_start)
                    .execute()
                    .data
                    or []
                )
                run_rows = (
                    client.table("tool_jobs")
                    .select("user_id,created_at,status")
                    .gte("created_at", window_start)
                    .execute()
                    .data
                    or []
                )

                from collections import defaultdict  # noqa: PLC0415
                event_count: dict = defaultdict(int)
                last_event: dict = {}
                for r in event_rows:
                    uid = r.get("user_id")
                    if not uid:
                        continue
                    event_count[uid] += 1
                    ts = r.get("created_at") or ""
                    if ts > last_event.get(uid, ""):
                        last_event[uid] = ts

                run_count: dict = defaultdict(int)
                last_run: dict = {}
                for r in run_rows:
                    uid = r.get("user_id")
                    if not uid:
                        continue
                    run_count[uid] += 1
                    ts = r.get("created_at") or ""
                    if ts > last_run.get(uid, ""):
                        last_run[uid] = ts

                for u in auth_users:
                    uid = getattr(u, "id", None) or (u.get("id") if isinstance(u, dict) else None)
                    if not uid:
                        continue
                    user_email = (
                        getattr(u, "email", None)
                        or (u.get("email") if isinstance(u, dict) else None)
                    )
                    created_at = (
                        getattr(u, "created_at", None)
                        or (u.get("created_at") if isinstance(u, dict) else None)
                    )
                    profile = profiles_by_id.get(uid, {})
                    last_activity = max(
                        last_event.get(uid, ""),
                        last_run.get(uid, ""),
                    ) or created_at or ""
                    users.append({
                        "user_id": uid,
                        "email": user_email,
                        "created_at": str(created_at)[:19] if created_at else "",
                        "signup_quality": profile.get("signup_quality") or "legacy",
                        "domain_class": profile.get("domain_class") or "",
                        "purpose": profile.get("purpose"),
                        "wallet_usd": balance_by_id.get(uid, 0.0),
                        "runs_30d": run_count.get(uid, 0),
                        "events_30d": event_count.get(uid, 0),
                        "last_activity": str(last_activity)[:19] if last_activity else "",
                    })
                users.sort(key=lambda u: u.get("last_activity") or "", reverse=True)
            except Exception:
                logger.warning("admin_users_list query failed", exc_info=True)

        return render_template("admin/users_list.html", users=users)

    @flask_app.route("/admin/users/<user_id>", methods=["GET"])
    def admin_user_detail(user_id: str):
        """Per-user activity timeline: events + tool runs + credits."""
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        viewer = session.get("user_email", "")
        if not viewer:
            return redirect(url_for("login", next=request.path))
        if viewer not in STAFF_EMAILS:
            return render_template("404.html"), 404

        client = get_service_client()
        target = {
            "user_id": user_id,
            "email": None,
            "created_at": "",
            "profile": {},
            "wallet_usd": 0.0,
            "timeline": [],
        }
        if client is None:
            return render_template("admin/user_detail.html", target=target)

        try:
            user_resp = client.auth.admin.get_user_by_id(user_id)
            user_obj = getattr(user_resp, "user", None)
            if user_obj is not None:
                target["email"] = getattr(user_obj, "email", None)
                target["created_at"] = (
                    str(getattr(user_obj, "created_at", "") or "")[:19]
                )
        except Exception:
            logger.warning("get_user_by_id failed for %s", user_id, exc_info=True)

        try:
            prof = (
                client.table("user_profiles")
                .select("*")
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            target["profile"] = getattr(prof, "data", None) or {}
        except Exception:
            target["profile"] = {}

        try:
            bal = (
                client.table("user_wallets")
                .select("balance_usd")
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            data = getattr(bal, "data", None) or {}
            target["wallet_usd"] = float(data.get("balance_usd") or 0)
        except Exception:
            target["wallet_usd"] = 0.0

        # Build a unified timeline by interleaving three sources.
        timeline: list[dict] = []
        try:
            events = (
                client.table("user_events")
                .select("event_type,path,props,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(200)
                .execute()
                .data
                or []
            )
            for e in events:
                timeline.append({
                    "kind": "event",
                    "label": e.get("event_type"),
                    "detail": e.get("path") or "",
                    "props": e.get("props") or {},
                    "created_at": e.get("created_at"),
                })
        except Exception:
            logger.warning("user_events query failed", exc_info=True)
        try:
            runs = (
                client.table("tool_jobs")
                .select("id,tool,preset,status,gpu_seconds_used,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
            for r in runs:
                gpu_s = r.get("gpu_seconds_used") or 0
                timeline.append({
                    "kind": "run",
                    "label": f"{r.get('tool')} run · {r.get('status')}",
                    "detail": (
                        f"preset={r.get('preset')}"
                        + (f" · {gpu_s} gpu-sec" if gpu_s else "")
                    ),
                    "job_id": r.get("id"),
                    "created_at": r.get("created_at"),
                })
        except Exception:
            logger.warning("tool_jobs query failed", exc_info=True)
        try:
            wallet_rows = (
                client.table("wallet_transactions")
                .select("kind,amount_usd,job_id,gpu_seconds,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
            for w in wallet_rows:
                amount = w.get("amount_usd") or 0
                bits = [f"${float(amount):+.4f}"]
                if w.get("gpu_seconds"):
                    bits.append(f"{w.get('gpu_seconds')} gpu-sec")
                timeline.append({
                    "kind": "wallet",
                    "label": f"{w.get('kind')}",
                    "detail": " · ".join(bits),
                    "created_at": w.get("created_at"),
                })
        except Exception:
            logger.warning("wallet_transactions query failed", exc_info=True)
        try:
            ledger = (
                client.table("credits_ledger")
                .select("kind,delta,reason,tool,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
            for l in ledger:
                # Internal margin-accounting ledger — kept for audit but
                # not the customer-facing money path (that's wallet_*).
                timeline.append({
                    "kind": "ledger",
                    "label": f"{l.get('kind')} ({l.get('delta')})",
                    "detail": l.get("reason") or "",
                    "tool": l.get("tool"),
                    "created_at": l.get("created_at"),
                })
        except Exception:
            logger.warning("credits_ledger query failed", exc_info=True)

        timeline.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        target["timeline"] = timeline
        return render_template("admin/user_detail.html", target=target)

    @flask_app.route("/admin/signups/rejected", methods=["GET"])
    def admin_signups_rejected():
        """Last 30 days of /signup rejections, grouped by reason."""
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        viewer = session.get("user_email", "")
        if not viewer:
            return redirect(url_for("login", next=request.path))
        if viewer not in STAFF_EMAILS:
            return render_template("404.html"), 404

        from collections import defaultdict  # noqa: PLC0415
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        rows: list[dict] = []
        client = get_service_client()
        if client is not None:
            try:
                window_start = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat()
                rows = (
                    client.table("signup_rejections")
                    .select("*")
                    .gte("created_at", window_start)
                    .order("created_at", desc=True)
                    .limit(500)
                    .execute()
                    .data
                    or []
                )
            except Exception:
                logger.warning(
                    "admin_signups_rejected query failed", exc_info=True
                )

        grouped: dict = defaultdict(list)
        for r in rows:
            grouped[r.get("reason") or "unknown"].append(r)
        groups = sorted(
            (
                {
                    "reason": reason,
                    "count": len(entries),
                    "entries": entries[:25],
                }
                for reason, entries in grouped.items()
            ),
            key=lambda g: g["count"],
            reverse=True,
        )

        return render_template(
            "admin/signups_rejected.html",
            groups=groups,
            total=len(rows),
        )

    @flask_app.errorhandler(404)
    def not_found(_):
        """Render the branded 404 page for unknown routes."""
        return render_template("404.html"), 404

    @flask_app.errorhandler(500)
    def server_error(_):
        """Render the branded 500 page for unhandled exceptions."""
        return render_template("500.html"), 500

    # ------------------------------------------------------------------
    # CLI commands — invoked by Railway cron or local `flask` runner
    # ------------------------------------------------------------------

    @flask_app.cli.command("digest:send")
    def cli_digest_send():
        """Build + send the daily digest to STAFF_NOTIFY_EMAIL.

        Usage::

            flask digest:send

        Override the trailing window with DIGEST_WINDOW_HOURS (default 24).
        """
        from cron.daily_digest import send_digest  # noqa: PLC0415

        with flask_app.app_context():
            ok = send_digest()
        click_msg = "sent" if ok else "failed (see logs)"
        # Use stdout so Railway cron logs show the outcome line.
        print(f"digest:send {click_msg}", flush=True)

    @flask_app.cli.command("reengagement:send")
    def cli_reengagement_send():
        """Sweep for unused-credit users and send the 7-day re-engagement email.

        Usage::

            flask reengagement:send

        No-ops cleanly when no user qualifies.
        """
        from cron.reengagement import send_reengagement  # noqa: PLC0415

        with flask_app.app_context():
            summary = send_reengagement()
        print(
            f"reengagement:send qualified={summary['qualified']} "
            f"sent={summary['sent']} "
            f"skipped_no_suggestions={summary['skipped_no_suggestions']} "
            f"errors={summary['errors']}",
            flush=True,
        )

    @flask_app.cli.command("jobs:sweep-stuck")
    def cli_sweep_stuck():
        """Terminalise stuck pending/running jobs and release their holds.

        Usage::

            flask jobs:sweep-stuck

        Override the age thresholds with STUCK_PENDING_AGE_MINUTES
        (default 30) and STUCK_RUNNING_AGE_HOURS (default 6).
        """
        from cron.sweep_stuck_jobs import sweep_stuck_jobs  # noqa: PLC0415

        with flask_app.app_context():
            summary = sweep_stuck_jobs()
        print(
            f"jobs:sweep-stuck pending={summary['pending_swept']} "
            f"running={summary['running_swept']} "
            f"errors={len(summary['errors'])}",
            flush=True,
        )
        for err in summary["errors"]:
            print(f"  err: {err}", flush=True)

    @flask_app.cli.command("indexnow:ping")
    def cli_indexnow_ping():
        """Submit the hub's high-value URLs to IndexNow.

        Usage::

            flask indexnow:ping

        No-ops if INDEXNOW_KEY is unset.
        """
        from cron.indexnow_ping import ping_high_value_urls  # noqa: PLC0415

        with flask_app.app_context():
            result = ping_high_value_urls()
        print(
            f"indexnow:ping status={result['status']} "
            f"submitted={result['submitted']} message={result['message']}",
            flush=True,
        )

    return flask_app


# ---------------------------------------------------------------------------
# Logging configuration — runs before create_app() so all loggers output
# to gunicorn's stderr in production.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Module-level app instance required for `gunicorn app:app`.
app = create_app()


if __name__ == "__main__":
    # Local dev entry point. Production uses gunicorn via Procfile.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
