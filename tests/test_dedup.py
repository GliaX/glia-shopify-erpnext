"""Dedup key tests."""

from __future__ import annotations

from glia_shopify_sync.dedup import donation_key, donor_key
from glia_shopify_sync.models import Donation, Donor


def _donor(email=None, first=None, last=None, phone=None, cust_id="c1") -> Donor:
    return Donor(
        shopify_customer_id=cust_id,
        donor_name=" ".join(p for p in (first, last) if p) or "X",
        first_name=first,
        last_name=last,
        email=email,
        phone=phone,
    )


def test_donor_key_prefers_email():
    d = _donor(email="A@B.com", first="Jo", last="Ng")
    assert donor_key(d) == "email:a@b.com"


def test_donor_key_case_insensitive_email():
    assert donor_key(_donor(email="A@B.com")) == donor_key(_donor(email="a@b.COM"))


def test_donor_key_falls_back_to_name_and_phone():
    d = _donor(first="Jo", last="Ng", phone="+1-555-222-1111")
    assert donor_key(d) == "name:jo ng|+1-555-222-1111"


def test_donor_key_falls_back_to_customer_id():
    assert donor_key(_donor(cust_id="gid://shopify/Customer/9")) == "cust:gid://shopify/Customer/9"


def test_donation_key_unique_per_line():
    d1 = Donation(
        shopify_order_id="gid://shopify/Order/1",
        shopify_order_name="#1",
        shopify_line_item_id="gid://shopify/LineItem/10",
        donor_shopify_customer_id="c",
        date="2026-01-01",
        amount=__import__("decimal").Decimal("1"),
        currency="CAD",
        amount_presentment=__import__("decimal").Decimal("1"),
        currency_presentment="CAD",
        donation_type="One-time",
        campaign="X",
    )
    d2 = Donation(**{**d1.__dict__, "shopify_line_item_id": "gid://shopify/LineItem/11"})
    assert donation_key(d1) != donation_key(d2)
    assert donation_key(d1) == "gid://shopify/Order/1|gid://shopify/LineItem/10"
