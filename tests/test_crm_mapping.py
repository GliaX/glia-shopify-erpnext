"""Mapping tests: Donor/Donation -> Contact / Donation."""

from __future__ import annotations

from decimal import Decimal

from glia_shopify_sync.crm_mapping import (
    donation_to_doc,
    donor_to_contact,
    filter_contact_by_shopify_id,
    filter_donation_by_key,
)
from glia_shopify_sync.models import Donation, Donor


def _donor() -> Donor:
    return Donor(
        shopify_customer_id="gid://shopify/Customer/1",
        donor_name="Anne Walsh",
        first_name="Anne",
        last_name="Walsh",
        email="anne@example.com",
        phone="+15551234",
        donor_type="Individual",
    )


def _donation(**kw) -> Donation:
    base = {
        "shopify_order_id": "gid://shopify/Order/9",
        "shopify_order_name": "#9",
        "shopify_line_item_id": "gid://shopify/LineItem/99",
        "donor_shopify_customer_id": "gid://shopify/Customer/1",
        "date": "2026-07-28",
        "amount": Decimal("54.74"),
        "currency": "CAD",
        "amount_presentment": Decimal("49.21"),
        "currency_presentment": "EUR",
        "donation_type": "One-time",
        "campaign": "Glia4Gaza - One-Time Gift",
        "tier": "$50 One Time Gift",
        "includes_tip": True,
        "financial_status": "PAID",
    }
    base.update(kw)
    return Donation(**base)


def test_donor_to_contact():
    doc = donor_to_contact(_donor())
    assert doc["doctype"] == "Contact"
    assert doc["first_name"] == "Anne"
    assert doc["last_name"] == "Walsh"
    assert doc["email_ids"] == [{"email_id": "anne@example.com", "is_primary": 1}]
    assert doc["phone_nos"] == [{"phone": "+15551234", "is_primary_mobile_no": 1}]
    assert doc["shopify_customer_id"] == "gid://shopify/Customer/1"


def test_donor_to_contact_without_email_or_phone():
    d = Donor(
        shopify_customer_id="x",
        donor_name="X",
        first_name=None,
        last_name=None,
        email=None,
        phone=None,
        donor_type="Individual",
    )
    doc = donor_to_contact(d)
    assert doc["email_ids"] == []
    assert doc["phone_nos"] == []


def test_donation_to_doc_links_contact_and_carries_both_amounts():
    doc = donation_to_doc(_donation(), contact_name="Probe Donor", donor_email="anne@example.com")
    assert doc["doctype"] == "Donation"
    assert doc["contact"] == "Probe Donor"
    assert doc["donor_email"] == "anne@example.com"
    assert doc["amount"] == 54.74
    assert doc["currency"] == "CAD"
    assert doc["amount_presentment"] == 49.21
    assert doc["currency_presentment"] == "EUR"
    assert doc["shopify_order_id"] == "gid://shopify/Order/9"


def test_filters():
    assert (
        filter_contact_by_shopify_id("gid://shopify/Customer/1")[0][3] == "gid://shopify/Customer/1"
    )
    fk = filter_donation_by_key("oid", "lid")
    assert fk[0][3] == "oid" and fk[1][3] == "lid"
