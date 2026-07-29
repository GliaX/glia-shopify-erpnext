"""FrappeClient tests (no real HTTP — injectable transport)."""

from __future__ import annotations

import pytest

from glia_shopify_sync.frappe_client import FrappeClient, FrappeError, FrappeResponse


def _resp(status: int, data=None, text: str = "") -> FrappeResponse:
    import json

    body = data if data is not None else {}
    return FrappeResponse(status, body, text or (json.dumps(body) if body else ""))


def test_get_returns_data_envelope():
    calls = []

    def transport(method, url, *, headers, params, json, timeout):
        calls.append((method, url))
        return _resp(200, {"data": {"name": "C-0001", "full_name": "Anne"}})

    c = FrappeClient(
        base_url="https://frappe.example", api_key="k", api_secret="s", transport=transport
    )
    doc = c.get("CRM Contact", "C-0001")
    assert doc == {"name": "C-0001", "full_name": "Anne"}
    assert calls == [("GET", "https://frappe.example/api/resource/CRM%20Contact/C-0001")]


def test_basic_auth_header_present():
    def transport(method, url, *, headers, params, json, timeout):
        assert headers["Authorization"].startswith("Basic ")
        assert headers["Accept"] == "application/json"
        return _resp(200, {"data": {}})

    FrappeClient(
        base_url="https://x.example", api_key="key", api_secret="sec", transport=transport
    ).get("CRM Contact", "x")


def test_4xx_raises_immediately():
    attempts = {"n": 0}

    def transport(method, url, *, headers, params, json, timeout):
        attempts["n"] += 1
        return _resp(404, text="not found")

    c = FrappeClient(
        base_url="https://x.example",
        api_key="k",
        api_secret="s",
        transport=transport,
        max_attempts=3,
    )
    with pytest.raises(FrappeError) as ei:
        c.get("CRM Contact", "missing")
    assert "404" in str(ei.value)
    assert attempts["n"] == 1  # NOT retried


def test_5xx_is_retried_then_raises():
    attempts = {"n": 0}

    def transport(method, url, *, headers, params, json, timeout):
        attempts["n"] += 1
        return _resp(500, text="boom")

    c = FrappeClient(
        base_url="https://x.example",
        api_key="k",
        api_secret="s",
        transport=transport,
        max_attempts=3,
        initial_wait_seconds=0.001,
        max_wait_seconds=0.001,
    )
    with pytest.raises(FrappeError):
        c.get("CRM Contact", "x")
    assert attempts["n"] == 3  # retried up to max_attempts


def test_5xx_recovers_within_budget():
    seq = iter([_resp(503, text="down"), _resp(200, {"data": {"ok": True}})])

    def transport(method, url, *, headers, params, json, timeout):
        return next(seq)

    c = FrappeClient(
        base_url="https://x.example",
        api_key="k",
        api_secret="s",
        transport=transport,
        max_attempts=3,
        initial_wait_seconds=0.001,
        max_wait_seconds=0.001,
    )
    assert c.get("CRM Contact", "x") == {"ok": True}


def test_insert_posts_resource_with_doctype():
    seen = {}

    def transport(method, url, *, headers, params, json, timeout):
        seen["method"] = method
        seen["url"] = url
        seen["json"] = json
        return _resp(200, {"data": {"name": "C-0001", **json}})

    c = FrappeClient(base_url="https://x.example", api_key="k", api_secret="s", transport=transport)
    out = c.insert({"doctype": "CRM Contact", "full_name": "Anne"})
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/resource/CRM%20Contact")
    assert seen["json"]["full_name"] == "Anne"
    assert out["name"] == "C-0001"


def test_insert_requires_doctype():
    c = FrappeClient(
        base_url="https://x.example",
        api_key="k",
        api_secret="s",
        transport=lambda *a, **k: _resp(200, {"data": {}}),
    )
    with pytest.raises(FrappeError):
        c.insert({"full_name": "Anne"})


def test_find_returns_first_or_none():
    pages = {
        (): [{"name": "C-0001"}],
    }

    def transport(method, url, *, headers, params, json, timeout):
        # params is the request params dict; we capture filters via 'filters'
        return _resp(200, {"data": pages[()]})

    c = FrappeClient(base_url="https://x.example", api_key="k", api_secret="s", transport=transport)
    assert c.find("CRM Contact", [["email", "=", "a@b.com"]]) == {"name": "C-0001"}

    empty_transport = lambda *a, **k: _resp(200, {"data": []})  # noqa: E731
    c2 = FrappeClient(
        base_url="https://x.example", api_key="k", api_secret="s", transport=empty_transport
    )
    assert c2.find("CRM Contact", [["email", "=", "none@x.com"]]) is None


def test_get_list_single_fetch_passes_fields_and_filters():
    # Frappe's REST ignores limit_page_start, so get_list does one large fetch.
    seen = {}

    def transport(method, url, *, headers, params, json, timeout):
        seen["params"] = params
        return _resp(200, {"data": [{"name": "a"}, {"name": "b"}]})

    c = FrappeClient(
        base_url="https://x.example",
        api_key="k",
        api_secret="s",
        transport=transport,
    )
    rows = c.get_list("Donation", fields=["name", "amount"], filters=[["Donation", "x", "=", 1]])
    assert [r["name"] for r in rows] == ["a", "b"]
    # always starts at offset 0 (offset pagination isn't supported by the endpoint)
    assert seen["params"]["limit_page_start"] == 0
    assert "name" in seen["params"]["fields"]
