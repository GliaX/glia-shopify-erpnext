"""launchgood_sync tests: two-phase orchestration, contact matching, dedup."""

from __future__ import annotations

import json

from glia_shopify_sync.launchgood_sync import sync_launchgood

DONOR = {
    "donorName": "Amina Yusuf",
    "donorEmail": "amina@example.org",
    "donorCountry": "Canada",
    "totalContributions": "270.00",
    "totalDonations": 2,
    "lastDonatedAt": "2026-04-12T18:22:00.000Z",
}

DONATION_MATCHED = {
    "ID": 19436284,
    "Name": "Amina Yusuf",
    "Location": "Ottawa, Canada",
    "IsAnonymous": False,
    "Amount": "170.0000",
    "CurrencyCode": "CAD",
    "DonationID": 21415308,
    "Date": "2026-02-25T02:53:26+00:00",
}

DONATION_ANON = {
    "ID": 18561441,
    "Name": "An Anonymous kind soul",
    "Location": "",
    "IsAnonymous": True,
    "Amount": "50.00",
    "CurrencyCode": "USD",
    "DonationID": 20461645,
    "Date": "2026-02-20T00:00:00+00:00",
}

DONATION_UNKNOWN = {
    "ID": 1,
    "Name": "Mystery Person",
    "Location": "Lyon, France",
    "IsAnonymous": False,
    "Amount": "25.00",
    "CurrencyCode": "EUR",
    "DonationID": 999,
    "Date": "2026-03-01T00:00:00+00:00",
}


class FakeLaunchGood:
    def __init__(self, donors=(), donations=(), campaign_ids=("367369",), report_csv=None):
        self._donors = list(donors)
        self._donations = list(donations)
        self._campaign_ids = list(campaign_ids)
        self._report_csv = report_csv

    def iter_donors(self, **kwargs):
        yield from self._donors

    def get_campaign_ids(self):
        return list(self._campaign_ids)

    def get_donor_report_csv(self, campaign_id, **kwargs):
        if self._report_csv is None:
            from glia_shopify_sync.launchgood_client import LaunchGoodError

            raise LaunchGoodError("no report available")
        return self._report_csv

    def iter_campaign_donations(self, campaign_id):
        yield from self._donations

    def get_campaign_card(self, campaign_id):
        return {"Name": "Scrubs for PTs"}


def test_dry_run_counts_but_does_not_write(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    stats = sync_launchgood(lg, fake_frappe, dry_run=True)
    assert stats.contacts_created == 1
    assert stats.donations_created == 1
    assert fake_frappe.inserted == []


def test_sync_writes_contact_and_donation_linked_by_name(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.contacts_created == 1
    assert stats.donations_created == 1
    contact, donation = fake_frappe.inserted
    assert contact["doctype"] == "Contact"
    assert contact["shopify_customer_id"] == "launchgood:email:amina@example.org"
    assert donation["doctype"] == "Donation"
    assert donation["contact"] == contact["name"]
    assert donation["source"] == "LaunchGood"
    assert donation["shopify_order_id"] == "launchgood:21415308"
    assert donation["campaign"] == "Scrubs for PTs"


def test_rerun_is_idempotent(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    sync_launchgood(lg, fake_frappe, dry_run=False)
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.contacts_created == 0
    assert stats.contacts_reused == 1
    assert stats.donations_created == 0
    assert stats.donations_skipped == 1


def test_anonymous_donation_uses_shared_contact(fake_frappe):
    lg = FakeLaunchGood(
        donors=[], donations=[DONATION_ANON, DONATION_ANON | {"DonationID": 2, "ID": 2}]
    )
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.donations_created == 2
    contacts = [d for d in fake_frappe.inserted if d["doctype"] == "Contact"]
    assert len(contacts) == 1  # one shared anonymous contact
    assert contacts[0]["shopify_customer_id"] == "launchgood:anonymous"
    donations = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"]
    assert all(d["contact"] == contacts[0]["name"] for d in donations)


def test_unknown_name_gets_name_only_contact(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_UNKNOWN])
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    contacts = [d for d in fake_frappe.inserted if d["doctype"] == "Contact"]
    assert len(contacts) == 2  # donor + name-only
    name_only = [c for c in contacts if c.get("first_name") == "Mystery"][0]
    assert name_only["shopify_customer_id"] == "launchgood:name:mystery person|france"
    assert stats.donations_created == 1


def test_existing_email_contact_is_reused(fake_frappe):
    fake_frappe.existing["Contact"] = {"C-1": {"name": "C-1", "email_id": "amina@example.org"}}
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.contacts_created == 0
    assert stats.contacts_reused == 1
    donation = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"][0]
    assert donation["contact"] == "C-1"


def test_date_filters_apply_to_donations(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    stats = sync_launchgood(lg, fake_frappe, dry_run=True, date_from="2026-03-01")
    assert stats.donations_created == 0
    stats = sync_launchgood(lg, fake_frappe, dry_run=True, date_from="2026-02-01")
    assert stats.donations_created == 1


def test_dump_dir_saves_raw_records(fake_frappe, tmp_path):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED])
    dump = tmp_path / "dump"
    sync_launchgood(lg, fake_frappe, dry_run=True, dump_dir=dump)
    donors = (dump / "donors.jsonl").read_text().strip().split("\n")
    donations = (dump / "donations.jsonl").read_text().strip().split("\n")
    assert json.loads(donors[0])["donorEmail"] == "amina@example.org"
    assert json.loads(donations[0])["campaign_id"] == "367369"


def test_ambiguous_name_not_attributed(fake_frappe):
    donor2 = dict(DONOR, donorEmail="amina2@example.org")  # same display name
    lg = FakeLaunchGood(donors=[DONOR, donor2], donations=[DONATION_MATCHED])
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    contacts = [d for d in fake_frappe.inserted if d["doctype"] == "Contact"]
    assert len(contacts) == 3  # two donors + name-only fallback
    donation = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"][0]
    name_only = [c for c in contacts if c["shopify_customer_id"].startswith("launchgood:name:")][0]
    assert donation["contact"] == name_only["name"]
    assert stats.donations_created == 1


def test_donation_matches_preexisting_contact_by_name(fake_frappe):
    """A donation for a name that only exists as a Shopify-era contact links to it."""
    fake_frappe.existing["Contact"] = {
        "C-9": {"name": "C-9", "first_name": "Mystery", "last_name": "Person"}
    }
    lg = FakeLaunchGood(donors=[], donations=[DONATION_UNKNOWN])
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    donation = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"][0]
    assert donation["contact"] == "C-9"
    contacts = [d for d in fake_frappe.inserted if d["doctype"] == "Contact"]
    assert contacts == []  # no duplicate contact created
    assert stats.donations_created == 1


REPORT_CSV = """Donation ID,Donation Reference Code,First Name,Last Name,Display Name,Email,Anonymous?,Status,Marketing Consent,Donation Currency (DC),Donation Amount (DC),Project Currency (PC),Amount (May be approx.) (PC),Has Reward,Reward,Reward Type,Reward Qty,Giving Level Fund Code,Giving Level Campaign Code,Created Date,Created Time,Type,Offline Donation?,Number of Donors,Comments,Reward Notes,Reward Email,Tax Receipt Requested,Email Address,Billing Name,Billing Address,Billing Address 2,Billing City,Billing State,Billing Zip,Billing Country,Source,User Profile,Planned Payment Date
21415308,LG-DON-PZM1QQ,Nora,Ballantyne,"Chris and Nora",nora@example.org,Public Donor,Charged,no,CAD,170.0000,USD,119.0900,1,"Donate 2 Scrub Sets",Level,1,,,02/25/2026,02:53:26,Visa,online,1,,,,No,,,,,,,,,internal_discover,,
20460376,LG-DON-NVRMO4,Esther,Huang,"Anonymous kind soul",esther@example.org,Anonymous Donor,Charged,no,CAD,43.0000,USD,29.9200,1,"Donate a Scrub Top or Bottom",Level,1,,,12/19/2025,17:00:19,Visa,online,1,,,,No,,,"12 Main St",,Toronto,ON,M1M1M1,CA,,,
99999999,LG-DON-XXXXXX,Test,Refund,"Anonymous kind soul",refund@example.org,Anonymous Donor,Refunded,no,USD,10.0000,USD,10.0000,0,,,,,,01/01/2026,00:00:00,Visa,online,1,,,,No,,,,,,,,,,,
"""


def test_csv_report_path_links_donations_by_email(fake_frappe):
    lg = FakeLaunchGood(donors=[], report_csv=REPORT_CSV)
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.donations_created == 2  # refunded row skipped
    contacts = [d for d in fake_frappe.inserted if d["doctype"] == "Contact"]
    donations = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"]
    assert {c["shopify_customer_id"] for c in contacts} == {
        "launchgood:email:nora@example.org",
        "launchgood:email:esther@example.org",
    }
    nora = [d for d in donations if d["shopify_order_id"] == "launchgood:21415308"][0]
    assert nora["amount"] == 119.09  # project currency amount
    assert nora["currency"] == "USD"
    assert nora["amount_presentment"] == 170.0
    assert nora["currency_presentment"] == "CAD"
    assert nora["donation_date"] == "2026-02-25"
    assert nora["donor_email"] == "nora@example.org"
    esther_c = [c for c in contacts if "esther" in c["shopify_customer_id"]][0]
    esther_d = [d for d in donations if d["shopify_order_id"] == "launchgood:20460376"][0]
    assert esther_d["contact"] == esther_c["name"]  # real identity despite "anonymous"


def test_csv_report_rerun_idempotent(fake_frappe):
    lg = FakeLaunchGood(donors=[], report_csv=REPORT_CSV)
    sync_launchgood(lg, fake_frappe, dry_run=False)
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.donations_created == 0
    assert stats.donations_skipped == 2


def test_csv_report_reuses_existing_email_contact(fake_frappe):
    fake_frappe.existing["Contact"] = {
        "C-5": {"name": "C-5", "email_id": "nora@example.org", "first_name": "Nora"}
    }
    lg = FakeLaunchGood(donors=[], report_csv=REPORT_CSV)
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    nora_d = [
        d
        for d in fake_frappe.inserted
        if d.get("doctype") == "Donation" and d["shopify_order_id"] == "launchgood:21415308"
    ][0]
    assert nora_d["contact"] == "C-5"  # matched pre-existing contact, no duplicate
    nora_contacts = [
        c
        for c in fake_frappe.inserted
        if c.get("doctype") == "Contact" and "nora" in str(c.get("shopify_customer_id"))
    ]
    assert nora_contacts == []
    assert stats.donations_created == 2


def test_csv_report_date_filter(fake_frappe):
    lg = FakeLaunchGood(donors=[], report_csv=REPORT_CSV)
    stats = sync_launchgood(lg, fake_frappe, dry_run=True, date_from="2026-01-01")
    assert stats.donations_created == 1  # only the 2026-02-25 row


def test_feed_fallback_when_report_fails(fake_frappe):
    lg = FakeLaunchGood(donors=[DONOR], donations=[DONATION_MATCHED], report_csv=None)
    stats = sync_launchgood(lg, fake_frappe, dry_run=False)
    assert stats.donations_created == 1
    donation = [d for d in fake_frappe.inserted if d["doctype"] == "Donation"][0]
    assert donation["shopify_order_id"] == "launchgood:21415308"
