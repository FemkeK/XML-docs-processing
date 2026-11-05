"""FastAPI application factory.

Start with:  uvicorn --factory app.main:create_app
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.base import RequestResponseEndpoint

from app.api import (
    health_router,
    router,
    stale_data_error_handler,
    xml_validation_error_handler,
)
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory
from app.logging_config import configure_logging, request_id_var
from app.validation import XmlValidationError

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings (for example a temporary database)."""
    settings = settings or get_settings()
    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Runs once: the code before `yield` at startup, the code after it at shutdown."""
        configure_logging(settings.log_level)
        if not settings.api_keys:
            logger.warning("API_KEYS is empty: every request to /v1/documents will be rejected")
        yield
        engine.dispose()

    app = FastAPI(
        title="XML Document Processor",
        version="0.1.0",
        description=(
            "Receives XML orders, validates them against an XSD, and processes them "
            "asynchronously. Authenticate with the `X-API-Key` header."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    app.add_exception_handler(XmlValidationError, xml_validation_error_handler)
    app.add_exception_handler(StaleDataError, stale_data_error_handler)
    app.include_router(router)
    app.include_router(health_router)

    @app.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Give every request a request ID (taken over from the client if sent) and log it."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "Request handled",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    return app
