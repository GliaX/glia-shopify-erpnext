"""Pure transforms: ShopCustomer -> ERPNext Customer (+ Address) dicts.

No I/O. Each returns a plain dict ready for `FrappeClient.insert`.

Dedup: a custom `shopify_customer_id` field on `Customer` (created by
`glia-shop-setup`). The customer's email is also stored in a `shopify_email`
custom field so it can be cross-referenced with the donation sync's `Contact`
records (donors) by email — a Customer for a donor and the donor's Contact are
distinct doctypes in ERPNext; linking them is deferred to the order phase.
"""

from __future__ import annotations

from typing import Any

from .models import ShopCustomer


def customer_to_doc(
    customer: ShopCustomer,
    *,
    customer_group: str,
    territory: str,
    currency: str,
) -> dict[str, Any]:
    return {
        "doctype": "Customer",
        "customer_name": customer.customer_name,
        "customer_type": "Company" if customer.is_company else "Individual",
        "customer_group": customer_group,
        "territory": territory,
        "default_currency": customer.currency or currency,
        "disabled": 1 if customer.state == "disabled" else 0,
        # custom fields (created by glia-shop-setup):
        "shopify_customer_id": customer.id,
        "shopify_email": customer.email or "",
    }


def address_to_doc(customer: ShopCustomer, *, customer_name: str) -> dict[str, Any] | None:
    """A Shipping Address linked to the Customer via Dynamic Link. None if no
    address."""
    addr = customer.default_address
    # address_line1 is mandatory on the Address doctype; skip without it.
    if not addr or not addr.address1:
        return None
    title = (
        customer.customer_name
        if not customer.is_company
        else (addr.company or customer.customer_name)
    )
    return {
        "doctype": "Address",
        "address_title": title[:140] or customer_name[:140],
        "address_type": "Shipping",
        "address_line1": addr.address1 or "",
        "address_line2": addr.address2 or "",
        "city": addr.city or "",
        "state": addr.province or "",
        "country": _country(addr.country),
        "pincode": addr.zip or "",
        "phone": addr.phone or "",
        "email_id": customer.email or "",
        "links": [{"link_doctype": "Customer", "link_name": customer_name}],
    }


def filter_customer_by_shopify_id(shopify_customer_id: str) -> list[Any]:
    return [["Customer", "shopify_customer_id", "=", shopify_customer_id]]


# ERPNext's Country names are title-cased ("Canada", "United States"). Shopify
# sends full country names too; this is a light normalization for common cases.
_COUNTRY_FIXES = {
    "USA": "United States",
    "US": "United States",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
}


def _country(raw: str | None) -> str:
    if not raw:
        return ""
    key = raw.strip()
    return _COUNTRY_FIXES.get(key.upper(), key)


__all__ = [
    "address_to_doc",
    "customer_to_doc",
    "filter_customer_by_shopify_id",
]
