"""`glia-sync-backfill` — backfill historical Shopify donations into Frappe CRM.

Reads Orders from Shopify (newest-first) and writes `Contact` + `Donation` to
ERPNext. Idempotent: re-running skips anything already ingested (dedup by
`shopify_customer_id` and by `shopify_order_id|shopify_line_item_id`). The
cursor is stored locally (state.json) — for the scheduled daily run (stateless,
cursor in ERPNext) use `glia-sync-daily`.

Examples:
    glia-sync-backfill --dry-run            # preview, no writes
    glia-sync-backfill --since 2024-01-01   # restrict range
    glia-sync-backfill --limit 50           # sample first 50 orders
    glia-sync-backfill                      # full history (resumes from cursor)
"""

from __future__ import annotations

import argparse

from .config import load_config, setup_logging
from .state import State
from .sync import build_clients, report_stats, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-backfill")
    parser.add_argument("--since", help="YYYY-MM-DD; default: cursor/config backfill.since")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N orders (sample)")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    shopify, frappe = build_clients(cfg)
    state = State(cfg.yaml.paths.state)

    stats = run_sync(
        cfg, shopify, frappe, state, since=args.since, dry_run=args.dry_run, limit=args.limit
    )
    return report_stats(stats)


if __name__ == "__main__":
    raise SystemExit(main())
