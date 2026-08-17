"""Platform API — Flask blueprint for /api/v1/*.

Endpoints (all JSON; Bearer auth via ``shared.api_auth.api_auth_required``):

    GET    /api/v1/targets
    POST   /api/v1/experiments
    POST   /api/v1/experiments/cost-estimate
    GET    /api/v1/experiments/{id}
    DELETE /api/v1/experiments/{id}
    GET    /api/v1/experiments/{id}/quote
    POST   /api/v1/quotes/{id}/confirm
    GET    /api/v1/experiments/{id}/results
    GET    /api/v1/openapi.json

Design notes
------------
- Conventions deliberately mirror Adaptyv Foundry where they apply
  (Bearer auth, sequences dict with ':' chain separator, results_status
  enum, status FSM). This is the load-bearing differentiator: agents
  already trained on the Adaptyv shape recognise ours without retraining.
- The differentiation vs Adaptyv lives in the result format
  (enrichment counts + called_hit vs kinetic constants) and in the
  pricing/scope model (library-scale triage vs per-sequence kinetics).
- Idempotency-Key header is honoured on POST /experiments. A repeat
  call with the same key from the same user returns the original row.
- Every response has ``X-Robots-Tag: noindex`` and ``Cache-Control:
  no-store`` (except the static openapi.json which is short-cached).
- CORS is wide-open — Bearer auth is safe across origins (no cookie).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from flask import Blueprint, Response, g, jsonify, make_response, request

from shared.api_auth import api_auth_required
from shared.campaigns import (
    ASSAY_TYPES,
    IdempotentReplay,
    Campaign,
    campaign_to_api_view,
    campaign_to_status_view,
    create_api_campaign,
    delete_api_campaign,
    get_campaign,
    transition_api_status,
)
from tools.platform_api.calibrated_targets import (
    cost_band,
    get_target,
    list_catalog,
    supported_experiment_types,
    supports_experiment_type,
)
from shared.webhooks import dispatch_webhook

logger = logging.getLogger(__name__)


platform_api_bp = Blueprint(
    "platform_api",
    __name__,
    url_prefix="/api/v1",
)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------


@platform_api_bp.after_request
def _api_response_headers(response):
    """Apply the API-wide response posture.

    Three concerns:
      1. Block search-engine indexing (the API is not user-readable).
      2. Wide-open CORS (Bearer auth is safe; no cookie identity).
      3. Disable caching on dynamic responses (the spec endpoint
         overrides this to 5 min public).
    """
    response.headers.setdefault("X-Robots-Tag", "noindex")
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault(
        "Access-Control-Allow-Headers",
        "Authorization,Content-Type,Idempotency-Key",
    )
    response.headers.setdefault(
        "Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS"
    )
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _error(status: int, code: str, message: str, **extra: Any):
    body = {"error": {"code": code, "message": message, **extra}}
    resp = jsonify(body)
    resp.status_code = status
    return resp


def _json_body() -> Optional[dict[str, Any]]:
    """Parse the request body as JSON. None on invalid input."""
    if not request.data:
        return None
    try:
        body = request.get_json(force=True, silent=False)
    except Exception:
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


_AMINO_RESIDUES = frozenset("ACDEFGHIKLMNPQRSTVWY")
_AMINO_ALPHABET = _AMINO_RESIDUES | frozenset(":")
_MAX_SEQUENCE_LEN = 2000  # generous upper bound for fusion constructs
_MAX_SEQUENCES_PER_SUBMIT = 50_000  # YDS library scale, hard cap
_MAX_CHAINS_PER_SEQUENCE = 4  # 99% of YDS designs are 1-2 chains; 4 is generous


def _validate_sequences(raw: Any) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Validate the sequences dict.

    Returns ``(sequences, None)`` on success or ``(None, error_message)``
    so the caller can return a precise 400. Adaptyv-compatible shape:
    dict of ``{user_key: str}`` where each value is uppercase amino acids
    with ``:`` between chains.

    FIX #11 (validation review): reject empty chains. ``::``, ``A:``,
    ``:B``, ``:`` all fail; library construction can't dispense empty
    inserts. Also caps chain count to head off accidental ``A:B:C:D:E``
    submissions.
    """
    if not isinstance(raw, dict):
        return None, "sequences must be a JSON object"
    if not raw:
        return None, "sequences cannot be empty"
    if len(raw) > _MAX_SEQUENCES_PER_SUBMIT:
        return None, (
            f"sequences exceeds per-submission cap of {_MAX_SEQUENCES_PER_SUBMIT}"
        )

    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            return None, "sequence keys must be non-empty strings"
        if not isinstance(value, str):
            return None, f"sequence {key!r} value must be a string"
        normalized = value.strip().upper()
        if not normalized:
            return None, f"sequence {key!r} value is empty"
        if len(normalized) > _MAX_SEQUENCE_LEN:
            return None, (
                f"sequence {key!r} exceeds per-sequence cap of "
                f"{_MAX_SEQUENCE_LEN} amino acids"
            )
        if not set(normalized).issubset(_AMINO_ALPHABET):
            return None, (
                f"sequence {key!r} contains non-canonical residues "
                "(expected uppercase ACDEFGHIKLMNPQRSTVWY with ':' as "
                "chain separator)"
            )
        # Per-chain checks: every chain must contain ≥1 residue from
        # the canonical alphabet (no leading/trailing/internal empty).
        chains = normalized.split(":")
        if len(chains) > _MAX_CHAINS_PER_SEQUENCE:
            return None, (
                f"sequence {key!r} has {len(chains)} chains; cap is "
                f"{_MAX_CHAINS_PER_SEQUENCE}"
            )
        for chain_idx, chain in enumerate(chains):
            if not chain:
                return None, (
                    f"sequence {key!r} contains an empty chain at position "
                    f"{chain_idx} (':' separator with no residues)"
                )
            if not set(chain).issubset(_AMINO_RESIDUES):
                return None, (
                    f"sequence {key!r} chain {chain_idx} has invalid residues"
                )
        cleaned[key] = normalized
    return cleaned, None


def _validate_webhook_url(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """Validate webhook_url for the SSRF guard (FIX #14).

    Delegates to :func:`shared.webhooks.validate_webhook_url_safe`, which
    rejects cleartext, embedded credentials, and any URL that resolves to
    a private/loopback/link-local IP (including Railway's CGNAT range).
    """
    from shared.webhooks import (  # noqa: PLC0415 — keep validator close to caller
        UnsafeWebhookURLError,
        validate_webhook_url_safe,
    )

    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return None, "webhook_url must be a string"
    if len(raw) > 2000:
        return None, "webhook_url is too long"
    try:
        validate_webhook_url_safe(raw)
    except UnsafeWebhookURLError as exc:
        return None, str(exc)
    return raw, None


def _idempotency_key_from_header() -> Optional[str]:
    raw = request.headers.get("Idempotency-Key", "").strip()
    if not raw:
        return None
    # 8–128 chars; printable ASCII. Reject unicode garbage early.
    if len(raw) < 8 or len(raw) > 128:
        return None
    if not raw.isprintable():
        return None
    return raw


# ---------------------------------------------------------------------------
# GET /api/v1/targets
# ---------------------------------------------------------------------------


@platform_api_bp.get("/targets")
@api_auth_required(read_only=True)
def list_targets():
    """List calibrated targets.

    Returns every entry from the alpha catalogue. Each entry carries
    ``supported_experiment_types`` and ``typical_campaign_range_usd``
    so a planning agent can decide whether to submit under the catalogue
    path or use a one-off ``custom`` target.

    The catalogue is small and curated by hand; it is not user-extensible.
    Use the ``custom`` target shape on POST /experiments for one-off
    antigens.
    """
    targets = list_catalog()
    return jsonify({"targets": targets, "total": len(targets)})


# ---------------------------------------------------------------------------
# POST /api/v1/experiments
# ---------------------------------------------------------------------------


@platform_api_bp.post("/experiments")
@api_auth_required()
def create_experiment():
    """Create a new experiment.

    Request body::

        {
          "name": "her2-mpnn-batch-01",
          "webhook_url": "https://...",
          "experiment_spec": {
            "experiment_type": "yeast_display",
            "target": {
              "custom": {"name": "HER2 ECD", "antigen_sequence": "...", "notes": "..."}
            },
            "library_design": {
              "mode": "designed_panel",
              "diversity_estimate": 12000,
              "notes": "MPNN top-1% by score"
            },
            "sequences": {"des001": "MASRYLLNPHWGV..."}
          }
        }

    Idempotency-Key header (8–128 chars) is honoured: a repeat with the
    same key from the same user returns the original experiment row.
    """
    body = _json_body()
    if body is None:
        return _error(400, "invalid_body", "Request body must be a JSON object.")

    spec = body.get("experiment_spec")
    if not isinstance(spec, dict):
        return _error(
            400, "invalid_body", "experiment_spec is required and must be an object."
        )

    experiment_type = spec.get("experiment_type")
    if experiment_type not in ASSAY_TYPES:
        return _error(
            400,
            "invalid_experiment_type",
            f"experiment_type must be one of {list(ASSAY_TYPES)}.",
        )

    target = spec.get("target")
    if not isinstance(target, dict):
        return _error(400, "invalid_target", "target is required.")

    target_id_raw = target.get("target_id")
    custom = target.get("custom")

    if target_id_raw is not None and custom is not None:
        return _error(
            400,
            "invalid_target",
            "target.target_id and target.custom are mutually exclusive; "
            "supply one or the other.",
        )

    if target_id_raw is not None:
        # Calibrated catalogue path.
        if not isinstance(target_id_raw, str):
            return _error(
                400, "invalid_target", "target.target_id must be a string."
            )
        calibrated_entry = get_target(target_id_raw.strip())
        if calibrated_entry is None:
            return _error(
                404,
                "unknown_target",
                f"target_id '{target_id_raw}' is not in the calibrated "
                "catalogue. List available ids via GET /api/v1/targets, "
                "or use the 'custom' shape for a one-off antigen.",
            )
        if not supports_experiment_type(calibrated_entry, experiment_type):
            supported = supported_experiment_types(calibrated_entry)
            return _error(
                400,
                "unsupported_experiment_type",
                f"target_id '{calibrated_entry['target_id']}' is not "
                f"calibrated for experiment_type '{experiment_type}'. "
                f"Supported: {supported}.",
            )
        target_name = calibrated_entry["name"]
        parts: list[str] = [f"catalogue_target_id: {calibrated_entry['target_id']}"]
        if calibrated_entry.get("uniprot_id"):
            parts.append(f"uniprot_id: {calibrated_entry['uniprot_id']}")
        if calibrated_entry.get("antigen_form"):
            parts.append(f"antigen_form: {calibrated_entry['antigen_form']}")
        if calibrated_entry.get("antigen_sequence_stub"):
            parts.append(
                f"antigen_sequence (catalogue stub): "
                f"{calibrated_entry['antigen_sequence_stub']}"
            )
        if calibrated_entry.get("calibration_notes"):
            parts.append(
                f"calibration_notes: {calibrated_entry['calibration_notes']}"
            )
        target_context = "\n".join(parts)
    else:
        # Custom one-off path. ``target.custom`` is required.
        if not isinstance(custom, dict):
            return _error(
                400,
                "invalid_target",
                "target.custom is required: "
                "{name, antigen_sequence?, notes?}.",
            )
        target_name = (custom.get("name") or "").strip()
        if not target_name:
            return _error(400, "invalid_target", "target.custom.name is required.")
        target_context_parts: list[str] = []
        antigen = (custom.get("antigen_sequence") or "").strip()
        if antigen:
            target_context_parts.append(f"antigen_sequence: {antigen}")
        notes = (custom.get("notes") or "").strip()
        if notes:
            target_context_parts.append(f"notes: {notes}")
        target_context = "\n".join(target_context_parts)

    sequences, err = _validate_sequences(spec.get("sequences"))
    if err:
        return _error(400, "invalid_sequences", err)

    library_design = spec.get("library_design")
    if library_design is not None and not isinstance(library_design, dict):
        return _error(
            400,
            "invalid_library_design",
            "library_design must be an object when provided.",
        )

    webhook_url, err = _validate_webhook_url(body.get("webhook_url"))
    if err:
        return _error(400, "invalid_webhook_url", err)

    idempotency_key = _idempotency_key_from_header()
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _error(400, "invalid_name", "name must be a string when provided.")

    try:
        campaign = create_api_campaign(
            user_id=g.api_user_id,
            name=name,
            assay_type=experiment_type,
            target_name=target_name,
            target_context=target_context,
            sequences=sequences,
            library_design=library_design,
            webhook_url=webhook_url,
            idempotency_key=idempotency_key,
        )
    except IdempotentReplay as replay:
        resp = jsonify(campaign_to_api_view(replay.campaign))
        resp.status_code = 200
        resp.headers["Idempotent-Replay"] = "true"
        return resp
    except ValueError as exc:
        return _error(400, "invalid_request", str(exc))

    if campaign is None:
        return _error(
            500,
            "submission_failed",
            "Could not persist the experiment. Try again or contact support.",
        )

    # Move to WaitingForConfirmation immediately. The alpha workflow
    # gates further progress on a human quote, so 'Draft' is a state
    # the customer never observes from the outside. The transition is
    # atomic at the DB level (transition_lab_campaign_api RPC); we use
    # the returned prev_status to populate the webhook payload honestly
    # instead of hardcoding "Draft" (FIX #6).
    result = transition_api_status(
        campaign.id, new_status="WaitingForConfirmation", by="system"
    )
    if result.campaign is not None:
        campaign = result.campaign
    if result.moved:
        _fire_webhook(
            campaign,
            event_type="experiment.waiting_for_confirmation",
            prev_status=result.prev_status or "Draft",
        )

    view = campaign_to_api_view(campaign)

    # Operator growth-signal alert (best-effort, fire-and-forget): a real
    # customer submission via the MCP server / REST API should never sit
    # unseen. Runs off the request thread so the 201 is never delayed or
    # failed by email latency — the analytics-off-the-hot-path rule from the
    # 2026-06-10 incident, applied to notifications. Only fires here on a
    # genuine create; the idempotent-replay path returned 200 above.
    try:
        from shared.email import notify_operator_new_submission  # noqa: PLC0415

        notify_operator_new_submission(
            experiment_id=view.get("experiment_id"),
            name=view.get("name"),
            experiment_type=experiment_type,
            target_name=target_name,
            sequence_count=len(sequences),
            submitter_user_id=g.api_user_id,
        )
    except Exception:  # never let an alert break a successful submission
        logger.debug("operator submission alert dispatch failed", exc_info=True)

    resp = jsonify(view)
    resp.status_code = 201
    return resp


# ---------------------------------------------------------------------------
# POST /api/v1/experiments/cost-estimate
# ---------------------------------------------------------------------------


@platform_api_bp.post("/experiments/cost-estimate")
@api_auth_required(read_only=True)
def cost_estimate():
    """Non-binding ballpark for a hypothetical submission.

    Inputs::

        {
          "experiment_type": "yeast_display" | "mammalian_display" | "dms",
          "candidate_count": 5000,
          "library_diversity": 12000,
          "target_kind": "catalog" | "custom",
          "target_id": "tgt_her2_ecd_v1"
        }

    Custom targets all return ``requires_human_quote: true`` with an
    order-of-magnitude placeholder band. Catalogue targets return a
    calibrated band keyed on the entry's ``typical_campaign_range_usd``
    map. Bands are wide on purpose: round count, sort gates, and NGS
    depth all shift the final number.
    """
    body = _json_body()
    if body is None:
        return _error(400, "invalid_body", "Request body must be a JSON object.")

    experiment_type = body.get("experiment_type")
    if experiment_type not in ASSAY_TYPES:
        return _error(
            400,
            "invalid_experiment_type",
            f"experiment_type must be one of {list(ASSAY_TYPES)}.",
        )
    target_kind = body.get("target_kind", "custom")
    if target_kind not in ("catalog", "custom"):
        return _error(
            400, "invalid_target_kind", "target_kind must be 'catalog' or 'custom'."
        )

    target_id = body.get("target_id")
    if target_kind == "catalog":
        if not isinstance(target_id, str) or not target_id.strip():
            return _error(
                400,
                "invalid_target_id",
                "target_id is required when target_kind is 'catalog'.",
            )
        entry = get_target(target_id.strip())
        if entry is None:
            return _error(
                404,
                "unknown_target",
                f"target_id '{target_id}' is not in the calibrated "
                "catalogue. List available ids via GET /api/v1/targets.",
            )
        band = cost_band(entry, experiment_type)
        if band is None:
            supported = supported_experiment_types(entry)
            return _error(
                400,
                "unsupported_experiment_type",
                f"target_id '{entry['target_id']}' is not calibrated for "
                f"experiment_type '{experiment_type}'. "
                f"Supported: {supported}.",
            )
        return jsonify(
            {
                "experiment_type": experiment_type,
                "target_kind": "catalog",
                "target_id": entry["target_id"],
                "target_name": entry["name"],
                "requires_human_quote": False,
                "estimated_range_usd": band,
                "note": (
                    "Calibrated band based on previously-run campaigns "
                    "against this catalogue entry. Final invoice depends "
                    "on round count, sort gates, and NGS depth set during "
                    "experiment design."
                ),
            }
        )

    return jsonify(
        {
            "experiment_type": experiment_type,
            "target_kind": target_kind,
            "requires_human_quote": True,
            "estimated_range_usd": _placeholder_range(experiment_type),
            "scoping_url": _scoping_url(),
            "note": (
                "The alpha returns a placeholder range for custom targets; "
                "a calibrated number is issued after a brief scoping call. "
                "Submit POST /experiments to start the conversation."
            ),
        }
    )


# ---------------------------------------------------------------------------
# GET /api/v1/experiments/{id}
# ---------------------------------------------------------------------------


@platform_api_bp.get("/experiments/<experiment_id>")
@api_auth_required(read_only=True)
def get_experiment(experiment_id: str):
    campaign = _load_owned_campaign(experiment_id)
    if not isinstance(campaign, Campaign):
        return campaign
    return jsonify(campaign_to_status_view(campaign))


# ---------------------------------------------------------------------------
# DELETE /api/v1/experiments/{id}  — withdraw a not-yet-started experiment
# ---------------------------------------------------------------------------

# Only an experiment that has not yet been committed to the lab may be
# withdrawn: the two pre-quote API-FSM states. Every later state (QuoteSent
# onward) represents real scoping/lab work whose record we keep, so a delete
# there returns 409. Web-form campaigns never reach these statuses and are
# already excluded by _load_owned_campaign (submission_source == "api"), so a
# member key can never delete a website submission through this route.
_WITHDRAWABLE_STATUSES = frozenset({"Draft", "WaitingForConfirmation"})


@platform_api_bp.delete("/experiments/<experiment_id>")
@api_auth_required()
def withdraw_experiment(experiment_id: str):
    """Withdraw (hard-delete) one of the caller's not-yet-started experiments.

    Allowed only while the experiment is in 'Draft' or
    'WaitingForConfirmation' (before a quote is issued or any lab work
    begins); past that it returns 409, so an in-flight or completed campaign
    can never be erased through the API. A second delete of the same id
    returns 404 (already gone).
    """
    campaign = _load_owned_campaign(experiment_id)
    if not isinstance(campaign, Campaign):
        # On a miss _load_owned_campaign returns an error Response (404);
        # only a real Campaign should fall through to the status check.
        return campaign

    if campaign.status not in _WITHDRAWABLE_STATUSES:
        return _error(
            409,
            "not_withdrawable",
            "This experiment can no longer be withdrawn through the API; it "
            "has moved past initial review. Contact the scoping team.",
            current_status=campaign.status,
        )

    # Status-guarded delete: the predicate (owner + api-source + withdrawable
    # status) lives on the DELETE itself, so a row that moved out of the
    # withdrawable window between the load above and here (TOCTOU — e.g. the
    # scoping team issues a quote concurrently) is NOT erased; the delete
    # simply matches nothing.
    if not delete_api_campaign(
        experiment_id,
        user_id=g.api_user_id,
        allowed_statuses=_WITHDRAWABLE_STATUSES,
    ):
        # Matched no row: the campaign changed under us between load and
        # delete. Re-resolve to answer honestly instead of a blanket 500.
        recheck = _load_owned_campaign(experiment_id)
        if not isinstance(recheck, Campaign):
            return recheck  # gone — withdrawn concurrently (404)
        if recheck.status not in _WITHDRAWABLE_STATUSES:
            return _error(
                409,
                "not_withdrawable",
                "This experiment changed state during the request (a quote "
                "or lab work began) and can no longer be withdrawn. Re-check "
                "its status.",
                current_status=recheck.status,
            )
        # Still owned and still withdrawable, yet nothing was deleted: a
        # genuine DB fault, not a client error.
        return _error(
            500,
            "withdraw_failed",
            "Could not withdraw the experiment. Please retry.",
        )

    return jsonify({"experiment_id": experiment_id, "status": "Withdrawn"})


# ---------------------------------------------------------------------------
# GET /api/v1/experiments/{id}/quote
# ---------------------------------------------------------------------------


@platform_api_bp.get("/experiments/<experiment_id>/quote")
@api_auth_required(read_only=True)
def get_experiment_quote(experiment_id: str):
    campaign = _load_owned_campaign(experiment_id)
    if not isinstance(campaign, Campaign):
        return campaign

    if campaign.status in ("Draft", "WaitingForConfirmation"):
        return _error(
            404,
            "quote_not_ready",
            "The quote has not been issued yet. Status will move to "
            "'QuoteSent' when the scoping team finishes review.",
            current_status=campaign.status,
        )

    # Persisted quote (migration 0030), written by the operator in the
    # admin UI via set_campaign_quote. The shape matches the OpenAPI Quote
    # schema. quote_id == experiment_id in the alpha (1:1). issued_at is the
    # QuoteSent transition time (last_transition_at).
    body = {
        "experiment_id": campaign.id,
        "quote_id": campaign.id,
        "status": campaign.status,
        "issued_at": campaign.last_transition_at,
        "line_items": campaign.quote_line_items or [],
        "total_usd": campaign.quote_total_usd,
        "currency": campaign.quote_currency or "USD",
        "valid_until": campaign.quote_valid_until,
        "terms_url": _terms_url(),
    }
    # Status reached QuoteSent but the operator has not posted a price yet.
    # Surface a soft note rather than implying a $0 / empty quote is final.
    if campaign.quote_total_usd is None:
        body["note"] = (
            "The quote is being finalised. total_usd and line_items will "
            "populate once the scoping team posts the price."
        )
    return jsonify(body)


# ---------------------------------------------------------------------------
# POST /api/v1/quotes/{id}/confirm
# ---------------------------------------------------------------------------


@platform_api_bp.post("/quotes/<quote_id>/confirm")
@api_auth_required()
def confirm_quote(quote_id: str):
    campaign = _load_owned_campaign(quote_id)
    if not isinstance(campaign, Campaign):
        return campaign

    if campaign.status != "QuoteSent":
        return _error(
            409,
            "quote_not_confirmable",
            "Only experiments in status 'QuoteSent' can be confirmed.",
            current_status=campaign.status,
        )

    # A row can reach QuoteSent without a posted price (e.g. an operator moved
    # it via the bare status control before finalising the quote). GET /quote
    # surfaces a "being finalised" note in that state; refuse to confirm it so
    # an agent never accepts a price-less quote and advances into lab work.
    if campaign.quote_total_usd is None:
        return _error(
            409,
            "quote_not_finalized",
            "The quote has no posted price yet. total_usd will populate once "
            "the scoping team finalises it; retry the confirm after that.",
            current_status=campaign.status,
        )

    result = transition_api_status(
        campaign.id, new_status="WaitingForMaterials", by="api"
    )
    if result.campaign is None:
        return _error(
            500,
            "transition_failed",
            "Could not confirm the quote. Try again or contact support.",
        )

    # Defensive check: the 409 gate above already guarantees status was
    # 'QuoteSent' on entry, so the RPC's no-op branch (prev_status ==
    # new_status) cannot fire here on the current code path. We keep
    # ``if result.moved`` as a belt-and-suspenders guard against a future
    # caller adding a confirm-twice retry path.
    if result.moved:
        _fire_webhook(
            result.campaign,
            event_type="experiment.confirmed",
            prev_status=result.prev_status or "QuoteSent",
        )
    return jsonify(campaign_to_status_view(result.campaign))


# ---------------------------------------------------------------------------
# GET /api/v1/experiments/{id}/results
# ---------------------------------------------------------------------------


@platform_api_bp.get("/experiments/<experiment_id>/results")
@api_auth_required(read_only=True)
def get_experiment_results(experiment_id: str):
    campaign = _load_owned_campaign(experiment_id)
    if not isinstance(campaign, Campaign):
        return campaign

    if campaign.results_status == "none":
        return _error(
            404,
            "results_not_ready",
            "Results are not available yet. Poll GET /experiments/{id} "
            "and watch for results_status to flip to 'partial' or 'all'.",
            current_status=campaign.status,
            results_status=campaign.results_status,
        )

    # Operator-attached results (migration 0031 dedicated ``results``
    # column, written by the admin UI). The stored envelope carries the
    # YDS rounds + sequences plus an internal download_paths map (logical
    # name -> storage object path). We mint a FRESH signed URL for each
    # path at read time so a customer never receives a link that already
    # expired in storage, then merge any operator-supplied external
    # downloads (e.g. large raw reads linked rather than uploaded).
    from shared.storage import StorageError, presigned_campaign_url  # noqa: PLC0415

    envelope = campaign.results if isinstance(campaign.results, dict) else {}
    rounds = envelope.get("rounds") or []
    sequences = envelope.get("sequences") or []

    downloads: dict[str, str] = {}
    download_paths = envelope.get("download_paths")
    if isinstance(download_paths, dict):
        for logical_name, object_path in download_paths.items():
            if not object_path:
                continue
            try:
                downloads[logical_name] = presigned_campaign_url(str(object_path))
            except StorageError:
                logger.warning(
                    "results: could not sign %s for %s",
                    object_path,
                    campaign.id,
                    exc_info=True,
                )
    external = envelope.get("downloads")
    if isinstance(external, dict):
        for key, value in external.items():
            if value:
                downloads.setdefault(key, str(value))

    return jsonify(
        {
            "experiment_id": campaign.id,
            "status": campaign.status,
            "results_status": campaign.results_status,
            "rounds": rounds,
            "sequences": sequences,
            "downloads": downloads,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/v1/openapi.json
# ---------------------------------------------------------------------------


@platform_api_bp.get("/openapi.json")
def openapi_spec():
    """Serve the OpenAPI 3.1 spec for tooling auto-generation."""
    from tools.platform_api.openapi_spec import build_spec  # noqa: PLC0415

    resp = jsonify(build_spec())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# Browsable docs page for humans. The openapi.json endpoint returns raw
# JSON (correct for machine consumption, ugly for humans clicking
# through). This page mounts Swagger UI against the same spec — single
# CDN-hosted bundle, no build step. The marketing landing page on
# ranomics.com/platform is a separate, larger follow-up.
_SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Ranomics Platform API — Reference</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,follow">
  <link rel="icon" href="/static/favicon.ico">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui.css">
  <style>
    body { margin: 0; background: #0b0f17; }
    .topbar { display: none; }
    .swagger-ui .info .title small.version-stamp { background: #2B9E7E; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
  <script>
    window.addEventListener('load', function () {
      window.ui = SwaggerUIBundle({
        url: '/api/v1/openapi.json',
        dom_id: '#swagger-ui',
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis],
        layout: 'BaseLayout',
        defaultModelsExpandDepth: 0,
        docExpansion: 'list',
      });
    });
  </script>
</body>
</html>
"""


@platform_api_bp.get("/docs")
def docs_page():
    """Swagger UI rendering of the OpenAPI spec at ``/api/v1/openapi.json``."""
    resp = make_response(_SWAGGER_UI_HTML)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# Handle CORS preflight on all blueprint paths.
@platform_api_bp.route(
    "/<path:_anything>", methods=["OPTIONS"]
)
@platform_api_bp.route("/", methods=["OPTIONS"])
def _preflight(_anything: str = ""):
    resp = jsonify({})
    resp.status_code = 204
    return resp


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_owned_campaign(experiment_id: str) -> "Campaign | Response":
    """Resolve an experiment id scoped to the authenticated user.

    Returns the Campaign on success, or a Flask error ``Response`` (404) on
    miss / wrong-user / non-API row. Callers MUST branch on
    ``not isinstance(result, Campaign)`` and return the Response as-is — NOT
    ``isinstance(result, tuple)``: ``_error`` returns a Response object, never
    a tuple, and that stale check silently 500'd the 404 path until it was
    corrected across the experiment endpoints.
    """
    campaign = get_campaign(experiment_id, user_id=g.api_user_id)
    if campaign is None:
        return _error(
            404,
            "experiment_not_found",
            "No experiment with that id is visible on this API key.",
        )
    if campaign.submission_source != "api":
        # A user with both web-form and API submissions shouldn't see
        # web rows via the API surface — different lifecycle, different
        # enum, different result shape.
        return _error(
            404,
            "experiment_not_found",
            "No experiment with that id is visible on this API key.",
        )
    return campaign


def _fire_webhook(campaign: Campaign, *, event_type: str, prev_status: str) -> None:
    """Fire-and-forget webhook on a transition. Never raises.

    The caller hands ``dispatch_webhook`` everything BUT ``delivery_id``;
    that field is minted inside ``dispatch_webhook`` (FIX #4) and grafted
    onto the body before signing so the signed bytes always reference
    the same id that lands in webhook_deliveries.id. Don't pass a sentinel
    here — the prior ``"delivery_id": None`` looked like a real bug to
    every reviewer who scanned this file (LO-08 fresh-review).

    CR-01: pass ``owner_user_id`` so the dispatcher can sign with the
    per-tenant HMAC secret and graft the owner id onto the payload for
    receiver-side cross-check.
    """
    try:
        dispatch_webhook(
            campaign_id=campaign.id,
            event_type=event_type,
            target_url=campaign.webhook_url,
            owner_user_id=campaign.user_id,
            payload={
                "event_type": event_type,
                "experiment_id": campaign.id,
                "prev_status": prev_status,
                "new_status": campaign.status,
                "results_status": campaign.results_status,
                "timestamp": campaign.last_transition_at,
            },
        )
    except Exception:
        logger.warning(
            "webhook dispatch raised for campaign %s", campaign.id, exc_info=True
        )


# ---------------------------------------------------------------------------
# Calibrated placeholders
# ---------------------------------------------------------------------------


def _placeholder_range(experiment_type: str) -> list[int]:
    """Order-of-magnitude USD range per assay.

    These are honest unit-economics bands, not promises. The scoping
    call replaces them with a calibrated number.
    """
    ranges = {
        "yeast_display": [12000, 80000],
        "mammalian_display": [25000, 150000],
        "dms": [18000, 120000],
    }
    return ranges.get(experiment_type, [10000, 100000])


def _scoping_url() -> str:
    return (
        os.environ.get("PLATFORM_API_SCOPING_URL")
        or "https://ranomics.com/ranomics-contact?service=platform-api"
    )


def _terms_url() -> str:
    return (
        os.environ.get("PLATFORM_API_TERMS_URL")
        or "https://tools.ranomics.com/terms"
    )
