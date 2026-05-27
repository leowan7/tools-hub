"""Upload-URL handoff schema.

Mirrors the request/response shape served by
``tools-hub/webhooks/uploads.py`` and consumed by the per-tool
``docker/<tool>/run_pipeline.py`` modules in llm-proteinDesigner when
they ``POST`` to the ``upload_urls_endpoint`` carried in the
``ToolPayload``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class UploadUrlsRequest(BaseModel):
    """Body sent by a pipeline runner to mint presigned PUT URLs."""

    model_config = ConfigDict(extra="allow")

    filenames: list[str]


class UploadUrlsResponse(BaseModel):
    """Response body returned by tools-hub for an UploadUrlsRequest."""

    model_config = ConfigDict(extra="allow")

    urls: dict[str, str]
