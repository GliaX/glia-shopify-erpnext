"""Shopify store -> ERPNext E Commerce migration.

This sub-package migrates the *shop* (products, variants, prices, collections,
shipping, customers, orders, and the storefront/checkout) from Shopify into
ERPNext's built-in E Commerce module. It is distinct from the donation sync in
the parent package, which only pushes donation line items into a custom
`Donation` doctype.

Both share the proven infrastructure of the parent package:
  * `shopify_client.ShopifyClient` (24h token + paginated GraphQL reader)
  * `frappe_client.FrappeClient` (doctype-agnostic REST with retry)
  * `config.load_config()` (YAML + env secrets)

== Architecture ==

  Shopify Admin API (GraphQL + REST)            ERPNext 16 E Commerce
    products + variants + images  ─┐              Item (template / variant)
    collections                    ├─ Phase 1 ─▶  Item Attribute / Item Price
    inventory + locations          │              Website Item / Website Category
                                   │              Item Group
    shipping zones (REST)          ─── Phase 2 ─▶ Shipping Rule
    customers                      ─── Phase 3 ─▶ Customer (+ link to Contacts)
    orders + fulfillments          ─── Phase 4 ─▶ Sales Order (+ Invoice/Payment)
    payment + checkout config      ─── Phase 5 ─▶ Shopping Cart + Payment Gateway
    ongoing deltas                 ─── Phase 6 ─▶ webhooks / scheduled sync

== Data-model mapping (Phase 1) ==

  Shopify                       ERPNext
  ──────────────────────────    ─────────────────────────────────
  Product (no options)          Item (has_variants=0)
  Product (>=1 real option)     Item (has_variants=1, template) +
                                  one Item per variant (variant_of=template)
  Product.option                Item Attribute (values from variant options)
  ProductVariant.price          Item Price (in the shop Price List + currency)
  Product.images[0]             Item.image / Website Item.website_image
  Product.descriptionHtml       Item.description (HTML preserved)
  Product.productType / tags    Item Group (+ ERPNext native tags)
  Collection                    Website Category (+ Item Group mirror)
  Product.handle                Item.item_code + Website Item.route (slug)

Dedup: custom fields `shopify_product_id` / `shopify_variant_id` on `Item`
(created idempotently by `glia-shop-setup`). Re-running the catalog sync is safe.

== Phases ==

* Phase 1 — Catalog (DONE in this revision): `models`, `shopify_catalog`
  (`iter_products`/`iter_collections`), `erpnext_catalog_mapping` (pure
  transforms), `setup` (`glia-shop-setup`), `catalog_sync`
  (`glia-shop-catalog-sync`). Fully unit-tested.
* Phase 2 — Shipping: `shipping_sync` (scaffolded). Shopify REST shipping_zones
  -> ERPNext Shipping Rule.
* Phase 3 — Customers: `customer_sync` (scaffolded). Shopify Customer ->
  ERPNext Customer, deduped against existing donor Contacts by email.
* Phase 4 — Orders: `order_sync` (scaffolded). Shopify Order -> ERPNext Sales
  Order + Sales Invoice + Payment Entry; donation orders reconciled with the
  existing `Donation` doctype (linked, not duplicated).
* Phase 5 — Checkout: `checkout_setup` (scaffolded). Shopping Cart + Payment
  Gateway + E Commerce settings (largely point-and-click; emitted as config +
  a runbook).

Phases 2-5 carry concrete signatures and raise NotImplementedError until wired.
"""

from __future__ import annotations
