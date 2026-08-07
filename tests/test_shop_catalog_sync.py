"""Catalog sync orchestrator tests (dry-run + idempotent re-run)."""

from __future__ import annotations

import types

from glia_shopify_sync.config import ShopConfig
from glia_shopify_sync.shop.catalog_sync import process_products
from tests.conftest import FakeFrappeClient


def _cfg(donation_gids, group_map=None):
    return types.SimpleNamespace(
        yaml=types.SimpleNamespace(
            company="Glia",
            shop=ShopConfig(group_map=group_map or {"Apparel": "Merch"}),
        ),
        donation_product_gids=set(donation_gids),
        recurring_product_gids=set(),
    )


def _fake_with_ecommerce() -> FakeFrappeClient:
    """A fake whose `Website Item` doctype is present (E Commerce installed),
    so the publishing path is exercised."""
    return FakeFrappeClient(existing={"DocType": {"Website Item": {"name": "Website Item"}}})


def test_dry_run_counts_template_and_simple(
    product_with_variants, product_simple, donation_product_gids
):
    cfg = _cfg(donation_product_gids)
    stats = process_products([product_with_variants, product_simple], None, cfg, dry_run=True)
    assert stats.products_seen == 2
    assert stats.products_created == 2  # template Item + simple Item
    assert stats.variants_created == 2  # two t-shirt variants
    assert stats.prices_created == 3  # 2 variant prices + 1 simple price
    assert stats.website_items_created == 2  # template + simple
    assert stats.attributes_created == 2  # Size, Color
    assert stats.errors == []


def test_sync_is_idempotent_on_second_run(product_with_variants, donation_product_gids):
    fake = _fake_with_ecommerce()
    # First run creates everything.
    cfg = _cfg(donation_product_gids)
    first = process_products([product_with_variants], fake, cfg, dry_run=False)
    assert first.products_created == 1
    assert first.variants_created == 2
    assert first.prices_created == 2
    assert first.website_items_created == 1
    assert first.errors == []

    # Second run refreshes the existing docs (no new creates).
    second = process_products([product_with_variants], fake, cfg, dry_run=False)
    assert second.products_created == 0
    assert second.variants_created == 0
    assert second.prices_created == 0
    assert second.website_items_created == 0
    assert second.products_updated == 1
    assert second.variants_updated == 2
    assert second.prices_updated == 2
    assert second.website_items_updated == 1
    assert second.errors == []


def test_publish_skipped_without_ecommerce_module(product_with_variants, donation_product_gids):
    # Bare fake -> no Website Item doctype -> publishing skipped, catalog still imported.
    fake = FakeFrappeClient()
    stats = process_products(
        [product_with_variants], fake, _cfg(donation_product_gids), dry_run=False
    )
    assert stats.products_created == 1
    assert stats.website_items_created == 0
    assert stats.website_items_skipped == 1  # the template
    assert stats.errors == []


def test_dry_run_makes_no_inserts(product_with_variants, donation_product_gids):
    fake = _fake_with_ecommerce()
    process_products([product_with_variants], fake, _cfg(donation_product_gids), dry_run=True)
    assert fake.inserted == []
