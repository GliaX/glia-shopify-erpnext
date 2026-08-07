"""Shop setup tests: idempotent creation of custom fields / Price List / Item Groups."""

from __future__ import annotations

import types

from glia_shopify_sync.config import ShopConfig
from glia_shopify_sync.shop.setup import run_setup


def _cfg():
    return types.SimpleNamespace(
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
    )


def test_run_setup_creates_everything_when_empty(fake_frappe):
    results = run_setup(fake_frappe, _cfg())
    by_name = {r.name: r.action for r in results}
    # Custom fields on Item + Item Group.
    assert by_name["Item.shopify_product_id"] == "created"
    assert by_name["Item.shopify_variant_id"] == "created"
    assert by_name["Item Group.shopify_collection_id"] == "created"
    # Price List + Item Groups.
    assert by_name["Standard Selling"] == "created"
    assert by_name["Products"] == "created"
    assert by_name["Donation"] == "created"

    doctypes_inserted = {d["doctype"] for d in fake_frappe.inserted}
    assert {"Custom Field", "Price List", "Item Group"} <= doctypes_inserted


def test_run_setup_is_idempotent(fake_frappe):
    run_setup(fake_frappe, _cfg())
    second = run_setup(fake_frappe, _cfg())
    assert all(r.action == "exists" for r in second)
    # Each Price List / Item Group inserted exactly once across both runs.
    price_list_inserts = [d for d in fake_frappe.inserted if d.get("doctype") == "Price List"]
    assert len(price_list_inserts) == 1


def test_run_setup_creates_mapped_item_groups():
    fake = type("F", (), {"existing": {}})  # not used; build a real fake
    from tests.conftest import FakeFrappeClient

    fake = FakeFrappeClient()
    shop = ShopConfig(item_group_map={"Apparel": "Merch", "Devices": "Devices"})
    cfg = types.SimpleNamespace(yaml=types.SimpleNamespace(company="Glia", shop=shop))
    run_setup(fake, cfg)
    groups = {d["item_group_name"] for d in fake.inserted if d.get("doctype") == "Item Group"}
    assert {"Products", "Donation", "Merch", "Devices"} <= groups
