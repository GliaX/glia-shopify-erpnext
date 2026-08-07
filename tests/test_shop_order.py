"""Order transform + sync tests (Phase 4)."""

from __future__ import annotations

import types
from decimal import Decimal

from glia_shopify_sync.config import ShopConfig
from glia_shopify_sync.shop.erpnext_order_mapping import sales_order_doc
from glia_shopify_sync.shop.models import order_from_node
from glia_shopify_sync.shop.order_sync import process_orders

DONATION = "gid://shopify/Product/7962927005795"  # a donation product id
TEE = "gid://shopify/Product/6575321776227"  # a merch product id
TEE_VARIANT = "gid://shopify/ProductVariant/43000000000001"


def _order_node():
    return {
        "id": "gid://shopify/Order/7001",
        "name": "#7001",
        "processedAt": "2026-05-01T10:00:00Z",
        "displayFinancialStatus": "PAID",
        "test": False,
        "currencyCode": "CAD",
        "email": "shopper@example.com",
        "customer": {"id": "gid://shopify/Customer/9", "email": "shopper@example.com"},
        "totalPriceSet": {"shopMoney": {"amount": "60.00", "currencyCode": "CAD"}},
        "lineItems": {
            "edges": [
                {  # merch line -> Sales Order item
                    "node": {
                        "id": "gid://shopify/LineItem/1",
                        "name": "Glia T-shirt - M",
                        "quantity": 2,
                        "product": {"id": TEE, "title": "Glia T-shirt", "tags": []},
                        "variant": {"id": TEE_VARIANT, "title": "M"},
                        "discountedTotalSet": {
                            "shopMoney": {"amount": "60.00", "currencyCode": "CAD"}
                        },
                    }
                },
                {  # donation line -> excluded (already a Donation record)
                    "node": {
                        "id": "gid://shopify/LineItem/2",
                        "name": "One Time Donation - $50",
                        "quantity": 1,
                        "product": {"id": DONATION, "title": "One Time Donation", "tags": []},
                        "variant": {"id": "gid://shopify/ProductVariant/d1", "title": "$50"},
                        "discountedTotalSet": {
                            "shopMoney": {"amount": "50.00", "currencyCode": "CAD"}
                        },
                    }
                },
            ]
        },
    }


def test_parses_order_and_excludes_donation_lines():
    o = order_from_node(_order_node(), donation_gids={DONATION})
    assert o.name == "#7001"
    assert o.financial_status == "PAID"
    assert o.total == Decimal("60.00")
    assert o.customer_id == "gid://shopify/Customer/9"
    assert len(o.lines) == 2
    shop_lines = o.shop_lines
    assert len(shop_lines) == 1
    assert shop_lines[0].product_id == TEE
    assert shop_lines[0].variant_id == TEE_VARIANT
    assert shop_lines[0].quantity == 2
    assert shop_lines[0].rate == Decimal("30.00")  # 60.00 / 2
    assert o.is_shop_order is True


def test_sales_order_doc_resolves_variant_and_skips_donation():
    o = order_from_node(_order_node(), donation_gids={DONATION})
    item_codes = {TEE_VARIANT: "glia-t-shirt-unisex-tee-m-blk", TEE: "glia-t-shirt-unisex"}
    doc = sales_order_doc(
        o,
        company="Glia",
        price_list="Standard Selling",
        currency="CAD",
        customer_name="Jane Doe",
        item_codes=item_codes,
        warehouse="FG - Canada",
    )
    assert doc["doctype"] == "Sales Order"
    assert doc["customer"] == "Jane Doe"
    assert doc["company"] == "Glia"
    assert doc["naming_series"] == "SAL-ORD-.YYYY.-"
    assert doc["transaction_date"] == "2026-05-01"
    assert doc["currency"] == "CAD"
    assert doc["shopify_order_id"] == "gid://shopify/Order/7001"
    assert len(doc["items"]) == 1
    assert doc["items"][0]["item_code"] == "glia-t-shirt-unisex-tee-m-blk"
    assert doc["items"][0]["qty"] == 2.0
    assert doc["items"][0]["rate"] == 30.0


def test_sales_order_none_for_pure_donation_order():
    node = _order_node()
    # replace merch line with another donation line
    node["lineItems"]["edges"][0]["node"]["product"]["id"] = DONATION
    o = order_from_node(node, donation_gids={DONATION})
    assert o.is_shop_order is False
    doc = sales_order_doc(
        o,
        company="Glia",
        price_list="Standard Selling",
        currency="CAD",
        customer_name="X",
        item_codes={},
        warehouse="FG",
    )
    assert doc is None


def test_sales_order_falls_back_to_product_id():
    o = order_from_node(_order_node(), donation_gids={DONATION})
    # variant not in map -> fall back to product id
    item_codes = {TEE: "glia-t-shirt-unisex"}
    doc = sales_order_doc(
        o,
        company="Glia",
        price_list="Standard Selling",
        currency="CAD",
        customer_name="X",
        item_codes=item_codes,
        warehouse="FG - Canada",
    )
    assert doc["items"][0]["item_code"] == "glia-t-shirt-unisex"


def test_dry_run_counts():
    cfg = types.SimpleNamespace(
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
        donation_product_gids={DONATION},
        recurring_product_gids=set(),
    )
    stats = process_orders([_order_node()], None, cfg, dry_run=True)
    assert stats.orders_seen == 1
    assert stats.sales_orders_created == 1
    assert stats.errors == []


def test_dry_run_skips_pure_donation():
    cfg = types.SimpleNamespace(
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
        donation_product_gids={DONATION},
        recurring_product_gids=set(),
    )
    node = _order_node()
    node["lineItems"]["edges"][0]["node"]["product"]["id"] = DONATION
    stats = process_orders([node], None, cfg, dry_run=True)
    assert stats.sales_orders_created == 0
    assert stats.orders_no_shop_items == 1
