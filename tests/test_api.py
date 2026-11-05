"""API tests, including the full flow: submit → worker → status → result.

The app and worker run in-process against a real (temporary) database. Only the HTTP
server and the separate worker process are left out. tests/e2e covers those.
"""

from app import repository
from tests.conftest import load_sample

XML = {"Content-Type": "application/xml"}


def _submit(client, sample="valid_order.xml", idempotency_key=None):
    headers = dict(XML)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return client.post("/documents", content=load_sample(sample), headers=headers)


# --- Full flow --------------------------------------------------------------------


def test_full_flow_from_submit_to_result(client, make_worker):
    response = _submit(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "RECEIVED"
    assert response.headers["Location"] == f"/documents/{body['id']}"

    # Not processed yet, so there is no result.
    assert client.get(body["links"]["result"]).status_code == 409

    make_worker().run_once()

    detail = client.get(body["links"]["self"]).json()
    assert detail["status"] == "COMPLETED"
    assert detail["attempts"] == 1
    assert [event["status"] for event in detail["events"]] == [
        "RECEIVED",
        "PROCESSING",
        "COMPLETED",
    ]

    result = client.get(body["links"]["result"]).json()
    assert result["result"]["orderId"] == "ORD-2026-0001"
    assert result["result"]["summary"]["totalAmount"] == "160.80"


def test_failed_processing_is_visible_and_can_be_requeued(client, make_worker):
    document_id = _submit(client, "total_mismatch_order.xml").json()["id"]
    make_worker().run_once()

    detail = client.get(f"/documents/{document_id}").json()
    assert detail["status"] == "FAILED"
    assert "does not match" in detail["last_error"]

    response = client.post(f"/documents/{document_id}/retry")
    assert response.status_code == 202
    assert response.json()["status"] == "RECEIVED"


def test_retry_of_non_failed_document_is_conflict(client):
    document_id = _submit(client).json()["id"]
    assert client.post(f"/documents/{document_id}/retry").status_code == 409


def test_list_documents_with_status_filter(client, make_worker):
    _submit(client)
    make_worker().run_once()
    _submit(client)

    assert len(client.get("/documents").json()) == 2
    received = client.get("/documents", params={"status": "RECEIVED"}).json()
    assert [document["status"] for document in received] == ["RECEIVED"]


# --- Idempotent submission --------------------------------------------------------


def test_resubmission_with_same_key_returns_existing_document(client):
    first = _submit(client, idempotency_key="order-0001")
    second = _submit(client, idempotency_key="order-0001")

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/documents").json()) == 1


def test_replay_shows_current_status(client, make_worker):
    _submit(client, idempotency_key="order-0001")
    make_worker().run_once()

    assert _submit(client, idempotency_key="order-0001").json()["status"] == "COMPLETED"


def test_same_key_for_different_document_is_rejected(client):
    _submit(client, idempotency_key="order-0001")
    response = _submit(client, "total_mismatch_order.xml", idempotency_key="order-0001")

    assert response.status_code == 422
    assert "different document" in response.json()["detail"]


def test_without_key_every_submission_is_a_new_document(client):
    _submit(client)
    _submit(client)
    assert len(client.get("/documents").json()) == 2


def test_simultaneous_duplicates_are_caught_by_unique_constraint(client, monkeypatch):
    first = _submit(client, idempotency_key="order-0001")

    # Simulate the race: the second request looks the key up before the first request's
    # commit is visible, so the lookup misses and only the unique constraint can stop it.
    real_find = repository.find_by_idempotency_key
    lookups = []

    def find_misses_first_time(session, key):
        lookups.append(key)
        return None if len(lookups) == 1 else real_find(session, key)

    monkeypatch.setattr(repository, "find_by_idempotency_key", find_misses_first_time)
    second = _submit(client, idempotency_key="order-0001")

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/documents").json()) == 1


# --- Concurrency ------------------------------------------------------------------


def test_concurrent_retry_returns_409_instead_of_500(
    client, make_worker, session_factory, monkeypatch
):
    document_id = _submit(client, "total_mismatch_order.xml").json()["id"]
    make_worker().run_once()  # → FAILED

    # Another request requeues the same document between this request's read and write.
    real_requeue = repository.requeue

    def requeue_after_concurrent_change(session, document, now):
        with session_factory.begin() as other_session:
            real_requeue(other_session, repository.get_document(other_session, document.id), now)
        real_requeue(session, document, now)

    monkeypatch.setattr(repository, "requeue", requeue_after_concurrent_change)
    response = client.post(f"/documents/{document_id}/retry")

    assert response.status_code == 409
    assert "concurrent" in response.json()["detail"]


# --- Rejection of invalid input ---------------------------------------------------


def test_schema_violation_returns_422_with_details(client):
    response = _submit(client, "invalid_schema_order.xml")
    assert response.status_code == 422
    assert response.json()["detail"] == "Document does not match the order schema"
    assert len(response.json()["errors"]) >= 3


def test_rejected_values_are_not_logged(client, caplog):
    # The sample has an invalid price (12.505) and a duplicate SKU. The client sees those
    # values in the error messages; the log must not contain them. The JSON formatter
    # writes every field of a record, so checking the records covers the log output.
    response = _submit(client, "invalid_schema_order.xml")
    assert "12.505" in " ".join(response.json()["errors"])

    logged = [record.__dict__ for record in caplog.records]
    assert any(entry.get("error_count") == 4 for entry in logged)
    assert "12.505" not in str(logged)
    assert "PIP-1000" not in str(logged)


def test_malformed_xml_returns_400(client):
    assert _submit(client, "malformed.xml").status_code == 400


def test_xxe_attempt_returns_400(client):
    response = _submit(client, "xxe_attack.xml")
    assert response.status_code == 400
    assert "DOCTYPE" in response.json()["detail"]


def test_rejected_documents_are_not_stored(client):
    _submit(client, "invalid_schema_order.xml")
    _submit(client, "malformed.xml")
    assert client.get("/documents").json() == []


def test_wrong_content_type_returns_415(client):
    response = client.post(
        "/documents", content=load_sample("valid_order.xml"), headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 415


def test_oversized_document_returns_413(client, settings):
    too_big = b"<order>" + b"x" * settings.max_document_bytes + b"</order>"
    assert client.post("/documents", content=too_big, headers=XML).status_code == 413


def test_empty_body_returns_400(client):
    assert client.post("/documents", content=b"", headers=XML).status_code == 400


def test_unknown_document_returns_404(client):
    assert client.get("/documents/00000000-0000-0000-0000-000000000000").status_code == 404


# --- Authentication and cross-cutting concerns ------------------------------------


def test_missing_or_wrong_api_key_returns_401(client):
    del client.headers["X-API-Key"]
    assert client.get("/documents").status_code == 401
    response = client.get("/documents", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "ApiKey"


def test_health_needs_no_api_key(client):
    del client.headers["X-API-Key"]
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_id_is_returned(client):
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"
