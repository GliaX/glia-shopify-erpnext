"""Shopify Admin API client: 24-hour token manager + paginated Orders reader.

Auth model (post Jan 1, 2026): Dev Dashboard custom apps no longer expose a
static access token. We exchange the app's Client ID + Secret for a 24-hour
access token via the OAuth *client credentials grant*, and refresh it before
expiry. See shopify_queries.py and
https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant

Both objects accept injectable transports so the pagination / token logic can be
unit-tested without real HTTP (see tests/test_shopify_client.py).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import structlog

from .shopify_queries import ORDERS_QUERY, TOKEN_ENDPOINT_TEMPLATE

log = structlog.get_logger()

# Refresh this far ahead of true expiry, so we never send an expired token.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


# --- transports -----------------------------------------------------------
# A transport is a thin callable so tests can stub the network. The default
# implementations use `requests` lazily (kept out of module top-level so tests
# that inject fakes never need the dependency installed at import time).

TokenTransport = Callable[[str, str, str], dict[str, Any]]
"""(shop_domain, client_id, client_secret) -> parsed token JSON."""


def _default_token_transport(
    shop_domain: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    import requests

    url = f"https://{shop_domain}{TOKEN_ENDPOINT_TEMPLATE}"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if "access_token" not in data:
        raise ShopifyError(f"Token endpoint returned no access_token: {data}")
    return data


GqlTransport = Callable[[str, str, str, str, dict[str, Any]], dict[str, Any]]
"""(shop_domain, api_version, access_token, query, variables) -> parsed GraphQL JSON."""


def _default_gql_transport(
    shop_domain: str,
    api_version: str,
    access_token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    import requests

    url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
    resp = requests.post(
        url,
        json={"query": query, "variables": variables},
        headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if "errors" in data:
        # GraphQL errors arrive with HTTP 200; surface them.
        msgs = data["errors"]
        raise ShopifyError(f"Shopify GraphQL errors: {msgs}")
    return data


# --- errors ---------------------------------------------------------------


class ShopifyError(RuntimeError):
    """Raised for unrecoverable Shopify API failures."""


# --- token manager --------------------------------------------------------


class TokenManager:
    """Mint and cache a 24-hour Shopify access token.

    Reuses the cached token until it's within `_TOKEN_REFRESH_MARGIN_SECONDS`
    of expiry, then mints a fresh one on the next call.
    """

    def __init__(
        self,
        shop_domain: str,
        client_id: str,
        client_secret: str,
        *,
        transport: TokenTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.shop_domain = shop_domain
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport or _default_token_transport
        self._clock = clock or _default_clock
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        now = self._clock()
        if self._token and now < self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        log.debug("minting_shopify_token", shop=self.shop_domain)
        resp = self._transport(self.shop_domain, self.client_id, self.client_secret)
        if "access_token" not in resp:
            raise ShopifyError(f"Token endpoint returned no access_token: {resp}")
        self._token = resp["access_token"]
        expires_in = float(resp.get("expires_in", 86399))
        self._expires_at = now + expires_in
        # Stash for diagnostics without leaking the secret.
        log.info(
            "shopify_token_refreshed",
            expires_in=expires_in,
            scope=resp.get("scope", ""),
        )
        return self._token


def _default_clock() -> float:
    import time

    return time.time()


# --- client ---------------------------------------------------------------


class ShopifyClient:
    """Read-only Shopify Admin API client.

    Currently implements `iter_orders` — a cursor-paginated generator over the
    Orders connection, optionally filtered by `processed_at >= since`. It yields
    raw Order *node* dicts (the shape defined in `ORDERS_QUERY`); the transform
    layer turns them into Donor + Donation models.
    """

    def __init__(
        self,
        token_manager: TokenManager,
        api_version: str,
        *,
        shop_domain: str | None = None,
        page_size: int = 250,
        transport: GqlTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.token_manager = token_manager
        self.api_version = api_version
        self.shop_domain = shop_domain or token_manager.shop_domain
        self.page_size = page_size
        self._transport = transport or _default_gql_transport
        self._sleep = sleep or _default_sleep
        self._clock = clock or _default_clock

    def iter_orders(
        self,
        *,
        since: str | None = None,
        max_pages: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield Order nodes newest-first, paginating automatically.

        `since` (YYYY-MM-DD) restricts to orders processed on/after that date.
        `max_pages` caps pagination (useful for tests / dry-run sampling).
        """
        cursor: str | None = None
        pages = 0
        while True:
            variables: dict[str, Any] = {
                "first": self.page_size,
                "after": cursor,
                "sortKey": "PROCESSED_AT",
                "reverse": True,
                "query": f"processed_at:>={since}" if since else None,
            }
            resp = self._run_query(ORDERS_QUERY, variables)
            conn = resp["data"]["orders"]
            for edge in conn.get("edges", []):
                yield edge["node"]

            page_info = conn.get("pageInfo", {})
            pages += 1
            self._maybe_throttle(resp)

            if not page_info.get("hasNextPage"):
                break
            if max_pages is not None and pages >= max_pages:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break

    def _run_query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        token = self.token_manager.get_token()
        return self._transport(self.shop_domain, self.api_version, token, query, variables)

    def _maybe_throttle(self, resp: dict[str, Any]) -> None:
        """Respect Shopify's GraphQL cost throttling.

        `throttleStatus.currentlyAvailable` is the remaining query budget. If it
        drops near zero we sleep for the time it takes to refill one page's cost
        before requesting the next page.
        """
        cost = (resp.get("extensions") or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        currently = float(status.get("currentlyAvailable", 2000.0))
        restore_rate = float(status.get("restoreRate", 100.0))
        requested = float(cost.get("requestedQueryCost", 0.0) or 0.0)
        if currently < requested and restore_rate > 0:
            wait = max(0.0, (requested - currently) / restore_rate)
            if wait > 0:
                log.debug("shopify_throttle_sleep", wait=wait, budget=currently)
                self._sleep(wait)


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


__all__ = ["ShopifyClient", "ShopifyError", "TokenManager"]
