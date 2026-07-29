"""sync.process_orders tests: transform + idempotent upsert with dedup."""

from __future__ import annotations

import json
from pathlib import Path

from glia_shopify_sync.sync import process_orders

FIXTURES = Path(__file__).parent / "fixtures"
DONATION_GIDS = {
    "gid://shopify/Product/7962927005795",
    "gid://shopify/Product/7967052890211",
    "gid://shopify/Product/7970874916963",
}
RECURRING_GIDS = {"gid://shopify/Product/7970874916963"}


def _order(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_merch_order_is_skipped(fake_frappe):
    orders = [_order("order_merch_only.json")]
    stats = process_orders(
        orders,
        fake_frappe,
        donation_gids=DONATION_GIDS,
        recurring_gids=RECURRING_GIDS,
        dry_run=True,
    )
    assert stats.orders_seen == 1
    assert stats.orders_skipped_not_donation == 1
    assert stats.contacts_created == 0
    assert stats.donations_created == 0


def test_dry_run_counts_but_does_not_write(fake_frappe):
    orders = [_order("order_onetime_tip.json"), _order("order_recurring.json")]
    stats = process_orders(
        orders,
        fake_frappe,
        donation_gids=DONATION_GIDS,
        recurring_gids=RECURRING_GIDS,
        dry_run=True,
    )
    assert stats.contacts_created == 2  # two distinct donors
    assert stats.donations_created == 2
    assert fake_frappe.inserted == []  # dry run writes nothing


def test_write_creates_contact_and_donation(fake_frappe):
    orders = [_order("order_onetime_tip.json")]
    stats = process_orders(
        orders, fake_frappe, donation_gids=DONATION_GIDS, recurring_gids=RECURRING_GIDS
    )
    assert stats.contacts_created == 1
    assert stats.donations_created == 1
    doctypes = [d["doctype"] for d in fake_frappe.inserted]
    assert doctypes == ["Contact", "Donation"]
    # Donation is linked to the created Contact's name.
    contact = next(d for d in fake_frappe.inserted if d["doctype"] == "Contact")
    donation = next(d for d in fake_frappe.inserted if d["doctype"] == "Donation")
    assert donation["contact"] == contact["name"]
    assert donation["donor_email"] == "loughryam@yahoo.com"  # denormalized from the donor


def test_idempotent_rerun_skips_existing(fake_frappe):
    """Pre-seed dedup sets as if a prior run already ingested the records."""
    contact_map = {"gid://shopify/Customer/9308425224291": "Existing Contact"}
    donation_keys = {"gid://shopify/Order/6984546943075|gid://shopify/LineItem/18115210575971"}
    orders = [_order("order_onetime_tip.json")]
    stats = process_orders(
        orders,
        fake_frappe,
        donation_gids=DONATION_GIDS,
        recurring_gids=RECURRING_GIDS,
        contact_map=contact_map,
        donation_keys=donation_keys,
    )
    assert stats.contacts_reused == 1
    assert stats.contacts_created == 0
    assert stats.donations_skipped == 1
    assert stats.donations_created == 0
    assert fake_frappe.inserted == []  # nothing new written


def test_last_processed_at_tracked(fake_frappe):
    orders = [_order("order_recurring.json"), _order("order_onetime_tip.json")]
    stats = process_orders(
        orders,
        fake_frappe,
        donation_gids=DONATION_GIDS,
        recurring_gids=RECURRING_GIDS,
        dry_run=True,
    )
    # newest processedAt among the two orders
    assert stats.last_processed_at in {"2026-07-28T17:18:22Z", "2026-07-28T14:08:51Z"}
    assert stats.last_processed_at == "2026-07-28T17:18:22Z"
