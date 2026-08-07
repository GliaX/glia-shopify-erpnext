"""Checkout setup tests (Phase 5) — idempotent + Stripe-key gating."""

from __future__ import annotations

import types

from glia_shopify_sync.config import ShopConfig
from glia_shopify_sync.shop.checkout_setup import run_checkout_setup

# Stripe Settings is named by gateway_name -> "Stripe".


def _cfg(with_keys: bool):
    from pydantic import SecretStr

    from glia_shopify_sync.config import Settings

    # _env_file=None so the test doesn't pick up the real STRIPE_* keys from .env
    s = Settings(_env_file=None)
    s.stripe_publishable_key = "pk_test_x" if with_keys else ""
    s.stripe_secret_key = SecretStr("sk_test_x" if with_keys else "")
    return types.SimpleNamespace(
        settings=s,
        yaml=types.SimpleNamespace(company="Glia", shop=ShopConfig()),
        donation_product_gids=set(),
        recurring_product_gids=set(),
    )


def test_without_keys_configures_webshop_and_gateway_shell(fake_frappe):
    # Stripe Settings doc absent in the fake -> find/get raise handled by fakes.
    results = run_checkout_setup(fake_frappe, _cfg(with_keys=False))
    by_name = {r.name: r.action for r in results}
    assert by_name["Webshop Settings"] == "configured"
    assert by_name["Payment Gateway"] == "created"
    assert by_name["Stripe Settings"] == "skipped"
    # Webshop Settings got enabled + checkout on.
    ws = fake_frappe.existing["Webshop Settings"]["Webshop Settings"]
    assert ws["enabled"] == 1 and ws["enable_checkout"] == 1
    assert ws["price_list"] == "Standard Selling"
    assert "payment_gateway_account" not in ws  # not linked without Stripe


def test_with_keys_is_idempotent(fake_frappe):
    # Seed the fakes so get() finds the Stripe-related docs (named "Stripe").
    fake_frappe.existing.setdefault("Stripe Settings", {})["Stripe"] = {"name": "Stripe"}
    fake_frappe.existing.setdefault("Payment Gateway Account", {})["Stripe - Glia"] = {
        "name": "Stripe - Glia"
    }
    run_checkout_setup(fake_frappe, _cfg(with_keys=True))
    # Second run re-configures without error.
    results = run_checkout_setup(fake_frappe, _cfg(with_keys=True))
    actions = {r.name: r.action for r in results}
    assert actions["Payment Gateway"] in ("exists", "created")
    assert actions["Stripe Settings"] == "configured"
    assert actions["Payment Gateway Account"] == "configured"
