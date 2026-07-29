"""doctor tests: read-only checks against an injected fake client."""

from __future__ import annotations

from glia_shopify_sync.config import AppConfig, Settings, YamlConfig
from glia_shopify_sync.doctor import run_doctor
from glia_shopify_sync.frappe_client import FrappeError
from tests.conftest import FakeFrappeClient


def _cfg() -> AppConfig:
    return AppConfig(settings=Settings(_env_file=None), yaml_cfg=YamlConfig(company="Glia"))


def _client_with(*present_doctypes: str) -> FakeFrappeClient:
    existing: dict = {
        "DocType": {dt: {} for dt in present_doctypes},
        "Company": {"Glia": {}},
        "Custom Field": {"Contact-shopify_customer_id": {"name": "Contact-shopify_customer_id"}},
    }
    return FakeFrappeClient(existing=existing)


def test_doctor_passes_when_crm_and_donation_present():
    client = _client_with("Contact", "CRM Organization", "Donation")
    results = run_doctor(_cfg(), client)  # shopify=None skips Shopify checks
    assert all(r.ok for r in results), [str(r) for r in results if not r.ok]


def test_doctor_flags_missing_donation_doctype():
    client = _client_with("Contact", "CRM Organization")  # Donation missing
    results = run_doctor(_cfg(), client)
    donation_check = next(r for r in results if r.name == "Doctype Donation (custom)")
    assert donation_check.ok is False
    assert "setup" in donation_check.detail


def test_doctor_reports_auth_failure_and_short_circuits():
    class NoAuth(FakeFrappeClient):
        def get(self, doctype, name):
            raise FrappeError("Frappe 401 on GET: unauthorized")

    results = run_doctor(_cfg(), NoAuth())
    assert results[0].ok is False
    assert len(results) == 1  # short-circuited after auth failure
