"""End-to-end tests against a running stack (API + worker + PostgreSQL).

    docker compose up -d --build
    E2E_BASE_URL=http://localhost:8000 E2E_API_KEY=dev-key uv run pytest -m e2e

Skipped when E2E_BASE_URL is not set.
"""

import os
import time

import httpx
import pytest

from tests.conftest import load_sample

BASE_URL = os.environ.get("E2E_BASE_URL")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL not set"),
]

FINAL_STATUSES = {"COMPLETED", "FAILED"}


@pytest.fixture
def api():
    headers = {"X-API-Key": os.environ.get("E2E_API_KEY", "dev-key")}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=10) as client:
        yield client


def _submit(api, sample):
    return api.post(
        "/documents", content=load_sample(sample), headers={"Content-Type": "application/xml"}
    )


def _wait_for_final_status(api, document_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = api.get(f"/documents/{document_id}").json()
        if detail["status"] in FINAL_STATUSES:
            return detail
        time.sleep(0.5)
    pytest.fail(f"Document {document_id} did not reach a final status within {timeout}s")


def test_valid_document_is_processed_by_worker(api):
    response = _submit(api, "valid_order.xml")
    assert response.status_code == 202

    detail = _wait_for_final_status(api, response.json()["id"])
    assert detail["status"] == "COMPLETED"

    result = api.get(response.json()["links"]["result"]).json()
    assert result["result"]["summary"]["totalAmount"] == "160.80"


def test_business_rule_violation_ends_in_failed(api):
    response = _submit(api, "total_mismatch_order.xml")
    assert response.status_code == 202

    detail = _wait_for_final_status(api, response.json()["id"])
    assert detail["status"] == "FAILED"


def test_invalid_document_is_rejected(api):
    assert _submit(api, "invalid_schema_order.xml").status_code == 422
