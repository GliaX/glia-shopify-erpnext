"""Generic Frappe REST client.

Authenticates with a Frappe API Key + Secret (HTTP Basic). Mirrors the proven
patterns from the sibling `erpnext-bank-integration` project: 5xx / connection /
timeout retried with exponential backoff; 4xx surfaced immediately with the
response body so real problems aren't masked.

The client is intentionally doctype-agnostic — it works for Frappe CRM's
`CRM Contact` / `CRM Organization`, core `Contact` / `Address`, our custom
`Donation` doctype, and `Custom Field` alike. Doctype-specific dedup and
mapping live in `crm_mapping.py` / the orchestrator.

A transport callable is injectable so the whole thing is unit-testable without
real HTTP (see `tests/test_frappe_client.py`).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import structlog
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger()


class FrappeError(RuntimeError):
    """Raised for unrecoverable Frappe REST failures."""


@dataclass
class FrappeResponse:
    """Minimal HTTP response shape returned by a transport."""

    status_code: int
    json_data: Any
    text: str


Transport = Callable[..., FrappeResponse]
"""(method, url, *, headers, params, json, timeout) -> FrappeResponse."""


def _default_transport(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: Any = None,
    json: Any = None,
    timeout: float = 30.0,
) -> FrappeResponse:
    import requests

    r = requests.request(method, url, headers=headers, params=params, json=json, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = None
    return FrappeResponse(r.status_code, body, r.text)


class FrappeClient:
    """Doctype-agnostic Frappe REST client with retry + 4xx fast-fail."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout: float = 30.0,
        max_attempts: int = 3,
        initial_wait_seconds: float = 2.0,
        max_wait_seconds: float = 60.0,
        backoff_multiplier: float = 2.0,
        transport: Transport | None = None,
        user_agent: str = "glia-shopify-sync/0.1",
    ) -> None:
        if not base_url:
            raise FrappeError("base_url is required")
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._retry = retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(
                multiplier=initial_wait_seconds,
                max=max_wait_seconds,
                exp_base=backoff_multiplier,
            ),
            retry=retry_if_exception_type(_TransientError),
            reraise=True,
            before_sleep=lambda rs: log.warning(
                "frappe_retry", attempt=rs.attempt_number, error=str(rs.outcome.exception())
            ),
        )

    # --- headers / url --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth_header,
            "User-Agent": "glia-shopify-sync/0.1",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # --- read -----------------------------------------------------------

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        """Fetch a single doc by name. Raises FrappeError if missing."""
        resp = self._request("GET", f"/api/resource/{quote(doctype)}/{quote(str(name), safe='')}")
        return _data(resp)

    def get_value(self, doctype: str, name: str, field: str) -> Any:
        resp = self._request(
            "POST",
            "/api/method/frappe.client.get_value",
            json={"doctype": doctype, "name": name, "fieldname": field},
        )
        return _data(resp)

    def get_list(
        self,
        doctype: str,
        *,
        fields: list[str] | None = None,
        filters: list[Any] | None = None,
        order_by: str | None = None,
        page_length: int = 500,
    ) -> list[dict[str, Any]]:
        """Fetch ALL matching rows, auto-paginating. Use sparingly on big tables."""
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self._get_list_page(
                doctype,
                fields=fields,
                filters=filters,
                order_by=order_by,
                page_length=page_length,
                page_start=start,
            )
            if not page:
                break
            rows.extend(page)
            if len(page) < page_length:
                break
            start += page_length
        return rows

    def find(
        self,
        doctype: str,
        filters: list[Any],
        *,
        fields: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Return the first matching doc (or None). Best for dedup lookups."""
        page = self._get_list_page(
            doctype, fields=fields, filters=filters, page_length=1, page_start=0
        )
        return page[0] if page else None

    def _get_list_page(
        self,
        doctype: str,
        *,
        fields: list[str] | None,
        filters: list[Any] | None,
        order_by: str | None = None,
        page_length: int,
        page_start: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "fields": json.dumps(fields) if fields else '["name"]',
            "filters": json.dumps(filters) if filters else "[]",
            "limit_page_length": page_length,
            "limit_page_start": page_start,
        }
        if order_by:
            params["order_by"] = order_by
        resp = self._request("GET", f"/api/resource/{quote(doctype)}", params=params)
        out = _data(resp)
        return out if isinstance(out, list) else []

    # --- write ----------------------------------------------------------

    def insert(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Insert a single doc (must include its `doctype`). Returns the saved doc."""
        if "doctype" not in doc:
            raise FrappeError("insert() requires a 'doctype' key in the doc")
        resp = self._request("POST", f"/api/resource/{quote(doc['doctype'])}", json=doc)
        return _data(resp)

    def insert_many(self, docs: list[dict[str, Any]]) -> list[str]:
        """Insert a batch via frappe.client.insert_many. Returns created names."""
        if not docs:
            return []
        resp = self._request("POST", "/api/method/frappe.client.insert_many", json={"docs": docs})
        out = _data(resp)
        return out if isinstance(out, list) else []

    def update(self, doctype: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """Partial update of an existing doc (PUT on the resource)."""
        resp = self._request(
            "PUT",
            f"/api/resource/{quote(doctype)}/{quote(str(name), safe='')}",
            json=values,
        )
        return _data(resp)

    def delete(self, doctype: str, name: str, *, cancel_first: bool = False) -> None:
        """Delete a doc. If it might be submitted (docstatus=1), cancel_first."""
        if cancel_first:
            self._best_effort_cancel(doctype, name)
        self._request(
            "DELETE",
            f"/api/resource/{quote(doctype)}/{quote(str(name), safe='')}",
        )

    def _best_effort_cancel(self, doctype: str, name: str) -> None:
        try:
            self._request(
                "POST",
                "/api/method/frappe.client.set_value",
                json={"doctype": doctype, "name": name, "fieldname": "docstatus", "value": 2},
            )
        except FrappeError as e:
            log.debug("best_effort_cancel_failed", name=name, error=str(e))

    # --- http plumbing --------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> FrappeResponse:
        @self._retry
        def _call() -> FrappeResponse:
            resp = self._transport(
                method,
                self._url(path),
                headers=self._headers(),
                params=params,
                json=json,
                timeout=self.timeout,
            )
            if 400 <= resp.status_code < 500:
                body = (resp.text or "")[:500]
                raise FrappeError(f"Frappe {resp.status_code} on {method} {path}: {body}")
            if resp.status_code >= 500:
                raise _TransientError(
                    f"Frappe {resp.status_code} on {method} {path}: {(resp.text or '')[:200]}"
                )
            return resp

        try:
            return _call()
        except FrappeError:
            raise
        except RetryError as e:  # pragma: no cover - tenacity reraises with reraise=True
            raise FrappeError(f"Frappe retries exhausted for {method} {path}") from e
        except _TransientError as e:
            raise FrappeError(str(e)) from e


class _TransientError(Exception):
    """Internal: retried by tenacity (5xx / network)."""


def _data(resp: FrappeResponse) -> Any:
    """Pull Frappe's `{"data": ...}` envelope (or fall back to the raw body)."""
    body = resp.json_data
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


__all__ = ["FrappeClient", "FrappeError", "FrappeResponse"]
