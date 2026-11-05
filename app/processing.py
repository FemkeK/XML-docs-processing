"""The actual processing step: turn a validated order XML into a JSON-ready result.

The function is pure (bytes in, dict out, no side effects). That makes it easy to test,
and safe to run again if a worker crashes halfway through (at-least-once delivery).
"""

from decimal import Decimal
from typing import Any

from lxml import etree

from app.validation import XmlValidationError, parse_and_validate

CENT = Decimal("0.01")


class PermanentProcessingError(Exception):
    """The document can never be processed successfully, so retrying is pointless."""


def _money(value: Decimal) -> str:
    # Money goes out as a string with two decimals. JSON numbers are often read as
    # floats, which lose precision.
    return str(value.quantize(CENT))


def _optional(element: etree._Element, path: str) -> str | None:
    """Text of an optional field, or None if it is absent."""
    return element.findtext(path)


def _required(element: etree._Element, path: str) -> str:
    """Text of a field the XSD makes mandatory.

    After validation this cannot be missing. If it is, the schema and this code have
    drifted apart, and that must fail loudly instead of producing a half-filled result.
    """
    value = element.findtext(path)
    if value is None:
        raise PermanentProcessingError(f"Required field '{path}' is missing")
    return value


def transform_order(content: bytes) -> dict[str, Any]:
    """Convert an order document to a structured result with calculated totals.

    The document is validated again here. The worker processes the stored raw bytes,
    and the schema may have changed since the document was received.
    """
    try:
        root = parse_and_validate(content)
    except XmlValidationError as exc:
        raise PermanentProcessingError(f"{exc.message}: {'; '.join(exc.errors)}") from exc

    lines: list[dict[str, Any]] = []
    computed_total = Decimal("0")
    total_quantity = 0
    for line in root.iterfind("lines/line"):
        quantity = int(_required(line, "quantity"))
        unit_price = Decimal(_required(line, "unitPrice"))
        line_total = unit_price * quantity
        computed_total += line_total
        total_quantity += quantity
        lines.append(
            {
                "sku": _required(line, "sku"),
                "description": _optional(line, "description"),
                "quantity": quantity,
                "unitPrice": _money(unit_price),
                "lineTotal": _money(line_total),
            }
        )

    # Business rule that the XSD cannot express: the declared total must match the lines.
    declared_total = Decimal(_required(root, "total"))
    if declared_total.quantize(CENT) != computed_total.quantize(CENT):
        raise PermanentProcessingError(
            f"Declared total {_money(declared_total)} does not match "
            f"sum of order lines {_money(computed_total)}"
        )

    return {
        "orderId": _required(root, "orderId"),
        "orderDate": _required(root, "orderDate"),
        "currency": _required(root, "currency"),
        "customer": {
            "customerId": _required(root, "customer/customerId"),
            "name": _required(root, "customer/name"),
            "email": _required(root, "customer/email"),
        },
        "lines": lines,
        "summary": {
            "lineCount": len(lines),
            "totalQuantity": total_quantity,
            "totalAmount": _money(computed_total),
        },
    }
