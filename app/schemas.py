"""Response models for the API (also used for the OpenAPI/Swagger documentation)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import DocumentStatus


class Links(BaseModel):
    """URLs of related resources, so clients do not have to build them."""

    self: str
    result: str


class SubmitResponse(BaseModel):
    """Response to POST /v1/documents and POST /v1/documents/{id}/retry."""

    id: uuid.UUID
    status: DocumentStatus
    links: Links


class DocumentEventOut(BaseModel):
    """One entry in a document's status history."""

    # from_attributes: built straight from the SQLAlchemy object (model_validate(event)).
    model_config = ConfigDict(from_attributes=True)

    status: DocumentStatus
    message: str | None
    created_at: datetime


class DocumentSummary(BaseModel):
    """A document in the list endpoint: status only, no content or history."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DocumentStatus
    attempts: int
    received_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentSummary):
    """GET /v1/documents/{id}: the summary plus the last error and the full history."""

    last_error: str | None
    next_attempt_at: datetime | None
    events: list[DocumentEventOut]
    links: Links


class DocumentResult(BaseModel):
    """GET /v1/documents/{id}/result: the JSON produced by the worker."""

    id: uuid.UUID
    result: dict


class ErrorResponse(BaseModel):
    """Body of a 400/422 rejection. `errors` lists every problem found, with line numbers."""

    detail: str
    errors: list[str] = []
