"""`glia-sync-backfill` — backfill historical Shopify donations into Frappe CRM.

Reads Orders from Shopify (newest-first) and writes `Contact` + `Donation` to
ERPNext. Idempotent: re-running skips anything already ingested (dedup by
`shopify_customer_id` and by `shopify_order_id|shopify_line_item_id`).

Examples:
    glia-sync-backfill --dry-run            # preview, no writes
    glia-sync-backfill --since 2024-01-01   # restrict range
    glia-sync-backfill --limit 50           # sample first 50 orders
    glia-sync-backfill                      # full history (config backfill.since)
"""

from __future__ import annotations

import argparse
import sys
from itertools import islice

import structlog

from .config import AppConfig, load_config, setup_logging
from .frappe_client import FrappeClient
from .shopify_client import ShopifyClient, TokenManager
from .state import State
from .sync import SyncStats, process_orders

log = structlog.get_logger()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-backfill")
    parser.add_argument("--since", help="YYYY-MM-DD; default: state/config backfill.since")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N orders (sample)")
    args = parser.parse_args(argv)

    cfg: AppConfig = load_config()
    setup_logging(cfg)
    s = cfg.settings

    frappe = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
    )
    shopify = ShopifyClient(
        TokenManager(
            shop_domain=s.shopify_shop_domain,
            client_id=s.shopify_client_id,
            client_secret=s.shopify_client_secret.get_secret_value(),
        ),
        api_version=s.shopify_api_version,
        page_size=cfg.yaml.sync.page_size,
    )

    state = State(cfg.yaml.paths.state)
    since = args.since or state.get_backfill_cursor() or cfg.yaml.backfill.since
    log.info("backfill_start", since=since, dry_run=args.dry_run, limit=args.limit)

    orders = shopify.iter_orders(since=since)
    if args.limit:
        orders = islice(orders, args.limit)

    stats: SyncStats = process_orders(
        orders,
        frappe,
        donation_gids=cfg.donation_product_gids,
        recurring_gids=cfg.recurring_product_gids,
        tip_mode=cfg.yaml.tip_mode,
        include_test_orders=cfg.yaml.sync.include_test_orders,
        paid_only=cfg.yaml.sync.paid_only,
        dry_run=args.dry_run,
    )

    if not args.dry_run and stats.last_processed_at:
        state.set_backfill_cursor(None, processed_at=stats.last_processed_at)

    print(stats)
    for err in stats.errors[:20]:
        print(f"  error: {err}", file=sys.stderr)
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
