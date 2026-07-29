"""Donation doctype + Contact custom field definition tests."""

from __future__ import annotations

from glia_shopify_sync.donation_doctype import (
    contact_custom_fields,
    donation_doctype_def,
    glia_module_def,
)

FIELD_NAMES = {
    "contact",
    "donor_name",
    "donor_email",
    "donation_date",
    "amount",
    "currency",
    "amount_presentment",
    "currency_presentment",
    "donation_type",
    "campaign",
    "tier",
    "includes_tip",
    "financial_status",
    "shopify_order_id",
    "shopify_order_name",
    "shopify_line_item_id",
}


def test_donation_doctype_shape():
    d = donation_doctype_def()
    assert d["doctype"] == "DocType"
    assert d["name"] == "Donation"
    assert d["module"] == "Glia"
    assert d["custom"] == 1
    assert {f["fieldname"] for f in d["fields"]} == FIELD_NAMES


def test_required_and_unique_fields():
    by_field = {f["fieldname"]: f for f in donation_doctype_def()["fields"]}
    for reqd in ("contact", "donation_date", "amount", "shopify_order_id", "shopify_line_item_id"):
        assert by_field[reqd]["reqd"] == 1, reqd
    assert not by_field["shopify_order_id"].get("unique")  # order can have several donation lines
    assert by_field["contact"]["fieldtype"] == "Link"
    assert by_field["contact"]["options"] == "Contact"
    assert by_field["amount"]["fieldtype"] == "Currency"
    assert "One-time" in by_field["donation_type"]["options"]


def test_currency_options_point_at_currency_fields():
    by_field = {f["fieldname"]: f for f in donation_doctype_def()["fields"]}
    assert by_field["amount"]["options"] == "currency"
    assert by_field["amount_presentment"]["options"] == "currency_presentment"


def test_contact_custom_field():
    cf = contact_custom_fields()[0]
    assert cf["dt"] == "Contact"
    assert cf["fieldname"] == "shopify_customer_id"
    assert cf["fieldtype"] == "Data"
    assert cf["unique"] == 1


def test_glia_module_def_has_app_name():
    m = glia_module_def()
    assert m["doctype"] == "Module Def"
    assert m["module_name"] == "Glia"
    assert m["app_name"] == "frappe"
