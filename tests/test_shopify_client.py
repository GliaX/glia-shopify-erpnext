"""Shopify client tests: token caching/refresh + cursor pagination.

Uses injectable transports and a fake clock — no real HTTP.
"""

from __future__ import annotations

import pytest

from glia_shopify_sync.shopify_client import ShopifyClient, ShopifyError, TokenManager

# --- TokenManager --------------------------------------------------------


def test_token_manager_caches_until_near_expiry(token_response):
    calls = {"n": 0}

    def fake_transport(shop_domain, client_id, client_secret):
        calls["n"] += 1
        return token_response

    times = [1_000_000.0]

    def fake_clock():
        return times[0]

    tm = TokenManager(
        shop_domain="glia2.myshopify.com",
        client_id="cid",
        client_secret="sec",
        transport=fake_transport,
        clock=fake_clock,
    )

    assert tm.get_token() == "shpat_synthetic_test_token"
    assert tm.get_token() == "shpat_synthetic_test_token"  # cached
    assert tm.get_token() == "shpat_synthetic_test_token"  # cached
    assert calls["n"] == 1

    # Advance past expiry (token_response.expires_in = 86399) minus the refresh
    # margin of 60s.
    times[0] += 86399 - 60 + 1
    assert tm.get_token() == "shpat_synthetic_test_token"  # new mint
    assert calls["n"] == 2


def test_token_manager_raises_when_transport_lacks_access_token():
    def bad_transport(*args):
        return {"scope": "..."}

    tm = TokenManager("shop", "cid", "sec", transport=bad_transport, clock=lambda: 0.0)
    with pytest.raises(ShopifyError):
        tm.get_token()


# --- ShopifyClient.iter_orders ------------------------------------------


def _make_order_node(name: str) -> dict:
    return {"id": f"gid://shopify/Order/{name}", "name": name}


def _page(nodes, *, has_next: bool, end_cursor: str | None) -> dict:
    return {
        "data": {
            "orders": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                "edges": [{"node": n} for n in nodes],
            }
        },
        "extensions": {
            "cost": {
                "requestedQueryCost": 2,
                "throttleStatus": {"currentlyAvailable": 1900, "restoreRate": 100},
            }
        },
    }


def test_iter_orders_paginates_across_pages(token_response):
    # Two pages: page1 has 2 nodes + a cursor, page2 has 1 node and no next.
    page1 = _make_order_node("1")
    page2 = _make_order_node("2")
    page3 = _make_order_node("3")

    calls = []

    def fake_gql(shop_domain, api_version, access_token, query, variables):
        calls.append(variables)
        if variables.get("after") is None:
            return _page([page1, page2], has_next=True, end_cursor="CURSOR_A")
        assert variables["after"] == "CURSOR_A"
        return _page([page3], has_next=False, end_cursor=None)

    sleeps: list[float] = []

    tm = TokenManager("shop", "cid", "sec", transport=lambda *a: token_response, clock=lambda: 0.0)
    client = ShopifyClient(
        tm, api_version="2025-07", transport=fake_gql, sleep=sleeps.append, clock=lambda: 0.0
    )

    nodes = list(client.iter_orders())

    assert [n["name"] for n in nodes] == ["1", "2", "3"]
    # Two page requests made; the second carried the endCursor forward.
    assert len(calls) == 2
    assert calls[0]["after"] is None
    assert calls[1]["after"] == "CURSOR_A"
    # Default page size passed through.
    assert calls[0]["first"] == 250
    # No throttling sleep needed (budget comfortably covers cost).
    assert sleeps == []


def test_iter_orders_passes_since_filter(token_response):
    captured = {}

    def fake_gql(shop_domain, api_version, access_token, query, variables):
        captured.update(variables)
        return _page([], has_next=False, end_cursor=None)

    tm = TokenManager("shop", "cid", "sec", transport=lambda *a: token_response, clock=lambda: 0.0)
    client = ShopifyClient(tm, "2025-07", transport=fake_gql, clock=lambda: 0.0)

    list(client.iter_orders(since="2024-06-01"))

    assert captured["query"] == "processed_at:>=2024-06-01"
    assert captured["reverse"] is True
    assert captured["sortKey"] == "PROCESSED_AT"


def test_iter_orders_respects_max_pages(token_response):
    def fake_gql(shop_domain, api_version, access_token, query, variables):
        # Always another page.
        return _page([_make_order_node(variables["after"] or "0")], has_next=True, end_cursor="C")

    tm = TokenManager("shop", "cid", "sec", transport=lambda *a: token_response, clock=lambda: 0.0)
    client = ShopifyClient(tm, "2025-07", transport=fake_gql, clock=lambda: 0.0)

    nodes = list(client.iter_orders(max_pages=3))
    assert len(nodes) == 3


def test_iter_orders_throttles_when_budget_low(token_response):
    def fake_gql(shop_domain, api_version, access_token, query, variables):
        # currentlyAvailable < requestedQueryCost -> should trigger a sleep.
        return {
            "data": {
                "orders": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "C"},
                    "edges": [{"node": _make_order_node("1")}],
                }
            },
            "extensions": {
                "cost": {
                    "requestedQueryCost": 500,
                    "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100},
                }
            },
        }

    sleeps: list[float] = []
    tm = TokenManager("shop", "cid", "sec", transport=lambda *a: token_response, clock=lambda: 0.0)
    client = ShopifyClient(
        tm, "2025-07", transport=fake_gql, sleep=sleeps.append, clock=lambda: 0.0
    )

    list(client.iter_orders(max_pages=1))
    # (500 - 100) / 100 = 4.0 seconds of sleep requested.
    assert sleeps == [pytest.approx(4.0)]
