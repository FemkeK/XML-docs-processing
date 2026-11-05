"""HTTP endpoints for submitting documents and checking their status and result."""

import logging
import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import repository
from app.auth import require_api_key
from app.models import Document, DocumentStatus
from app.repository import utcnow
from app.schemas import (
    DocumentDetail,
    DocumentEventOut,
    DocumentResult,
    DocumentSummary,
    ErrorResponse,
    Links,
    SubmitResponse,
)
from app.validation import (
    EXAMPLE_PATH,
    MalformedXmlError,
    XmlValidationError,
    parse_and_validate,
)

logger = logging.getLogger(__name__)

XML_MEDIA_TYPES = {"application/xml", "text/xml"}
EXAMPLE_ORDER_XML = EXAMPLE_PATH.read_text(encoding="utf-8")
SUBMIT_DESCRIPTION = f"""Validates the XML against the order schema and queues it for processing.

Send an `Idempotency-Key` to make retries safe: the same key with the same document
returns the existing document (200) instead of creating a duplicate.

Example order (copy it into the request body under *Try it out*):

```xml
{EXAMPLE_ORDER_XML}```
"""

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(require_api_key)])
health_router = APIRouter(tags=["health"])


# --- Dependencies -----------------------------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    """One database session per request, closed afterwards. Endpoints commit explicitly."""
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Optional. Send the same key again to retry safely without duplicates.",
    ),
]


async def read_xml_body(request: Request) -> bytes:
    """Read the request body. Checks the Content-Type and enforces the size limit.

    The body is read in chunks, so an oversized upload is stopped early and never held
    in memory in full.
    """
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type not in XML_MEDIA_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Content-Type must be application/xml or text/xml",
        )

    limit = request.app.state.settings.max_document_bytes
    too_large = HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, f"Document exceeds maximum size of {limit} bytes"
    )
    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > limit:
        raise too_large

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Request body is empty")
    return bytes(body)


# --- Helpers ----------------------------------------------------------------------


def _links(document_id: uuid.UUID) -> Links:
    """Links to the document's status and result."""
    return Links(self=f"/documents/{document_id}", result=f"/documents/{document_id}/result")


def _get_or_404(session: Session, document_id: uuid.UUID, with_events: bool = False) -> Document:
    """Load a document, or answer 404 if it does not exist."""
    document = repository.get_document(session, document_id, with_events=with_events)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return document


def _submit_response(document: Document, response: Response) -> SubmitResponse:
    """Response body for an accepted document, with a Location header pointing to it."""
    response.headers["Location"] = f"/documents/{document.id}"
    return SubmitResponse(id=document.id, status=document.status, links=_links(document.id))


def _replay(document: Document, content: bytes, response: Response) -> SubmitResponse:
    """Answer a resubmission with the existing document (200 instead of 202).

    Reusing a key for a different document is a client error. Silently returning the
    old document would hide that the new one was never accepted.
    """
    if document.content_sha256 != repository.content_hash(content):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Idempotency-Key was already used for a different document",
        )
    logger.info("Idempotent replay", extra={"document_id": str(document.id)})
    response.status_code = status.HTTP_200_OK
    response.headers["Idempotent-Replayed"] = "true"
    return _submit_response(document, response)


async def stale_data_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    """A concurrent request changed the same document between our read and our write.

    The optimistic version check stopped our write. That is a conflict (409), not a
    server error.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Document was changed by a concurrent request; reload and retry"},
    )


async def xml_validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Turn validation errors into 400 (not well-formed) or 422 (does not match the schema)."""
    # Registered for XmlValidationError only. Starlette types every handler with Exception.
    assert isinstance(exc, XmlValidationError)
    if isinstance(exc, MalformedXmlError):
        status_code = status.HTTP_400_BAD_REQUEST
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    # The client gets the full messages. The log gets only the count: lxml quotes the
    # rejected values (an email address, for example), and personal data does not
    # belong in logs.
    logger.warning(
        "Document rejected", extra={"reason": exc.message, "error_count": len(exc.errors)}
    )
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(detail=exc.message, errors=exc.errors).model_dump(),
    )


# --- Endpoints --------------------------------------------------------------------
# No docstrings on the endpoint functions: FastAPI would show them in Swagger. The
# `summary` and `description` arguments document the endpoints for API users instead.


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit an XML document for processing",
    description=SUBMIT_DESCRIPTION,
    responses={
        200: {
            "model": SubmitResponse,
            "description": "Replay: a document with this Idempotency-Key already exists",
        },
        400: {"model": ErrorResponse, "description": "Not well-formed XML"},
        413: {"description": "Document too large"},
        415: {"description": "Wrong Content-Type"},
        422: {
            "model": ErrorResponse,
            "description": "XML does not match the schema, or the Idempotency-Key was "
            "already used for a different document",
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/xml": {
                    "schema": {"type": "string"},
                    # Shown as a request sample by Redoc (/redoc) and used by tools such as
                    # Postman. Swagger UI cannot show a literal XML example for a string
                    # schema, so the same example is also in the description (see above).
                    "examples": {"order": {"summary": "Valid order", "value": EXAMPLE_ORDER_XML}},
                }
            },
        }
    },
)
def submit_document(
    response: Response,
    content: Annotated[bytes, Depends(read_xml_body)],
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> SubmitResponse:
    # A client that is unsure whether its earlier request arrived (timeout, dropped
    # connection) can send it again with the same key, without creating a duplicate.
    if idempotency_key and (
        existing := repository.find_by_idempotency_key(session, idempotency_key)
    ):
        return _replay(existing, content, response)

    # Validate synchronously: the client gets immediate feedback, and invalid documents
    # never reach the queue.
    parse_and_validate(content)

    try:
        document = repository.create_document(session, content, utcnow(), idempotency_key)
        # Only after this commit is the document stored durably. We return 202 after it,
        # so an accepted document can never be lost.
        session.commit()
    except IntegrityError:
        # A concurrent request with the same key was stored first; the unique constraint
        # stopped this one. Answer as a replay of that request.
        session.rollback()
        if not idempotency_key:
            raise
        existing = repository.find_by_idempotency_key(session, idempotency_key)
        if existing is None:
            raise
        return _replay(existing, content, response)

    logger.info("Document accepted", extra={"document_id": str(document.id)})
    return _submit_response(document, response)


@router.get("", summary="List documents, optionally filtered by status")
def list_documents(
    session: SessionDep,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentSummary]:
    documents = repository.list_documents(session, status_filter, limit, offset)
    return [DocumentSummary.model_validate(document) for document in documents]


@router.get(
    "/{document_id}",
    summary="Status, attempts and history of a document",
    responses={404: {"description": "Document not found"}},
)
def get_document(document_id: uuid.UUID, session: SessionDep) -> DocumentDetail:
    document = _get_or_404(session, document_id, with_events=True)
    return DocumentDetail(
        **DocumentSummary.model_validate(document).model_dump(),
        last_error=document.last_error,
        # Only meaningful while a retry is scheduled.
        next_attempt_at=(
            document.next_attempt_at if document.status == DocumentStatus.RETRYING else None
        ),
        events=[DocumentEventOut.model_validate(event) for event in document.events],
        links=_links(document.id),
    )


@router.get(
    "/{document_id}/result",
    summary="Processing result (JSON) of a completed document",
    responses={
        404: {"description": "Document not found"},
        409: {"description": "Document is not (yet) completed"},
    },
)
def get_result(document_id: uuid.UUID, session: SessionDep) -> DocumentResult:
    document = _get_or_404(session, document_id)
    if document.status != DocumentStatus.COMPLETED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Result not available: document status is {document.status}",
        )
    return DocumentResult(id=document.id, result=document.result)


@router.post(
    "/{document_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Put a FAILED document back in the queue",
    responses={
        404: {"description": "Document not found"},
        409: {"description": "Document is not in FAILED status"},
    },
)
def retry_document(
    document_id: uuid.UUID, response: Response, session: SessionDep
) -> SubmitResponse:
    document = _get_or_404(session, document_id)
    try:
        repository.requeue(session, document, utcnow())
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()

    logger.info("Document requeued", extra={"document_id": str(document.id)})
    return _submit_response(document, response)


@health_router.get("/health", summary="Liveness and database check (no authentication)")
def health(request: Request) -> JSONResponse:
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ok"})
