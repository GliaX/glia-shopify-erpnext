# AGENTS.md — orientation for AI agents working on this repo

> Single source of truth for "what is this repo, how do I run it, what's the live
> environment." Read this at the start of every session. **Secret values live in
> `.env` (gitignored) — read that file when you actually need to call an API.**
> Never paste secret values into committed files.
>
> **Shop migration?** Read `SHOP_MIGRATION_NOTES.md` — the full history, tips for
> success, the "never forget" deploy checklist, and what's left (Phase 6). It
> pairs with this file (phase tracker + env) and `OPERATIONS.md §12` (runbook).

## What this repo is

`glia-shopify-erpnext` is a Python 3.11+ sync/migration toolkit between the Glia
Shopify store and their ERPNext instance. It has two distinct efforts:

1. **Donation sync** (DONE, in production) — `src/glia_shopify_sync/` (flat).
   Pushes Shopify **donation** line items (a curated allow-list of 17 donation
   product IDs in `config.yaml`) into ERPNext CRM as core `Contact` + a custom
   `Donation` doctype (module `Glia`). Patreon donors also synced. Runs as k8s
   CronJobs. See `README.md` and `OPERATIONS.md`.

2. **Shop migration** (IN PROGRESS) — `src/glia_shopify_sync/shop/` sub-package.
   Full recreation of the Shopify store (products, variants, prices,
   collections, shipping, customers, orders, checkout) on ERPNext's **E
   Commerce** module. Goal: eventually decommission Shopify. See the architecture
   docstring in `shop/__init__.py`.

## Live environment (non-secret)

| Thing | Value |
|---|---|
| Shopify shop domain | `glia2.myshopify.com` |
| Shopify Admin API version | `2025-07` (set in `.env` as `SHOPIFY_API_VERSION`) |
| Shopify auth | OAuth **client-credentials grant** — Client ID + Secret mint a 24h token at runtime via `TokenManager`. No static token (deprecated Jan 1 2026). |
| ERPNext instance | `https://asset.glia.org` (ERPNext 16.30 / Frappe 16.29 / Frappe CRM 1.80, on DigitalOcean k8s, namespace `erpnext`) |
| ERPNext auth | Frappe REST, **HTTP Basic** with API Key + Secret (`Authorization: Basic base64(key:secret)`). API user is System Manager. |
| ERPNext company | `Glia` (in `config.yaml`) |
| Shop / accounting currency | `CAD` |
| Patreon campaign id | auto-fetched (or `PATREON_CAMPAIGN_ID` in `.env`) |

### Secret var names (values in `.env`, gitignored — DO NOT commit)
- Shopify: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`
- ERPNext: `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`
- Patreon: `PATREON_CREATOR_ACCESS_TOKEN`, `PATREON_CREATOR_REFRESH_TOKEN`,
  `PATREON_CLIENT_ID`, `PATREON_CLIENT_SECRET`

### Shopify app scopes
Currently granted: `read_products`, `read_orders`, `read_all_orders`,
`read_customers`. These cover the donation sync **and** Phase 1 of the shop
migration (products/variants/collections/images all come under `read_products`).

When you reach these phases, the app needs **additional** scopes (re-issue the
token / update the app in the Shopify Dev Dashboard):
- Inventory sync (stock quantities) → add `read_inventory`, `read_locations`
- Fulfillment read (Phase 4) → `read_fulfillments` (often covered by
  `read_orders`, but add explicitly to be safe)
- Shipping zones (REST `/admin/api/2025-07/shipping_zones.json`) → no extra scope

## How to run things (dev)

```bash
cd /home/orangey/workspace/glia-shopify-erpnext
. .venv/bin/activate         # venv already exists
pytest -q                    # test suite (~62 tests + shop tests)
pytest --cov=glia_shopify_sync --cov-report=term-missing
ruff check src tests         # lint
ruff format src tests        # format
mypy                         # type check (informational; relaxed config)
```

CLIs (entry points in `pyproject.toml`):
- Donation sync: `glia-sync-doctor`, `glia-sync-setup-erpnext`,
  `glia-sync-backfill`, `glia-sync-daily`, `glia-sync-send-test`,
  `glia-sync-patreon`.
- Shop migration (Phase 1): `glia-shop-setup`, `glia-shop-catalog-sync`.

To call the live APIs from this machine: `.env` is already populated, so the
CLIs and `load_config()` will pick up real credentials. **Production writes go
to `asset.glia.org` — take a DB backup first** (see `OPERATIONS.md` §10). Prefer
`--dry-run` until certain.

## Code conventions (follow exactly)

- **Structure**: donation sync is flat in `glia_shopify_sync/`; shop migration
  is nested in `glia_shopify_sync/shop/` to avoid name collisions (both have
  `models`/`sync`/`transform`-shaped files).
- **Pure transforms**: Shopify JSON → frozen dataclass → Frappe doc dict, in
  separate `_mapping`/`transform` modules. No I/O in transforms; trivially
  unit-testable. See `transform.py`, `crm_mapping.py`.
- **Injectable transports**: clients accept `transport`/`clock`/`sleep` callables
  so tests use no real HTTP. See `shopify_client.py`, `frappe_client.py`.
- **Config**: secrets via pydantic-settings `Settings` (env/systemd-creds);
  non-secret via `YamlConfig` (`config.yaml`). `load_config()` is lru-cached.
- **Idempotent setup**: `_ensure_doc` pattern with `StepResult`("created"/
  "exists"). Custom doctypes/fields created via REST. See `setup_erpnext.py`.
- **Dedup**: pre-loaded maps from ERPNext (`load_contact_map`, etc.) + custom
  `shopify_*_id` fields; syncs are safely re-runnable.
- **Logging**: `structlog` (`log.info("event_name", **kwargs)`).
- **Style**: `ruff` (line-length 100, py311), double quotes, no inline comments
  unless asked. `pytest` with `filterwarnings = ["error"]` (strict).

## ERPNext/Frappe gotchas (learned the hard way — see `OPERATIONS.md` §8)

- `limit_page_start` (offset) is **silently ignored** by Frappe REST — fetch in
  one big page, don't paginate by offset.
- Filters/fields must be `json.dumps()`, not `str()`.
- Resource endpoints wrap `{"data": ...}`; RPC `frappe.client.*` wrap
  `{"message": ...}` — both unwrapped by `frappe_client._data()`.
- Custom doctypes created via REST get **zero** role permissions by default —
  add System Manager + Sales Manager explicitly.
- `bench clear-cache` after creating/modifying Notifications, doctype fields, or
  custom fields, or changes won't take effect.
- 4xx → fast-fail (real error); 5xx/network → retry with backoff (tenacity).

## Shop migration — phase tracker

| Phase | Module | Status |
|---|---|---|
| 1 — Catalog (products/variants/prices/collections/website items) | `shop/{models,shopify_catalog,erpnext_catalog_mapping,setup,catalog_sync}.py` | **DONE & imported** (29 active products → 200 Items, 171 variants, 189 prices, 29 Website Items published) |
| 2 — Shipping (Shopify shipping_zones → ERPNext Shipping Rule) | `shop/shipping_sync.py` | **No-op**: Shopify has 30 zones but **0 rates** (merch is fulfilled by a POD partner); nothing to migrate |
| 3 — Customers (Shopify Customer → ERPNext Customer; dedup vs donor Contacts) | `shop/{erpnext_customer_mapping,customer_sync}.py` | **DONE & imported** (4,265 customers-with-orders → Customers + Addresses; 20k zero-order accounts skipped by default, `--all-customers` to include) |
| 4 — Orders (Shopify Order → Sales Order + Invoice + Payment; reconcile donations) | `shop/{erpnext_order_mapping,order_sync}.py` | **DONE & imported** (580 paid shop orders → Sales Orders CAD $139k; donation orders skipped, already `Donation`s) |
| 5 — Checkout (Shopping Cart + Payment Gateway + E Commerce settings) | `shop/checkout_setup.py` | Scaffolded (E Commerce now installed — see below) |
| 6 — Ongoing sync (webhooks / scheduled deltas) | — | Not started |

User-confirmed scope for the migration (do not re-litigate): **full replacement**
(decommission Shopify eventually), **all ~50 products** (including donations),
**catalog + order history**, **full checkout setup**.

### E Commerce is now installed (Webshop app)

`asset.glia.org` originally shipped with E Commerce **stripped out** (custom
`ghcr.io/gliax/erpnext` image had only frappe/erpnext/crm; the `shopping_cart/`
module was an empty shell). E Commerce was enabled by bundling two extracted
apps into the image (`GliaX/helm-erpnext` Dockerfile, tag
`v16.30.0-crm-v1.80.0-ecommerce`):
  * **`frappe/payments`** (version-16) — payment plumbing; webshop depends on it.
  * **`frappe/webshop`** (version-16) — the E Commerce app (provides `Website
    Item`, `Webshop Settings`, `Website Item Group`). Note: the old
    `E Commerce Settings` / `Shopping Cart Settings` are combined into
    **`Webshop Settings`** in this version; there's no `frappe/e_commerce` repo.

Install gotcha (learned the hard way): `bench install-app payments` fails mid
`after_install` (`payments.utils.make_custom_fields`) because the Web Form
`payment_gateway` Link field validates before the `Payment Gateway` doctype is
synced. Fix: `bench migrate` first (syncs the doctype), then
`bench execute payments.utils.make_custom_fields`, then `install-app webshop`.

**Webshop asset gotcha (blank storefront)**: `bench build` compiles webshop's
bundles but does NOT register `web.bundle.js` / `webshop-web.bundle.css` in the
shared `assets/assets.json` manifest, so Frappe's `include_script()` can't
resolve them and `/all-products` renders blank. The Dockerfile injects those
two entries (full `/assets/webshop/...` paths) explicitly. AND `get_assets_json()`
is cached in **valkey-cache** (`client_cache`, key `assets_json`) which
`bench clear-cache` does NOT flush — so after **every** image rebuild that
changes assets, restart valkey-cache or the storefront breaks again:
`kubectl -n erpnext rollout restart deploy/erpnext-valkey-cache`.

Why an image rebuild was required (not an in-place kubectl install):
`apps/` is on the **ephemeral overlay** in this chart — only `sites/` is on the
PVC. So `bench get-app` in a running pod would vanish on restart, and
`install-app` would persist DB migrations without the code → broken site. The
apps must live in the image. The Shopify app grants every read scope needed
(`read_inventory`, `read_locations`, `read_fulfillments`, `read_shipping`).

### Phase 1 run notes (read before re-running the catalog sync)

- **Backup before writes**: `kubectl -n erpnext exec deploy/erpnext-gunicorn --
  bash -lc 'cd /home/frappe/frappe-bench && bench --site asset.glia.org backup
  --with-files'`. Clear cache after doctype/attribute changes: same with
  `clear-cache`.
- **Item Group root is `All Item Groups`** (`shop.item_group_parent`); a
  `Donation` group (singular) already exists, so `shop.item_group_donations` is
  `Donation`.
- **Pre-existing manufacturing data collides**: `Size`/`Color` Item Attributes
  use spelled-out values with auto abbreviations; Shopify uses abbreviations +
  mixed case (`Black`/`black`). Handled by parse-time normalization
  (`_norm_option`) + case-insensitive dedup + collision-safe abbreviations.
- The `Stethoscope` Item pre-exists as a non-template; the sync links it
  (`shopify_product_id`) and degrades gracefully (variants skipped, logged as
  `products_degraded`). Review before converting.
- `ProductVariant.weight`/`weightUnit` were removed in API `2025-07`; weight now
  comes from `inventoryItem.measurement.weight.{value,unit}`.
- **Customers (Phase 3)**: `Customer.ordersCount`/`totalSpent` were also removed
  in API `2025-07` (now under a `stats` sub-object); they're omitted from the
  query since Customer/Address docs don't need them. Address requires
  `address_line1` (mandatory), so customers with no street are imported without
  an address. Default filter is `orders_count:>0` (skip the ~20k zero-order
  accounts); `--all-customers` includes everyone. Dedup via
  `Customer.shopify_customer_id`; donor `Contact`s (3,271) coexist — Customer↔
  Contact linking is deferred to the order phase.
