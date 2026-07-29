"""Transform tests: Shopify Order JSON -> Donor + Donation models."""

from __future__ import annotations

from decimal import Decimal

from glia_shopify_sync.transform import transform_order


def test_onetime_donation_with_tip_folded(order_onetime_tip, donation_gids, recurring_gids):
    result = transform_order(
        order_onetime_tip,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
        tip_mode="fold",
    )
    assert result is not None
    donor = result.donor
    assert donor.shopify_customer_id == "gid://shopify/Customer/9308425224291"
    assert donor.donor_name == "Anne L Walsh"
    assert donor.email == "loughryam@yahoo.com"
    assert donor.donor_type == "Individual"

    assert len(result.donations) == 1
    d = result.donations[0]
    # Tip folded in: 50.00 + 4.74 = 54.74 CAD
    assert d.amount == Decimal("54.74")
    assert d.currency == "CAD"
    assert d.amount_presentment == Decimal("49.21")  # 44.95 + 4.26 EUR
    assert d.currency_presentment == "EUR"
    assert d.donation_type == "One-time"
    assert d.campaign == "Glia4Gaza - One-Time Gift"
    assert d.tier == "$50 One Time Gift"
    assert d.includes_tip is True
    assert d.shopify_order_id == "gid://shopify/Order/6984546943075"
    assert d.shopify_line_item_id == "gid://shopify/LineItem/18115210575971"
    assert d.date == "2026-07-28"
    assert d.financial_status == "PAID"


def test_tip_ignored(order_onetime_tip, donation_gids, recurring_gids):
    result = transform_order(
        order_onetime_tip,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
        tip_mode="ignore",
    )
    assert result is not None
    d = result.donations[0]
    assert d.amount == Decimal("50.00")
    assert d.amount_presentment == Decimal("44.95")
    assert d.includes_tip is False


def test_recurring_donation_classified(order_recurring, donation_gids, recurring_gids):
    result = transform_order(
        order_recurring,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
    )
    assert result is not None
    d = result.donations[0]
    assert d.donation_type == "Recurring"
    assert d.amount == Decimal("25.00")
    assert d.currency == "CAD"
    assert d.amount_presentment == Decimal("19.00")
    assert d.currency_presentment == "USD"
    assert d.includes_tip is False  # no tip on this order
    assert d.campaign == "Glia4Gaza - Monthly Gift"


def test_merch_order_is_skipped(order_merch_only, donation_gids, recurring_gids):
    result = transform_order(
        order_merch_only,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
    )
    assert result is None  # t-shirt is not a donation


def test_recurring_by_name_when_product_not_in_recurring_set(order_recurring, donation_gids):
    # Recurring product ID removed from the recurring set; name still says "Recurring".
    recurring_gids_empty: set[str] = set()
    result = transform_order(
        order_recurring,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids_empty,
    )
    assert result is not None
    assert result.donations[0].donation_type == "Recurring"


def test_organization_donor_type(order_onetime_tip, donation_gids, recurring_gids):
    order = {
        **order_onetime_tip,
        "customer": {
            **order_onetime_tip["customer"],
            "defaultAddress": {
                **order_onetime_tip["customer"]["defaultAddress"],
                "company": "Acme Corp",
            },
        },
    }
    result = transform_order(order, donation_gids=donation_gids, recurring_gids=recurring_gids)
    assert result is not None
    assert result.donor.donor_type == "Organization"


def test_guest_checkout_no_customer_uses_order_email(donation_gids, recurring_gids):
    order = {
        "id": "gid://shopify/Order/1",
        "name": "#1",
        "processedAt": "2026-01-02T03:04:05Z",
        "displayFinancialStatus": "PAID",
        "test": False,
        "email": "guest@example.com",
        "currencyCode": "CAD",
        "presentmentCurrencyCode": "CAD",
        "customer": None,
        "lineItems": {
            "edges": [
                {
                    "node": {
                        "id": "gid://shopify/LineItem/1",
                        "name": "One Time Donation - $10",
                        "quantity": 1,
                        "product": {
                            "id": "gid://shopify/Product/7962927005795",
                            "title": "One Time Donation",
                        },
                        "variant": {"id": "v1", "title": "$10"},
                        "discountedTotalSet": {
                            "shopMoney": {"amount": "10.00", "currencyCode": "CAD"},
                            "presentmentMoney": {"amount": "10.00", "currencyCode": "CAD"},
                        },
                    }
                }
            ]
        },
    }
    result = transform_order(order, donation_gids=donation_gids, recurring_gids=recurring_gids)
    assert result is not None
    assert result.donor.email == "guest@example.com"
    assert result.donor.shopify_customer_id == ""
    # Name falls back to the email local part.
    assert result.donor.donor_name == "guest"


def test_test_order_skipped(order_onetime_tip, donation_gids, recurring_gids):
    order = {**order_onetime_tip, "test": True}
    result = transform_order(order, donation_gids=donation_gids, recurring_gids=recurring_gids)
    assert result is None


def test_test_order_included_when_configured(order_onetime_tip, donation_gids, recurring_gids):
    order = {**order_onetime_tip, "test": True}
    result = transform_order(
        order,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
        include_test_orders=True,
    )
    assert result is not None


def test_unpaid_order_skipped_when_paid_only(order_onetime_tip, donation_gids, recurring_gids):
    order = {**order_onetime_tip, "displayFinancialStatus": "REFUNDED"}
    assert (
        transform_order(order, donation_gids=donation_gids, recurring_gids=recurring_gids) is None
    )
    # ...but processed when paid_only is off.
    result = transform_order(
        order,
        donation_gids=donation_gids,
        recurring_gids=recurring_gids,
        paid_only=False,
    )
    assert result is not None
    assert result.donations[0].financial_status == "REFUNDED"


def test_multiple_donation_lines_each_become_a_donation(
    order_onetime_tip, donation_gids, recurring_gids
):
    # Append a second donation line item to the order.
    second = {
        "node": {
            "id": "gid://shopify/LineItem/9",
            "name": "Donate to the Glia Project - $20",
            "quantity": 1,
            "product": {"id": "gid://shopify/Product/7962927005795", "title": "One Time Donation"},
            "variant": {"id": "v9", "title": "$20"},
            "discountedTotalSet": {
                "shopMoney": {"amount": "20.00", "currencyCode": "CAD"},
                "presentmentMoney": {"amount": "18.00", "currencyCode": "EUR"},
            },
        }
    }
    order = {
        **order_onetime_tip,
        "lineItems": {"edges": order_onetime_tip["lineItems"]["edges"] + [second]},
    }
    result = transform_order(order, donation_gids=donation_gids, recurring_gids=recurring_gids)
    assert result is not None
    assert len(result.donations) == 2
    # Tip folds onto the FIRST donation only.
    amounts = sorted(d.amount for d in result.donations)
    assert amounts == [Decimal("20.00"), Decimal("54.74")]
    assert sum(d.includes_tip for d in result.donations) == 1
