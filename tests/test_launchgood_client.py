"""LaunchGood client tests: token handling, pagination, v3 donation cursor.

No real HTTP — an injectable fake transport drives every call.
"""

from __future__ import annotations

import json

import pytest

from glia_shopify_sync.launchgood_client import (
    LaunchGoodClient,
    LaunchGoodError,
    TransportResponse,
)


class FakeTransport:
    """Scripted transport: maps (method, path-suffix) -> response."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], TransportResponse] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def add(self, method: str, suffix: str, status: int, body) -> None:
        self.routes[(method, suffix)] = TransportResponse(
            status_code=status,
            text=json.dumps(body) if not isinstance(body, str) else body,
        )

    def __call__(self, method, url, **kwargs) -> TransportResponse:
        from urllib.parse import urlparse

        path = urlparse(url).path
        self.calls.append((method, path, kwargs))
        for (m, suffix), resp in self.routes.items():
            if m == method and path.endswith(suffix):
                return resp
        return TransportResponse(status_code=404, text='{"message": "not found"}')


def _client(tmp_path, transport, account_id=None) -> LaunchGoodClient:
    token_file = tmp_path / "auth.json"
    token_file.write_text(json.dumps({"access_token": "at", "expires_at": 0}))
    return LaunchGoodClient(token_file, transport=transport, account_id=account_id)


def test_save_access_token_strips_quotes_and_persists(tmp_path):
    lg = LaunchGoodClient(tmp_path / "auth.json", transport=FakeTransport())
    lg.save_access_token('"tok123"')
    assert lg.authenticated
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["access_token"] == "tok123"
    with pytest.raises(LaunchGoodError, match="empty"):
        lg.save_access_token("  ")


def test_unauthenticated_raises(tmp_path):
    lg = LaunchGoodClient(tmp_path / "missing.json", transport=FakeTransport())
    with pytest.raises(LaunchGoodError, match="login"):
        lg.get_session()


def test_iter_donors_stops_when_api_ignores_skip(tmp_path):
    """A fake API that always returns a full page must not loop forever."""
    t = FakeTransport()
    t.add("GET", "/v4/accounts/7/donors", 200, {"donors": [{"donorName": "A"}, {"donorName": "B"}]})
    lg = _client(tmp_path, t, account_id=7)
    donors = list(lg.iter_donors(page_size=2))
    assert [d["donorName"] for d in donors] == ["A", "B"]
    assert t.calls[0][2]["params"]["take"] == 2
    assert t.calls[0][2]["params"]["skip"] == 0
    assert len(t.calls) == 2  # second (identical) page -> stop


def test_iter_donors_multiple_pages(tmp_path):
    pages = {0: [{"donorName": "A"}, {"donorName": "B"}], 2: [{"donorName": "C"}]}

    def transport(method, url, **kwargs):
        skip = (kwargs.get("params") or {}).get("skip", 0)
        return TransportResponse(status_code=200, text=json.dumps({"donors": pages.get(skip, [])}))

    lg = _client(tmp_path, transport, account_id=7)
    assert [d["donorName"] for d in lg.iter_donors(page_size=2)] == ["A", "B", "C"]


def test_resolve_account_id_prefers_corporate(tmp_path):
    t = FakeTransport()
    t.add(
        "GET",
        "/v4/auth/session",
        200,
        {
            "currentUserData": {
                "accounts": [
                    {"accountId": 53180, "type": "personal"},
                    {"accountId": 9657, "type": "corporate"},
                ]
            }
        },
    )
    lg = _client(tmp_path, t)
    assert lg.resolve_account_id() == "9657"


def test_get_campaign_ids(tmp_path):
    t = FakeTransport()
    t.add("GET", "/v4/accounts/7/campaigns", 200, [{"id": 1}, {"id": 2}, {"noid": True}])
    lg = _client(tmp_path, t, account_id=7)
    assert lg.get_campaign_ids() == ["1", "2"]


def test_iter_campaign_donations_lastid_cursor(tmp_path):
    pages = {0: [{"ID": 10, "DonationID": 100}] * 20, 10: [{"ID": 9, "DonationID": 99}]}
    calls = []

    def transport(method, url, **kwargs):
        body = kwargs.get("json") or {}
        calls.append(body.get("lastId"))
        donors = pages.get(body.get("lastId", 0), [])
        return TransportResponse(
            status_code=200, text=json.dumps({"result": {"count": 21, "donors": donors}})
        )

    lg = _client(tmp_path, transport)
    donations = list(lg.iter_campaign_donations(367369))
    assert len(donations) == 21
    assert calls == [0, 10]


def test_iter_campaign_donations_single_short_page(tmp_path):
    t = FakeTransport()
    t.add(
        "POST",
        "/api/projectapi/donors",
        200,
        {"result": {"count": 2, "donors": [{"ID": 5}, {"ID": 4}]}},
    )
    lg = _client(tmp_path, t)
    assert len(list(lg.iter_campaign_donations(1))) == 2
    assert len(t.calls) == 1


def test_401_raises_friendly_error(tmp_path):
    t = FakeTransport()
    t.add("GET", "/v4/auth/session", 401, {"message": "Unauthorized"})
    lg = _client(tmp_path, t)
    with pytest.raises(LaunchGoodError, match="401"):
        lg.get_session()


def test_campaign_card(tmp_path):
    t = FakeTransport()
    t.add(
        "GET",
        "/cards/campaign/by-id/367369",
        200,
        {"results": [{"Type": "campaign", "ID": 367369, "Name": "Scrubs"}]},
    )
    lg = _client(tmp_path, t)
    assert lg.get_campaign_card(367369)["Name"] == "Scrubs"


def test_get_donor_report_csv_flow(tmp_path):
    """downloadInfo -> generate -> status(done+url) -> S3 download."""
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/api/projectreports/downloadInfo"):
            return TransportResponse(
                200,
                json.dumps(
                    {
                        "success": True,
                        "result": {
                            "identifier": "donors|367369##x",
                            "type": "donors",
                            "format": "csv",
                        },
                    }
                ),
            )
        if url.endswith("/api/reports/action"):
            body = kwargs.get("json") or {}
            if body.get("verb") == "generate":
                return TransportResponse(
                    200, json.dumps({"success": True, "result": {"state": "generating"}})
                )
            return TransportResponse(
                200,
                json.dumps(
                    {
                        "success": True,
                        "result": {"state": "done", "url": "https://s3.example/report.csv"},
                    }
                ),
            )
        if url == "https://s3.example/report.csv":
            return TransportResponse(200, "Donation ID,Email\n1,a@b.co\n")
        return TransportResponse(404, "{}")

    lg = _client(tmp_path, transport)
    csv_text = lg.get_donor_report_csv(367369, poll_interval=0)
    assert csv_text.startswith("Donation ID")
    verbs = [c[2].get("verb") for c in calls if c[2] and "verb" in c[2]]
    assert verbs == ["generate", "status"]


def test_get_donor_report_csv_timeout(tmp_path):
    def transport(method, url, **kwargs):
        if url.endswith("/api/projectreports/downloadInfo"):
            return TransportResponse(
                200,
                json.dumps(
                    {
                        "success": True,
                        "result": {"identifier": "x"},
                    }
                ),
            )
        return TransportResponse(
            200,
            json.dumps(
                {
                    "success": True,
                    "result": {"state": "generating"},
                }
            ),
        )

    lg = _client(tmp_path, transport)
    with pytest.raises(LaunchGoodError, match="timed out"):
        lg.get_donor_report_csv(1, poll_interval=0, poll_timeout=0.01, sleep=lambda s: None)
