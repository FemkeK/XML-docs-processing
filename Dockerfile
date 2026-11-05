# One image for both the API and the worker; docker-compose picks the command.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

# Compile .pyc files at build time (faster start), install into /opt/venv and put it on
# PATH. PYTHONUNBUFFERED: log lines appear immediately in `docker compose logs`.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Install dependencies first (cached layer), then copy the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Do not run as root.
RUN useradd --system --no-create-home appuser
USER appuser

# Default command: the API. 0.0.0.0 makes it reachable from outside the container;
# request logging is done by the app itself (JSON), so uvicorn's access log is off.
EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
