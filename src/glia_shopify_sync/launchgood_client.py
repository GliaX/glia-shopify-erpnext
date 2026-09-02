"""LaunchGood API client.

LaunchGood has no public API, so this client scrapes the internal APIs their
SPA uses, discovered from the site's JS bundles:

1. **v4 API** (`https://api.launchgood.com`, NestJS) — account-level data:
   session, campaign ids, donor aggregates (WITH emails), recurring plans.
2. **v3 API** (`https://www.launchgood.com/api`, legacy) — per-campaign
   donation lists (per-donation records: id, amount, date, display name).
3. **Open search** (`https://v3.launchgood.com/v3`, unauthenticated) —
   campaign id → name/slug lookup.

Two quirks:

* **Cloudflare bot protection** — plain HTTP clients get 403. `curl_cffi`
  with Chrome TLS impersonation passes, so that is the default transport.
* **Auth** — the SPA's OAuth token exchange is server-side (confidential
  client) and password login is reCAPTCHA-guarded, so neither is scriptable.
  Instead the user logs in normally in a browser and copies the session token
  (`localStorage.getItem("token")` in devtools); it works as a Bearer token on
  both the v3 and v4 APIs. Cached in a JSON file; re-paste when it expires.

Endpoints used:

  * ``GET  /v4/auth/session``                        — user + account ids
  * ``GET  /v4/accounts/{id}/campaigns``             — campaign ids (only ids!)
  * ``GET  /v4/accounts/{id}/donors``                — donor aggregates + emails
  * ``GET  /v4/accounts/{id}/recurring-plans/subscribers`` — recurring donors
  * ``POST {v3}/api/projectapi/donors``              — per-campaign donations
  * ``GET  {search}/cards/campaign/by-id/{id}``      — campaign name/slug

The transport is injectable so tests never touch the network: a transport is a
callable ``(method, url, *, params, json, headers) -> TransportResponse``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

API_BASE = "https://api.launchgood.com"
V3_BASE = "https://www.launchgood.com"
SEARCH_BASE = "https://v3.launchgood.com/v3"

DEFAULT_PAGE_SIZE = 100
V3_PAGE_SIZE = 20  # fixed by the v3 donors endpoint


class LaunchGoodError(Exception):
    """API/transport error (message includes status when available)."""


@dataclass
class TransportResponse:
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


Transport = Callable[..., TransportResponse]


def curl_cffi_transport(method: str, url: str, **kwargs: Any) -> TransportResponse:
    """Default transport: curl_cffi with Chrome impersonation (Cloudflare bypass)."""
    from curl_cffi import requests as cr

    r = cr.request(method, url, impersonate="chrome", timeout=60, **kwargs)
    return TransportResponse(status_code=r.status_code, text=r.text, headers=dict(r.headers))


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0  # unix ts; 0 = unknown
    scope: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and time.time() >= self.expires_at - 60


class LaunchGoodClient:
    """Authenticated client for LaunchGood's internal v3/v4 APIs."""

    def __init__(
        self,
        token_file: Path | str,
        *,
        transport: Transport | None = None,
        account_id: str | int | None = None,
    ) -> None:
        self.token_file = Path(token_file)
        self.transport = transport or curl_cffi_transport
        self.account_id = str(account_id) if account_id else None
        self._tokens = self._load_tokens()

    # --- auth ----------------------------------------------------------

    def _load_tokens(self) -> TokenSet | None:
        if not self.token_file.is_file():
            return None
        try:
            d = json.loads(self.token_file.read_text(encoding="utf-8"))
            token = d.get("access_token", "")
            if not token:
                return None
            return TokenSet(
                access_token=token,
                refresh_token=d.get("refresh_token", ""),
                expires_at=float(d.get("expires_at") or 0),
                scope=d.get("scope", ""),
            )
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("launchgood_token_file_unreadable", error=str(e))
            return None

    def _save_tokens(self, tokens: TokenSet) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_at": tokens.expires_at,
                    "scope": tokens.scope,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_file)
        self._tokens = tokens

    @property
    def authenticated(self) -> bool:
        return bool(self._tokens and self._tokens.access_token)

    def save_access_token(self, access_token: str, expires_at: float = 0.0) -> None:
        """Persist the bearer token captured from the browser (localStorage).

        The user logs in at launchgood.com normally, then copies
        ``localStorage.getItem("token")`` from devtools. It authenticates as
        ``Authorization: Bearer`` against both API hosts.
        """
        access_token = access_token.strip().strip('"').strip("'")
        if not access_token:
            raise LaunchGoodError("empty access token")
        self._save_tokens(TokenSet(access_token=access_token, expires_at=expires_at))
        log.info("launchgood_token_saved", expires_at=expires_at or None)

    def _token(self) -> str:
        if not self._tokens or not self._tokens.access_token:
            raise LaunchGoodError(
                "No token — run `glia-sync-launchgood login --access-token ...` first."
            )
        if self._tokens.expired:
            raise LaunchGoodError("Token expired — log in again and re-paste it.")
        return self._tokens.access_token

    # --- HTTP ------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        r = self.transport(
            method,
            url,
            params={k: v for k, v in (params or {}).items() if v is not None} or None,
            json=json_body,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
            },
        )
        if r.status_code == 401:
            raise LaunchGoodError("401 Unauthorized — token rejected; log in again.")
        if r.status_code >= 400:
            raise LaunchGoodError(f"{method} {url} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", f"{API_BASE}{path}", params=params)

    # --- v4 API ------------------------------------------------------------

    def get_session(self) -> dict[str, Any]:
        """Current session: user info + the account(s) they administer."""
        data = self._get("/v4/auth/session")
        return data if isinstance(data, dict) else {"data": data}

    def resolve_account_id(self) -> str:
        """The org account id to pull donors for (explicit > session-derived).

        When the session lists several accounts, prefer a corporate one (orgs
        run campaigns from corporate accounts).
        """
        if self.account_id:
            return self.account_id
        session = self.get_session()
        account_id = _account_id_from_session(session)
        if not account_id:
            raise LaunchGoodError(
                "Could not determine account id from session — pass --account-id."
            )
        self.account_id = str(account_id)
        return self.account_id

    def get_campaign_ids(self, account_id: str | None = None) -> list[str]:
        """Campaign ids for the account (the endpoint returns only ids)."""
        account_id = account_id or self.resolve_account_id()
        data = self._get(f"/v4/accounts/{account_id}/campaigns")
        return [str(c["id"]) for c in _as_list(data) if c.get("id")]

    def iter_donors(
        self,
        account_id: str | None = None,
        *,
        campaign_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Yield donor aggregate records (name/email/country/totals).

        Paginates skip/take until a short page. Defensive: if the API ignores
        `skip` and returns an identical page twice, stop instead of looping.
        """
        account_id = account_id or self.resolve_account_id()
        skip = 0
        prev_page: list[dict[str, Any]] | None = None
        while True:
            data = self._get(
                f"/v4/accounts/{account_id}/donors",
                params={
                    "campaignIds": ",".join(campaign_ids) if campaign_ids else None,
                    "from": date_from,
                    "to": date_to,
                    "take": page_size,
                    "skip": skip,
                },
            )
            page = _as_list(data, "donors")
            if not page:
                return
            if prev_page is not None and page == prev_page:
                log.warning("launchgood_pagination_stalled", skip=skip)
                return
            yield from page
            if len(page) < page_size:
                return
            prev_page = page
            skip += page_size

    def iter_recurring_subscribers(
        self, account_id: str | None = None, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Yield recurring-giving subscribers (full PII incl. address)."""
        account_id = account_id or self.resolve_account_id()
        skip = 0
        prev_page: list[dict[str, Any]] | None = None
        while True:
            data = self._get(
                f"/v4/accounts/{account_id}/recurring-plans/subscribers",
                params={"take": page_size, "skip": skip},
            )
            page = _as_list(data, "data")
            if not page:
                return
            if prev_page is not None and page == prev_page:
                log.warning("launchgood_subscriber_pagination_stalled", skip=skip)
                return
            yield from page
            if len(page) < page_size:
                return
            prev_page = page
            skip += page_size

    # --- v3 reports (per-campaign donor report CSV, owner-only PII) ---------

    def get_donor_report_csv(
        self,
        campaign_id: str | int,
        *,
        poll_interval: float = 3.0,
        poll_timeout: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str:
        """Download the per-campaign "Donor Report" CSV (one row per donation).

        This is the classic dashboard's download (``openDownloadPopup`` in the
        AngularJS bundle): rows carry Donation ID, real First/Last Name,
        Email, billing address, and amounts in both donor and project
        currency — including for donors marked "Anonymous" publicly.

        Flow: ``projectreports/downloadInfo`` → identifier, then
        ``reports/action`` verb=generate, poll verb=status until the S3 URL is
        ready, then fetch it.
        """
        info = self._v3_rpc(
            "projectreports/downloadInfo",
            {"id": int(campaign_id), "type": "donors", "format": "csv", "isAdmin": False},
        )
        identifier = info.get("identifier")
        if not identifier:
            raise LaunchGoodError(f"downloadInfo gave no identifier: {str(info)[:200]}")
        self._v3_rpc(
            "reports/action", {"verb": "generate", "identifier": identifier, "format": "csv"}
        )
        deadline = time.monotonic() + poll_timeout
        while True:
            status = self._v3_rpc(
                "reports/action", {"verb": "status", "identifier": identifier, "format": "csv"}
            )
            state = status.get("state")
            if state == "done" and status.get("url"):
                break
            if state not in ("generating", "none", "queued", "pending", None):
                raise LaunchGoodError(f"report generation failed: {str(status)[:200]}")
            if time.monotonic() > deadline:
                raise LaunchGoodError(f"report generation timed out for campaign {campaign_id}")
            sleep(poll_interval)
        r = self.transport("GET", status["url"])
        if r.status_code != 200:
            raise LaunchGoodError(f"report download failed ({r.status_code})")
        return r.text

    def _v3_rpc(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST to the legacy v3 API and unwrap {success, result}."""
        data = self._request("POST", f"{V3_BASE}/api/{path}", json_body=body)
        if not isinstance(data, dict) or not data.get("success"):
            msg = data.get("message") if isinstance(data, dict) else str(data)[:200]
            raise LaunchGoodError(f"v3 {path} failed: {msg}")
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    # --- v3 API (per-campaign donations) ------------------------------------

    def iter_campaign_donations(self, campaign_id: str | int) -> Iterator[dict[str, Any]]:
        """Yield individual donation records for one campaign.

        v3 ``POST /api/projectapi/donors {id, lastId}`` — pages of 20, cursored
        by the last record's ``ID``. Records have DonationID, Name, Location,
        Amount, CurrencyCode, Date, IsAnonymous (public visibility fields, so
        no email — that join happens in the sync via the donors endpoint).
        """
        last_id = 0
        prev_page: list[dict[str, Any]] | None = None
        while True:
            data = self._request(
                "POST",
                f"{V3_BASE}/api/projectapi/donors",
                json_body={"id": int(campaign_id), "lastId": last_id},
            )
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                raise LaunchGoodError(f"unexpected projectapi/donors response: {str(data)[:200]}")
            page = [d for d in (result.get("donors") or []) if isinstance(d, dict)]
            if not page:
                return
            if prev_page is not None and page == prev_page:
                log.warning("launchgood_campaign_pagination_stalled", campaign=campaign_id)
                return
            yield from page
            if len(page) < V3_PAGE_SIZE:
                return
            last = page[-1].get("ID")
            if not last:
                return
            prev_page = page
            last_id = last

    # --- open search (no auth needed, but harmless to send) -----------------

    def get_campaign_card(self, campaign_id: str | int) -> dict[str, Any]:
        """Campaign id → card with Name/Slug/etc. (unauthenticated endpoint)."""
        data = self._request("GET", f"{SEARCH_BASE}/cards/campaign/by-id/{campaign_id}")
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return results[0]
        return {}


def _account_id_from_session(session: dict[str, Any]) -> str | None:
    """Extract an org account id from a session payload.

    Looks in currentUserData.accounts (preferring corporate accounts), then
    falls back to any accountId/account_id key in the top two levels.
    """
    accounts = (session.get("currentUserData") or {}).get("accounts")
    if isinstance(accounts, list) and accounts:
        for a in accounts:
            if isinstance(a, dict) and a.get("type") == "corporate" and a.get("accountId"):
                return str(a["accountId"])
        first = accounts[0]
        if isinstance(first, dict) and first.get("accountId"):
            return str(first["accountId"])

    def dig(obj: Any, depth: int) -> str | None:
        if depth < 0 or not isinstance(obj, dict):
            return None
        for key in ("accountId", "account_id"):
            if obj.get(key):
                return str(obj[key])
        for v in obj.values():
            if isinstance(v, dict):
                found = dig(v, depth - 1)
                if found:
                    return found
        return None

    return dig(session, 2)


def _as_list(data: Any, *preferred_keys: str) -> list[dict[str, Any]]:
    """Normalize list-ish API responses: bare list or {key: [...]}."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in (*preferred_keys, "data", "items", "results", "donors", "campaigns"):
            v = data.get(key)
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
    return []


__all__ = [
    "API_BASE",
    "LaunchGoodClient",
    "LaunchGoodError",
    "TokenSet",
    "TransportResponse",
]
