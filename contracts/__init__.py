"""Shared RPC contract module — vendored between tools-hub and llm-proteinDesigner.

Live source lives in this directory (tools-hub/contracts/). The sibling
llm-proteinDesigner repo consumes a copy of this package by mounting it
into each composite Modal image at ``/opt/contracts`` via
``modal.Image.add_local_dir(...)``.

Bump ``CONTRACT_VERSION`` for any breaking change to the payload shape
and log the change in ORCH-LOG.md.
"""

from contracts.rpc import ToolPayload
from contracts.upload_urls import UploadUrlsRequest, UploadUrlsResponse

CONTRACT_VERSION = "1.0.0"

__all__ = [
    "CONTRACT_VERSION",
    "ToolPayload",
    "UploadUrlsRequest",
    "UploadUrlsResponse",
]
