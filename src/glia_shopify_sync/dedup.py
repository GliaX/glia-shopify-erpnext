"""Deduplication key derivation.

  * Donor key  -> email (lowercased), with stable fallbacks for guest checkouts
    and contacts with no email.
  * Donation key -> "<shopify_order_gid>|<shopify_line_item_gid>" (globally
    unique per donation line).

These keys are what Phase 2's ERPNext client checks before creating a doc, so
the backfill is safely re-runnable / resumable.
"""

from __future__ import annotations

from .models import Donation, Donor


def donor_key(donor: Donor) -> str:
    """Stable identity key for a donor.

    Preference order: email > first+last+phone > shopify customer id.
    Always returns a non-empty string.
    """
    if donor.email:
        return f"email:{donor.email.lower()}"
    name_phone = _join([donor.first_name, donor.last_name], donor.phone)
    if name_phone:
        return f"name:{name_phone}"
    if donor.shopify_customer_id:
        return f"cust:{donor.shopify_customer_id}"
    # Last resort: hash-ish of the donor name. Keeps it non-empty & deterministic.
    return f"name:{donor.donor_name}"


def donation_key(donation: Donation) -> str:
    """Globally unique key for a single donation line item."""
    return f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"


def _join(name_parts: list[str | None], phone: str | None) -> str:
    name = " ".join(p for p in name_parts if p).strip().lower()
    if phone:
        name += f"|{phone.strip()}"
    return name


__all__ = ["donor_key", "donation_key"]
