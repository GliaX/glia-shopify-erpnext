"""launchgood_transform tests: LaunchGood records -> Donor/Donation models."""

from __future__ import annotations

from decimal import Decimal

from glia_shopify_sync.launchgood_transform import (
    ANONYMOUS_CONTACT_KEY,
    anonymous_donor,
    donation_record_to_donation,
    donor_identity_key,
    donor_record_to_donor,
    normalize_name,
    subscriber_to_donor,
)

DONOR_RECORD = {
    "donorName": "Amina Yusuf",
    "donorEmail": "Amina@Example.org",
    "donorProfileImage": None,
    "donorCountry": "Canada",
    "donorCountryCode": "CA",
    "totalContributions": "5110.47",
    "totalDonations": 7,
    "lastDonatedAt": "2026-08-26T08:15:23.000Z",
}

DONATION_RECORD = {
    "ID": 19436284,
    "Name": "Amina Yusuf",
    "Location": "Ottawa, Canada",
    "IsAnonymous": False,
    "IsPrivateAmount": False,
    "Amount": "170.0000",
    "CurrencyCode": "CAD",
    "CurrencySymbol": "$",
    "DonationID": 21415308,
    "Date": "2026-02-25T02:53:26+00:00",
}


def test_donor_record_to_donor():
    donor = donor_record_to_donor(DONOR_RECORD)
    assert donor.shopify_customer_id == "launchgood:email:amina@example.org"
    assert donor.donor_name == "Amina Yusuf"
    assert donor.first_name == "Amina"
    assert donor.last_name == "Yusuf"
    assert donor.email == "amina@example.org"


def test_donor_without_email_falls_back_to_name_key():
    donor = donor_record_to_donor({"donorName": "Bilal Khan", "donorCountry": "Canada"})
    assert donor.shopify_customer_id == "launchgood:name:bilal khan|canada"


def test_donor_empty_record_returns_none():
    assert donor_record_to_donor({}) is None


def test_donation_record_to_donation():
    d = donation_record_to_donation(DONATION_RECORD, campaign_name="Scrubs for PTs")
    assert d.shopify_order_id == "launchgood:21415308"
    assert d.shopify_line_item_id == "launchgood:21415308"
    assert d.amount == Decimal("170.00")
    assert d.currency == "CAD"
    assert d.date == "2026-02-25"
    assert d.campaign == "Scrubs for PTs"
    assert d.source == "LaunchGood"
    assert d.donor_shopify_customer_id == ""  # resolved by the sync


def test_anonymous_donation_uses_shared_key():
    record = {**DONATION_RECORD, "IsAnonymous": True, "Name": "An Anonymous kind soul"}
    d = donation_record_to_donation(record, campaign_name="X")
    assert d.donor_shopify_customer_id == ANONYMOUS_CONTACT_KEY


def test_donation_without_id_or_amount_dropped():
    assert donation_record_to_donation({"Name": "X"}, campaign_name="c") is None
    assert donation_record_to_donation({"DonationID": 1}, campaign_name="c") is None


def test_anonymous_donor_model():
    d = anonymous_donor()
    assert d.shopify_customer_id == ANONYMOUS_CONTACT_KEY
    assert d.email is None


def test_subscriber_to_donor_with_address():
    sub = {
        "user": {
            "email": "Amanda@Example.com",
            "name": "Amanda Gray",
            "firstName": "Amanda",
            "lastName": "Gray",
        },
        "phoneNumber": "+1 555 123 4567",
        "properties": {
            "subscriptionAddressData": {
                "city": "Homer",
                "state": "NY",
                "country": "United States",
                "postalCode": "13077",
                "streetName": "North Main Street",
            }
        },
    }
    donor = subscriber_to_donor(sub)
    assert donor.shopify_customer_id == "launchgood:email:amanda@example.com"
    assert donor.phone == "+1 555 123 4567"
    assert donor.address is not None
    assert donor.address.city == "Homer"
    assert donor.address.country == "United States"


def test_normalize_name():
    assert normalize_name("  Amina   Yusuf ") == "amina yusuf"
    assert normalize_name("") == ""


def test_donor_identity_key():
    assert donor_identity_key("X Y", "X@y.org") == "launchgood:email:x@y.org"
    assert donor_identity_key("X Y", None, "Canada") == "launchgood:name:x y|canada"
    assert donor_identity_key("", None) == "launchgood:unknown"


CSV_ROW = {
    "Donation ID": "21415308",
    "First Name": "Nora",
    "Last Name": "Ballantyne",
    "Display Name": "Chris and Nora",
    "Email": "Nora@Example.org",
    "Anonymous?": "Public Donor",
    "Status": "Charged",
    "Donation Currency (DC)": "CAD",
    "Donation Amount (DC)": "170.0000",
    "Project Currency (PC)": "USD",
    "Amount (May be approx.) (PC)": "119.0900",
    "Created Date": "02/25/2026",
    "Billing Country": "CA",
    "Billing City": "Ottawa",
    "Billing Address": "12 Main St",
    "Billing State": "ON",
    "Billing Zip": "K1A0B1",
}


def test_csv_row_to_donation_dual_currency():
    from glia_shopify_sync.launchgood_transform import csv_row_to_donation

    d = csv_row_to_donation(CSV_ROW, campaign_name="Scrubs")
    assert d.shopify_order_id == "launchgood:21415308"
    assert d.amount == Decimal("119.09")
    assert d.currency == "USD"
    assert d.amount_presentment == Decimal("170.00")
    assert d.currency_presentment == "CAD"
    assert d.date == "2026-02-25"
    assert d.financial_status == "paid"


def test_csv_row_to_donation_skips_non_charged():
    from glia_shopify_sync.launchgood_transform import csv_row_to_donation

    for status in ("Refunded", "Pending", "Failed", ""):
        assert csv_row_to_donation({**CSV_ROW, "Status": status}, campaign_name="c") is None


def test_csv_row_to_donor_real_identity_with_address():
    from glia_shopify_sync.launchgood_transform import csv_row_to_donor

    donor = csv_row_to_donor(CSV_ROW)
    assert donor.shopify_customer_id == "launchgood:email:nora@example.org"
    assert donor.donor_name == "Nora Ballantyne"
    assert donor.address is not None
    assert donor.address.city == "Ottawa"
    assert donor.address.postal_code == "K1A0B1"


def test_csv_row_to_donor_no_email_name_key():
    from glia_shopify_sync.launchgood_transform import csv_row_to_donor

    row = {**CSV_ROW, "Email": "", "Email Address": ""}
    donor = csv_row_to_donor(row)
    assert donor.shopify_customer_id == "launchgood:name:nora ballantyne|ca"


def test_clean_email_drops_masked_and_malformed():
    from glia_shopify_sync.launchgood_transform import csv_row_to_donor

    for bad in ("0f9f6891a0558d02cf59be65559d5bdc", "ashamohamed282@gmai", "kavya", ""):
        donor = csv_row_to_donor({**CSV_ROW, "Email": bad, "Email Address": ""})
        assert donor.email is None
        assert donor.shopify_customer_id.startswith("launchgood:name:")
    donor = csv_row_to_donor(CSV_ROW)
    assert donor.email == "nora@example.org"


def test_csv_row_old_format_single_currency():
    """2022-era reports lack the DC columns — Amount (PC) covers both."""
    from glia_shopify_sync.launchgood_transform import csv_row_to_donation

    row = {
        "Donation ID": "5625777",
        "First Name": "Zachary",
        "Last Name": "Petriw",
        "Status": "Charged",
        "Project Currency (PC)": "USD",
        "Amount (PC)": "20.0000",
        "Created Date": "03/27/2022",
    }
    d = csv_row_to_donation(row, campaign_name="Old Campaign")
    assert d is not None
    assert d.amount == Decimal("20.00")
    assert d.amount_presentment == Decimal("20.00")
    assert d.currency == d.currency_presentment == "USD"
    assert d.date == "2022-03-27"
