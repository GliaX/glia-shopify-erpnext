"""`glia-sync-send-test` — push one synthetic donor + donation, then clean up.

Verifies the full write path end to end against the live Frappe CRM:
CRM Contacts -> Donation. By default it DELETES what it created so the instance
is left pristine. Use --keep to inspect the result in the UI.

WRITE operation against ERPNext. Take a DB backup first.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from .crm_mapping import donation_to_doc, donor_to_contact
from .doctor import CheckResult
from .frappe_client import FrappeClient, FrappeError
from .models import Donation, Donor

log = structlog.get_logger()


def _synthetic_donor() -> Donor:
    return Donor(
        shopify_customer_id="gid://shopify/Customer/TEST_SYNC",
        donor_name="Glia Sync Test",
        first_name="Glia Sync",
        last_name="Test",
        email="glia-sync-test@example.com",
        phone="+15550000000",
        donor_type="Individual",
    )


def push_test_record(client: FrappeClient, *, keep: bool) -> CheckResult:
    """Push Contact + Donation; delete unless keep. Returns a CheckResult."""
    donor = _synthetic_donor()
    created: list[tuple[str, str]] = []  # (doctype, name)
    try:
        contact = client.insert(donor_to_contact(donor))
        created.append(("Contact", contact["name"]))
        donation = Donation(
            shopify_order_id="gid://shopify/Order/TEST_SYNC_ORDER",
            shopify_order_name="#TEST-1",
            shopify_line_item_id="gid://shopify/LineItem/TEST_SYNC_LINE",
            donor_shopify_customer_id=donor.shopify_customer_id,
            date=datetime.now(UTC).date().isoformat(),
            amount=Decimal("1.00"),
            currency="CAD",
            amount_presentment=Decimal("1.00"),
            currency_presentment="CAD",
            donation_type="One-time",
            campaign="Sync Test",
            tier="$1 Test",
            includes_tip=False,
            financial_status="PAID",
        )
        don = client.insert(donation_to_doc(donation, contact_name=contact["name"]))
        created.append(("Donation", don["name"]))

        detail = "created " + ", ".join(f"{dt}={name}" for dt, name in created)
        if not keep:
            _cleanup(client, created)
            detail += " (then deleted)"
        return CheckResult("Write test", True, detail)
    except FrappeError as e:
        if not keep:
            _cleanup(client, created)
        return CheckResult("Write test", False, f"{e} | created then cleaned: {created}")


def _cleanup(client: FrappeClient, created: list[tuple[str, str]]) -> None:
    for doctype, name in reversed(created):
        try:
            client.delete(doctype, name)
        except FrappeError as e:
            log.warning("cleanup_failed", doctype=doctype, name=name, error=str(e))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-send-test")
    parser.add_argument(
        "--keep", action="store_true", help="keep the created records (else delete)"
    )
    args = parser.parse_args(argv)

    from .config import load_config, setup_logging

    cfg = load_config()
    setup_logging(cfg)
    s = cfg.settings
    client = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
        max_attempts=2,
    )

    result = push_test_record(client, keep=args.keep)
    print(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
