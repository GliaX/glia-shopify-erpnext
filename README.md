# glia-shopify-sync

Sync **Shopify donations** (one-time and recurring) from `glia2.myshopify.com`
into **ERPNext core CRM** at `https://asset.glia.org`: each donor becomes a
`Customer` (+ `Contact` + `Address`), and each gift becomes a custom `Donation`
doctype (created idempotently by the setup CLI — no Frappe app install needed).

## Status — Phase 2 (ERPNext wiring)

| Phase | What | Status |
|-------|------|--------|
| 1 | Scaffold: config, Shopify client (24h token + pagination), transform, dedup, state, tests | **Done** |
| 2 | ERPNext wiring: `frappe_client`, core-CRM mapping, custom `Donation` doctype, `doctor`/`setup`/`send-test` CLIs | **Done** |
| 3 | Backfill: `glia-sync-backfill` (resumable, dry-run) | Pending |
| 4 | Ongoing automation: `glia-sync-daily` + systemd timer + webhooks | Pending |

> **Target note:** the ERPNext Nonprofit module (`Donor`/`Donation`) is
> deprecated (the `frappe/non_profit` app is archived; doctypes removed from
> core ERPNext in v15+), and Frappe CRM isn't installed on the instance. So we
> use core CRM doctypes + one custom doctype. Infra for the instance itself is
> now IaC at [`GliaX/helm-erpnext`](https://github.com/GliaX/helm-erpnext).

## Architecture

```
Shopify Admin GraphQL API              glia-shopify-erpnext (Python 3.11+)
  Dev Dashboard custom app               ├─ shopify_client.py
  Client ID + Secret ──► 24h token         │   ├─ TokenManager (client-credentials grant)
  Orders (filtered by curated              │   └─ ShopifyClient.iter_orders() (cursor pages)
   donation-product allow-list)           ├─ transform.py  Order/LineItem → Donor/Donation
                                          ├─ frappe_client.py   Frappe REST (retry, 4xx fast-fail)
                                          ├─ erpnext_mapping.py Donor/Donation → Customer/Contact/Address/Donation
                                          └─►  ERPNext core CRM: Customer + Contact + Address + Donation (custom)
```

**Why Orders is the universal source:** every recurring billing cycle (native
Shopify Subscriptions / selling plans) emits a normal Order, so one code path
covers both one-time and recurring donations.

## Key design decisions

- **Donation filter = curated allow-list** of Shopify product IDs in
  `config.yaml` (the store has ~17 donation products across campaigns, not all
  consistently typed/tagged). Review/extend the list there.
- **Tip line items** are folded into the first donation on the order
  (`tip_mode: fold`); that Donation is flagged `includes_tip=true`.
- **Money is dual-currency**: primary `amount`/`currency` = shop money (CAD,
  accounting currency); `amount_presentment`/`currency_presentment` = what the
  donor actually paid in their own currency. Both preserved.
- **One Donation per donation-product line item** (handles multi-donation
  orders); `Donor` dedup key = lowercased email; `Donation` dedup key =
  `<shopify_order_gid>|<shopify_line_item_gid>`.
- **Auth**: Shopify no longer issues static tokens (deprecated Jan 1, 2026).
  The app mints a 24-hour access token from Client ID + Secret via the OAuth
  client-credentials grant and refreshes it automatically.

## Installation (dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

1. Copy the example config and edit (company, allow-list, currency):
   ```bash
   cp config.example.yaml config.yaml
   ```
2. Copy the secrets template and fill in (never commit):
   ```bash
   cp .env.example .env
   ```
   - **Shopify**: from the Dev Dashboard app → Settings → Credentials
     (`SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`). The app must be installed
     on `glia2` with scopes `read_products`, `read_orders`, `read_all_orders`,
     `read_customers`.
   - **ERPNext**: create an API user with **System Manager** (needs read+write
     on `Customer`/`Contact`/`Address`, plus `Custom Field` and `DocType` for
     the one-time setup), generate API Key/Secret.

## Development

```bash
pytest                       # run tests
pytest --cov=glia_shopify_sync --cov-report=term-missing
ruff check src tests         # lint
ruff format src tests        # format
mypy                         # type check (informational)
```

## CLIs (Phase 2)

```bash
glia-sync-doctor                 # read-only prerequisite checks (Shopify + ERPNext)
glia-sync-doctor --with-write-test   # also push+delete a test donor/donation
glia-sync-setup-erpnext          # idempotent: create Glia module, Donors group,
                                 # Customer.shopify_customer_id, Donation doctype
glia-sync-send-test              # push a synthetic donor+donation, then delete
glia-sync-send-test --keep       # ...keep it for inspection in the UI
```

> The setup/send-test CLIs **write** to ERPNext. Take a DB backup first
> (see [`GliaX/helm-erpnext`](https://github.com/GliaX/helm-erpnext) README).

## Repo layout

```
glia-shopify-erpnext/
├── pyproject.toml
├── config.example.yaml          # company, donation allow-list, currency, knobs
├── .env.example                 # secrets template
├── src/glia_shopify_sync/
│   ├── config.py                # Settings (env) + YamlConfig + load_config()
│   ├── models.py                # Donor / Donation dataclasses
│   ├── shopify_queries.py       # GraphQL query strings
│   ├── shopify_client.py        # TokenManager + ShopifyClient (paginated)
│   ├── transform.py             # Order → (Donor, [Donation, ...])  (pure)
│   ├── dedup.py                 # donor_key / donation_key
│   ├── frappe_client.py         # generic Frappe REST client (retry, 4xx fast-fail)
│   ├── erpnext_mapping.py       # Donor/Donation → Customer/Contact/Address/Donation
│   ├── donation_doctype.py      # custom Donation DocType + Custom Field defs
│   ├── setup_erpnext.py         # CLI: create doctype + custom fields (idempotent)
│   ├── doctor.py                # CLI: prerequisite checks
│   ├── send_test.py             # CLI: push+cleanup a test record
│   └── state.py                 # JSON state (backfill cursor, last run)
└── tests/                       # fixtures + pytest suite
```

## License

MIT. See `pyproject.toml`.
