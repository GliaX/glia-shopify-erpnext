"""Sync orchestration: Shopify Orders -> CRM Contact + Donation.

Pulls Orders (newest-first), transforms each into a Donor + any Donations, and
idempotently upserts them into Frappe CRM:

  * Contact  dedup by `shopify_customer_id` (pre-loaded map).
  * Donation dedup by `shopify_order_id|shopify_line_item_id` (pre-loaded set).

Re-running is safe: already-ingested records are skipped. Designed so the order
source is an iterable (tests pass a list; the CLI passes `ShopifyClient.iter_orders`).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

import structlog

from .crm_mapping import donation_to_doc, donor_to_contact
from .frappe_client import FrappeClient
from .shopify_client import ShopifyClient, TokenManager
from .transform import transform_order

log = structlog.get_logger()


@dataclass
class SyncStats:
    orders_seen: int = 0
    orders_skipped_not_donation: int = 0
    contacts_created: int = 0
    contacts_reused: int = 0
    donations_created: int = 0
    donations_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    last_processed_at: str | None = None

    @property
    def donations_total(self) -> int:
        return self.donations_created + self.donations_skipped

    def __str__(self) -> str:
        return (
            f"orders={self.orders_seen} ({self.orders_skipped_not_donation} non-donation) | "
            f"contacts: {self.contacts_created} new, {self.contacts_reused} reused | "
            f"donations: {self.donations_created} new, {self.donations_skipped} existing"
            + (f" | errors={len(self.errors)}" if self.errors else "")
        )


def load_contact_map(frappe: Any) -> dict[str, str]:
    """Existing {shopify_customer_id: contact_name} for dedup."""
    rows = frappe.get_list(
        "Contact",
        fields=["name", "shopify_customer_id"],
        filters=[["Contact", "shopify_customer_id", "is", "set"]],
    )
    return {r["shopify_customer_id"]: r["name"] for r in rows if r.get("shopify_customer_id")}


def load_donation_keys(frappe: Any) -> set[str]:
    """Existing set of '<order_id>|<line_item_id>' for dedup."""
    rows = frappe.get_list("Donation", fields=["shopify_order_id", "shopify_line_item_id"])
    return {
        f"{r['shopify_order_id']}|{r['shopify_line_item_id']}"
        for r in rows
        if r.get("shopify_order_id")
    }


def process_orders(
    orders: Iterable[dict[str, Any]],
    frappe: Any,
    *,
    donation_gids: set[str],
    recurring_gids: set[str],
    tip_mode: str = "fold",
    include_test_orders: bool = False,
    paid_only: bool = True,
    dry_run: bool = False,
    contact_map: dict[str, str] | None = None,
    donation_keys: set[str] | None = None,
) -> SyncStats:
    stats = SyncStats()
    if contact_map is None:
        contact_map = {} if dry_run else load_contact_map(frappe)
    if donation_keys is None:
        donation_keys = set() if dry_run else load_donation_keys(frappe)

    for order in orders:
        stats.orders_seen += 1
        processed_at = order.get("processedAt")
        if processed_at and (not stats.last_processed_at or processed_at > stats.last_processed_at):
            stats.last_processed_at = processed_at

        try:
            result = transform_order(
                order,
                donation_gids=donation_gids,
                recurring_gids=recurring_gids,
                tip_mode=tip_mode,
                include_test_orders=include_test_orders,
                paid_only=paid_only,
            )
        except Exception as e:  # noqa: BLE001
            stats.errors.append(f"transform {order.get('name')}: {e}")
            continue

        if result is None:
            stats.orders_skipped_not_donation += 1
            continue

        contact_name = _ensure_contact(frappe, result.donor, contact_map, stats, dry_run)
        if contact_name is None:
            continue  # error already recorded

        for donation in result.donations:
            _ensure_donation(frappe, donation, contact_name, donation_keys, stats, dry_run)

    return stats


def _ensure_contact(
    frappe: Any, donor: Any, contact_map: dict[str, str], stats: SyncStats, dry_run: bool
) -> str | None:
    cid = donor.shopify_customer_id
    if cid and cid in contact_map:
        stats.contacts_reused += 1
        return contact_map[cid]
    try:
        if dry_run:
            stats.contacts_created += 1
            return f"<dry:{donor.donor_name}>"
        saved = frappe.insert(donor_to_contact(donor))
        name = saved["name"]
        if cid:
            contact_map[cid] = name
        stats.contacts_created += 1
        return name
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"contact {donor.donor_name}: {e}")
        return None


def _ensure_donation(
    frappe: Any, donation: Any, contact_name: str, keys: set[str], stats: SyncStats, dry_run: bool
) -> None:
    key = f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"
    if key in keys:
        stats.donations_skipped += 1
        return
    try:
        if dry_run:
            stats.donations_created += 1
            keys.add(key)
            return
        frappe.insert(donation_to_doc(donation, contact_name=contact_name))
        keys.add(key)
        stats.donations_created += 1
    except Exception as e:  # noqa: BLE001
        stats.errors.append(f"donation {donation.shopify_order_name}: {e}")


def build_clients(cfg: Any) -> tuple[ShopifyClient, FrappeClient]:
    """Construct Shopify + Frappe clients from app config."""
    s = cfg.settings
    shopify = ShopifyClient(
        TokenManager(
            shop_domain=s.shopify_shop_domain,
            client_id=s.shopify_client_id,
            client_secret=s.shopify_client_secret.get_secret_value(),
        ),
        api_version=s.shopify_api_version,
        page_size=cfg.yaml.sync.page_size,
    )
    frappe = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
    )
    return shopify, frappe


def run_sync(
    cfg: Any,
    shopify: ShopifyClient,
    frappe: FrappeClient,
    state: Any,
    *,
    since: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> SyncStats:
    """Run one sync pass using a state backend (get_cursor/set_cursor).

    `since` overrides the cursor; otherwise it resumes from the cursor (daily
    incremental), falling back to the configured backfill window on first run.
    """
    effective_since = since or state.get_cursor() or cfg.yaml.backfill.since
    log.info("sync_start", since=effective_since, dry_run=dry_run, limit=limit)
    orders = shopify.iter_orders(since=effective_since)
    if limit:
        orders = islice(orders, limit)

    stats = process_orders(
        orders,
        frappe,
        donation_gids=cfg.donation_product_gids,
        recurring_gids=cfg.recurring_product_gids,
        tip_mode=cfg.yaml.tip_mode,
        include_test_orders=cfg.yaml.sync.include_test_orders,
        paid_only=cfg.yaml.sync.paid_only,
        dry_run=dry_run,
    )
    if not dry_run and stats.last_processed_at:
        state.set_cursor(stats.last_processed_at)
    return stats


def report_stats(stats: SyncStats) -> int:
    """Print stats + errors; return process exit code (0 ok, 1 if any errors)."""
    print(stats)
    for err in stats.errors[:20]:
        print(f"  error: {err}", file=sys.stderr)
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s).", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "SyncStats",
    "build_clients",
    "load_contact_map",
    "load_donation_keys",
    "process_orders",
    "report_stats",
    "run_sync",
]
