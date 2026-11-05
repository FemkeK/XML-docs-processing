"""Input validation: well-formedness, the XSD and protection against XML attacks."""

import pytest

from app.validation import (
    EXAMPLE_PATH,
    MalformedXmlError,
    SchemaValidationError,
    parse_and_validate,
)
from tests.conftest import load_sample


def test_valid_order_passes():
    root = parse_and_validate(load_sample("valid_order.xml"))
    assert root.tag == "order"
    assert root.findtext("orderId") == "ORD-2026-0001"


def test_malformed_xml_is_rejected():
    with pytest.raises(MalformedXmlError) as exc_info:
        parse_and_validate(load_sample("malformed.xml"))
    assert exc_info.value.errors  # the parser's message, including the line number


def test_schema_violations_are_all_reported():
    with pytest.raises(SchemaValidationError) as exc_info:
        parse_and_validate(load_sample("invalid_schema_order.xml"))

    errors = " ".join(exc_info.value.errors)
    assert "email" in errors  # required element missing
    assert "-1" in errors  # quantity must be positive
    assert "12.505" in errors  # at most two decimal places
    assert "uniqueSku" in errors  # duplicate SKU


def test_doctype_is_rejected_to_prevent_xxe():
    with pytest.raises(MalformedXmlError, match="DOCTYPE"):
        parse_and_validate(load_sample("xxe_attack.xml"))


def test_entity_expansion_is_rejected():
    billion_laughs = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
    <order><orderId>&lol2;</orderId></order>"""
    with pytest.raises(MalformedXmlError):
        parse_and_validate(billion_laughs)


def test_wrong_root_element_is_rejected():
    with pytest.raises(SchemaValidationError):
        parse_and_validate(b"<invoice><id>1</id></invoice>")


def test_example_in_api_docs_is_valid():
    # The example in Swagger must always be accepted. Otherwise "Try it out" shows a 422.
    parse_and_validate(EXAMPLE_PATH.read_bytes())
