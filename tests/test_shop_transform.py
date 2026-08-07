"""Transform tests: Shopify catalog JSON -> ERPNext Item/Price/Website Item dicts."""

from __future__ import annotations

from decimal import Decimal

from glia_shopify_sync.shop.erpnext_catalog_mapping import (
    is_template,
    item_attribute_def,
    item_group_for,
    item_price_doc,
    product_to_item,
    template_attributes,
    template_item_code,
    variant_attributes,
    variant_item_code,
    variant_to_item,
    website_item_doc,
)
from glia_shopify_sync.shop.models import ShopVariantOption, product_from_node

COMMON = {
    "default_group": "Products",
    "group_map": {"Apparel": "Merch"},
    "donation_group": "Donations",
}


# --- parsing -------------------------------------------------------------


def test_parses_variant_product(product_with_variants, donation_product_gids):
    p = product_from_node(
        product_with_variants, currency="CAD", donation_gids=donation_product_gids
    )
    assert p.id == "gid://shopify/Product/6575321776227"
    assert p.handle == "glia-t-shirt-unisex"
    assert p.product_type == "Apparel"
    assert p.tags == ("Merch", "Glia")
    assert p.options == ("Size", "Color")
    assert len(p.variants) == 2
    v = p.variants[0]
    assert v.sku == "TEE-M-BLK"
    assert v.price == Decimal("30.00")
    assert v.currency == "CAD"
    assert v.barcode == "0123456789012"
    assert v.options == (
        ShopVariantOption(name="Size", value="M"),
        ShopVariantOption(name="Color", value="Black"),
    )
    assert p.images[0].url.endswith("tee.jpg")
    assert p.collections == ("Merch",)
    assert p.is_donation is False
    assert p.has_real_variants is True


def test_simple_product_is_not_template(product_simple, donation_product_gids):
    p = product_from_node(product_simple, donation_gids=donation_product_gids)
    assert p.has_real_variants is False
    assert p.is_donation is True
    assert p.variants[0].title == "Default Title"


# --- template + variants -------------------------------------------------


def test_template_item_built_from_variant_product(product_with_variants):
    p = product_from_node(product_with_variants, currency="CAD")
    assert is_template(p) is True

    doc = product_to_item(p, company="Glia", **COMMON)
    assert doc["doctype"] == "Item"
    assert doc["item_code"] == "glia-t-shirt-unisex"
    assert doc["item_name"] == "Glia T-shirt (Unisex)"
    assert doc["item_group"] == "Merch"  # Apparel -> Merch via group_map
    assert doc["has_variants"] == 1
    assert doc["is_stock_item"] == 1
    assert doc["disabled"] == 0
    assert doc["shopify_product_id"] == p.id
    assert doc["shopify_is_donation"] == 0
    # Template attributes carry option names only (no values).
    assert template_attributes(p) == [{"attribute": "Size"}, {"attribute": "Color"}]


def test_variant_items_linked_to_template(product_with_variants):
    p = product_from_node(product_with_variants, currency="CAD")
    tcode = template_item_code(p)
    v1 = p.variants[0]
    doc = variant_to_item(p, v1, template_code=tcode, **COMMON)
    assert doc["variant_of"] == "glia-t-shirt-unisex"
    assert doc["item_code"] == "glia-t-shirt-unisex-tee-m-blk"
    assert doc["item_name"] == "Glia T-shirt (Unisex) - M / Black"
    assert doc["shopify_variant_id"] == v1.id
    assert doc["barcode"] == "0123456789012"
    assert variant_attributes(p, v1) == [
        {"attribute": "Size", "attribute_value": "M"},
        {"attribute": "Color", "attribute_value": "Black"},
    ]


def test_variant_item_code_falls_back_to_gid_when_no_sku():
    from glia_shopify_sync.shop.models import ShopVariant

    class _P:
        handle = "no-sku-prod"
        id = "gid://shopify/Product/1"

    v = ShopVariant(
        id="gid://shopify/ProductVariant/999",
        sku=None,
        title="x",
        price=Decimal("1"),
        currency="CAD",
    )
    assert variant_item_code(_P(), v) == "no-sku-prod-999"


# --- simple / donation products ------------------------------------------


def test_simple_item_has_no_variants(product_simple, donation_product_gids):
    p = product_from_node(product_simple, donation_gids=donation_product_gids)
    doc = product_to_item(p, company="Glia", **COMMON)
    assert doc["has_variants"] == 0
    assert doc["attributes"] == []
    # Donation products are non-stock and land in the donation group.
    assert doc["is_stock_item"] == 0
    assert doc["item_group"] == "Donations"
    assert doc["shopify_is_donation"] == 1


def test_donation_price_doc(product_simple):
    p = product_from_node(product_simple)
    doc = item_price_doc(
        item_code="one-time-donation",
        variant=p.variants[0],
        price_list="Standard Selling",
        currency="CAD",
    )
    assert doc["doctype"] == "Item Price"
    assert doc["item_code"] == "one-time-donation"
    assert doc["price_list"] == "Standard Selling"
    assert doc["price_list_rate"] == 10.0
    assert doc["currency"] == "CAD"
    assert doc["selling"] == 1


# --- item group resolution -----------------------------------------------


def test_item_group_fallbacks():
    from glia_shopify_sync.shop.models import ShopProduct

    def _prod(ptype, donation=False):
        return ShopProduct(
            id="x",
            title="x",
            handle="x",
            product_type=ptype,
            is_donation=donation,
        )

    gmap = {"Apparel": "Merch"}
    assert (
        item_group_for(
            _prod("Apparel"), default_group="Products", group_map=gmap, donation_group="Donations"
        )
        == "Merch"
    )
    # Unknown productType -> used as-is (setup ensures it exists).
    assert (
        item_group_for(
            _prod("Devices"), default_group="Products", group_map=gmap, donation_group="Donations"
        )
        == "Devices"
    )
    # No productType -> default.
    assert (
        item_group_for(
            _prod(None), default_group="Products", group_map=gmap, donation_group="Donations"
        )
        == "Products"
    )
    # Donation wins regardless.
    assert (
        item_group_for(
            _prod("Apparel", donation=True),
            default_group="Products",
            group_map=gmap,
            donation_group="Donations",
        )
        == "Donations"
    )


# --- website item + attribute def ----------------------------------------


def test_website_item_doc(product_with_variants):
    p = product_from_node(product_with_variants, currency="CAD")
    doc = website_item_doc(p, item_code="glia-t-shirt-unisex", publish=True, **COMMON)
    assert doc["doctype"] == "Website Item"
    assert doc["item_code"] == "glia-t-shirt-unisex"
    assert doc["route"] == "glia-t-shirt-unisex"
    assert doc["website_image"].endswith("tee.jpg")
    assert doc["published"] == 1
    assert doc["website_item_groups"] == [{"item_group": "Merch"}]


def test_archived_product_not_published():
    from glia_shopify_sync.shop.models import ShopProduct

    p = ShopProduct(id="x", title="X", handle="x", status="ARCHIVED")
    doc = website_item_doc(p, item_code="x", publish=True, **COMMON)
    assert doc["published"] == 0


def test_item_attribute_def_dedupes_values():
    doc = item_attribute_def("Size", ["M", "L", "M", "XL"])
    vals = [r["attribute_value"] for r in doc["item_attribute_values"]]
    assert vals == ["M", "L", "XL"]


def test_archived_product_marked_disabled():
    from glia_shopify_sync.shop.models import ShopProduct

    p = ShopProduct(id="x", title="X", handle="x", status="ARCHIVED")
    doc = product_to_item(p, company="Glia", **COMMON)
    assert doc["disabled"] == 1
