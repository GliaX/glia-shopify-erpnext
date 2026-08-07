"""Pure transforms: ShopProduct / ShopVariant / ShopCollection -> Frappe doc dicts.

No I/O. Each function returns a plain dict ready for `FrappeClient.insert` /
`update`. Naming follows ERPNext 16 core doctypes:

  * `Item`              - one record per product (template if it has variants)
  * `Item Attribute`    - definition of each variant option + its allowed values
  * `Item Price`        - per-variant price in the shop Price List + currency
  * `Website Item`      - the E Commerce storefront entry (linked to an Item)
  * `Item Group`        - inventory taxonomy (mirrors Shopify productType)

Variant model
-------------
A Shopify product with a real option (anything other than the 'Title' placeholder)
becomes an ERPNext *template* Item (`has_variants=1`) with one variant Item per
Shopify variant (`variant_of=<template>`). A single-variant / no-option product
becomes a single simple Item (`has_variants=0`).

Item code stability
-------------------
Template: the Shopify handle (already a unique URL slug). Variant:
`<handle>-<sku>` when an SKU exists, else `<handle>-<last GID segment>`. Both are
deterministic across re-runs so dedup/upsert is stable.

NOTE: `Website Item` field names (`website_image`, `website_item_groups`, etc.)
are the v14+/v16 E Commerce schema. If the target instance differs, run
`glia-shop-catalog-sync --dry-run` and adjust here. The `Item` / `Item Price` /
`Item Attribute` / `Item Group` dicts are stable across all supported versions.
"""

from __future__ import annotations

import re
from typing import Any

from .models import TEMPLATE_OPTION_NAME, ShopProduct, ShopVariant

DEFAULT_STOCK_UOM = "Nos"


# --- classification / keys ------------------------------------------------


def is_template(product: ShopProduct) -> bool:
    """Does this product need an ERPNext template Item + variant Items?"""
    return product.has_real_variants


def template_item_code(product: ShopProduct) -> str:
    return _slugify(product.handle or _gid_num(product.id) or product.title)


def variant_item_code(product: ShopProduct, variant: ShopVariant) -> str:
    base = template_item_code(product)
    suffix = _slugify(variant.sku) if variant.sku else _gid_num(variant.id)
    return f"{base}-{suffix}"


def item_group_for(
    product: ShopProduct,
    *,
    default_group: str,
    group_map: dict[str, str],
    donation_group: str,
) -> str:
    """Pick an ERPNext Item Group for a product.

    Donations go to the dedicated donation group. Otherwise an explicit
    `group_map[productType]` wins, else the productType itself (the setup CLI
    ensures those groups exist), else the default group.
    """
    if product.is_donation:
        return donation_group
    ptype = (product.product_type or "").strip()
    if ptype and ptype in group_map:
        return group_map[ptype]
    if ptype:
        return ptype
    return default_group


# --- Item (template / simple / variant) -----------------------------------


def product_to_item(
    product: ShopProduct,
    *,
    company: str,
    default_group: str,
    group_map: dict[str, str],
    donation_group: str,
) -> dict[str, Any]:
    """Template or simple Item for a product (no variants here)."""
    template = is_template(product)
    return {
        "doctype": "Item",
        "item_code": template_item_code(product),
        "item_name": product.title,
        "item_group": item_group_for(
            product,
            default_group=default_group,
            group_map=group_map,
            donation_group=donation_group,
        ),
        "stock_uom": DEFAULT_STOCK_UOM,
        "description": product.description_html or product.title,
        "brand": product.vendor or "",
        # Donations / digital goods are not stocked physical inventory.
        "is_stock_item": 0 if product.is_donation else 1,
        "has_variants": 1 if template else 0,
        "attributes": template_attributes(product) if template else [],
        "disabled": 1 if product.status == "ARCHIVED" else 0,
        # custom fields created by `glia-shop-setup`:
        "shopify_product_id": product.id,
        "shopify_handle": product.handle,
        "shopify_product_type": product.product_type or "",
        "shopify_is_donation": 1 if product.is_donation else 0,
        "shopify_is_recurring": 1 if product.is_recurring else 0,
    }


def variant_to_item(
    product: ShopProduct,
    variant: ShopVariant,
    *,
    template_code: str,
    default_group: str,
    group_map: dict[str, str],
    donation_group: str,
) -> dict[str, Any]:
    """A variant Item linked to its template via `variant_of`."""
    title = (
        product.title
        if not variant.title or variant.title == "Default Title"
        else f"{product.title} - {variant.title}"
    )
    return {
        "doctype": "Item",
        "item_code": variant_item_code(product, variant),
        "item_name": title,
        "variant_of": template_code,
        "item_group": item_group_for(
            product,
            default_group=default_group,
            group_map=group_map,
            donation_group=donation_group,
        ),
        "stock_uom": DEFAULT_STOCK_UOM,
        "description": product.description_html or product.title,
        "brand": product.vendor or "",
        "is_stock_item": 0 if product.is_donation else 1,
        "attributes": variant_attributes(product, variant),
        "disabled": 1 if product.status == "ARCHIVED" else 0,
        # custom fields:
        "shopify_product_id": product.id,
        "shopify_variant_id": variant.id,
        "shopify_handle": product.handle,
        "shopify_sku": variant.sku or "",
        "barcode": variant.barcode or "",
    }


# --- Item Attribute -------------------------------------------------------


def template_attributes(product: ShopProduct) -> list[dict[str, Any]]:
    """Child-table rows for the template Item's real option names."""
    return [{"attribute": name} for name in product.options if name != TEMPLATE_OPTION_NAME]


def variant_attributes(product: ShopProduct, variant: ShopVariant) -> list[dict[str, Any]]:
    """Concrete attribute values for a variant Item."""
    return [
        {"attribute": opt.name, "attribute_value": opt.value}
        for opt in variant.options
        if opt.name != TEMPLATE_OPTION_NAME
    ]


def item_attribute_def(name: str, values: list[str]) -> dict[str, Any]:
    """An `Item Attribute` doc with its allowed values (idempotent target).

    Abbreviations are derived from each value but forced unique (a numeric
    suffix is appended on collision) so ERPNext's `abbr` uniqueness check can't
    reject a value whose natural abbreviation is already taken (e.g. a
    pre-existing "Small" with abbr "S" vs a Shopify "S").
    """
    unique_values = list(dict.fromkeys(values))
    # ERPNext's attribute-value uniqueness is case-insensitive; collapse casing
    # variants (defensively, in case normalization at parse time missed any).
    seen_lower: set[str] = set()
    taken: set[str] = set()
    rows = []
    for v in unique_values:
        key = (v or "").lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        rows.append({"attribute_value": v, "abbr": _unique_abbr(v, taken)})
    return {
        "doctype": "Item Attribute",
        "attribute_name": name,
        "item_attribute_values": rows,
    }


def _unique_abbr(value: str, taken: set[str]) -> str:
    """An abbreviation for `value`, guaranteed not already in `taken`.

    `taken` is mutated (the chosen abbr is added) so a batch of values all get
    distinct abbrs. Falls back to a numeric suffix when the natural form clashes.
    """
    base = (value or "")[:140] or "v"
    abbr = base
    i = 1
    while abbr in taken:
        suffix = str(i)
        abbr = base[: 140 - len(suffix)] + suffix
        i += 1
    taken.add(abbr)
    return abbr


# --- Item Price -----------------------------------------------------------


def item_price_doc(
    *,
    item_code: str,
    variant: ShopVariant,
    price_list: str,
    currency: str,
) -> dict[str, Any]:
    return {
        "doctype": "Item Price",
        "item_code": item_code,
        "price_list": price_list,
        "price_list_rate": float(variant.price),
        "currency": variant.currency or currency,
        "selling": 1,
        "buying": 0,
    }


# --- Website Item ---------------------------------------------------------


def website_item_doc(
    product: ShopProduct,
    *,
    item_code: str,
    publish: bool,
    default_group: str,
    group_map: dict[str, str],
    donation_group: str,
) -> dict[str, Any]:
    """The E Commerce storefront entry for an Item.

    `route` mirrors the Shopify handle for SEO/URL parity. `website_image` is
    the first Shopify image URL (ERPNext Attach Image accepts a URL string).
    """
    image = product.images[0] if product.images else None
    group = item_group_for(
        product,
        default_group=default_group,
        group_map=group_map,
        donation_group=donation_group,
    )
    return {
        "doctype": "Website Item",
        "item_code": item_code,
        "item_name": product.title,
        "route": _slugify(product.handle or item_code),
        "description": product.description_html or product.title,
        "website_image": image.url if image else "",
        "website_image_alt": (image.alt_text if image else "") or product.title,
        "published": 1 if publish and product.status != "ARCHIVED" else 0,
        "website_item_groups": [{"item_group": group}],
    }


# --- helpers --------------------------------------------------------------


def _slugify(value: str) -> str:
    s = (value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "item"


def _gid_num(gid: str) -> str:
    """Trailing numeric segment of a Shopify GID (e.g. '.../Product/123' -> '123')."""
    m = re.search(r"(\d+)$", gid or "")
    return m.group(1) if m else ""


__all__ = [
    "DEFAULT_STOCK_UOM",
    "item_attribute_def",
    "item_group_for",
    "item_price_doc",
    "is_template",
    "product_to_item",
    "template_attributes",
    "template_item_code",
    "variant_attributes",
    "variant_item_code",
    "variant_to_item",
    "website_item_doc",
]
