"""Shopify catalog client tests: iter_products / iter_collections pagination.

Uses injectable transports and a fake clock — no real HTTP. Mirrors the
orders-client tests in test_shopify_client.py.
"""

from __future__ import annotations

from glia_shopify_sync.shopify_client import ShopifyClient, TokenManager


def _product_node(num: int) -> dict:
    return {
        "id": f"gid://shopify/Product/{num}",
        "title": f"P{num}",
        "handle": f"p{num}",
        "variants": {"edges": []},
        "images": {"edges": []},
        "collections": {"edges": []},
    }


def _products_page(nodes, *, has_next, end_cursor) -> dict:
    return {
        "data": {
            "products": {
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


def _collections_page(nodes, *, has_next, end_cursor) -> dict:
    return {
        "data": {
            "collections": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                "edges": [{"node": n} for n in nodes],
            }
        },
        "extensions": {
            "cost": {"requestedQueryCost": 2, "throttleStatus": {"currentlyAvailable": 1900}}
        },
    }


def _client(transport, token_response):
    tm = TokenManager("shop", "cid", "sec", transport=lambda *a: token_response, clock=lambda: 0.0)
    return ShopifyClient(tm, "2025-07", transport=transport, clock=lambda: 0.0)


def test_iter_products_paginates(token_response):
    calls = []

    def gql(shop_domain, api_version, access_token, query, variables):
        calls.append(variables)
        if variables.get("after") is None:
            return _products_page(
                [_product_node(1), _product_node(2)], has_next=True, end_cursor="A"
            )
        assert variables["after"] == "A"
        return _products_page([_product_node(3)], has_next=False, end_cursor=None)

    client = _client(gql, token_response)
    handles = [n["handle"] for n in client.iter_products()]
    assert handles == ["p1", "p2", "p3"]
    assert len(calls) == 2
    assert calls[0]["after"] is None
    assert calls[1]["after"] == "A"


def test_iter_products_defaults_to_active_only(token_response):
    captured = {}

    def gql(shop_domain, api_version, access_token, query, variables):
        captured.update(variables)
        return _products_page([], has_next=False, end_cursor=None)

    client = _client(gql, token_response)
    list(client.iter_products())
    assert captured["query"] == "status:ACTIVE"
    assert captured["sortKey"] == "TITLE"


def test_iter_products_includes_archived_when_asked(token_response):
    captured = {}

    def gql(shop_domain, api_version, access_token, query, variables):
        captured.update(variables)
        return _products_page([], has_next=False, end_cursor=None)

    client = _client(gql, token_response)
    list(client.iter_products(include_archived=True))
    # No status filter when include_archived is set.
    assert "query" not in captured


def test_iter_products_respects_max_pages(token_response):
    def gql(shop_domain, api_version, access_token, query, variables):
        return _products_page([_product_node(1)], has_next=True, end_cursor="C")

    client = _client(gql, token_response)
    nodes = list(client.iter_products(max_pages=2))
    assert len(nodes) == 2


def test_iter_collections_paginates(token_response):
    calls = []

    def gql(shop_domain, api_version, access_token, query, variables):
        calls.append(variables.get("after"))
        if variables.get("after") is None:
            return _collections_page(
                [{"id": "c1", "title": "Merch"}], has_next=True, end_cursor="X"
            )
        return _collections_page(
            [{"id": "c2", "title": "Devices"}], has_next=False, end_cursor=None
        )

    client = _client(gql, token_response)
    titles = [n["title"] for n in client.iter_collections()]
    assert titles == ["Merch", "Devices"]
    assert calls == [None, "X"]
