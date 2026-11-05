"""Secure XML parsing and validation against the order XSD.

XML from outside the system is untrusted. The parser is set up so that it never
resolves entities, loads DTDs or makes network requests. Documents with a DOCTYPE are
rejected outright. That blocks XXE (reading local files or calling internal URLs) and
entity-expansion attacks such as "billion laughs".
"""

import threading
from pathlib import Path

from lxml import etree

SCHEMA_PATH = Path(__file__).parent / "xsd" / "order.xsd"
# Valid example document, shown in the API docs (Swagger "Try it out").
EXAMPLE_PATH = Path(__file__).parent / "xsd" / "order-example.xml"

# lxml parser and schema objects keep internal state (such as the error log), so each
# thread gets its own. The API serves requests from a thread pool.
_thread_local = threading.local()


class XmlValidationError(Exception):
    """Base class: the document is not acceptable. `errors` holds the details for the client."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []


class MalformedXmlError(XmlValidationError):
    """The input is not well-formed XML, or it uses forbidden constructs."""


class SchemaValidationError(XmlValidationError):
    """The XML is well-formed but does not match the order schema."""


def _secure_parser() -> etree.XMLParser:
    """An lxml parser that never fetches anything from outside the document itself."""
    return etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_blank_text=True,
    )


def _schema() -> etree.XMLSchema:
    """The compiled order schema, loaded once per thread."""
    schema = getattr(_thread_local, "schema", None)
    if schema is None:
        schema = etree.XMLSchema(etree.parse(str(SCHEMA_PATH), _secure_parser()))
        _thread_local.schema = schema
    return schema


def parse_and_validate(content: bytes) -> etree._Element:
    """Parse `content` and validate it against the order schema.

    Returns the root element. Raises MalformedXmlError or SchemaValidationError.
    """
    try:
        root = etree.fromstring(content, _secure_parser())
    except etree.XMLSyntaxError as exc:
        raise MalformedXmlError("Document is not well-formed XML", [str(exc)]) from exc

    # lxml-stubs is missing DocInfo.doctype and _ErrorLog.__iter__; both exist in lxml.
    if root.getroottree().docinfo.doctype:  # type: ignore[union-attr]
        raise MalformedXmlError("DOCTYPE declarations are not allowed")

    schema = _schema()
    if not schema.validate(root):
        errors = [
            f"line {error.line}: {error.message}"
            for error in schema.error_log  # type: ignore[attr-defined]
        ]
        raise SchemaValidationError("Document does not match the order schema", errors)

    return root
