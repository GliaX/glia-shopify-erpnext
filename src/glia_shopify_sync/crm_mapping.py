"""Map internal Donor/Donation models to Frappe CRM (core Contact) + custom Donation.

Frappe CRM has no top-level contact doctype of its own — it uses the **core
`Contact`** doctype for people (`CRM Contacts` is only a child table of
leads/deals). So:

  * `Contact`  <- donor (first_name, last_name, email_ids, phone_nos) + a custom
                  `shopify_customer_id` field for idempotent dedup.
  * `Donation` <- one donation line item (CUSTOM doctype, module Glia), linked to
                  the `Contact`. See `donation_doctype.py`.

Pure functions (model -> dict); HTTP is handled by `FrappeClient`.
"""

from __future__ import annotations

from typing import Any

from .models import Donation, Donor

DONATION_MODULE = "Glia"


def donor_to_contact(donor: Donor) -> dict[str, Any]:
    """Donor -> core `Contact` document (email/phone via standard child tables)."""
    email_ids: list[dict[str, Any]] = []
    if donor.email:
        email_ids.append({"email_id": donor.email, "is_primary": 1})
    phone_nos: list[dict[str, Any]] = []
    if donor.phone:
        phone_nos.append({"phone": donor.phone, "is_primary_mobile_no": 1})
    return {
        "doctype": "Contact",
        "first_name": donor.first_name or "",
        "last_name": donor.last_name or "",
        "email_ids": email_ids,
        "phone_nos": phone_nos,
        # custom field (created by setup CLI):
        "shopify_customer_id": donor.shopify_customer_id,
    }


def donation_to_doc(
    donation: Donation, *, contact_name: str, donor_email: str = ""
) -> dict[str, Any]:
    """Donation -> custom `Donation` document, linked to its Contact."""
    return {
        "doctype": "Donation",
        "contact": contact_name,
        "donor_name": _donor_label(donation),
        "donor_email": donor_email,
        "donation_date": donation.date,
        "amount": _to_float(donation.amount),
        "currency": donation.currency,
        "amount_presentment": _to_float(donation.amount_presentment),
        "currency_presentment": donation.currency_presentment,
        "donation_type": donation.donation_type,
        "campaign": donation.campaign,
        "tier": donation.tier or "",
        "includes_tip": 1 if donation.includes_tip else 0,
        "financial_status": donation.financial_status,
        "shopify_order_id": donation.shopify_order_id,
        "shopify_order_name": donation.shopify_order_name,
        "shopify_line_item_id": donation.shopify_line_item_id,
    }


# --- dedup filter builders ------------------------------------------------


def filter_contact_by_shopify_id(shopify_customer_id: str) -> list[Any]:
    return [["Contact", "shopify_customer_id", "=", shopify_customer_id]]


def filter_contact_by_email(email: str) -> list[Any]:
    return [["Contact", "email_id", "=", (email or "").lower()]]


def filter_donation_by_key(shopify_order_id: str, shopify_line_item_id: str) -> list[Any]:
    return [
        ["Donation", "shopify_order_id", "=", shopify_order_id],
        ["Donation", "shopify_line_item_id", "=", shopify_line_item_id],
    ]


# --- helpers --------------------------------------------------------------


def _to_float(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _donor_label(donation: Donation) -> str:
    return f"{donation.shopify_order_name} · {donation.campaign}".strip(" ·")


__all__ = [
    "DONATION_MODULE",
    "donation_to_doc",
    "donor_to_contact",
    "filter_contact_by_email",
    "filter_contact_by_shopify_id",
    "filter_donation_by_key",
]
