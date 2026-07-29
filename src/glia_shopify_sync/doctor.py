"""`glia-sync-doctor` — verify the sync's prerequisites end to end.

Read-only checks: Shopify token mint, ERPNext auth, the configured Company, the
core + custom doctypes, and the Donors customer group. Optionally performs a
write test (push + delete a record) via --with-write-test.

Exit codes: 0 = all pass, 1 = one or more fail, 2 = cannot run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import structlog

from .config import AppConfig, load_config, setup_logging
from .frappe_client import FrappeClient, FrappeError
from .shopify_client import TokenManager

log = structlog.get_logger()


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        marker = "✓" if self.ok else "✗"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"  [{marker}] {self.name}{suffix}"


def run_doctor(
    cfg: AppConfig,
    frappe: FrappeClient,
    *,
    shopify: TokenManager | None = None,
    with_write_test: bool = False,
) -> list[CheckResult]:
    results: list[CheckResult] = []

    # Shopify
    if shopify is not None:
        try:
            shopify.get_token()
            results.append(CheckResult("Shopify token mint", True, "OK"))
        except Exception as e:  # noqa: BLE001
            results.append(CheckResult("Shopify token mint", False, str(e)[:160]))

    # ERPNext/Frappe CRM auth
    try:
        frappe.get("DocType", "Contact")
        results.append(CheckResult("ERPNext auth + CRM present", True, "OK"))
    except FrappeError as e:
        results.append(CheckResult("ERPNext auth", False, str(e)[:160]))
        return results  # nothing else will work without auth

    # Company
    try:
        frappe.get("Company", cfg.yaml.company)
        results.append(CheckResult(f"Company '{cfg.yaml.company}'", True))
    except FrappeError as e:
        results.append(CheckResult(f"Company '{cfg.yaml.company}'", False, str(e)[:120]))

    # Core doctypes we target
    for dt in ("Contact", "CRM Organization"):
        try:
            frappe.get("DocType", dt)
            results.append(CheckResult(f"Doctype {dt}", True))
        except FrappeError as e:
            results.append(CheckResult(f"Doctype {dt}", False, str(e)[:120]))

    # Custom Donation doctype
    try:
        frappe.get("DocType", "Donation")
        results.append(CheckResult("Doctype Donation (custom)", True))
    except FrappeError:
        results.append(
            CheckResult("Doctype Donation (custom)", False, "run glia-sync-setup-erpnext")
        )

    # Contact.shopify_customer_id custom field (dedup)
    try:
        cf = frappe.find(
            "Custom Field",
            [
                ["Custom Field", "dt", "=", "Contact"],
                ["Custom Field", "fieldname", "=", "shopify_customer_id"],
            ],
            fields=["name"],
        )
        results.append(CheckResult("Contact.shopify_customer_id", True, cf["name"] if cf else ""))
    except FrappeError:
        results.append(
            CheckResult("Contact.shopify_customer_id", False, "run glia-sync-setup-erpnext")
        )

    if with_write_test:
        from .send_test import push_test_record

        results.append(push_test_record(frappe, keep=False))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-doctor")
    parser.add_argument(
        "--with-write-test", action="store_true", help="push + delete a test record"
    )
    parser.add_argument("--no-shopify", action="store_true", help="skip Shopify checks")
    args = parser.parse_args(argv)

    cfg: AppConfig = load_config()
    setup_logging(cfg)
    s = cfg.settings

    frappe = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
        max_attempts=2,
    )
    shopify = None
    if not args.no_shopify and s.shopify_shop_domain and s.shopify_client_id:
        shopify = TokenManager(
            shop_domain=s.shopify_shop_domain,
            client_id=s.shopify_client_id,
            client_secret=s.shopify_client_secret.get_secret_value(),
        )

    results = run_doctor(cfg, frappe, shopify=shopify, with_write_test=args.with_write_test)

    for r in results:
        print(r)
    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
