"""Tool RPC payload schema.

Mirrors ``tools-hub/gpu/modal_client.py::ModalClient._build_payload``.
Every key produced by that builder is represented here so a payload
constructed via ``ToolPayload(...).model_dump()`` round-trips identically
to the legacy dict-literal shape.

Schema fields are intentionally permissive:

* Only ``job_id`` and ``job_spec`` are required at validation time —
  the remaining keys default to empty strings or ``None`` so every
  current production submission validates unchanged even if a key
  was previously omitted.
* ``job_spec`` is typed as ``dict[str, Any]`` because it is a
  free-form per-tool configuration blob; its inner shape is owned
  by the individual ``run_pipeline.py`` consumers.
* ``model_config = ConfigDict(extra="allow")`` allows forward-compat
  keys (e.g. fields added by a newer tools-hub against an older
  llm-proteinDesigner image) to flow through without breaking
  validation on the receive side.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ToolPayload(BaseModel):
    """Payload passed to every composite Modal app's ``run_tool``.

    See ``tools-hub/gpu/modal_client.py::ModalClient._build_payload``
    for the producer and the per-tool ``docker/<tool>/run_pipeline.py``
    files for the consumers.
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    job_spec: dict[str, Any]
    job_token: Optional[str] = ""
    job_tier: Optional[str] = ""
    tier: Optional[str] = ""
    webhook_url: Optional[str] = ""
    input_presigned_url: Optional[str] = ""
    input_pdb_url: Optional[str] = ""
    upload_urls_endpoint: Optional[str] = ""
    total_budget_hours: Optional[float] = 4
