"""`glia-sync-patreon` — sync Patreon members into ERPNext (Contacts + Donations).

Backfills a lifetime donation per member + the current monthly charge for active
patrons. Idempotent (dedup by patreon:{member_id}:{period} composite key).

For ongoing monthly charges, re-run periodically (CronJob): new charge dates
produce new monthly Donation records automatically.
"""

from __future__ import annotations

import argparse

import structlog

from .config import AppConfig, load_config, setup_logging
from .crm_mapping import donation_to_doc, donor_to_contact
from .frappe_client import FrappeClient, FrappeError
from .patreon_client import PatreonClient
from .patreon_transform import (
    member_to_donor,
    member_to_lifetime_donation,
    member_to_monthly_donation,
)
from .sync import SyncStats, load_contact_map, load_donation_keys, report_stats

log = structlog.get_logger()


def sync_patreon(
    patreon: PatreonClient,
    frappe: FrappeClient,
    campaign_id: str,
    *,
    dry_run: bool = False,
) -> SyncStats:
    stats = SyncStats()
    contact_map = {} if dry_run else load_contact_map(frappe)
    donation_keys = set() if dry_run else load_donation_keys(frappe)
    # v2 members don't include emails; fetch them from the v1 pledges endpoint.
    user_emails = {} if dry_run else patreon.fetch_user_emails(campaign_id)

    for member in patreon.iter_members(campaign_id):
        stats.orders_seen += 1
        a = member.get("attributes", {})
        if a.get("patron_status") in (None, ""):
            stats.orders_skipped_not_donation += 1
            continue

        # enrich: merge v1 email into _user (v2 doesn't return member emails)
        uid = (member.get("relationships", {}).get("user", {}).get("data") or {}).get("id", "")
        if uid and uid in user_emails:
            member["_user"]["email"] = user_emails[uid]

        # --- ensure Contact ---
        donor = member_to_donor(member)
        cid = donor.shopify_customer_id
        if cid and cid in contact_map:
            stats.contacts_reused += 1
            contact_name = contact_map[cid]
        else:
            try:
                if dry_run:
                    stats.contacts_created += 1
                    contact_name = f"<dry:{donor.donor_name}>"
                else:
                    saved = frappe.insert(donor_to_contact(donor))
                    contact_name = saved["name"]
                    if cid:
                        contact_map[cid] = contact_name
                    stats.contacts_created += 1
            except FrappeError as e:
                stats.errors.append(f"contact {donor.donor_name}: {e}")
                continue

        # --- ensure donations (lifetime backfill + current monthly) ---
        for make in (member_to_lifetime_donation, member_to_monthly_donation):
            donation = make(member)
            if donation is None:
                continue
            key = f"{donation.shopify_order_id}|{donation.shopify_line_item_id}"
            if key in donation_keys:
                stats.donations_skipped += 1
                continue
            try:
                if dry_run:
                    stats.donations_created += 1
                    donation_keys.add(key)
                    continue
                frappe.insert(
                    donation_to_doc(
                        donation, contact_name=contact_name, donor_email=donor.email or ""
                    )
                )
                donation_keys.add(key)
                stats.donations_created += 1
            except FrappeError as e:
                stats.errors.append(f"donation {donation.shopify_order_name}: {e}")

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glia-sync-patreon")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg: AppConfig = load_config()
    setup_logging(cfg)
    s = cfg.settings

    patreon = PatreonClient(
        access_token=s.patreon_creator_access_token,
        refresh_token=s.patreon_creator_refresh_token,
        client_id=s.patreon_client_id,
        client_secret=s.patreon_client_secret,
    )
    frappe = FrappeClient(
        base_url=s.erpnext_base_url,
        api_key=s.erpnext_api_key,
        api_secret=s.erpnext_api_secret.get_secret_value(),
    )

    campaign_id = s.patreon_campaign_id or patreon.get_campaign_id()
    log.info("patreon_sync_start", campaign=campaign_id, dry_run=args.dry_run)

    stats = sync_patreon(patreon, frappe, campaign_id, dry_run=args.dry_run)
    return report_stats(stats)


if __name__ == "__main__":
    raise SystemExit(main())
