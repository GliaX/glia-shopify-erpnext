"""Phase 5 — Checkout setup: Webshop Settings + Stripe Payment Gateway. (LIVE)

Configures ERPNext E Commerce / Shopping Cart end-to-end:

  * `Webshop Settings`      - enable shopping cart + checkout, default Price List,
                              Customer Group, company, payment gateway account.
  * `Payment Gateway`       - the "Stripe" gateway doc.
  * `Stripe Settings`       - publishable + secret key (from `.env`).
  * `Payment Gateway Account` - links Stripe -> the Glia CAD bank account; set as
                              default and wired into Webshop Settings.

The Stripe API keys are the only secret part; they live in `.env`
(`STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`). If absent, this CLI still
configures everything else and prints a reminder — add the keys and re-run.

Idempotent: safe to re-run. This is a WRITE op against ERPNext — back up first.

Examples:
    glia-shop-checkout-setup --dry-run      # preview
    glia-shop-checkout-setup                # configure (skip Stripe if no keys)
    # add STRIPE_* to .env, then:
    glia-shop-checkout-setup                # finishes Stripe + links it
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

STRIPE_GATEWAY = "Stripe"
WEBSHOP_SETTINGS = "Webshop Settings"
WEBSHOP_SETTINGS_NAME = "Webshop Settings"


@dataclass
class StepResult:
    name: str
    action: str  # "configured" | "created" | "exists" | "skipped"
    detail: str = ""


def run_checkout_setup(client: FrappeClient, cfg: AppConfig) -> list[StepResult]:
    results: list[StepResult] = []
    s = cfg.settings
    has_keys = bool(s.stripe_publishable_key and s.stripe_secret_key.get_secret_value())

    results.append(_configure_webshop(client, cfg, link_gateway=has_keys))
    results.append(_ensure_payment_gateway(client))

    if not has_keys:
        results.append(
            StepResult(
                "Stripe Settings",
                "skipped",
                "STRIPE_PUBLISHABLE_KEY / STRIPE_SECRET_KEY not in .env; "
                "add them and re-run to finish Stripe wiring",
            )
        )
        return results

    results.append(_ensure_stripe_settings(client, cfg))
    results.append(_ensure_payment_gateway_account(client, cfg))
    # Now that a Payment Gateway Account exists, link it into Webshop Settings.
    results.append(_configure_webshop(client, cfg, link_gateway=True))
    return results


# --- Webshop Settings -----------------------------------------------------


def _configure_webshop(client: FrappeClient, cfg: AppConfig, *, link_gateway: bool) -> StepResult:
    shop = cfg.yaml.shop
    values: dict[str, Any] = {
        "enabled": 1,
        "enable_checkout": 1,
        "company": cfg.yaml.company,
        "price_list": shop.price_list,
        "default_customer_group": shop.customer_group,
        "show_price": 1,
        "products_per_page": 24,
        "payment_success_url": "Orders",
    }
    if link_gateway:
        pga = _find_payment_gateway_account(client, cfg)
        if pga:
            values["payment_gateway_account"] = pga
    client.update(WEBSHOP_SETTINGS, WEBSHOP_SETTINGS_NAME, values)
    return StepResult(WEBSHOP_SETTINGS, "configured", f"{len(values)} fields")


# --- Payment Gateway ------------------------------------------------------


def _ensure_payment_gateway(client: FrappeClient) -> StepResult:
    # Payment Gateway is named by `gateway`. The controller is linked once the
    # Stripe Settings doc exists.
    existing = client.find("Payment Gateway", [["Payment Gateway", "gateway", "=", STRIPE_GATEWAY]])
    if existing:
        return StepResult("Payment Gateway", "exists", existing["name"])
    saved = client.insert(
        {
            "doctype": "Payment Gateway",
            "gateway": STRIPE_GATEWAY,
            # gateway_settings/gateway_controller set when Stripe Settings exists.
        }
    )
    return StepResult("Payment Gateway", "created", saved.get("name", ""))


def _ensure_stripe_settings(client: FrappeClient, cfg: AppConfig) -> StepResult:
    s = cfg.settings
    name = STRIPE_GATEWAY  # Stripe Settings is named by gateway_name
    doc = {
        "doctype": "Stripe Settings",
        "gateway_name": STRIPE_GATEWAY,
        "publishable_key": s.stripe_publishable_key,
        "secret_key": s.stripe_secret_key.get_secret_value(),
    }
    try:
        client.get("Stripe Settings", name)
        client.update("Stripe Settings", name, doc)
        action = "configured"
    except FrappeError as e:
        if "404" not in str(e) and "not found" not in str(e).lower():
            raise
        client.insert(doc)
        action = "created"
    # Link the Payment Gateway to its controller now that Stripe Settings exists.
    client.update(
        "Payment Gateway",
        STRIPE_GATEWAY,
        {
            "gateway_settings": "Stripe Settings",
            "gateway_controller": STRIPE_GATEWAY,
        },
    )
    return StepResult("Stripe Settings", action)


def _ensure_payment_gateway_account(client: FrappeClient, cfg: AppConfig) -> StepResult:
    shop = cfg.yaml.shop
    name = f"{STRIPE_GATEWAY} - {cfg.yaml.company}"
    doc = {
        "doctype": "Payment Gateway Account",
        "payment_gateway": STRIPE_GATEWAY,
        "payment_account": shop.payment_account,
        "company": cfg.yaml.company,
        "message": "Pay with Stripe",
        "is_default": 1,
    }
    try:
        client.get("Payment Gateway Account", name)
        client.update("Payment Gateway Account", name, doc)
        action = "configured"
    except FrappeError as e:
        if "404" not in str(e) and "not found" not in str(e).lower():
            raise
        client.insert(doc)
        action = "created"
    return StepResult("Payment Gateway Account", action)


def _find_payment_gateway_account(client: FrappeClient, cfg: AppConfig) -> str | None:
    row = client.find(
        "Payment Gateway Account",
        [["Payment Gateway Account", "payment_gateway", "=", STRIPE_GATEWAY]],
        fields=["name"],
    )
    return row["name"] if row else None


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-shop-checkout-setup")
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
        has_keys = bool(s.stripe_publishable_key and s.stripe_secret_key.get_secret_value())
        print(
            "Dry run — would:\n"
            f"  - enable Webshop Settings (company={cfg.yaml.company}, "
            f"price_list={cfg.yaml.shop.price_list}, checkout=on)\n"
            "  - create Payment Gateway 'Stripe'\n"
            + (
                "  - create Stripe Settings + Payment Gateway Account "
                f"(bank={cfg.yaml.shop.payment_account}) and link checkout\n"
                if has_keys
                else "  - SKIP Stripe Settings (no STRIPE_* keys in .env)\n"
            )
        )
        return 0

    client = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
        max_attempts=2,
    )

    try:
        results = run_checkout_setup(client, cfg)
    except FrappeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for r in results:
        marker = {"configured": "*", "created": "+", "exists": "=", "skipped": "-"}.get(
            r.action, "?"
        )
        suffix = f"  ({r.detail})" if r.detail else ""
        print(f"  [{marker}] {r.name}: {r.action}{suffix}")
    print("\nCheckout setup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
