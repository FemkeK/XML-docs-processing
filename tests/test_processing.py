"""The XML -> JSON transformation and the business rule that the total must add up."""

import pytest

from app.processing import PermanentProcessingError, transform_order
from tests.conftest import load_sample


def test_transforms_order_and_calculates_totals():
    result = transform_order(load_sample("valid_order.xml"))

    assert result["orderId"] == "ORD-2026-0001"
    assert result["currency"] == "EUR"
    assert result["customer"]["email"] == "inkoop@labsupplies.example"
    assert [line["lineTotal"] for line in result["lines"]] == ["125.00", "35.80"]
    assert result["summary"] == {"lineCount": 2, "totalQuantity": 14, "totalAmount": "160.80"}


def test_total_mismatch_is_a_permanent_error():
    with pytest.raises(PermanentProcessingError, match="does not match"):
        transform_order(load_sample("total_mismatch_order.xml"))


def test_invalid_document_is_a_permanent_error():
    with pytest.raises(PermanentProcessingError):
        transform_order(load_sample("invalid_schema_order.xml"))


def test_transformation_is_idempotent():
    # Required for at-least-once processing: running twice gives the same result.
    content = load_sample("valid_order.xml")
    assert transform_order(content) == transform_order(content)
