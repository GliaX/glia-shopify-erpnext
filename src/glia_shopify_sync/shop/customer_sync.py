"""Phase 3 — Customer sync: Shopify Customer -> ERPNext Customer (+ Address).

Idempotent and resumable. Dedup is driven by a custom `shopify_customer_id`
field on `Customer` (created by `glia-shop-setup`). Each new customer also gets
a Shipping `Address` (linked via Dynamic Link) when Shopify has one.

By default only customers with `orders_count > 0` are migrated (the useful
subset for order-history backfill — the store has ~20k accounts, most zero-order
newsletter/discarded accounts). Use `--all-customers` to include everyone.

Examples:
    glia-shop-customer-sync --dry-run            # preview counts (with orders only)
    glia-shop-customer-sync --limit 50           # sample first 50
    glia-shop-customer-sync                      # migrate customers-with-orders
    glia-shop-customer-sync --all-customers      # include zero-order accounts
"""

from __future__ import annotations

import argparse
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
from .erpnext_customer_mapping import address_to_doc, customer_to_doc
from .models import customer_from_node

log = structlog.get_logger()


@dataclass
class CustomerStats:
    customers_seen: int = 0
    customers_created: int = 0
    customers_skipped: int = 0
    addresses_created: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"customers: {self.customers_seen} seen "
            f"({self.customers_created} new, {self.customers_skipped} existing) | "
            f"addresses: {self.addresses_created} new"
            + (f" | errors={len(self.errors)}" if self.errors else "")
        )


# --- dedup map -----------------------------------------------------------


def load_customer_map(frappe: Any) -> dict[str, str]:
    """`shopify_customer_id` -> Customer docname, for dedup."""
    rows = frappe.get_list(
        "Customer",
        fields=["name", "shopify_customer_id"],
        filters=[["Customer", "shopify_customer_id", "is", "set"]],
    )
    return {r["shopify_customer_id"]: r["name"] for r in rows if r.get("shopify_customer_id")}


# --- orchestration --------------------------------------------------------


def process_customers(
    customers: Iterable[dict[str, Any]],
    frappe: Any,
    cfg: AppConfig,
    *,
    dry_run: bool = False,
) -> CustomerStats:
    stats = CustomerStats()
    shop = cfg.yaml.shop
    customer_map: dict[str, str] = {} if dry_run else load_customer_map(frappe)

    for node in customers:
        stats.customers_seen += 1
        try:
            customer = customer_from_node(node, currency=shop.currency)
            name = _ensure_customer(frappe, customer, customer_map, shop, stats, dry_run)
            if name:
                _ensure_address(frappe, customer, name, stats, dry_run)
        except Exception as e:  # noqa: BLE001
            label = node.get("displayName") or node.get("id") or "?"
            stats.errors.append(f"customer {label}: {e}")
            log.error("customer_failed", customer=label, error=str(e))

    return stats


def _ensure_customer(
    frappe: Any,
    customer: Any,
    customer_map: dict[str, str],
    shop: Any,
    stats: CustomerStats,
    dry_run: bool,
) -> str | None:
    if customer.id in customer_map:
        stats.customers_skipped += 1
        return customer_map[customer.id]
    doc = customer_to_doc(
        customer,
        customer_group=shop.customer_group,
        territory=shop.customer_territory,
        currency=shop.currency,
    )
    if dry_run:
        stats.customers_created += 1
        name = f"<dry:{customer.customer_name}>"
        customer_map[customer.id] = name
        return name
    try:
        saved = frappe.insert(doc)
    except FrappeError as e:
        stats.errors.append(f"customer {customer.customer_name}: {e}")
        return None
    name = saved.get("name") or saved.get("customer_name") or doc["customer_name"]
    customer_map[customer.id] = name
    stats.customers_created += 1
    return name


def _ensure_address(
    frappe: Any, customer: Any, customer_name: str, stats: CustomerStats, dry_run: bool
) -> None:
    doc = address_to_doc(customer, customer_name=customer_name)
    if not doc:
        return
    if dry_run:
        stats.addresses_created += 1
        return
    try:
        frappe.insert(doc)
        stats.addresses_created += 1
    except FrappeError as e:
        # An address failing shouldn't block the customer; record and continue.
        stats.errors.append(f"address for {customer_name}: {e}")


# --- CLI ------------------------------------------------------------------


def report_stats(stats: CustomerStats) -> int:
    print(stats)
    for err in stats.errors[:20]:
        print(f"  error: {err}", file=sys.stderr)
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s).", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-shop-customer-sync")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N customers (sample)")
    parser.add_argument(
        "--all-customers",
        action="store_true",
        help="include zero-order accounts (default: only customers with >=1 order)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    shopify, frappe = build_clients(cfg)

    customers: Iterable[dict[str, Any]] = shopify.iter_customers(
        only_with_orders=not args.all_customers,
    )
    if args.limit:
        customers = islice(customers, args.limit)

    log.info(
        "customer_sync_start",
        dry_run=args.dry_run,
        limit=args.limit,
        only_with_orders=not args.all_customers,
    )
    stats = process_customers(customers, frappe, cfg, dry_run=args.dry_run)
    return report_stats(stats)


__all__ = [
    "CustomerStats",
    "ShopifyClient",
    "load_customer_map",
    "main",
    "process_customers",
    "report_stats",
]
