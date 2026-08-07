"""Pure transforms: ShopOrder -> ERPNext Sales Order dict.

Phase 4 backfills Shopify order history as ERPNext `Sales Order`s.

Scope decisions (see OPERATIONS.md §12.7):
  * Only **non-donation** line items are placed on the Sales Order. Donation
    orders are already represented as `Donation` records (donation sync);
    putting them on a Sales Order too would double-count revenue.
  * Pure-donation orders (no shop lines) are skipped entirely.
  * Each line item is linked to the migrated `Item` by `shopify_variant_id`
    (preferred) or `shopify_product_id`.
  * The customer is the migrated `Customer` (Phase 3); guest orders fall back to
    a configurable walk-in customer.
  * Payment Entry creation is deferred (Sales Order = the order record).

No I/O here — the caller resolves customer/item names from preloaded maps.
"""

from __future__ import annotations

from typing import Any

from .models import ShopOrder


def sales_order_doc(
    order: ShopOrder,
    *,
    company: str,
    price_list: str,
    currency: str,
    customer_name: str,
    item_codes: dict[str, str],  # variant_id OR product_id -> item_code
    warehouse: str,
    order_type: str = "Sales",
) -> dict[str, Any] | None:
    """Build a `Sales Order` doc for the order's shop (non-donation) lines.

    Returns None if the order has no resolvable shop lines (e.g. pure donation
    order, or all items failed to map).
    """
    items: list[dict[str, Any]] = []
    for ln in order.shop_lines:
        code = _resolve_item_code(ln, item_codes)
        if not code:
            continue
        items.append(
            {
                "item_code": code,
                "qty": float(ln.quantity),
                "rate": float(ln.rate),
                "warehouse": warehouse,
            }
        )
    if not items:
        return None

    return {
        "doctype": "Sales Order",
        "customer": customer_name,
        "company": company,
        "naming_series": "SAL-ORD-.YYYY.-",
        "order_type": order_type,
        "transaction_date": _date_only(order.processed_at),
        "delivery_date": _date_only(order.processed_at),
        "currency": order.currency or currency,
        "exchange_rate": 1.0,
        "selling_price_list": price_list,
        "price_list_currency": currency,
        "plc_conversion_rate": 1.0,
        "items": items,
        # custom field (created by glia-shop-setup):
        "shopify_order_id": order.id,
        "shopify_order_name": order.name,
    }


def _resolve_item_code(line: Any, item_codes: dict[str, str]) -> str | None:
    """Pick the migrated Item code for a line: variant ID first, then product ID."""
    if line.variant_id and line.variant_id in item_codes:
        return item_codes[line.variant_id]
    if line.product_id and line.product_id in item_codes:
        return item_codes[line.product_id]
    return None


def _date_only(iso: str) -> str:
    """'2026-05-01T10:00:00Z' -> '2026-05-01' (ERPNext date fields)."""
    return (iso or "")[:10] or ""


def filter_sales_order_by_shopify_id(shopify_order_id: str) -> list[Any]:
    return [["Sales Order", "shopify_order_id", "=", shopify_order_id]]


__all__ = [
    "filter_sales_order_by_shopify_id",
    "sales_order_doc",
]
