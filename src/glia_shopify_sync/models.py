"""Internal data models for the transform pipeline.

These are deliberately decoupled from ERPNext's DocType field names. Phase 2's
ERPNext client maps `Donor` / `Donation` to the actual Frappe doctype dicts.
Money is held as `Decimal` for accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class AddressData:
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    province: str | None = None  # provinceCode
    country: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    company: str | None = None


@dataclass(frozen=True)
class Donor:
    """A donor-to-be in ERPNext (maps to the Nonprofit `Donor` doctype)."""

    shopify_customer_id: str
    donor_name: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: AddressData | None = None
    # "Individual" | "Organization"
    donor_type: str = "Individual"


@dataclass(frozen=True)
class Donation:
    """A single donation line item (maps to the Nonprofit `Donation` doctype)."""

    shopify_order_id: str
    shopify_order_name: str  # "#6076"
    shopify_line_item_id: str
    donor_shopify_customer_id: str
    # ISO date (YYYY-MM-DD) derived from Order.processedAt
    date: str
    # Shop money (accounting currency, e.g. CAD) — the primary amount.
    amount: Decimal
    currency: str
    # Donor's original presentment money, preserved verbatim.
    amount_presentment: Decimal
    currency_presentment: str
    # "One-time" | "Recurring"
    donation_type: str
    # Shopify product title (e.g. "Glia4Gaza - One-Time Gift"); the campaign.
    campaign: str
    # Variant title for the tier (e.g. "$50: One-Time Gift").
    tier: str | None = None
    includes_tip: bool = False
    financial_status: str = ""


@dataclass(frozen=True)
class TransformResult:
    """Output of transforming a single Shopify Order."""

    donor: Donor
    donations: list[Donation] = field(default_factory=list)


__all__ = ["AddressData", "Donor", "Donation", "TransformResult"]
