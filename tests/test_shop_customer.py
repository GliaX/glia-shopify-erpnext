"""Customer transform + sync tests (Phase 3)."""

from __future__ import annotations

import types
from decimal import Decimal

from glia_shopify_sync.config import ShopConfig
from glia_shopify_sync.shop.customer_sync import process_customers
from glia_shopify_sync.shop.erpnext_customer_mapping import (
    address_to_doc,
    customer_to_doc,
    filter_customer_by_shopify_id,
)
from glia_shopify_sync.shop.models import customer_from_node


def _node(**over):
    base = {
        "id": "gid://shopify/Customer/9325989658723",
        "firstName": "Jane",
        "lastName": "Doe",
        "displayName": "Jane Doe",
        "email": "jane@example.com",
        "phone": "+14165551234",
        "tags": ["vip"],
        "state": "enabled",
        "createdAt": "2024-01-15T10:00:00Z",
        "ordersCount": 4,
        "totalSpent": {"amount": "240.00", "currencyCode": "CAD"},
        "defaultAddress": {
            "address1": "100 Main St",
            "address2": "Apt 2",
            "city": "Toronto",
            "provinceCode": "ON",
            "country": "Canada",
            "zip": "M1M1M1",
            "phone": "+14165550000",
            "company": None,
        },
    }
    base.update(over)
    return base


def test_parses_customer():
    c = customer_from_node(_node(), currency="CAD")
    assert c.id.endswith("/9325989658723")
    assert c.email == "jane@example.com"
    assert c.first_name == "Jane"
    assert c.orders_count == 4
    assert c.total_spent == Decimal("240.00")
    assert isinstance(c.total_spent, Decimal)
    assert c.currency == "CAD"
    assert c.default_address.city == "Toronto"
    assert c.is_company is False


def test_customer_to_doc():
    c = customer_from_node(_node())
    doc = customer_to_doc(
        c, customer_group="Individual", territory="All Territories", currency="CAD"
    )
    assert doc["doctype"] == "Customer"
    assert doc["customer_name"] == "Jane Doe"
    assert doc["customer_type"] == "Individual"
    assert doc["customer_group"] == "Individual"
    assert doc["territory"] == "All Territories"
    assert doc["disabled"] == 0
    assert doc["shopify_customer_id"].endswith("/9325989658723")
    assert doc["shopify_email"] == "jane@example.com"


def test_company_customer_type_when_address_has_company():
    node = _node(defaultAddress={**_node()["defaultAddress"], "company": "Acme Inc"})
    c = customer_from_node(node)
    assert c.is_company is True
    doc = customer_to_doc(
        c, customer_group="Commercial", territory="All Territories", currency="CAD"
    )
    assert doc["customer_type"] == "Company"


def test_disabled_customer_marked_disabled():
    c = customer_from_node(_node(state="disabled"))
    assert (
        customer_to_doc(
            c, customer_group="Individual", territory="All Territories", currency="CAD"
        )["disabled"]
        == 1
    )


def test_name_fallbacks_to_email():
    c = customer_from_node(_node(firstName=None, lastName=None, displayName="", email="x@y.com"))
    assert c.customer_name == "x@y.com"


def test_address_doc_linked_to_customer():
    c = customer_from_node(_node())
    doc = address_to_doc(c, customer_name="Jane Doe")
    assert doc["doctype"] == "Address"
    assert doc["address_type"] == "Shipping"
    assert doc["address_line1"] == "100 Main St"
    assert doc["country"] == "Canada"
    assert doc["links"] == [{"link_doctype": "Customer", "link_name": "Jane Doe"}]


def test_address_none_when_blank():
    c = customer_from_node(_node(defaultAddress={"address1": None, "city": None, "zip": None}))
    assert address_to_doc(c, customer_name="Jane Doe") is None


def test_country_normalization():
    c = customer_from_node(_node(defaultAddress={**_node()["defaultAddress"], "country": "USA"}))
    assert address_to_doc(c, customer_name="Jane Doe")["country"] == "United States"


def test_filter():
    f = filter_customer_by_shopify_id("gid://shopify/Customer/1")
    assert f[0][3] == "gid://shopify/Customer/1"


def test_dry_run_counts():
    cfg = types.SimpleNamespace(
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
        donation_product_gids=set(),
        recurring_product_gids=set(),
    )
    nodes = [_node(), _node(id="gid://shopify/Customer/2", email="b@y.com")]
    stats = process_customers(nodes, None, cfg, dry_run=True)
    assert stats.customers_seen == 2
    assert stats.customers_created == 2
    assert stats.addresses_created == 2  # both have a default address
    assert stats.errors == []


def test_dry_run_makes_no_inserts(fake_frappe):
    cfg = types.SimpleNamespace(
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
        donation_product_gids=set(),
        recurring_product_gids=set(),
    )
    process_customers([_node()], fake_frappe, cfg, dry_run=True)
    assert fake_frappe.inserted == []
