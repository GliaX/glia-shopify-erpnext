"""Patreon API v2 client: token refresh + members endpoint + pagination.

Auth: Creator's Access Token (refreshed via Creator's Refresh Token when it expires).
The access token is a long-lived bearer token; if the API returns 401, we refresh.

Transport-injectable for unit testing (same pattern as ShopifyClient/FrappeClient).
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

import structlog

log = structlog.get_logger()

PATREON_BASE = "https://www.patreon.com"

MEMBER_FIELDS = ",".join(
    [
        "full_name",
        "patron_status",
        "currently_entitled_amount_cents",
        "last_charge_date",
        "last_charge_status",
        "lifetime_support_cents",
        "pledge_relationship_start",
    ]
)
USER_FIELDS = "email,full_name"
TIER_FIELDS = "title,amount_cents"

Transport = Callable[..., dict[str, Any]]
"""(method, url, *, headers, data) -> parsed JSON dict."""


def _default_transport(
    method: str, url: str, *, headers: dict | None = None, data: bytes | None = None
) -> dict[str, Any]:
    import requests

    r = requests.request(method, url, headers=headers, data=data, timeout=30)
    return r.json()


class PatreonError(RuntimeError):
    pass


class PatreonClient:
    """Patreon API v2 client for fetching campaign members."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self._transport = transport or _default_transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": "Glia-Patreon-Sync/1.0",
        }

    def _refresh_token(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        resp = self._transport(
            "POST",
            f"{PATREON_BASE}/api/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        if "access_token" not in resp:
            raise PatreonError(f"Patreon token refresh failed: {resp}")
        self.access_token = resp["access_token"]
        self.refresh_token = resp.get("refresh_token", self.refresh_token)
        log.info("patreon_token_refreshed", expires_in=resp.get("expires_in"))

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{PATREON_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        resp = self._transport("GET", url, headers=self._headers())
        if resp.get("errors"):
            if any(str(e.get("status")) == "401" for e in resp["errors"]):
                log.info("patreon_token_expired_refreshing")
                self._refresh_token()
                resp = self._transport("GET", url, headers=self._headers())
            if resp.get("errors"):
                raise PatreonError(f"Patreon API errors: {resp['errors'][:3]}")
        return resp

    def get_campaign_id(self) -> str:
        resp = self._get("/api/oauth2/v2/campaigns", {"fields[campaign]": "creation_name"})
        return resp["data"][0]["id"]

    def fetch_user_emails(self, campaign_id: str) -> dict[str, str]:
        """Fetch {user_id: email} via the v1 pledges endpoint.

        The v2 members endpoint does NOT return member emails (a known API
        limitation), but the v1 pledges endpoint does. We use v1 for emails
        and v2 for detailed member data (amounts, charge status, lifetime).
        """
        emails: dict[str, str] = {}
        url: str | None = (
            f"{PATREON_BASE}/api/oauth2/api/campaigns/{campaign_id}/pledges?page[count]=200"
        )
        while url:
            resp = self._transport("GET", url, headers=self._headers())
            if resp.get("errors"):
                log.warning("patreon_v1_error", errors=resp["errors"][:2])
                break
            for i in resp.get("included", []):
                if i.get("type") == "user":
                    email = (i.get("attributes") or {}).get("email")
                    if email:
                        emails[i["id"]] = email
            url = (resp.get("links") or {}).get("next")
        log.info("patreon_emails_fetched", count=len(emails))
        return emails

    def iter_members(self, campaign_id: str) -> Iterator[dict[str, Any]]:
        """Yield enriched member dicts (with _user + _tiers merged in) via cursor pagination."""
        cursor: str | None = None
        while True:
            params = {
                "fields[member]": MEMBER_FIELDS,
                "fields[user]": USER_FIELDS,
                "fields[tier]": TIER_FIELDS,
                "include": "user,currently_entitled_tiers",
                "page[count]": "200",
            }
            if cursor:
                params["page[cursor]"] = cursor
            resp = self._get(f"/api/oauth2/v2/campaigns/{campaign_id}/members", params)
            included = {i["id"]: i for i in resp.get("included", [])}
            for m in resp.get("data", []):
                uid = (m.get("relationships", {}).get("user", {}).get("data") or {}).get("id")
                m["_user"] = (included.get(uid) or {}).get("attributes", {})
                tier_ids = [
                    t["id"]
                    for t in (
                        m.get("relationships", {}).get("currently_entitled_tiers", {}).get("data")
                        or []
                    )
                ]
                m["_tiers"] = [
                    (included.get(tid) or {}).get("attributes", {}).get("title", "")
                    for tid in tier_ids
                ]
                yield m
            cursor = (resp.get("meta", {}).get("pagination", {}).get("cursors") or {}).get("next")
            if not cursor:
                break


__all__ = ["PatreonClient", "PatreonError"]
