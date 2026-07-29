"""`glia-sync-setup-erpnext` — make ERPNext/Frappe CRM ready for the donation sync.

Idempotently creates, on the live instance:
  * the `Glia` module
  * a `shopify_customer_id` Custom Field on `CRM Contact` (donor dedup)
  * the custom `Donation` DocType (linked to `CRM Contact`)

Safe to re-run: every step checks for existence before creating. The Donation
DocType's `on_update` hook creates its DB table.

This is a WRITE operation against ERPNext. Take a DB backup first
(`bench --site asset.glia.org backup` or a managed-DB snapshot).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import structlog

from .config import AppConfig, load_config, setup_logging
from .donation_doctype import (
    contact_custom_fields,
    donation_doctype_def,
    glia_module_def,
)
from .frappe_client import FrappeClient, FrappeError

log = structlog.get_logger()


@dataclass
class StepResult:
    name: str
    action: str  # "created" | "exists" | "skipped"
    detail: str = ""


def run_setup(client: FrappeClient) -> list[StepResult]:
    results: list[StepResult] = []
    results.append(_ensure_module(client))
    results.extend(_ensure_contact_custom_fields(client))
    results.append(_ensure_donation_doctype(client))
    return results


def _ensure_module(client: FrappeClient) -> StepResult:
    return _ensure_doc(client, "Module Def", "Glia", glia_module_def(), label_key="module_name")


def _ensure_contact_custom_fields(client: FrappeClient) -> list[StepResult]:
    out: list[StepResult] = []
    for cf in contact_custom_fields():
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


def _ensure_donation_doctype(client: FrappeClient) -> StepResult:
    return _ensure_doc(client, "DocType", "Donation", donation_doctype_def())


def _ensure_doc(
    client: FrappeClient,
    doctype: str,
    name: str,
    defn: dict,
    *,
    label_key: str = "name",
) -> StepResult:
    try:
        client.get(doctype, name)
        return StepResult(name, "exists")
    except FrappeError as e:
        if "404" not in str(e) and "not found" not in str(e).lower():
            raise
    doc = dict(defn)
    doc.setdefault(label_key, name)
    created = client.insert(doc)
    return StepResult(name, "created", created.get("name", name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-setup-erpnext")
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

    client = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
        max_attempts=2,
    )

    if args.dry_run:
        print("Dry run — would ensure: Glia module, Contact.shopify_customer_id, Donation doctype")
        return 0

    try:
        results = run_setup(client)
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
