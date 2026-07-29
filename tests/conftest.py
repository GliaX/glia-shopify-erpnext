"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from glia_shopify_sync.frappe_client import FrappeError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFrappeClient:
    """In-memory stand-in for FrappeClient used by setup/doctor/send-test tests.

    `existing` maps doctype -> {name -> doc}. `get` raises FrappeError (404-like)
    for unknown names so idempotent `ensure_*` logic exercises both branches.
    """

    def __init__(self, existing: dict[str, dict[str, dict]] | None = None) -> None:
        self.existing: dict[str, dict[str, dict]] = existing or {}
        self.inserted: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.tagged: list[tuple[str, str, str]] = []
        self._n = 0

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        try:
            return self.existing[doctype][name]
        except KeyError:
            raise FrappeError(
                f"Frappe 404 on GET /api/resource/{doctype}/{name}: not found"
            ) from None

    def find(
        self, doctype: str, filters: list[Any], *, fields: list[str] | None = None
    ) -> dict[str, Any] | None:
        rows = list(self.existing.get(doctype, {}).values())
        return rows[0] if rows else None

    def get_list(
        self,
        doctype: str,
        *,
        fields: list[str] | None = None,
        filters: list[Any] | None = None,
        order_by: str | None = None,
        page_length: int = 500,
    ) -> list[dict[str, Any]]:
        rows = list(self.existing.get(doctype, {}).values())
        if fields:
            return [{k: r.get(k) for k in fields} for r in rows]
        return [{"name": r.get("name")} for r in rows]

    def insert(self, doc: dict[str, Any]) -> dict[str, Any]:
        name = self._derive_name(doc)
        saved = {"name": name, **doc}
        self.inserted.append(saved)
        self.existing.setdefault(doc.get("doctype", ""), {})[name] = saved
        return saved

    def delete(self, doctype: str, name: str, *, cancel_first: bool = False) -> None:
        self.deleted.append((doctype, name))
        self.existing.get(doctype, {}).pop(name, None)

    def add_tag(self, tag: str, doctype: str, name: str) -> str:
        self.tagged.append((tag, doctype, name))
        return tag

    # Frappe names docs by a doctype-specific field; mirror that so idempotency
    # (get-after-insert) behaves like the real system.
    _NAME_FIELDS = {
        "Module Def": "module_name",
        "Customer Group": "customer_group_name",
        "Customer": "customer_name",
        "DocType": "name",
    }

    def _derive_name(self, doc: dict[str, Any]) -> str:
        dt = doc.get("doctype", "")
        if doc.get("name"):
            return doc["name"]
        fld = self._NAME_FIELDS.get(dt)
        if fld and doc.get(fld):
            return doc[fld]
        self._n += 1
        return f"{dt}-{self._n}"


@pytest.fixture
def fake_frappe() -> FakeFrappeClient:
    return FakeFrappeClient()


@pytest.fixture
def donation_gids() -> set[str]:
    return {
        "gid://shopify/Product/7962927005795",  # One Time Donation
        "gid://shopify/Product/7962927038563",  # Monthly Donation
        "gid://shopify/Product/7967052890211",  # Glia4Gaza - One-Time Gift
        "gid://shopify/Product/7970874916963",  # Glia4Gaza - Monthly Gift
    }


@pytest.fixture
def recurring_gids() -> set[str]:
    return {
        "gid://shopify/Product/7962927038563",  # Monthly Donation
        "gid://shopify/Product/7970874916963",  # Glia4Gaza - Monthly Gift
    }


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def order_onetime_tip() -> dict:
    return load_fixture("order_onetime_tip.json")


@pytest.fixture
def order_recurring() -> dict:
    return load_fixture("order_recurring.json")


@pytest.fixture
def order_merch_only() -> dict:
    return load_fixture("order_merch_only.json")


@pytest.fixture
def token_response() -> dict:
    return load_fixture("token_response.json")
