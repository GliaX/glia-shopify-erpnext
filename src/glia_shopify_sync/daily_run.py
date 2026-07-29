"""`glia-sync-daily` — scheduled incremental sync (Shopify -> CRM).

Meant for the daily Kubernetes CronJob: the pod is **stateless** — the
incremental cursor lives in ERPNext (the `Glia Sync State` singleton), read and
written via the REST API. The run pulls only orders newer than the cursor;
re-running is safe (idempotent dedup). Falls back to the configured backfill
window on the very first run.
"""

from __future__ import annotations

import argparse

from .config import load_config, setup_logging
from .state import FrappeState
from .sync import build_clients, report_stats, run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-daily")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    parser.add_argument("--limit", type=int, help="process at most N orders (sample)")
    args = parser.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg)
    shopify, frappe = build_clients(cfg)
    state = FrappeState(frappe)  # cursor in ERPNext -> pod is stateless

    stats = run_sync(cfg, shopify, frappe, state, dry_run=args.dry_run, limit=args.limit)
    return report_stats(stats)


if __name__ == "__main__":
    raise SystemExit(main())
