"""`glia-shop-setup` — make ERPNext ready for the shop catalog migration.

Idempotently creates, on the live instance:
  * custom `shopify_*` fields on `Item` (product/variant dedup + flags)
  * a custom `shopify_collection_id` field on `Website Category` (collection dedup)
  * the shop `Price List` (e.g. "Standard Selling") in the shop currency, selling-enabled
  * the `Item Group`s referenced by `config.yaml -> shop` (default, donations,
    and every value in `item_group_map`)

Safe to re-run: every step checks for existence before creating. This is a WRITE
operation against ERPNext — take a DB backup first (`bench --site asset.glia.org
backup` or a managed-DB snapshot).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

import structlog

from ..config import AppConfig, load_config, setup_logging
from ..frappe_client import FrappeClient, FrappeError

log = structlog.get_logger()


@dataclass
class StepResult:
    name: str
    action: str  # "created" | "exists" | "skipped"
    detail: str = ""


# --- custom field definitions --------------------------------------------


def item_custom_fields() -> list[dict[str, Any]]:
    """Custom `shopify_*` fields on `Item` for dedup + classification.

    None are marked `unique`: a template Item and its variants legitimately share
    `shopify_product_id`, and templates carry a blank `shopify_variant_id`. Dedup
    is handled in memory by the sync (see `catalog_sync.load_item_map`). Same
    lesson as the donation doctype's `shopify_order_id` (see OPERATIONS.md §8.6).
    """
    fields = [
        ("Shopify Product ID", "shopify_product_id", "Data"),
        ("Shopify Variant ID", "shopify_variant_id", "Data"),
        ("Shopify Handle", "shopify_handle", "Data"),
        ("Shopify Product Type", "shopify_product_type", "Data"),
        ("Shopify SKU", "shopify_sku", "Data"),
        ("Shopify Is Donation", "shopify_is_donation", "Check"),
        ("Shopify Is Recurring", "shopify_is_recurring", "Check"),
    ]
    return [
        _custom_field("Item", label, fieldname, fieldtype) for label, fieldname, fieldtype in fields
    ]


def item_group_custom_fields() -> list[dict[str, Any]]:
    """Custom field on `Item Group` to dedup Shopify Collections (Phase 2 will
    map Shopify collections -> Item Groups)."""
    return [
        _custom_field("Item Group", "Shopify Collection ID", "shopify_collection_id", "Data"),
    ]


def customer_custom_fields() -> list[dict[str, Any]]:
    """Custom fields on `Customer` for Phase 3 dedup + donor cross-reference."""
    return [
        _custom_field("Customer", "Shopify Customer ID", "shopify_customer_id", "Data"),
        _custom_field("Customer", "Shopify Email", "shopify_email", "Data"),
    ]


def sales_order_custom_fields() -> list[dict[str, Any]]:
    """Custom fields on `Sales Order` for Phase 4 dedup + Shopify reference."""
    return [
        _custom_field("Sales Order", "Shopify Order ID", "shopify_order_id", "Data"),
        _custom_field("Sales Order", "Shopify Order Name", "shopify_order_name", "Data"),
    ]


def _custom_field(dt: str, label: str, fieldname: str, fieldtype: str) -> dict[str, Any]:
    return {
        "doctype": "Custom Field",
        "dt": dt,
        "label": label,
        "fieldname": fieldname,
        "fieldtype": fieldtype,
        "no_copy": 1,
        "translatable": 0,
    }


# --- top-level setup ------------------------------------------------------


def run_setup(client: FrappeClient, cfg: AppConfig) -> list[StepResult]:
    results: list[StepResult] = []
    results.extend(_ensure_custom_fields(client, item_custom_fields()))
    results.extend(_ensure_custom_fields(client, item_group_custom_fields()))
    results.extend(_ensure_custom_fields(client, customer_custom_fields()))
    results.extend(_ensure_custom_fields(client, sales_order_custom_fields()))
    results.append(_ensure_price_list(client, cfg))
    results.extend(_ensure_item_groups(client, cfg))
    results.extend(_ensure_customer_setup(client, cfg))
    results.append(_ensure_guest_customer(client, cfg))
    return results


# --- step implementations -------------------------------------------------


def _ensure_custom_fields(client: FrappeClient, defs: list[dict[str, Any]]) -> list[StepResult]:
    out: list[StepResult] = []
    for cf in defs:
        existing = client.find(
            "Custom Field",
            [
                ["Custom Field", "dt", "=", cf["dt"]],
                ["Custom Field", "fieldname", "=", cf["fieldname"]],
            ],
            fields=["name"],
        )
        if existing:
            out.append(StepResult(f"{cf['dt']}.{cf['fieldname']}", "exists", existing["name"]))
        else:
            created = client.insert(cf)
            out.append(
                StepResult(f"{cf['dt']}.{cf['fieldname']}", "created", created.get("name", ""))
            )
    return out


def _ensure_price_list(client: FrappeClient, cfg: AppConfig) -> StepResult:
    name = cfg.yaml.shop.price_list
    if _exists(client, "Price List", name):
        return StepResult(name, "exists")
    client.insert(
        {
            "doctype": "Price List",
            "price_list_name": name,
            "currency": cfg.yaml.shop.currency,
            "enabled": 1,
            "selling": 1,
            "buying": 0,
        }
    )
    return StepResult(name, "created")


def _ensure_item_groups(client: FrappeClient, cfg: AppConfig) -> list[StepResult]:
    names = {cfg.yaml.shop.item_group_default, cfg.yaml.shop.item_group_donations}
    names.update(cfg.yaml.shop.item_group_map.values())
    out: list[StepResult] = []
    for name in sorted(n for n in names if n):
        if _exists(client, "Item Group", name):
            out.append(StepResult(name, "exists"))
            continue
        client.insert(
            {
                "doctype": "Item Group",
                "item_group_name": name,
                "parent_item_group": cfg.yaml.shop.item_group_parent,
                "is_group": 0,
            }
        )
        out.append(StepResult(name, "created"))
    return out


def _exists(client: FrappeClient, doctype: str, name: str) -> bool:
    try:
        client.get(doctype, name)
        return True
    except FrappeError as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return False
        raise


def _ensure_customer_setup(client: FrappeClient, cfg: AppConfig) -> list[StepResult]:
    """Ensure the default Customer Group + Territory referenced by Phase 3 exist.

    ERPNext ships with `All Customer Groups` -> `Individual` and
    `All Territories`; these normally already exist, so this is a no-op safety net.
    """
    out: list[StepResult] = []
    group = cfg.yaml.shop.customer_group
    if group and not _exists(client, "Customer Group", group):
        client.insert(
            {
                "doctype": "Customer Group",
                "customer_group_name": group,
                "parent_customer_group": "All Customer Groups",
                "is_group": 0,
            }
        )
        out.append(StepResult(group, "created"))
    else:
        out.append(StepResult(group, "exists"))
    territory = cfg.yaml.shop.customer_territory
    if territory and not _exists(client, "Territory", territory):
        client.insert(
            {
                "doctype": "Territory",
                "territory_name": territory,
                "parent_territory": "All Territories",
                "is_group": 0,
            }
        )
        out.append(StepResult(territory, "created"))
    else:
        out.append(StepResult(territory, "exists"))
    return out


def _ensure_guest_customer(client: FrappeClient, cfg: AppConfig) -> StepResult:
    """Phase 4 fallback Customer for guest Shopify orders (no customer account)."""
    name = cfg.yaml.shop.guest_customer
    if _exists(client, "Customer", name):
        return StepResult(name, "exists")
    client.insert(
        {
            "doctype": "Customer",
            "customer_name": name,
            "customer_type": "Individual",
            "customer_group": cfg.yaml.shop.customer_group,
            "territory": cfg.yaml.shop.customer_territory,
        }
    )
    return StepResult(name, "created")


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-shop-setup")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args(argv)

    cfg: AppConfig = load_config()
    setup_logging(cfg)
    s = cfg.settings
    if not (s.erpnext_base_url and s.erpnext_api_key and s.erpnext_api_secret.get_secret_value()):
        print(
            "ERROR: ERPNEXT_BASE_URL / ERPNEXT_API_KEY / ERPNEXT_API_SECRET not set",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        groups = sorted(
            {cfg.yaml.shop.item_group_default, cfg.yaml.shop.item_group_donations}
            | set(cfg.yaml.shop.item_group_map.values())
        )
        print(
            "Dry run — would ensure:\n"
            "  - 7 shopify_* custom fields on Item\n"
            "  - shopify_collection_id custom field on Item Group\n"
            "  - shopify_customer_id + shopify_email custom fields on Customer\n"
            "  - shopify_order_id + shopify_order_name custom fields on Sales Order\n"
            f"  - Price List '{cfg.yaml.shop.price_list}' ({cfg.yaml.shop.currency})\n"
            f"  - Item Groups: {', '.join(groups)}\n"
            f"  - Customer Group '{cfg.yaml.shop.customer_group}' + Territory '{cfg.yaml.shop.customer_territory}'\n"
            f"  - Guest customer '{cfg.yaml.shop.guest_customer}'"
        )
        return 0

    client = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
        max_attempts=2,
    )

    try:
        results = run_setup(client, cfg)
    except FrappeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for r in results:
        marker = {"created": "+", "exists": "=", "skipped": "-"}.get(r.action, "?")
        suffix = f"  ({r.detail})" if r.detail else ""
        print(f"  [{marker}] {r.name}: {r.action}{suffix}")
    created = sum(1 for r in results if r.action == "created")
    print(f"\nSetup complete. {created} created, {len(results) - created} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
