"""Phase 4 — Order backfill: Shopify Order -> ERPNext Sales Order.

Creates a `Sales Order` for each Shopify order that has at least one
non-donation line item, with items linked to the migrated `Item`s and the
customer linked to the migrated `Customer`. Pure-donation orders are skipped
(already `Donation` records); a Payment Entry is not created in this phase.

Idempotent + resumable. Dedup by a custom `shopify_order_id` field on
`Sales Order` (created by `glia-shop-setup`). Default scope is PAID orders
(matches the donation sync's paid-only philosophy and bounds the backfill to
the meaningful transactional history).

Examples:
    glia-shop-order-sync --dry-run --limit 10   # preview first 10 paid orders
    glia-shop-order-sync                        # backfill paid orders
    glia-shop-order-sync --all                  # include unpaid/non-paid too
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

import structlog

from ..config import AppConfig, load_config, setup_logging
from ..frappe_client import FrappeError
from ..shopify_client import ShopifyClient
from ..sync import build_clients
from .erpnext_order_mapping import sales_order_doc
from .models import order_from_node

log = structlog.get_logger()


@dataclass
class OrderStats:
    orders_seen: int = 0
    sales_orders_created: int = 0
    sales_orders_skipped: int = 0  # already imported
    orders_no_shop_items: int = 0  # pure donation / no resolvable items
    orders_no_customer: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"orders: {self.orders_seen} seen | "
            f"sales orders: {self.sales_orders_created} created, "
            f"{self.sales_orders_skipped} existing | "
            f"skipped (no shop items): {self.orders_no_shop_items}, "
            f"(no customer): {self.orders_no_customer}"
            + (f" | errors={len(self.errors)}" if self.errors else "")
        )


# --- dedup / lookup maps -------------------------------------------------


def load_sales_order_map(frappe: Any) -> set[str]:
    rows = frappe.get_list(
        "Sales Order",
        fields=["shopify_order_id"],
        filters=[["Sales Order", "shopify_order_id", "is", "set"]],
    )
    return {r["shopify_order_id"] for r in rows if r.get("shopify_order_id")}


def load_item_code_map(frappe: Any) -> dict[str, str]:
    """variant_id/product_id -> item_code, for line-item resolution.

    Only variant Items are keyed by `shopify_variant_id`. For the product-id
    fallback we key only SIMPLE items (has_variants=0) — a template Item can't
    be sold on a Sales Order (only its variants can), so falling back to a
    template would make ERPNext reject the order.
    """
    out: dict[str, str] = {}
    rows = frappe.get_list(
        "Item",
        fields=["item_code", "shopify_product_id", "shopify_variant_id", "has_variants"],
        filters=[["Item", "shopify_product_id", "is", "set"]],
    )
    for r in rows:
        if r.get("shopify_variant_id"):
            out[r["shopify_variant_id"]] = r["item_code"]
        elif not r.get("has_variants"):
            # simple item — safe to resolve by product_id
            out[r.get("shopify_product_id") or ""] = r["item_code"]
    return out


def load_customer_map(frappe: Any) -> dict[str, str]:
    rows = frappe.get_list(
        "Customer",
        fields=["name", "shopify_customer_id"],
        filters=[["Customer", "shopify_customer_id", "is", "set"]],
    )
    return {r["shopify_customer_id"]: r["name"] for r in rows if r.get("shopify_customer_id")}


# --- orchestration --------------------------------------------------------


def process_orders(
    orders: Iterable[dict[str, Any]],
    frappe: Any,
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    paid_only: bool = True,
) -> OrderStats:
    stats = OrderStats()
    shop = cfg.yaml.shop
    donation_gids = cfg.donation_product_gids

    if dry_run:
        so_map: set[str] = set()
        item_map: dict[str, str] = {}
        customer_map: dict[str, str] = {}
        guest = "<guest>"
    else:
        so_map = load_sales_order_map(frappe)
        item_map = load_item_code_map(frappe)
        customer_map = load_customer_map(frappe)
        guest = shop.guest_customer

    for node in orders:
        stats.orders_seen += 1
        try:
            order = order_from_node(node, currency=shop.currency, donation_gids=donation_gids)
            # Client-side paid filter (Shopify's GraphQL financial_status filter
            # is unreliable; the donation sync filters the same way).
            if paid_only and order.financial_status not in ("PAID", "PARTIALLY_PAID"):
                continue
            _sync_order(
                order,
                frappe,
                company=cfg.yaml.company,
                price_list=shop.price_list,
                currency=shop.currency,
                warehouse=shop.warehouse,
                guest=guest,
                so_map=so_map,
                item_map=item_map,
                customer_map=customer_map,
                dry_run=dry_run,
                stats=stats,
            )
        except Exception as e:  # noqa: BLE001
            label = node.get("name") or node.get("id") or "?"
            stats.errors.append(f"order {label}: {e}")
            log.error("order_failed", order=label, error=str(e))

    return stats


def _sync_order(
    order: Any,
    frappe: Any,
    *,
    company: str,
    price_list: str,
    currency: str,
    warehouse: str,
    guest: str,
    so_map: set[str],
    item_map: dict[str, str],
    customer_map: dict[str, str],
    dry_run: bool,
    stats: OrderStats,
) -> None:
    if order.id in so_map:
        stats.sales_orders_skipped += 1
        return
    if not order.is_shop_order:
        stats.orders_no_shop_items += 1
        return

    customer = (customer_map.get(order.customer_id) if order.customer_id else None) or guest
    if not customer:
        stats.orders_no_customer += 1
        return

    # Dry-run can't resolve item codes (no ERPNext read), so count any shop order
    # with a customer as createable — the real run resolves items + skips misses.
    if dry_run:
        stats.sales_orders_created += 1
        so_map.add(order.id)
        return

    doc = sales_order_doc(
        order,
        company=company,
        price_list=price_list,
        currency=currency,
        customer_name=customer,
        item_codes=item_map,
        warehouse=warehouse,
    )
    if not doc:
        stats.orders_no_shop_items += 1
        return

    try:
        frappe.insert(doc)
    except FrappeError as e:
        stats.errors.append(f"order {order.name}: {e}")
        return
    so_map.add(order.id)
    stats.sales_orders_created += 1


# --- CLI ------------------------------------------------------------------


def report_stats(stats: OrderStats) -> int:
    print(stats)
    # Distinct error patterns with counts (dedup so 100s of the same error
    # collapse to one line; the raw list can be huge for a full backfill).
    patterns: dict[str, int] = {}
    for err in stats.errors:
        key = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "DATE", err)[:160]
        patterns[key] = patterns.get(key, 0) + 1
    for key, n in sorted(patterns.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  ({n}x) {key}", file=sys.stderr)
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s) total.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-shop-order-sync")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N orders (sample)")
    parser.add_argument(
        "--all",
        action="store_true",
        help="include non-paid orders (default: paid only)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    shopify, frappe = build_clients(cfg)

    orders: Iterable[dict[str, Any],] = shopify.iter_orders()
    if args.limit:
        orders = islice(orders, args.limit)

    log.info("order_sync_start", dry_run=args.dry_run, limit=args.limit, paid_only=not args.all)
    stats = process_orders(orders, frappe, cfg, dry_run=args.dry_run, paid_only=not args.all)
    return report_stats(stats)


__all__ = [
    "OrderStats",
    "ShopifyClient",
    "load_customer_map",
    "load_item_code_map",
    "load_sales_order_map",
    "main",
    "process_orders",
    "report_stats",
]
