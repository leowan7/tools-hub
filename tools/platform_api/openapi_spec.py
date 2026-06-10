"""OpenAPI 3.1 spec for the Platform API.

Hand-authored — the contract is the spec, and the spec is shipped on
``GET /api/v1/openapi.json``. Mirrors Adaptyv Bio's conventions where they
apply (Bearer auth, sequences dict, results_status enum). The YDS result
shape (per-sequence enrichment counts + downloads dict) is the
deliberate divergence — it's the Ranomics differentiator.

To regenerate the JSON response: just edit ``build_spec()``. The blueprint
calls ``build_spec()`` per request (cheap; no IO) so an in-process spec
edit shows up immediately. The 5-minute browser cache is fine for
crawler agents.
"""

from __future__ import annotations

from typing import Any


SPEC_VERSION = "0.1.0"
SERVERS = [
    {"url": "https://tools.ranomics.com/api/v1", "description": "Production"},
]


def build_spec() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Ranomics Platform API",
            "version": SPEC_VERSION,
            "summary": "Wet-lab as an API for binder-design agents",
            "description": (
                "Programmatic submission of binder candidates to Ranomics for "
                "yeast-display, mammalian-display, or DMS triage. Convention-"
                "compatible with Adaptyv Foundry where shapes align (Bearer "
                "auth, sequences dict, results_status enum). Result format "
                "is YDS-specific: per-sequence enrichment counts + signed "
                "download URLs, not kinetic constants.\n\n"
                "**Private alpha.** Mint a key via "
                "https://tools.ranomics.com/account/api-keys after the "
                "scoping team has invited you."
            ),
            "contact": {
                "name": "Ranomics Platform",
                "url": "https://ranomics.com/platform",
                "email": "info@ranomics.com",
            },
            "license": {
                "name": "Proprietary — alpha access",
                "url": "https://ranomics.com/platform",
            },
        },
        "servers": SERVERS,
        "security": [{"bearerAuth": []}],
        "tags": [
            {
                "name": "targets",
                "description": "Calibrated antigen catalogue. Empty during the alpha.",
            },
            {
                "name": "experiments",
                "description": "Create, poll, and retrieve results for an experiment.",
            },
            {
                "name": "quotes",
                "description": "Quote retrieval and confirmation.",
            },
            {"name": "meta", "description": "Spec and discovery."},
        ],
        "paths": {
            "/targets": {
                "get": {
                    "tags": ["targets"],
                    "summary": "List calibrated targets",
                    "description": (
                        "Returns the calibrated antigen catalogue. Each "
                        "entry carries `supported_experiment_types` and "
                        "`typical_campaign_range_usd`. Use the entry's "
                        "`target_id` on POST /experiments to skip human "
                        "scoping, or use the `custom` target shape for a "
                        "one-off antigen."
                    ),
                    "responses": {
                        "200": {
                            "description": "Target listing.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TargetList"}
                                }
                            },
                        }
                    },
                }
            },
            "/experiments": {
                "post": {
                    "tags": ["experiments"],
                    "summary": "Create an experiment",
                    "parameters": [
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "schema": {"type": "string", "minLength": 8, "maxLength": 128},
                            "description": (
                                "Optional. Re-submitting with the same key from "
                                "the same key-holder returns the original "
                                "experiment with an `Idempotent-Replay: true` "
                                "header."
                            ),
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateExperimentRequest"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Experiment"}
                                }
                            },
                        },
                        "200": {
                            "description": "Idempotent replay of an earlier create.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Experiment"}
                                }
                            },
                        },
                        "400": {"$ref": "#/components/responses/Error400"},
                        "401": {"$ref": "#/components/responses/Error401"},
                    },
                }
            },
            "/experiments/cost-estimate": {
                "post": {
                    "tags": ["experiments"],
                    "summary": "Non-binding cost estimate",
                    "description": (
                        "Returns a USD range. Catalogue targets "
                        "(`target_kind=catalog` + `target_id`) get a "
                        "calibrated band with `requires_human_quote=false`. "
                        "Custom targets get an order-of-magnitude "
                        "placeholder with `requires_human_quote=true`."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CostEstimateRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Estimate.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CostEstimate"}
                                }
                            },
                        },
                        "400": {"$ref": "#/components/responses/Error400"},
                        "401": {"$ref": "#/components/responses/Error401"},
                    },
                }
            },
            "/experiments/{experiment_id}": {
                "get": {
                    "tags": ["experiments"],
                    "summary": "Poll experiment status",
                    "parameters": [{"$ref": "#/components/parameters/ExperimentId"}],
                    "responses": {
                        "200": {
                            "description": "Status snapshot.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ExperimentStatus"}
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Error401"},
                        "404": {"$ref": "#/components/responses/Error404"},
                    },
                },
                "delete": {
                    "tags": ["experiments"],
                    "summary": "Withdraw an experiment",
                    "description": (
                        "Withdraw (delete) one of your experiments while it "
                        "is still in 'Draft' or 'WaitingForConfirmation', "
                        "before a quote is issued or any lab work begins. "
                        "Returns 409 once it has moved past initial review."
                    ),
                    "parameters": [{"$ref": "#/components/parameters/ExperimentId"}],
                    "responses": {
                        "200": {
                            "description": "Withdrawn; the experiment row was deleted.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "experiment_id": {
                                                "type": "string",
                                                "format": "uuid",
                                            },
                                            "status": {
                                                "type": "string",
                                                "enum": ["Withdrawn"],
                                            },
                                        },
                                    }
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Error401"},
                        "403": {"$ref": "#/components/responses/Error403"},
                        "404": {"$ref": "#/components/responses/Error404"},
                        "409": {"$ref": "#/components/responses/Error409"},
                    },
                },
            },
            "/experiments/{experiment_id}/quote": {
                "get": {
                    "tags": ["quotes"],
                    "summary": "Retrieve the quote",
                    "parameters": [{"$ref": "#/components/parameters/ExperimentId"}],
                    "responses": {
                        "200": {
                            "description": "Quote details.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Quote"}
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Error401"},
                        "404": {"$ref": "#/components/responses/Error404"},
                    },
                }
            },
            "/quotes/{quote_id}/confirm": {
                "post": {
                    "tags": ["quotes"],
                    "summary": "Accept the quote",
                    "description": (
                        "Moves the experiment from 'QuoteSent' to "
                        "'WaitingForMaterials'. Returns 409 with `code: "
                        "quote_not_confirmable` if the status is not "
                        "'QuoteSent', or `code: quote_not_finalized` if it is "
                        "'QuoteSent' but no price has been posted yet "
                        "(total_usd is null). Fetch GET /experiments/{id}/quote "
                        "first and confirm only once total_usd is present."
                    ),
                    "parameters": [
                        {
                            "name": "quote_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                            "description": (
                                "Quote id. In the alpha this equals the "
                                "experiment_id."
                            ),
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Confirmed; status moved to "
                            "'WaitingForMaterials'.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ExperimentStatus"}
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Error401"},
                        "404": {"$ref": "#/components/responses/Error404"},
                        "409": {"$ref": "#/components/responses/Error409"},
                    },
                }
            },
            "/experiments/{experiment_id}/results": {
                "get": {
                    "tags": ["experiments"],
                    "summary": "Fetch experiment results",
                    "description": (
                        "Returns 404 with `code: results_not_ready` until "
                        "`results_status != \"none\"`. Result shape is "
                        "yeast-display-specific: per-sequence pre/post sort "
                        "counts, log2 enrichment, called_hit boolean, and "
                        "signed download URLs."
                    ),
                    "parameters": [{"$ref": "#/components/parameters/ExperimentId"}],
                    "responses": {
                        "200": {
                            "description": "Results.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Results"}
                                }
                            },
                        },
                        "401": {"$ref": "#/components/responses/Error401"},
                        "404": {"$ref": "#/components/responses/Error404"},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "Opaque rk_live_… token",
                }
            },
            "parameters": {
                "ExperimentId": {
                    "name": "experiment_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                }
            },
            "responses": {
                "Error400": _err_response("Validation error."),
                "Error401": _err_response("Missing or invalid API key."),
                "Error403": _err_response("Read-only key on a write endpoint."),
                "Error404": _err_response("Not found."),
                "Error409": _err_response("State conflict."),
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "additionalProperties": True,
                        }
                    },
                },
                "TargetList": {
                    "type": "object",
                    "required": ["targets", "total"],
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/CalibratedTarget"},
                        },
                        "total": {"type": "integer"},
                    },
                },
                "CalibratedTarget": {
                    "type": "object",
                    "required": [
                        "target_id",
                        "name",
                        "supported_experiment_types",
                        "typical_campaign_range_usd",
                    ],
                    "properties": {
                        "target_id": {
                            "type": "string",
                            "description": (
                                "Opaque, stable, human-readable catalogue id, "
                                "e.g. `tgt_her2_ecd_v1`."
                            ),
                        },
                        "name": {"type": "string"},
                        "official_symbol": {
                            "type": "string",
                            "description": "HGNC gene symbol, when applicable.",
                        },
                        "uniprot_id": {"type": "string"},
                        "antigen_form": {
                            "type": "string",
                            "description": (
                                "Form delivered to the wet lab "
                                "(e.g. recombinant soluble ECD, biotinylated)."
                            ),
                        },
                        "antigen_sequence_stub": {
                            "type": "string",
                            "description": (
                                "Canonical sequence stub for the form used "
                                "at the lab (signal peptide trimmed; ECD "
                                "or soluble form only). Submitted alongside "
                                "the experiment as part of target_context "
                                "when a target_id is used. Use UniProt for "
                                "the authoritative reference."
                            ),
                        },
                        "supported_experiment_types": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "yeast_display",
                                    "mammalian_display",
                                    "dms",
                                ],
                            },
                        },
                        "indication_area": {"type": "string"},
                        "calibration_notes": {
                            "type": "string",
                            "description": (
                                "Operator-authored notes about sort gates, "
                                "panel scaffolds, or epitope anchoring used "
                                "during previous campaigns."
                            ),
                        },
                        "typical_campaign_range_usd": {
                            "type": "object",
                            "description": (
                                "Per-experiment-type cost band, "
                                "`{experiment_type: [low_usd, high_usd]}`. "
                                "Bands are wide on purpose: round count, "
                                "sort gates, and NGS depth all shift the "
                                "final number."
                            ),
                            "additionalProperties": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                        },
                    },
                },
                "CreateExperimentRequest": {
                    "type": "object",
                    "required": ["experiment_spec"],
                    "properties": {
                        "name": {"type": "string", "maxLength": 200},
                        "webhook_url": {
                            "type": "string",
                            "format": "uri",
                            "description": (
                                "Optional. POST is signed with X-Ranomics-"
                                "Signature: t=<ts>,v1=<hex hmac-sha256>. "
                                "The HMAC key is the **per-tenant** webhook "
                                "signing secret shown once at /account/api-"
                                "keys (whsec_…). Payload shape: see the "
                                "WebhookEvent schema."
                            ),
                        },
                        "experiment_spec": {
                            "$ref": "#/components/schemas/ExperimentSpec"
                        },
                    },
                },
                "ExperimentSpec": {
                    "type": "object",
                    "required": ["experiment_type", "target", "sequences"],
                    "properties": {
                        "experiment_type": {
                            "type": "string",
                            "enum": ["yeast_display", "mammalian_display", "dms"],
                        },
                        "target": {"$ref": "#/components/schemas/Target"},
                        "library_design": {
                            "$ref": "#/components/schemas/LibraryDesign"
                        },
                        "sequences": {
                            "type": "object",
                            "description": (
                                "Map of `{user_key: AMINO_STRING}`. Each value "
                                "is uppercase one-letter amino-acid codes; use "
                                "`:` to separate chains for multi-chain "
                                "submissions (Adaptyv-compatible)."
                            ),
                            "additionalProperties": {"type": "string"},
                        },
                    },
                },
                "Target": {
                    "type": "object",
                    "description": (
                        "Supply EITHER `target_id` (catalogue path) or "
                        "`custom` (one-off antigen). Mutually exclusive."
                    ),
                    "properties": {
                        "target_id": {
                            "type": "string",
                            "description": (
                                "Catalogue target id from "
                                "`GET /api/v1/targets`. The experiment is "
                                "constructed against the catalogue entry "
                                "and skips human scoping."
                            ),
                        },
                        "custom": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "antigen_sequence": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                            "description": (
                                "One-off antigen. Routes through human "
                                "scoping; cost-estimate returns a "
                                "placeholder range."
                            ),
                        },
                    },
                },
                "LibraryDesign": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": [
                                "designed_panel",
                                "combinatorial",
                                "site_saturation",
                                "error_prone",
                                "nnk",
                            ],
                        },
                        "diversity_estimate": {"type": "integer", "minimum": 1},
                        "notes": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "Experiment": {
                    "type": "object",
                    "required": [
                        "experiment_id",
                        "status",
                        "results_status",
                        "experiment_spec",
                    ],
                    "properties": {
                        "experiment_id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string"},
                        "status": {"$ref": "#/components/schemas/StatusEnum"},
                        "results_status": {
                            "$ref": "#/components/schemas/ResultsStatusEnum"
                        },
                        "experiment_spec": {
                            "$ref": "#/components/schemas/ExperimentSpec"
                        },
                        "notes_customer": {"type": "string"},
                        "webhook_url": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "last_transition_at": {
                            "type": "string",
                            "format": "date-time",
                        },
                        "status_log": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "status": {"$ref": "#/components/schemas/StatusEnum"},
                                    "at": {"type": "string", "format": "date-time"},
                                    "by": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "ExperimentStatus": {
                    "type": "object",
                    "required": ["experiment_id", "status", "results_status"],
                    "properties": {
                        "experiment_id": {"type": "string", "format": "uuid"},
                        "status": {"$ref": "#/components/schemas/StatusEnum"},
                        "results_status": {
                            "$ref": "#/components/schemas/ResultsStatusEnum"
                        },
                        "notes_customer": {"type": "string"},
                        "last_transition_at": {
                            "type": "string",
                            "format": "date-time",
                        },
                        "status_log": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                },
                "StatusEnum": {
                    "type": "string",
                    "enum": [
                        "Draft",
                        "WaitingForConfirmation",
                        "QuoteSent",
                        "WaitingForMaterials",
                        "LibraryConstruction",
                        "Sorting",
                        "NGS",
                        "DataAnalysis",
                        "InReview",
                        "Done",
                        "Cancelled",
                    ],
                },
                "ResultsStatusEnum": {
                    "type": "string",
                    "enum": ["none", "partial", "all"],
                },
                "CostEstimateRequest": {
                    "type": "object",
                    "required": ["experiment_type"],
                    "properties": {
                        "experiment_type": {
                            "type": "string",
                            "enum": ["yeast_display", "mammalian_display", "dms"],
                        },
                        "candidate_count": {"type": "integer", "minimum": 1},
                        "library_diversity": {"type": "integer", "minimum": 1},
                        "target_kind": {
                            "type": "string",
                            "enum": ["catalog", "custom"],
                            "description": (
                                "Default `custom`. Set to `catalog` and "
                                "pass `target_id` to get a calibrated band."
                            ),
                        },
                        "target_id": {
                            "type": "string",
                            "description": (
                                "Required when `target_kind=catalog`. "
                                "Available ids from GET /api/v1/targets."
                            ),
                        },
                    },
                },
                "CostEstimate": {
                    "type": "object",
                    "required": ["experiment_type", "requires_human_quote"],
                    "properties": {
                        "experiment_type": {"type": "string"},
                        "target_kind": {"type": "string"},
                        "target_id": {
                            "type": "string",
                            "description": (
                                "Echoed back when the estimate was keyed "
                                "to a catalogue entry."
                            ),
                        },
                        "target_name": {"type": "string"},
                        "requires_human_quote": {
                            "type": "boolean",
                            "description": (
                                "`false` for calibrated catalogue entries; "
                                "`true` for custom targets."
                            ),
                        },
                        "estimated_range_usd": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "scoping_url": {"type": "string", "format": "uri"},
                        "note": {"type": "string"},
                    },
                },
                "Quote": {
                    "type": "object",
                    "properties": {
                        "experiment_id": {"type": "string", "format": "uuid"},
                        "quote_id": {"type": "string", "format": "uuid"},
                        "status": {"$ref": "#/components/schemas/StatusEnum"},
                        "issued_at": {"type": "string", "format": "date-time"},
                        "line_items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "amount_usd": {"type": "number"},
                                    "notes": {"type": "string"},
                                },
                            },
                        },
                        "total_usd": {"type": "number"},
                        "currency": {"type": "string", "default": "USD"},
                        "valid_until": {
                            "type": "string",
                            "format": "date-time",
                        },
                        "terms_url": {"type": "string", "format": "uri"},
                    },
                },
                "Results": {
                    "type": "object",
                    "description": (
                        "Yeast-display result shape. Per-sequence enrichment "
                        "counts and called_hit booleans, plus signed download "
                        "URLs for the enrichment table, hits FASTA, and the "
                        "raw NGS reads (the last only when the customer opted "
                        "in at submission)."
                    ),
                    "properties": {
                        "experiment_id": {"type": "string", "format": "uuid"},
                        "status": {"$ref": "#/components/schemas/StatusEnum"},
                        "results_status": {
                            "$ref": "#/components/schemas/ResultsStatusEnum"
                        },
                        "rounds": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "round_id": {"type": "string"},
                                    "sort_gate": {"type": "string"},
                                    "input_diversity": {"type": "integer"},
                                    "output_diversity": {"type": "integer"},
                                },
                            },
                        },
                        "sequences": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "user_key": {"type": "string"},
                                    "sequence": {"type": "string"},
                                    "pre_count": {"type": "integer"},
                                    "post_count": {"type": "integer"},
                                    "log2_enrichment": {"type": "number"},
                                    "percentile": {"type": "number"},
                                    "called_hit": {"type": "boolean"},
                                },
                            },
                        },
                        "downloads": {
                            "type": "object",
                            "properties": {
                                "enrichment_table_csv": {
                                    "type": "string",
                                    "format": "uri",
                                },
                                "hits_fasta": {"type": "string", "format": "uri"},
                                "raw_reads_fastq": {
                                    "type": "string",
                                    "format": "uri",
                                },
                            },
                        },
                    },
                },
                "WebhookEvent": {
                    "type": "object",
                    "description": (
                        "Body Ranomics POSTs to ``webhook_url`` on every "
                        "status transition. Verify the X-Ranomics-Signature "
                        "header against your per-tenant ``whsec_…`` secret "
                        "before trusting the body. Receivers SHOULD also "
                        "check ``owner_user_id`` matches the recipient "
                        "tenant before acting (CR-01 defense-in-depth)."
                    ),
                    "required": [
                        "delivery_id",
                        "event_type",
                        "experiment_id",
                        "new_status",
                        "results_status",
                        "timestamp",
                    ],
                    "properties": {
                        "delivery_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": (
                                "Stable id for receiver-side dedup. Repeats "
                                "of the same delivery_id are retries; treat "
                                "them as idempotent."
                            ),
                        },
                        "event_type": {
                            "type": "string",
                            "example": "experiment.status_changed",
                        },
                        "experiment_id": {"type": "string", "format": "uuid"},
                        "prev_status": {
                            "$ref": "#/components/schemas/StatusEnum"
                        },
                        "new_status": {
                            "$ref": "#/components/schemas/StatusEnum"
                        },
                        "results_status": {
                            "$ref": "#/components/schemas/ResultsStatusEnum"
                        },
                        "owner_user_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": (
                                "CR-01: the tenant who owns the experiment. "
                                "Use this to confirm the event is intended "
                                "for the recipient before acting."
                            ),
                        },
                        "timestamp": {
                            "type": "string",
                            "format": "date-time",
                        },
                        "notes_customer": {
                            "type": "string",
                            "description": (
                                "Optional operator note. Present only when the "
                                "operator opted in for this transition; never "
                                "carries internal notes."
                            ),
                        },
                    },
                },
            },
        },
    }


def _err_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/Error"}
            }
        },
    }
