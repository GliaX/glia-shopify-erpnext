"""Catalog sync: Shopify Products/Variants/Collections -> ERPNext E Commerce.

Pulls products (cursor-paginated), parses them, and idempotently upserts:

  * `Item`            - template (has_variants=1) or simple (has_variants=0)
  * `Item Attribute`  - each real option + its allowed values
  * `Item` (variant)  - one per Shopify variant, `variant_of=<template>`
  * `Item Price`      - per leaf Item, in the shop Price List + currency
  * `Website Item`    - the storefront entry (route = Shopify handle)

Re-running is safe. Dedup is driven by the custom `shopify_product_id` /
`shopify_variant_id` fields (created by `glia-shop-setup`) plus item_code for
prices/website items. Existing docs have their mutable fields refreshed.

Examples:
    glia-shop-catalog-sync --dry-run            # preview, no writes
    glia-shop-catalog-sync --limit 5            # sample first 5 products
    glia-shop-catalog-sync                      # full catalog
    glia-shop-catalog-sync --include-archived   # also pull ARCHIVED (marked disabled)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..config import AppConfig, load_config, setup_logging
from ..frappe_client import FrappeError
from ..shopify_client import ShopifyClient
from ..sync import build_clients
from .erpnext_catalog_mapping import (
    is_template,
    item_attribute_def,
    item_group_for,
    item_price_doc,
    product_to_item,
    variant_to_item,
    website_item_doc,
)
from .models import TEMPLATE_OPTION_NAME, product_from_node

log = structlog.get_logger()


@dataclass
class CatalogStats:
    products_seen: int = 0
    products_created: int = 0
    products_updated: int = 0
    products_linked: int = 0
    variants_created: int = 0
    variants_updated: int = 0
    prices_created: int = 0
    prices_updated: int = 0
    website_items_created: int = 0
    website_items_updated: int = 0
    website_items_skipped: int = 0
    images_attached: int = 0
    attributes_created: int = 0
    products_degraded: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        web = (
            f"website items: {self.website_items_created} new, "
            f"{self.website_items_updated} refreshed"
        )
        if self.website_items_skipped:
            web += f", {self.website_items_skipped} skipped (no E Commerce)"
        if self.images_attached:
            web += f", {self.images_attached} images"
        return (
            f"products: {self.products_seen} seen "
            f"({self.products_created} new, {self.products_updated} refreshed, "
            f"{self.products_linked} linked) | "
            f"variants: {self.variants_created} new, {self.variants_updated} refreshed | "
            f"prices: {self.prices_created} new, {self.prices_updated} refreshed | "
            f"{web} | "
            f"attributes: {self.attributes_created} created"
            + (f" | degraded={self.products_degraded}" if self.products_degraded else "")
            + (f" | errors={len(self.errors)}" if self.errors else "")
        )


# --- dedup maps -----------------------------------------------------------


def load_item_map(frappe: Any) -> dict[str, str]:
    """`shopify_product_id` -> `item_code`, for template/simple Items only.

    Variant Items also carry the parent `shopify_product_id`, so we exclude rows
    that have a `shopify_variant_id`.
    """
    rows = frappe.get_list(
        "Item",
        fields=["item_code", "shopify_product_id", "shopify_variant_id"],
        filters=[["Item", "shopify_product_id", "is", "set"]],
    )
    return {
        r["shopify_product_id"]: r["item_code"]
        for r in rows
        if r.get("shopify_product_id") and not r.get("shopify_variant_id")
    }


def load_variant_map(frappe: Any) -> dict[str, str]:
    """`shopify_variant_id` -> `item_code`, for variant Items."""
    rows = frappe.get_list(
        "Item",
        fields=["item_code", "shopify_variant_id"],
        filters=[["Item", "shopify_variant_id", "is", "set"]],
    )
    return {r["shopify_variant_id"]: r["item_code"] for r in rows if r.get("shopify_variant_id")}


def load_price_map(frappe: Any, price_list: str) -> set[str]:
    rows = frappe.get_list(
        "Item Price",
        fields=["item_code"],
        filters=[["Item Price", "price_list", "=", price_list]],
    )
    return {r["item_code"] for r in rows if r.get("item_code")}


def load_website_map(frappe: Any) -> set[str]:
    rows = frappe.get_list(
        "Website Item",
        fields=["item_code"],
        filters=[["Website Item", "item_code", "is", "set"]],
    )
    return {r["item_code"] for r in rows if r.get("item_code")}


def load_attribute_map(frappe: Any) -> set[str]:
    rows = frappe.get_list("Item Attribute", fields=["name"])
    return {r["name"] for r in rows if r.get("name")}


def _website_item_available(frappe: Any) -> bool:
    """True if the `Website Item` doctype exists (i.e. the E Commerce module is
    installed). When false, storefront publishing is skipped and only the
    inventory catalog (Items/Prices) is imported.
    """
    try:
        frappe.get("DocType", "Website Item")
        return True
    except FrappeError as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return False
        raise


# --- orchestration --------------------------------------------------------


def process_products(
    products: Iterable[dict[str, Any]],
    frappe: Any,
    cfg: AppConfig,
    *,
    dry_run: bool = False,
) -> CatalogStats:
    stats = CatalogStats()
    shop = cfg.yaml.shop
    donation_gids = cfg.donation_product_gids
    recurring_gids = cfg.recurring_product_gids

    if dry_run:
        item_map: dict[str, str] = {}
        variant_map: dict[str, str] = {}
        price_map: set[str] = set()
        website_map: set[str] = set()
        attr_map: set[str] = set()
        publish = shop.publish_website_items
    else:
        item_map = load_item_map(frappe)
        variant_map = load_variant_map(frappe)
        price_map = load_price_map(frappe, shop.price_list)
        attr_map = load_attribute_map(frappe)
        publish = shop.publish_website_items and _website_item_available(frappe)
        # Only touch the Website Item table if the doctype exists; otherwise
        # get_list on a missing doctype raises 404.
        website_map = load_website_map(frappe) if publish else set()
        if shop.publish_website_items and not publish:
            log.warning(
                "website_item_unavailable",
                hint="E Commerce module not installed; importing Items/Prices only. "
                "Storefront publishing will work once E Commerce is enabled.",
            )

    common = {
        "default_group": shop.item_group_default,
        "group_map": dict(shop.item_group_map),
        "donation_group": shop.item_group_donations,
    }

    # Pass 0: parse everything up front (materialize the generator) so a second
    # pass can reason about the whole catalog.
    parsed: list[Any] = []
    for node in products:
        stats.products_seen += 1
        try:
            parsed.append(
                product_from_node(
                    node,
                    currency=shop.currency,
                    donation_gids=donation_gids,
                    recurring_gids=recurring_gids,
                )
            )
        except Exception as e:  # noqa: BLE001
            label = node.get("handle") or node.get("id") or "?"
            stats.errors.append(f"parse {label}: {e}")
            log.error("catalog_parse_failed", product=label, error=str(e))

    # Pass A: ensure every Item Group + Item Attribute the catalog references
    # already exists. Attribute value sets are computed across the WHOLE catalog
    # (not merged product-by-product), which avoids duplicate-value/abbr errors.
    if not dry_run:
        _ensure_groups_for(parsed, frappe, shop)
    _ensure_attributes_for(parsed, frappe, attr_map, stats, dry_run=dry_run)

    # Pass B: upsert template/simple Items, variant Items, prices, website items.
    for product in parsed:
        try:
            _sync_product(
                product,
                frappe,
                company=cfg.yaml.company,
                common=common,
                shop=shop,
                publish=publish,
                dry_run=dry_run,
                stats=stats,
                item_map=item_map,
                variant_map=variant_map,
                price_map=price_map,
                website_map=website_map,
            )
        except Exception as e:  # noqa: BLE001
            label = getattr(product, "handle", None) or getattr(product, "id", "?")
            stats.errors.append(f"product {label}: {e}")
            log.error("catalog_product_failed", product=label, error=str(e))

    return stats


def _sync_product(
    product: Any,
    frappe: Any,
    *,
    company: str,
    common: dict[str, Any],
    shop: Any,
    publish: bool,
    dry_run: bool,
    stats: CatalogStats,
    item_map: dict[str, str],
    variant_map: dict[str, str],
    price_map: set[str],
    website_map: set[str],
) -> None:
    template = is_template(product)

    if template:
        tdoc = product_to_item(product, company=company, **common)
        tcode = _upsert_item(frappe, tdoc, product.id, item_map, stats, dry_run)

        # A Shopify template product may have been LINKED to a pre-existing
        # non-template Item (e.g. a manufacturing record with the same code).
        # Variants can't be attached to a non-template Item without converting
        # it (risky), so degrade gracefully: price the Item itself and move on.
        if not dry_run and not _item_has_variants(frappe, tcode):
            log.warning(
                "linked_item_not_template_skip_variants",
                item=tcode,
                product=product.handle,
            )
            stats.products_degraded += 1
            if product.variants:
                _upsert_price(frappe, product.variants[0], tcode, shop, price_map, stats, dry_run)
            _upsert_website(
                frappe, product, tcode, shop, common, publish, website_map, stats, dry_run
            )
            return

        for v in product.variants:
            vdoc = variant_to_item(product, v, template_code=tcode, **common)
            vcode = _upsert_variant(frappe, vdoc, v.id, variant_map, stats, dry_run)
            _upsert_price(frappe, v, vcode, shop, price_map, stats, dry_run)

        _upsert_website(frappe, product, tcode, shop, common, publish, website_map, stats, dry_run)
        return

    # Simple product (single or no real options).
    doc = product_to_item(product, company=company, **common)
    code = _upsert_item(frappe, doc, product.id, item_map, stats, dry_run)
    if product.variants:
        _upsert_price(frappe, product.variants[0], code, shop, price_map, stats, dry_run)
    _upsert_website(frappe, product, code, shop, common, publish, website_map, stats, dry_run)


def _item_has_variants(frappe: Any, code: str) -> bool:
    """Whether the named Item is a variant template (has_variants=1)."""
    try:
        doc = frappe.get("Item", code)
        return bool(doc.get("has_variants"))
    except FrappeError:
        return False


# --- upserts --------------------------------------------------------------


_ITEM_MUTABLE = (
    "item_name",
    "description",
    "brand",
    "disabled",
    "shopify_product_type",
    "shopify_is_donation",
    "shopify_is_recurring",
)


def _upsert_item(
    frappe: Any,
    doc: dict[str, Any],
    key: str,
    key_map: dict[str, str],
    stats: CatalogStats,
    dry_run: bool,
) -> str:
    existing = key_map.get(key)
    if existing:
        if not dry_run:
            frappe.update("Item", existing, {k: doc[k] for k in _ITEM_MUTABLE if k in doc})
        stats.products_updated += 1
        return existing
    if dry_run:
        stats.products_created += 1
        code = doc["item_code"]
        key_map[key] = code
        return code
    try:
        saved = frappe.insert(doc)
    except FrappeError as e:
        # An Item with this item_code already exists but isn't tracked by
        # shopify_product_id (e.g. a pre-existing manufacturing Item whose code
        # collides with a Shopify handle). Link it instead of failing: stamp the
        # shopify_product_id so future runs recognize it; core fields are left
        # untouched to avoid clobbering the existing record.
        if "DuplicateEntry" in str(e) or "Duplicate entry" in str(e):
            code = doc["item_code"]
            try:
                frappe.update(
                    "Item", code, {"shopify_product_id": doc.get("shopify_product_id", "")}
                )
            except FrappeError:
                raise
            key_map[key] = code
            stats.products_linked += 1
            log.warning("item_code_collision_linked", item_code=code, shopify_id=key)
            return code
        raise
    code = saved.get("item_code") or saved.get("name") or doc["item_code"]
    key_map[key] = code
    stats.products_created += 1
    return code


def _upsert_variant(
    frappe: Any,
    doc: dict[str, Any],
    key: str,
    key_map: dict[str, str],
    stats: CatalogStats,
    dry_run: bool,
) -> str:
    existing = key_map.get(key)
    if existing:
        if not dry_run:
            frappe.update(
                "Item",
                existing,
                {k: doc[k] for k in ("item_name", "description", "brand", "disabled") if k in doc},
            )
        stats.variants_updated += 1
        return existing
    if dry_run:
        stats.variants_created += 1
        code = doc["item_code"]
        key_map[key] = code
        return code
    saved = frappe.insert(doc)
    code = saved.get("item_code") or saved.get("name") or doc["item_code"]
    key_map[key] = code
    stats.variants_created += 1
    return code


def _upsert_price(
    frappe: Any,
    variant: Any,
    item_code: str,
    shop: Any,
    price_map: set[str],
    stats: CatalogStats,
    dry_run: bool,
) -> None:
    if item_code in price_map:
        if not dry_run:
            existing = frappe.find(
                "Item Price",
                [
                    ["Item Price", "item_code", "=", item_code],
                    ["Item Price", "price_list", "=", shop.price_list],
                ],
                fields=["name"],
            )
            if existing:
                frappe.update(
                    "Item Price",
                    existing["name"],
                    {
                        "price_list_rate": float(variant.price),
                        "currency": variant.currency or shop.currency,
                    },
                )
        stats.prices_updated += 1
        return
    if dry_run:
        stats.prices_created += 1
        price_map.add(item_code)
        return
    frappe.insert(
        item_price_doc(
            item_code=item_code,
            variant=variant,
            price_list=shop.price_list,
            currency=shop.currency,
        )
    )
    price_map.add(item_code)
    stats.prices_created += 1


def _upsert_website(
    frappe: Any,
    product: Any,
    item_code: str,
    shop: Any,
    common: dict[str, Any],
    publish: bool,
    website_map: set[str],
    stats: CatalogStats,
    dry_run: bool,
) -> None:
    if not publish:
        stats.website_items_skipped += 1
        return
    if item_code in website_map:
        if not dry_run:
            existing = frappe.find(
                "Website Item",
                [["Website Item", "item_code", "=", item_code]],
                fields=["name"],
            )
            if existing:
                wdoc = website_item_doc(
                    product,
                    item_code=item_code,
                    publish=shop.publish_website_items,
                    default_group=common["default_group"],
                    group_map=common["group_map"],
                    donation_group=common["donation_group"],
                )
                # `website_image` can't be set as a bare URL (ERPNext blanks it);
                # it's attached via a File doc in _ensure_website_image below.
                frappe.update(
                    "Website Item",
                    existing["name"],
                    {
                        "item_name": wdoc["item_name"],
                        "route": wdoc["route"],
                        "description": wdoc["description"],
                        "website_image_alt": wdoc["website_image_alt"],
                        "published": wdoc["published"],
                    },
                )
                _ensure_website_image(frappe, product, existing["name"], item_code, stats, dry_run)
        stats.website_items_updated += 1
        return
    if dry_run:
        stats.website_items_created += 1
        website_map.add(item_code)
        return
    saved = frappe.insert(
        website_item_doc(
            product,
            item_code=item_code,
            publish=shop.publish_website_items,
            default_group=common["default_group"],
            group_map=common["group_map"],
            donation_group=common["donation_group"],
        )
    )
    website_map.add(item_code)
    stats.website_items_created += 1
    _ensure_website_image(frappe, product, saved.get("name", ""), item_code, stats, dry_run)


def _ensure_website_image(
    frappe: Any,
    product: Any,
    wi_name: str,
    item_code: str,
    stats: CatalogStats,
    dry_run: bool,
) -> None:
    """Attach the Shopify product image to the Website Item (and Item.image).

    ERPNext's `website_image` Attach field rejects a bare remote URL (it's
    silently blanked), but a `File` doc that references the URL is accepted — so
    we create one File per image (deduped by file_url) and point `website_image`
    at it. Idempotent: skipped if the Website Item already has an image.
    """
    if not product.images or not wi_name:
        return
    url = product.images[0].url
    if dry_run or not url:
        return
    try:
        if frappe.get("Website Item", wi_name).get("website_image"):
            return
        file_doc = frappe.find(
            "File", [["File", "file_url", "=", url]], fields=["name", "file_url"]
        )
        if not file_doc:
            file_doc = frappe.insert(
                {
                    "doctype": "File",
                    "file_url": url,
                    "is_private": 0,
                    "attached_to_doctype": "Website Item",
                    "attached_to_name": wi_name,
                }
            )
        file_url = file_doc.get("file_url") or url
        frappe.update("Website Item", wi_name, {"website_image": file_url})
        if item_code:
            frappe.update("Item", item_code, {"image": file_url})
        stats.images_attached += 1
    except FrappeError as e:
        stats.errors.append(f"image {wi_name}: {e}")


def _ensure_groups_for(products: list[Any], frappe: Any, shop: Any) -> None:
    """Create any Item Group the catalog will reference but that doesn't yet
    exist (e.g. an unmapped Shopify productType used as a group name)."""
    existing = {r["name"] for r in frappe.get_list("Item Group", fields=["name"])}
    needed: set[str] = {shop.item_group_default, shop.item_group_donations}
    needed.update(shop.item_group_map.values())
    for p in products:
        needed.add(
            item_group_for(
                p,
                default_group=shop.item_group_default,
                group_map=shop.item_group_map,
                donation_group=shop.item_group_donations,
            )
        )
    for name in sorted(n for n in needed if n):
        if name in existing:
            continue
        frappe.insert(
            {
                "doctype": "Item Group",
                "item_group_name": name,
                "parent_item_group": shop.item_group_parent,
                "is_group": 0,
            }
        )
        existing.add(name)


def _ensure_attributes_for(
    products: list[Any],
    frappe: Any,
    attr_map: set[str],
    stats: CatalogStats,
    *,
    dry_run: bool,
) -> None:
    """Ensure each real variant option exists as an Item Attribute carrying the
    union of its values across the WHOLE catalog (computed once, not merged
    product-by-product — that was the source of duplicate-value/abbr errors).
    """
    values_by_attr: dict[str, list[str]] = {}
    for p in products:
        if not is_template(p):
            continue
        for opt_name in p.options:
            if opt_name == TEMPLATE_OPTION_NAME:
                continue
            bucket = values_by_attr.setdefault(opt_name, [])
            for v in p.variants:
                for opt in v.options:
                    if opt.name == opt_name and opt.value and opt.value not in bucket:
                        bucket.append(opt.value)

    for name, vals in values_by_attr.items():
        if name in attr_map:
            if not dry_run:
                _merge_attribute_values(frappe, name, vals, stats)
            continue
        if dry_run:
            stats.attributes_created += 1
            attr_map.add(name)
            continue
        frappe.insert(item_attribute_def(name, vals))
        attr_map.add(name)
        stats.attributes_created += 1


def _merge_attribute_values(frappe: Any, name: str, vals: list[str], stats: CatalogStats) -> None:
    """Append missing values to an existing Item Attribute in a single pass.

    The existing rows are de-duplicated and re-abbreviation-validated before
    saving: the earlier incremental-merge run could leave duplicate values, and
    pre-existing values may carry abbreviations that collide with new Shopify
    values (e.g. "Small"->"S" vs "S"->"S"). Both are made unique.
    """
    from .erpnext_catalog_mapping import _unique_abbr

    try:
        doc = frappe.get("Item Attribute", name)
        existing_rows = list(doc.get("item_attribute_values") or [])

        clean: list[dict[str, Any]] = []
        have_val: set[str] = set()  # lowercased — ERPNext uniqueness is case-insensitive
        taken_abbr: set[str] = set()
        for r in existing_rows:
            v = (r or {}).get("attribute_value")
            if not v or v.lower() in have_val:
                continue
            have_val.add(v.lower())
            a = (r or {}).get("abbr")
            if not a or a in taken_abbr:
                a = _unique_abbr(v, taken_abbr)
            else:
                taken_abbr.add(a)
            clean.append({"attribute_value": v, "abbr": a})

        for v in vals:
            if not v or v.lower() in have_val:
                continue
            clean.append({"attribute_value": v, "abbr": _unique_abbr(v, taken_abbr)})
            have_val.add(v.lower())

        if len(clean) != len(existing_rows):
            frappe.update("Item Attribute", name, {"item_attribute_values": clean})
    except FrappeError as e:
        stats.errors.append(f"attribute {name}: {e}")


# --- CLI ------------------------------------------------------------------


def report_stats(stats: CatalogStats) -> int:
    print(stats)
    for err in stats.errors[:20]:
        print(f"  error: {err}", file=sys.stderr)
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s).", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-shop-catalog-sync")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N products (sample)")
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="also pull ARCHIVED/DRAFT products (still marked disabled)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    shopify, frappe = build_clients(cfg)

    from itertools import islice

    products: Iterable[dict[str, Any]] = shopify.iter_products(
        include_archived=args.include_archived or cfg.yaml.shop.include_archived,
    )
    if args.limit:
        products = islice(products, args.limit)

    log.info(
        "catalog_sync_start",
        dry_run=args.dry_run,
        limit=args.limit,
        include_archived=args.include_archived,
    )
    stats = process_products(products, frappe, cfg, dry_run=args.dry_run)
    return report_stats(stats)


# Re-exported so callers can construct a typed client pair if needed.
__all__ = [
    "CatalogStats",
    "ShopifyClient",
    "load_attribute_map",
    "load_item_map",
    "load_price_map",
    "load_variant_map",
    "load_website_map",
    "main",
    "process_products",
    "report_stats",
]
