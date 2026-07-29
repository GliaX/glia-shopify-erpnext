"""send_test tests: push_test_record creates Contact + Donation then cleans up."""

from __future__ import annotations

from glia_shopify_sync.send_test import push_test_record
from tests.conftest import FakeFrappeClient


def test_push_test_record_creates_two_docs_then_deletes():
    client = FakeFrappeClient()
    result = push_test_record(client, keep=False)
    assert result.ok, result.detail

    inserted_doctypes = [d["doctype"] for d in client.inserted]
    assert inserted_doctypes == ["Contact", "Donation"]
    # Cleanup deleted everything, newest-first.
    deleted_doctypes = [dt for dt, _ in client.deleted]
    assert deleted_doctypes == ["Donation", "Contact"]


def test_push_test_record_keep_retains_records():
    client = FakeFrappeClient()
    result = push_test_record(client, keep=True)
    assert result.ok
    assert client.deleted == []
    assert len(client.inserted) == 2


def test_push_test_record_records_have_expected_link():
    client = FakeFrappeClient()
    push_test_record(client, keep=True)
    by_dt = {d["doctype"]: d for d in client.inserted}
    contact_name = by_dt["Contact"]["name"]
    assert by_dt["Donation"]["contact"] == contact_name
    assert by_dt["Donation"]["shopify_order_id"] == "gid://shopify/Order/TEST_SYNC_ORDER"
    assert by_dt["Contact"]["shopify_customer_id"] == "gid://shopify/Customer/TEST_SYNC"
