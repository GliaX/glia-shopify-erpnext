"""`glia-sync-launchgood` — sync LaunchGood donors/donations into ERPNext.

LaunchGood has no public API, so this scrapes their internal APIs — see
`launchgood_client.py` for the gory details (Cloudflare bypass + browser-token
auth). The data comes in two complementary shapes, synced in two phases:

  1. **Donors** (v4 ``/accounts/{id}/donors``) — name/email/country/aggregates
     → ERPNext Contacts (dedup: existing contact by email, else
     ``launchgood:email:{email}`` external id).
  2. **Donations** (v3 ``/api/projectapi/donors`` per campaign) — individual
     per-donation records (amount/date/currency, no email) → ERPNext Donations
     (dedup: ``launchgood:{DonationID}``), linked to Contacts by name (+country)
     matching against the phase-1 donors; anonymous/ambiguous donations go to a
     shared "LaunchGood Anonymous" / name-only contact.

Use ``sync --dump-dir DIR`` on the first real run to save the raw API pages —
these APIs are undocumented, so this keeps us honest about response shapes.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
from pathlib import Path
from typing import Any

import structlog

from .config import AppConfig, load_config, setup_logging
from .crm_mapping import donation_to_doc, donor_to_contact
from .frappe_client import FrappeClient, FrappeError
from .launchgood_client import LaunchGoodClient, LaunchGoodError
from .launchgood_transform import (
    ANONYMOUS_CONTACT_KEY,
    anonymous_donor,
    csv_row_to_donation,
    csv_row_to_donor,
    donation_record_to_donation,
    donor_identity_key,
    donor_record_to_donor,
    normalize_name,
)
from .models import Donation, Donor
from .sync import SyncStats, load_contact_map, load_donation_keys, report_stats

log = structlog.get_logger()

DEFAULT_TOKEN_FILE = Path("launchgood_auth.json")


class _ContactResolver:
    """Matches campaign-donation display names to known donor contacts.

    Built from the phase-1 donor aggregates (which have emails) plus whatever
    already exists in ERPNext. Matching is deliberately conservative: a
    donation is linked to a donor only when the normalized name matches
    exactly one known donor (country disambiguates when the Location string
    provides one). Ambiguous/unknown names get a name-only contact so amounts
    are never attributed to the wrong person.
    """

    def __init__(self) -> None:
        self.by_name: dict[
            str, list[tuple[str, str, str]]
        ] = {}  # name -> [(key, contact, country)]

    def add(self, name: str, country: str, ext_key: str, contact_name: str) -> None:
        norm = normalize_name(name)
        if not norm:
            return
        self.by_name.setdefault(norm, []).append((ext_key, contact_name, country.lower()))

    def resolve(self, display_name: str, location: str) -> tuple[str, str] | None:
        """Return (ext_key, contact_name) for a donation's display name, or None."""
        norm = normalize_name(display_name)
        candidates = self.by_name.get(norm, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1]
        country = _location_country(location)
        if country:
            matched = [c for c in candidates if c[2] == country]
            if len(matched) == 1:
                return matched[0][0], matched[0][1]
        return None


def _location_country(location: str) -> str:
    """Last comma-segment of "City, ST, Country" — lowercase, or ""."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    return parts[-1].lower() if parts else ""


def sync_launchgood(
    lg: LaunchGoodClient,
    frappe: FrappeClient,
    *,
    campaign_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = False,
    dump_dir: Path | None = None,
) -> SyncStats:
    stats = SyncStats()
    contact_map = {} if dry_run else load_contact_map(frappe)
    email_map = {} if dry_run else _load_email_map(frappe)
    donation_keys = set() if dry_run else load_donation_keys(frappe)
    resolver = _ContactResolver()
    created_this_run: dict[str, str] = {}  # ext_key -> contact_name
    # Seed the name resolver with ALL pre-existing ERPNext contacts (Shopify /
    # Patreon donors), so LaunchGood donations attributed to the same display
    # name land on the same person instead of a new name-only contact.
    if not dry_run:
        for full_name, contact_name in _load_contact_names(frappe):
            resolver.add(full_name, "", f"existing:{contact_name}", contact_name)

    def ensure_contact(ext_key: str, make_doc, donor_name: str) -> str | None:
        if ext_key in contact_map:
            stats.contacts_reused += 1
            return contact_map[ext_key]
        if ext_key in created_this_run:
            stats.contacts_reused += 1
            return created_this_run[ext_key]
        try:
            if dry_run:
                stats.contacts_created += 1
                created_this_run[ext_key] = f"<dry:{donor_name}>"
                return created_this_run[ext_key]
            saved = frappe.insert(make_doc())
            contact_map[ext_key] = saved["name"]
            created_this_run[ext_key] = saved["name"]
            stats.contacts_created += 1
            return saved["name"]
        except FrappeError as e:
            stats.errors.append(f"contact {donor_name}: {e}")
            return None

    # --- Phase 1: donors -> Contacts ---
    dump_donors = _dump_fp(dump_dir, "donors.jsonl")
    try:
        for record in lg.iter_donors(
            campaign_ids=campaign_ids, date_from=date_from, date_to=date_to
        ):
            stats.orders_seen += 1
            if dump_donors:
                dump_donors.write(json.dumps(record) + "\n")
            donor = donor_record_to_donor(record)
            if donor is None:
                stats.orders_skipped_not_donation += 1
                continue
            # Reuse an existing ERPNext contact with the same email (e.g. a
            # Shopify donor) instead of creating a duplicate person.
            if donor.email and donor.email in email_map:
                contact_name = email_map[donor.email]
                contact_map[donor.shopify_customer_id] = contact_name
                created_this_run[donor.shopify_customer_id] = contact_name
                stats.contacts_reused += 1
            else:
                contact_name = ensure_contact(
                    donor.shopify_customer_id,
                    lambda d=donor: donor_to_contact(d),
                    donor.donor_name,
                )
                if contact_name and donor.email and not dry_run:
                    email_map.setdefault(donor.email, contact_name)
            if contact_name:
                resolver.add(
                    donor.donor_name,
                    record.get("donorCountry") or "",
                    donor.shopify_customer_id,
                    contact_name,
                )
    finally:
        if dump_donors:
            dump_donors.close()

    # --- Phase 2: campaign donations -> Donations ---
    ids = campaign_ids or lg.get_campaign_ids()
    log.info("launchgood_donations_phase", campaigns=len(ids))
    dump_donations = _dump_fp(dump_dir, "donations.jsonl")
    try:
        for cid in ids:
            campaign_name = _campaign_name(lg, cid)
            try:
                csv_text = lg.get_donor_report_csv(cid)
            except LaunchGoodError as e:
                log.warning(
                    "launchgood_report_failed_falling_back_to_feed", campaign=cid, error=str(e)
                )
                csv_text = None
            if csv_text is not None:
                _sync_campaign_csv(
                    cid,
                    csv_text,
                    campaign_name,
                    lg,
                    frappe,
                    contact_map=contact_map,
                    email_map=email_map,
                    donation_keys=donation_keys,
                    ensure_contact=ensure_contact,
                    stats=stats,
                    dry_run=dry_run,
                    date_from=date_from,
                    date_to=date_to,
                    dump_dir=dump_dir,
                )
                continue
            for record in lg.iter_campaign_donations(cid):
                if dump_donations:
                    dump_donations.write(json.dumps({"campaign_id": cid, **record}) + "\n")
                donation = donation_record_to_donation(record, campaign_name=campaign_name)
                if donation is None:
                    stats.orders_skipped_not_donation += 1
                    continue
                if date_from and donation.date < date_from:
                    continue
                if date_to and donation.date > date_to:
                    continue
                key = f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"
                if key in donation_keys:
                    stats.donations_skipped += 1
                    continue

                contact_name, donor_email = _resolve_donation_contact(
                    record, resolver, ensure_contact
                )
                if not contact_name:
                    continue
                _insert_donation(
                    frappe, donation, contact_name, donor_email, donation_keys, stats, dry_run
                )
    finally:
        if dump_donations:
            dump_donations.close()
    return stats


def _sync_campaign_csv(
    campaign_id: str,
    csv_text: str,
    campaign_name: str,
    lg: LaunchGoodClient,
    frappe: FrappeClient,
    *,
    contact_map: dict[str, str],
    email_map: dict[str, str],
    donation_keys: set[str],
    ensure_contact,
    stats: SyncStats,
    dry_run: bool,
    date_from: str | None,
    date_to: str | None,
    dump_dir: Path | None,
) -> None:
    """Import one campaign's Donor Report CSV (per-donation rows with PII)."""
    if dump_dir:
        (dump_dir / f"report_{campaign_id}.csv").write_text(csv_text, encoding="utf-8")
    for row in csv.DictReader(io.StringIO(csv_text)):
        donation = csv_row_to_donation(row, campaign_name=campaign_name)
        if donation is None:
            continue
        if date_from and donation.date and donation.date < date_from:
            continue
        if date_to and donation.date and donation.date > date_to:
            continue
        key = f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"
        if key in donation_keys:
            stats.donations_skipped += 1
            continue
        donor = csv_row_to_donor(row)
        if donor is None:
            donor = anonymous_donor()
        ext_key = donor.shopify_customer_id
        # Cross-source dedup: reuse any existing ERPNext contact with this
        # email (Shopify/Patreon donor), else the LaunchGood-keyed one.
        if donor.email and donor.email in email_map:
            contact_name = email_map[donor.email]
            contact_map[ext_key] = contact_name
            stats.contacts_reused += 1
        else:
            contact_name = ensure_contact(
                ext_key, lambda d=donor: donor_to_contact(d), donor.donor_name
            )
            if contact_name and donor.email:
                email_map.setdefault(donor.email, contact_name)
        if not contact_name:
            continue
        _insert_donation(
            frappe, donation, contact_name, donor.email or "", donation_keys, stats, dry_run
        )


def _resolve_donation_contact(
    record: dict[str, Any],
    resolver: _ContactResolver,
    ensure_contact,
) -> tuple[str | None, str]:
    """Pick (or create) the Contact for a campaign donation record."""
    name = (record.get("Name") or "").strip()
    if record.get("IsAnonymous") or not name:
        donor = anonymous_donor()
        contact = ensure_contact(
            ANONYMOUS_CONTACT_KEY, lambda: donor_to_contact(donor), donor.donor_name
        )
        return contact, ""
    found = resolver.resolve(name, record.get("Location") or "")
    if found:
        return found[1], ""
    # Unknown/ambiguous name: name-only contact keyed by name+location.
    ext_key = donor_identity_key(name, None, _location_country(record.get("Location") or ""))
    first, _, last = name.partition(" ")
    donor = Donor(
        shopify_customer_id=ext_key,
        donor_name=name,
        first_name=first or None,
        last_name=last or None,
    )
    contact = ensure_contact(ext_key, lambda: donor_to_contact(donor), name)
    return contact, ""


def _insert_donation(
    frappe: FrappeClient,
    donation: Donation,
    contact_name: str,
    donor_email: str,
    donation_keys: set[str],
    stats: SyncStats,
    dry_run: bool,
) -> None:
    key = f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"
    try:
        if dry_run:
            stats.donations_created += 1
            donation_keys.add(key)
            return
        frappe.insert(donation_to_doc(donation, contact_name=contact_name, donor_email=donor_email))
        donation_keys.add(key)
        stats.donations_created += 1
    except FrappeError as e:
        stats.errors.append(f"donation {donation.shopify_order_name}: {e}")


def _campaign_name(lg: LaunchGoodClient, campaign_id: str) -> str:
    try:
        card = lg.get_campaign_card(campaign_id)
    except LaunchGoodError as e:
        log.warning("launchgood_campaign_card_failed", campaign=campaign_id, error=str(e))
        card = {}
    return card.get("Name") or f"LaunchGood campaign {campaign_id}"


def _load_email_map(frappe: FrappeClient) -> dict[str, str]:
    """Existing {lowercase email: contact_name} for person-level dedup."""
    rows = frappe.get_list(
        "Contact",
        fields=["name", "email_id"],
        filters=[["Contact", "email_id", "is", "set"]],
    )
    return {r["email_id"].lower(): r["name"] for r in rows if r.get("email_id") and r.get("name")}


def _load_contact_names(frappe: FrappeClient) -> list[tuple[str, str]]:
    """Existing contacts as (full_name, contact_name) pairs for name matching."""
    rows = frappe.get_list("Contact", fields=["name", "first_name", "last_name"])
    out = []
    for r in rows:
        full = " ".join(p for p in (r.get("first_name"), r.get("last_name")) if p).strip()
        if full and r.get("name"):
            out.append((full, r["name"]))
    return out


def _dump_fp(dump_dir: Path | None, name: str):
    if not dump_dir:
        return None
    dump_dir.mkdir(parents=True, exist_ok=True)
    return (dump_dir / name).open("a", encoding="utf-8")


# --- CLI ---------------------------------------------------------------


def _build_client(cfg: AppConfig, args: argparse.Namespace) -> LaunchGoodClient:
    lg_cfg = cfg.yaml.launchgood
    token_file = Path(args.token_file or lg_cfg.token_file or DEFAULT_TOKEN_FILE)
    account_id = args.account_id or lg_cfg.account_id or None
    return LaunchGoodClient(token_file, account_id=account_id)


def _cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    setup_logging(cfg)
    lg = _build_client(cfg, args)

    token = args.access_token
    if not token:
        print("\nLog in to https://www.launchgood.com in your browser, then in the")
        print("devtools console (F12) run:\n")
        print("    copy(localStorage.getItem('token'))\n")
        print("and paste the value here (or pass --access-token).\n")
        try:
            token = getpass.getpass("token: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
    if not token:
        return 0
    lg.save_access_token(token, expires_at=args.token_expiry or 0.0)
    # Smoke-test the token against the session endpoint.
    session = lg.get_session()
    user = (session.get("currentUserData") or {}).get("email") or "?"
    accounts = (session.get("currentUserData") or {}).get("accounts") or []
    print(f"Token OK — logged in as {user}, {len(accounts)} account(s):")
    for a in accounts:
        print(f"  {a.get('accountId')}\t{a.get('type')}\t{a.get('name')}")
    print(f"(saved to {lg.token_file})")
    return 0


def _cmd_campaigns(args: argparse.Namespace) -> int:
    cfg = load_config()
    setup_logging(cfg)
    lg = _build_client(cfg, args)
    account_id = lg.resolve_account_id()
    print(f"account: {account_id}\n")
    for cid in lg.get_campaign_ids(account_id):
        print(f"  {cid}\t{_campaign_name(lg, cid)}")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    setup_logging(cfg)
    lg = _build_client(cfg, args)
    frappe = FrappeClient(
        base_url=cfg.settings.erpnext_base_url,
        api_key=cfg.settings.erpnext_api_key,
        api_secret=cfg.settings.erpnext_api_secret.get_secret_value(),
    )
    log.info(
        "launchgood_sync_start",
        campaigns=args.campaign_id,
        since=args.since,
        until=args.until,
        dry_run=args.dry_run,
        dump_dir=args.dump_dir,
    )
    stats = sync_launchgood(
        lg,
        frappe,
        campaign_ids=args.campaign_id,
        date_from=args.since,
        date_to=args.until,
        dry_run=args.dry_run,
        dump_dir=Path(args.dump_dir) if args.dump_dir else None,
    )
    return report_stats(stats)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-launchgood")
    parser.add_argument("--token-file", default=None, help="override config.yaml value")
    parser.add_argument("--account-id", default=None, help="override config.yaml value")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="save the browser session token")
    p_login.add_argument(
        "--access-token",
        default=None,
        help="value of localStorage 'token' from a logged-in launchgood.com session",
    )
    p_login.add_argument("--token-expiry", type=float, default=None)
    p_login.set_defaults(func=_cmd_login)

    p_camp = sub.add_parser("campaigns", help="list account campaigns")
    p_camp.set_defaults(func=_cmd_campaigns)

    p_sync = sub.add_parser("sync", help="sync donors/donations into ERPNext")
    p_sync.add_argument("--campaign-id", action="append", default=None)
    p_sync.add_argument("--since", default=None, help="donations on/after YYYY-MM-DD")
    p_sync.add_argument("--until", default=None, help="donations on/before YYYY-MM-DD")
    p_sync.add_argument("--dump-dir", default=None, help="save raw API pages here")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=_cmd_sync)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LaunchGoodError as e:
        print(f"launchgood error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
