"""Transform Patreon member data into Donor + Donation models.

Two types of donations are produced:
  * **Lifetime backfill** — one donation per member for their total historical
    support (lifetime_support_cents), dated at pledge_relationship_start.
  * **Monthly charge** — one donation per member for the most recent monthly
    charge (currently_entitled_amount_cents), dated at last_charge_date.

The sync's dedup keys (patreon:{member_id}:{period}) ensure idempotency:
backfill lifetime donations are created once; monthly donations are created
only when a new charge_date appears.
"""

from __future__ import annotations

from decimal import Decimal

from .models import Donation, Donor

USD = "USD"


def member_to_donor(member: dict) -> Donor:
    """Patreon member → Donor."""
    a = member.get("attributes", {})
    u = member.get("_user", {})
    mid = member.get("id", "")
    return Donor(
        shopify_customer_id=f"patreon:{mid}",
        donor_name=a.get("full_name") or "Unknown Patron",
        email=u.get("email") or None,
        donor_type="Individual",
    )


def member_to_lifetime_donation(member: dict) -> Donation | None:
    """One donation representing total historical Patreon support."""
    a = member.get("attributes", {})
    lifetime = int(a.get("lifetime_support_cents") or 0)
    if lifetime <= 0:
        return None
    mid = member.get("id", "")
    tiers = member.get("_tiers") or []
    amt = Decimal(lifetime) / Decimal(100)
    return Donation(
        shopify_order_id=f"patreon:{mid}",
        shopify_order_name="Patreon (backfill)",
        shopify_line_item_id=f"patreon:{mid}:lifetime",
        donor_shopify_customer_id=f"patreon:{mid}",
        date=_date(a.get("pledge_relationship_start")),
        amount=amt,
        currency=USD,
        amount_presentment=amt,
        currency_presentment=USD,
        donation_type="Recurring",
        campaign=f"Patreon - {', '.join(tiers) if tiers else 'General'}",
        financial_status=a.get("last_charge_status") or "",
        source="Patreon",
    )


def member_to_monthly_donation(member: dict) -> Donation | None:
    """One donation for the most recent paid monthly charge (if active + Paid)."""
    a = member.get("attributes", {})
    if a.get("patron_status") != "active_patron":
        return None
    cents = int(a.get("currently_entitled_amount_cents") or 0)
    if cents <= 0 or a.get("last_charge_status") != "Paid":
        return None
    lcd = a.get("last_charge_date") or ""
    if not lcd:
        return None
    mid = member.get("id", "")
    tiers = member.get("_tiers") or []
    amt = Decimal(cents) / Decimal(100)
    return Donation(
        shopify_order_id=f"patreon:{mid}",
        shopify_order_name=f"Patreon {lcd[:7]}",
        shopify_line_item_id=f"patreon:{mid}:{lcd[:10]}",
        donor_shopify_customer_id=f"patreon:{mid}",
        date=_date(lcd),
        amount=amt,
        currency=USD,
        amount_presentment=amt,
        currency_presentment=USD,
        donation_type="Recurring",
        campaign=f"Patreon - {', '.join(tiers) if tiers else 'General'}",
        financial_status="Paid",
        source="Patreon",
    )


def _date(ts: str | None) -> str:
    if not ts:
        return ""
    return ts[:10]


__all__ = ["member_to_donor", "member_to_lifetime_donation", "member_to_monthly_donation"]
