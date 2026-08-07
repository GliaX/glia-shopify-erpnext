"""Phase 2 — Shipping: Shopify shipping zones -> ERPNext Shipping Rules. (STUB)

Shopify exposes shipping zones + rates only via the **REST** Admin API
(`GET /admin/api/2025-07/shipping_zones.json`), not GraphQL. Each zone carries
weight-based and price-based rates that map naturally to an ERPNext
`Shipping Rule`:

  Shopify shipping_zones[].country_codes[]   -> Shipping Rule country filter
  Shopify weight_based_rates[]               -> Shipping Rule (by weight band)
  Shopify price_based_rates[]                -> Shipping Rule (by amount band)
  Shopify carrier_shipping_rates[]           -> (out of scope; ERPNext has no
                                                 carrier-service equivalent)

A REST reader (`ShopifyClient.get_shipping_zones`) and the transform
(`shipping_zone_to_rule`) will be implemented here. No additional Shopify scope
is required for the shipping_zones endpoint.
"""

from __future__ import annotations

from typing import Any


def shipping_zone_to_rule(zone: dict[str, Any], *, currency: str = "CAD") -> dict[str, Any]:
    """Map one Shopify shipping zone (REST JSON) to an ERPNext Shipping Rule dict.

    SCAFFOLDED — not yet implemented. Will emit a `Shipping Rule` with
    `rule_type`, amount/weight conditions, and a country filter derived from the
    zone's countries.
    """
    raise NotImplementedError("Phase 2 shipping sync is scaffolded, not yet wired")


def process_shipping_zones(zones: list[dict[str, Any]], frappe: Any) -> int:
    """Idempotently upsert Shipping Rules for the given Shopify zones."""
    raise NotImplementedError("Phase 2 shipping sync is scaffolded, not yet wired")


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("glia-shop-shipping-sync CLI is not yet implemented")
