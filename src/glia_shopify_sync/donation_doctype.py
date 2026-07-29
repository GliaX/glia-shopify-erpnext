"""Definitions for the custom `Donation` doctype and the CRM Contacts custom field.

POSTed to Frappe by the setup CLI (`setup_erpnext.py`):

  * A custom `Donation` DocType (module: Glia) linked to `CRM Contacts`.
  * A custom field `shopify_customer_id` on `CRM Contacts` for donor dedup.
  * The `Glia` Module Def (namespace for the Donation doctype).

Creating a custom DocType via the REST API is a standard, upgrade-safe Frappe
pattern (`custom: 1`). The DocType's `on_update` hook creates the DB table.
"""

from __future__ import annotations

from typing import Any

from .crm_mapping import DONATION_MODULE


def donation_doctype_def() -> dict[str, Any]:
    """Full DocType document for the custom `Donation` doctype.

    POST to /api/resource/DocType. Idempotent at the setup layer (check first).
    """
    return {
        "doctype": "DocType",
        "name": "Donation",
        "module": DONATION_MODULE,
        "custom": 1,
        "autoname": "hash",
        "naming_rule": "Expression",
        "istable": 0,
        "is_submittable": 0,
        "track_changes": 1,
        "quick_entry": 0,
        "label": "Donation",
        "fields": _donation_fields(),
        "permissions": _permissions(),
    }


def _permissions() -> list[dict[str, Any]]:
    """Role permissions for the custom Donation doctype (custom doctypes created
    via REST get none by default). System Manager + Sales Manager = full access.

    Only basic perms are set to avoid doctype-flag dependencies (e.g. `import`
    requires the doctype to be importable).
    """
    base = {
        "read": 1,
        "write": 1,
        "create": 1,
        "delete": 1,
        "print": 1,
        "email": 1,
        "report": 1,
        "share": 1,
    }
    return [
        {"role": "System Manager", **base, "set_user_permissions": 1},
        {"role": "Sales Manager", **base},
    ]


def _donation_fields() -> list[dict[str, Any]]:
    """Field definitions matching `crm_mapping.donation_to_doc`."""
    return [
        {
            "fieldname": "contact",
            "label": "Contact",
            "fieldtype": "Link",
            "options": "Contact",
            "reqd": 1,
            "in_list_view": 1,
        },
        {"fieldname": "donor_name", "label": "Donor Label", "fieldtype": "Data", "in_list_view": 1},
        {
            "fieldname": "donor_email",
            "label": "Donor Email",
            "fieldtype": "Data",
            "options": "email",  # Frappe email validation + shows in Notification recipient dropdown
            "in_list_view": 1,
            "columns": 2,
            # denormalized from the linked Contact so the Notification engine can
            # address thank-you emails directly via "recipient by document field".
        },
        {
            "fieldname": "donation_date",
            "label": "Donation Date",
            "fieldtype": "Date",
            "reqd": 1,
            "in_list_view": 1,
        },
        {
            "fieldname": "amount",
            "label": "Amount",
            "fieldtype": "Currency",
            "options": "currency",
            "reqd": 1,
            "in_list_view": 1,
        },
        {"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency"},
        {
            "fieldname": "amount_presentment",
            "label": "Amount (Presentment)",
            "fieldtype": "Currency",
            "options": "currency_presentment",
        },
        {
            "fieldname": "currency_presentment",
            "label": "Currency (Presentment)",
            "fieldtype": "Link",
            "options": "Currency",
        },
        {
            "fieldname": "donation_type",
            "label": "Donation Type",
            "fieldtype": "Select",
            "options": "\nOne-time\nRecurring",
            "default": "One-time",
            "in_list_view": 1,
        },
        {"fieldname": "campaign", "label": "Campaign", "fieldtype": "Data"},
        {"fieldname": "tier", "label": "Tier", "fieldtype": "Data"},
        {
            "fieldname": "includes_tip",
            "label": "Includes Tip",
            "fieldtype": "Check",
            "default": "0",
        },
        {"fieldname": "financial_status", "label": "Financial Status", "fieldtype": "Data"},
        {
            "fieldname": "shopify_order_id",
            "label": "Shopify Order ID",
            "fieldtype": "Data",
            "reqd": 1,
            # NOT unique: an order can contain several donation line items, so
            # multiple Donation rows legitimately share an order ID. Dedup is on
            # the composite order|line_item (handled by the sync's dedup map).
            "set_only_once": 1,
        },
        {"fieldname": "shopify_order_name", "label": "Shopify Order #", "fieldtype": "Data"},
        {
            "fieldname": "shopify_line_item_id",
            "label": "Shopify Line Item ID",
            "fieldtype": "Data",
            "reqd": 1,
            "set_only_once": 1,
        },
    ]


def contact_custom_fields() -> list[dict[str, Any]]:
    """Custom Field docs to add to core `Contact` for donor dedup."""
    return [
        {
            "doctype": "Custom Field",
            "dt": "Contact",
            "label": "Shopify Customer ID",
            "fieldname": "shopify_customer_id",
            "fieldtype": "Data",
            "unique": 1,
            "no_copy": 1,
            "translatable": 0,
            # Custom Field names are auto-generated; looked up by dt+fieldname.
        }
    ]


def glia_module_def() -> dict[str, Any]:
    """A `Glia` module so our custom Donation doctype has a clean namespace.

    `app_name` is mandatory on Module Def in newer Frappe; custom modules belong
    to the `frappe` app (they have no Python package of their own).
    """
    return {
        "doctype": "Module Def",
        "module_name": DONATION_MODULE,
        "app_name": "frappe",
        "custom": 1,
    }


def sync_state_doctype_def() -> dict[str, Any]:
    """Singleton `Glia Sync State` doctype holding the incremental-sync cursor.

    Stored in ERPNext so the daily CronJob pod is stateless (no PVC needed): it
    reads/writes `last_processed_at` here via the REST API.
    """
    return {
        "doctype": "DocType",
        "name": "Glia Sync State",
        "module": DONATION_MODULE,
        "custom": 1,
        "issingle": 1,
        "autoname": "hash",
        "naming_rule": "Expression",
        "track_changes": 0,
        "fields": [
            {
                "fieldname": "last_processed_at",
                "label": "Last Processed At",
                "fieldtype": "Data",
                "length": 40,
            },
        ],
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1},
            {"role": "Sales Manager", "read": 1, "write": 1},
        ],
    }


__all__ = [
    "contact_custom_fields",
    "donation_doctype_def",
    "glia_module_def",
    "sync_state_doctype_def",
]
