"""setup_erpnext tests: idempotent creation of module / Contact field / doctype."""

from __future__ import annotations

from glia_shopify_sync.setup_erpnext import run_setup
from tests.conftest import FakeFrappeClient


def test_run_setup_creates_everything_when_empty(fake_frappe: FakeFrappeClient):
    results = run_setup(fake_frappe)
    actions = {r.name: r.action for r in results}
    assert actions["Glia"] == "created"
    assert actions["Contact.shopify_customer_id"] == "created"
    assert actions["Donation"] == "created"

    doctypes_inserted = {d["doctype"] for d in fake_frappe.inserted}
    assert {"Module Def", "Custom Field", "DocType"} <= doctypes_inserted


def test_run_setup_is_idempotent(fake_frappe: FakeFrappeClient):
    run_setup(fake_frappe)
    second = run_setup(fake_frappe)
    assert all(r.action == "exists" for r in second)
    donation_inserts = [d for d in fake_frappe.inserted if d.get("doctype") == "DocType"]
    assert len(donation_inserts) == 1
