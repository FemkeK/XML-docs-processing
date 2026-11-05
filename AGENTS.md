# AGENTS.md

Guidance for AI coding agents (and people) working on this repository. The [README](README.md)
explains the design and the trade-offs; this file lists the commands, conventions and rules
that keep the code correct. Read the README's "Architectuur" and "Keuzes en afwegingen" sections before
changing the queue, the worker or the API.

## Commands

```bash
make install        # dependencies (Python 3.12 via uv)
make check          # lint, mypy, tests with coverage, pip-audit: must pass before you are done
make up             # full stack in Docker (PostgreSQL, migrations, API, worker)
make e2e            # end-to-end tests against the running stack
uv run alembic revision --autogenerate -m "..."   # new migration after a model change
```

Tests use SQLite by default. Run them against PostgreSQL too when you touch queries or the
schema: `TEST_DATABASE_URL=postgresql+psycopg://... uv run pytest`.

## Where things live

| Module | Responsibility |
|---|---|
| `app/api.py` | HTTP endpoints, request validation, error mapping |
| `app/validation.py` | Secure XML parsing and XSD validation (only order-specific part, with processing.py) |
| `app/processing.py` | XML to JSON transformation; a pure function |
| `app/repository.py` | All queue operations and status transitions |
| `app/worker.py` | The claim, process, finish loop |
| `migrations/` | Alembic migrations; the schema is never created any other way |

The `documents` table is both the store and the queue; the README (choice 1) explains why.

## Rules

These hold the design together. Do not break them without updating the README and the tests.

- **Status changes only through `repository._transition()`.** It also writes the history
  (`document_events`), which must stay complete.
- **Processing stays idempotent and free of side effects.** Delivery is at-least-once: after
  a crash, a document is processed again.
- **Workers write only while their claim is valid.** Use the `ClaimedDocument` and the
  `mark_*` / `schedule_retry` functions; never update a claimed document directly.
- **A model change needs a migration.** `tests/test_migrations.py` fails otherwise.
- **No document content in logs.** Documents contain personal data. Log IDs, counts and error
  types; full messages belong in `last_error` or the API response.
- **Untrusted XML goes through `parse_and_validate()`.** Never parse with a default lxml parser
  (XXE).
- **Money is `Decimal`, and leaves the API as a string.** Never use float for amounts.
- **No docstrings on endpoint functions.** FastAPI shows them in Swagger; use `summary=` and
  `description=` instead.
- **No new dependencies without checking where they come from.** Prefer the standard library
  and the packages already in `pyproject.toml`.

## Style

- Code, comments and docstrings in English; the README is in Dutch.
- Comments explain *why*, not *what*. Every module, class and non-trivial function has a short
  docstring.
- Type annotations everywhere in `app/` (mypy enforces this). Ruff formats and lints; in
  Claude Code a hook (`.claude/hooks/format-python.sh`) runs it after every edit.

## Definition of done

1. `make check` passes, and new behaviour has tests (unit, or integration via `tests/test_api.py`).
2. Schema changes come with a migration.
3. The README is updated when a choice, an endpoint or a configuration setting changes.
