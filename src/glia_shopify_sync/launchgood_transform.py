"""Transform LaunchGood records into Donor + Donation models.

Three record shapes (see launchgood_client.py for where each comes from):

  * **Donor aggregates** (v4 ``/accounts/{id}/donors``): donorName, donorEmail,
    donorCountry, totalContributions, totalDonations, lastDonatedAt. PII
    (email) but no per-donation detail → ERPNext Contacts.
  * **Donor Report CSV rows** (v3 ``projectreports/downloadInfo`` +
    ``reports/action``, per campaign): one row per donation with Donation ID,
    real First/Last Name, Email, billing address, and amounts in donor + project
    currency — even for publicly-"Anonymous" donors. → ERPNext Donations with
    precise Contact linkage (by email).
  * **Campaign donations feed** (v3 ``/api/projectapi/donors``): DonationID,
    display Name, Location, Amount, Date, IsAnonymous — public fields only, no
    email. Fallback when the CSV report fails; Contacts matched by name.

External IDs reuse the generic ``shopify_customer_id`` / ``shopify_order_id``
model fields with a ``launchgood:`` prefix (same convention as the Patreon
sync), so ERPNext dedup keeps working unchanged. Donor identity is email-based
(``launchgood:email:{email}``) because the donors endpoint has no stable id.

Pure functions — no I/O.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .models import AddressData, Donation, Donor

SOURCE = "LaunchGood"

ANONYMOUS_CONTACT_KEY = "launchgood:anonymous"
ANONYMOUS_CONTACT_NAME = "LaunchGood Anonymous"


def _clean_email(v: Any) -> str | None:
    """Normalize an email; drop masked hashes and malformed addresses.

    LaunchGood rows sometimes contain masked values (32-hex-char hashes) or
    typos ('name@gmail', 'kavya') that ERPNext's email validation rejects.
    """
    email = str(v or "").strip().lower()
    if not email or "@" not in email:
        return None
    local, _, domain = email.rpartition("@")
    if not local or "." not in domain:
        return None
    return email


def csv_row_to_donor(row: dict[str, str]) -> Donor | None:
    """Donor Report CSV row → Donor (real identity, even for public-anonymous).

    Identity is email-based; without an email, first+last+billing-country.
    """
    email = _clean_email(row.get("Email") or row.get("Email Address"))
    first = (row.get("First Name") or "").strip()
    last = (row.get("Last Name") or "").strip()
    name = " ".join(p for p in (first, last) if p) or (row.get("Display Name") or "").strip()
    if not name and not email:
        return None
    country = (row.get("Billing Country") or "").strip()
    address = None
    if (row.get("Billing Address") or row.get("Billing City") or "").strip():
        address = AddressData(
            address1=(row.get("Billing Address") or "").strip() or None,
            address2=(row.get("Billing Address 2") or "").strip() or None,
            city=(row.get("Billing City") or "").strip() or None,
            province=(row.get("Billing State") or "").strip() or None,
            country=country or None,
            postal_code=(row.get("Billing Zip") or "").strip() or None,
        )
    return Donor(
        shopify_customer_id=donor_identity_key(name, email, country),
        donor_name=name or (email or "LaunchGood Donor"),
        first_name=first or None,
        last_name=last or None,
        email=email,
        address=address,
        donor_type="Individual",
    )


def csv_row_to_donation(row: dict[str, str], *, campaign_name: str) -> Donation | None:
    """Donor Report CSV row → Donation.

    Amounts: `Donation Amount (DC)` is the donor's own currency (presentment);
    `Amount (May be approx.) (PC)` is the project's accounting currency. Older
    (2022-era) reports lack the DC columns entirely — then `Amount (PC)` is
    both. Only ``Charged`` rows are real money (skip pending/failed/refunded).
    """
    if (row.get("Status") or "").strip().lower() != "charged":
        return None
    donation_id = (row.get("Donation ID") or "").strip()
    if not donation_id:
        return None
    amount_pc = _money(row.get("Amount (May be approx.) (PC)") or row.get("Amount (PC)"))
    amount_dc = _money(row.get("Donation Amount (DC)")) or amount_pc
    if amount_dc is None or amount_dc <= 0:
        return None
    if amount_pc is None:
        amount_pc = amount_dc
    currency_pc = (row.get("Project Currency (PC)") or "USD").strip().upper()
    currency_dc = (row.get("Donation Currency (DC)") or currency_pc).strip().upper()
    date = _us_date(row.get("Created Date") or "")
    return Donation(
        shopify_order_id=f"launchgood:{donation_id}",
        shopify_order_name=f"LaunchGood #{donation_id}",
        shopify_line_item_id=f"launchgood:{donation_id}",
        donor_shopify_customer_id="",  # resolved by the sync
        date=date,
        amount=amount_pc,
        currency=currency_pc,
        amount_presentment=amount_dc,
        currency_presentment=currency_dc,
        donation_type="One-time",
        campaign=campaign_name,
        financial_status="paid",
        source=SOURCE,
        tags=("launchgood",),
    )


def _us_date(mmddyyyy: str) -> str:
    """'02/25/2026' → '2026-02-25' (passes through anything else)."""
    parts = (mmddyyyy or "").strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        m, d, y = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return (mmddyyyy or "")[:10]


def donor_record_to_donor(record: dict[str, Any]) -> Donor | None:
    """v4 donor aggregate → Donor (identity: email, else name+country)."""
    name = (record.get("donorName") or "").strip()
    email = _clean_email(record.get("donorEmail"))
    country = (record.get("donorCountry") or "").strip()
    if not name and not email:
        return None
    first, _, last = name.partition(" ")
    return Donor(
        shopify_customer_id=donor_identity_key(name, email, country),
        donor_name=name or (email or "LaunchGood Donor"),
        first_name=first or None,
        last_name=last or None,
        email=email,
    )


def donor_identity_key(name: str, email: str | None, country: str = "") -> str:
    """Stable external key for a LaunchGood donor (no id in the API)."""
    if email:
        return f"launchgood:email:{email.lower()}"
    norm = normalize_name(name)
    if norm:
        return f"launchgood:name:{norm}|{country.strip().lower()}"
    return "launchgood:unknown"


def normalize_name(name: str) -> str:
    """Lowercased, whitespace-collapsed name for fuzzy donor matching."""
    return " ".join((name or "").lower().split())


def donation_record_to_donation(record: dict[str, Any], *, campaign_name: str) -> Donation | None:
    """v3 campaign donation record → Donation (keyed by DonationID)."""
    donation_id = record.get("DonationID")
    if not donation_id:
        return None
    amount = _money(record.get("Amount"))
    if amount is None:
        return None
    currency = (record.get("CurrencyCode") or "USD").upper()
    is_anon = bool(record.get("IsAnonymous"))
    name = (record.get("Name") or "").strip()
    return Donation(
        shopify_order_id=f"launchgood:{donation_id}",
        shopify_order_name=f"LaunchGood #{donation_id}",
        shopify_line_item_id=f"launchgood:{donation_id}",
        donor_shopify_customer_id=(
            ANONYMOUS_CONTACT_KEY
            if is_anon or not name
            else ""  # resolved to a real contact key by the sync
        ),
        date=_date(record.get("Date")),
        amount=amount,
        currency=currency,
        amount_presentment=amount,
        currency_presentment=currency,
        donation_type="One-time",
        campaign=campaign_name,
        financial_status="paid",
        source=SOURCE,
        tags=("launchgood",),
    )


def anonymous_donor() -> Donor:
    return Donor(
        shopify_customer_id=ANONYMOUS_CONTACT_KEY,
        donor_name=ANONYMOUS_CONTACT_NAME,
        donor_type="Individual",
    )


def subscriber_to_donor(record: dict[str, Any]) -> Donor | None:
    """v4 recurring subscriber → Donor with address (PII-rich record)."""
    user = record.get("user") or {}
    email = (user.get("email") or "").strip().lower() or None
    name = (user.get("name") or "").strip()
    if not name and not email:
        return None
    addr = (record.get("properties") or {}).get("subscriptionAddressData") or {}
    address = None
    if addr.get("formattedAddress") or addr.get("city"):
        from .models import AddressData

        address = AddressData(
            address1=addr.get("streetName") or None,
            city=addr.get("city") or None,
            province=addr.get("state") or None,
            country=addr.get("country") or None,
            postal_code=addr.get("postalCode") or None,
        )
    first = user.get("firstName") or (name.partition(" ")[0] if name else None)
    last = user.get("lastName") or (name.partition(" ")[2] if " " in name else None)
    return Donor(
        shopify_customer_id=donor_identity_key(name, email, addr.get("country", "")),
        donor_name=name or (email or "LaunchGood Donor"),
        first_name=first or None,
        last_name=last or None,
        email=email,
        phone=record.get("phoneNumber") or None,
        address=address,
        donor_type="Individual",
    )


def _money(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(ts: Any) -> str:
    if not ts:
        return ""
    return str(ts)[:10]


__all__ = [
    "ANONYMOUS_CONTACT_KEY",
    "ANONYMOUS_CONTACT_NAME",
    "SOURCE",
    "anonymous_donor",
    "csv_row_to_donation",
    "csv_row_to_donor",
    "donation_record_to_donation",
    "donor_identity_key",
    "donor_record_to_donor",
    "normalize_name",
    "subscriber_to_donor",
]
