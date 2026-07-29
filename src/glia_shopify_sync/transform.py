"""Transform Shopify Order JSON into Donor + Donation models.

Pure functions: no I/O, no globals — trivially unit-testable. The input is the
raw Order *node* dict as produced by `ShopifyClient.iter_orders` (shape defined
in `shopify_queries.ORDERS_QUERY`).

Rules (locked with the user):
  * Donation filter = curated allow-list of Shopify product GIDs. Orders with
    no matching line item are dropped (return None).
  * One Donation per qualifying line item (handles multi-donation orders).
  * "Tip" line item (product == null, name "Tip") is folded into the FIRST
    donation line on the order and flags it with includes_tip=True. The fold
    mode is configurable (fold | ignore | separate); only `fold` and `ignore`
    are implemented in Phase 1.
  * Amounts: primary `amount`/`currency` = shopMoney (accounting currency,
    e.g. CAD). `amount_presentment`/`currency_presentment` = what the donor
    actually paid in their own currency. Both preserved.
  * donation_type: Recurring if the line's product GID is in the recurring
    allow-list (or its name contains "recurring"), else One-time.
  * Donor dedup key = email (lowercased); fallbacks handled in dedup.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from .models import AddressData, Donation, Donor, TransformResult

log = structlog.get_logger()

_TIP_NAMES = {"tip", "tip (optional)"}
# Financial statuses that mean money was collected.
_COLLECTED_STATUSES = {"PAID", "PARTIALLY_PAID"}


def transform_order(
    order: dict[str, Any],
    *,
    donation_gids: set[str],
    recurring_gids: set[str],
    tip_mode: str = "fold",
    include_test_orders: bool = False,
    paid_only: bool = True,
) -> TransformResult | None:
    """Convert one Shopify Order node into a TransformResult, or None to skip.

    Returns None when the order is: not a donation, a test order (when excluded),
    or not in a collected financial state (when paid_only).
    """
    if not include_test_orders and _is_test(order):
        log.debug("skip_test_order", name=order.get("name"))
        return None

    if paid_only and not _is_collected(order):
        log.debug(
            "skip_non_paid_order",
            name=order.get("name"),
            status=order.get("displayFinancialStatus"),
        )
        return None

    line_items = _line_items(order)
    donation_lines = [li for li in line_items if _line_product_gid(li) in donation_gids]
    if not donation_lines:
        return None  # merch / service / device — not a donation

    tip_line = _find_tip_line(line_items)
    tip_shop_amt = _line_shop_amount(tip_line) if tip_line else Decimal("0")
    tip_pres_amt = _line_presentment_amount(tip_line) if tip_line else Decimal("0")

    donor = _build_donor(order)
    tip_used = False

    donations: list[Donation] = []
    for li in donation_lines:
        shop_amt, shop_cur = _line_shop_money_tuple(li)
        pres_amt, pres_cur = _line_presentment_money_tuple(li)

        includes_tip = False
        if tip_mode == "fold" and tip_line is not None and not tip_used:
            shop_amt = shop_amt + tip_shop_amt
            pres_amt = pres_amt + tip_pres_amt
            includes_tip = True
            tip_used = True

        product = li.get("product") or {}
        product_title = product.get("title") or ""
        variant = li.get("variant") or {}
        donation_type = _classify_type(_line_product_gid(li), li.get("name", ""), recurring_gids)

        donations.append(
            Donation(
                shopify_order_id=order.get("id", ""),
                shopify_order_name=order.get("name", ""),
                shopify_line_item_id=li.get("id", ""),
                donor_shopify_customer_id=donor.shopify_customer_id,
                date=_iso_date(order.get("processedAt")),
                amount=shop_amt,
                currency=shop_cur or _order_currency(order),
                amount_presentment=pres_amt,
                currency_presentment=pres_cur or _order_presentment_currency(order),
                donation_type=donation_type,
                campaign=product_title,
                tier=variant.get("title"),
                includes_tip=includes_tip,
                financial_status=order.get("displayFinancialStatus") or "",
            )
        )

    # tip_mode == "separate" emits an extra Donation (campaign="Tip"); planned
    # for a later phase. For now fold/ignore are the implemented modes.
    if tip_mode == "separate" and tip_line is not None:
        log.warning("tip_separate_not_implemented", order=order.get("name"))

    return TransformResult(donor=donor, donations=donations)


# --- order-level helpers --------------------------------------------------


def _is_test(order: dict[str, Any]) -> bool:
    return bool(order.get("test"))


def _is_collected(order: dict[str, Any]) -> bool:
    status = (order.get("displayFinancialStatus") or "").upper()
    return status in _COLLECTED_STATUSES


def _order_currency(order: dict[str, Any]) -> str:
    return (order.get("currencyCode") or "").upper()


def _order_presentment_currency(order: dict[str, Any]) -> str:
    return (order.get("presentmentCurrencyCode") or "").upper()


def _line_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    edges = (order.get("lineItems") or {}).get("edges") or []
    return [e.get("node") or {} for e in edges]


def _find_tip_line(line_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A Tip line: no product attached, name matches the tip set."""
    for li in line_items:
        if li.get("product") is None:
            name = (li.get("name") or "").strip().lower()
            if name in _TIP_NAMES or name.startswith("tip"):
                return li
    return None


def _line_product_gid(li: dict[str, Any]) -> str:
    product = li.get("product") or {}
    return product.get("id") or ""


def _classify_type(product_gid: str, line_name: str, recurring_gids: set[str]) -> str:
    if product_gid in recurring_gids:
        return "Recurring"
    if "recurring" in (line_name or "").lower():
        return "Recurring"
    return "One-time"


# --- money helpers --------------------------------------------------------


def _to_decimal(amount: Any) -> Decimal:
    if amount is None:
        return Decimal("0")
    try:
        return Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _money(money_dict: dict[str, Any] | None) -> tuple[Decimal, str]:
    """Extract (amount, currencyCode) from a `{amount, currencyCode}` dict."""
    if not money_dict:
        return Decimal("0"), ""
    return _to_decimal(money_dict.get("amount")), (money_dict.get("currencyCode") or "").upper()


def _dts(li: dict[str, Any]) -> dict[str, Any]:
    return li.get("discountedTotalSet") or {}


def _line_shop_money_tuple(li: dict[str, Any]) -> tuple[Decimal, str]:
    return _money(_dts(li).get("shopMoney"))


def _line_presentment_money_tuple(li: dict[str, Any]) -> tuple[Decimal, str]:
    return _money(_dts(li).get("presentmentMoney"))


def _line_shop_amount(li: dict[str, Any]) -> Decimal:
    return _line_shop_money_tuple(li)[0]


def _line_presentment_amount(li: dict[str, Any]) -> Decimal:
    return _line_presentment_money_tuple(li)[0]


# --- donor helpers --------------------------------------------------------


def _build_donor(order: dict[str, Any]) -> Donor:
    customer = order.get("customer") or {}
    cust_id = customer.get("id") or ""
    first = _clean(customer.get("firstName"))
    last = _clean(customer.get("lastName"))
    email = _clean(customer.get("email") or order.get("email"))
    if email:
        email = email.lower()
    phone = _clean(customer.get("phone"))
    address = _build_address(customer.get("defaultAddress"))
    donor_type = "Organization" if (address and address.company) else "Individual"
    donor_name = _join_name(first, last) or _name_from_email(email) or cust_id or "Unknown Donor"
    return Donor(
        shopify_customer_id=cust_id,
        donor_name=donor_name,
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
        address=address,
        donor_type=donor_type,
    )


def _build_address(addr: dict[str, Any] | None) -> AddressData | None:
    if not addr:
        return None
    return AddressData(
        address1=_clean(addr.get("address1")),
        address2=_clean(addr.get("address2")),
        city=_clean(addr.get("city")),
        province=_clean(addr.get("provinceCode") or addr.get("province")),
        country=_clean(addr.get("country")),
        postal_code=_clean(addr.get("zip")),
        phone=_clean(addr.get("phone")),
        company=_clean(addr.get("company")),
    )


def _iso_date(iso_ts: str | None) -> str:
    """Reduce a Shopify ISO-8601 timestamp to a calendar date (YYYY-MM-DD)."""
    if not iso_ts:
        return ""
    try:
        # Shopify timestamps are UTC (Z). fromisoformat handles the trailing Z
        # in Python 3.11+.
        return (
            datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(UTC).date().isoformat()
        )
    except ValueError:
        return iso_ts[:10]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _join_name(first: str | None, last: str | None) -> str | None:
    parts = [p for p in (first, last) if p]
    return " ".join(parts) if parts else None


def _name_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0]
    return local or None


__all__ = ["transform_order"]
