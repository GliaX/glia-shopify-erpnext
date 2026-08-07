# Shop Migration — Operation Notes & Handoff

> A narrative history + hard-won tips for the Shopify → ERPNext E Commerce
> migration. Read this before touching the shop migration again. Pair with
> `AGENTS.md` (phase tracker + env) and `OPERATIONS.md §12` (per-phase runbook).
>
> **Scope the user confirmed (do not re-litigate):** full replacement of Shopify
> on ERPNext E Commerce; all products (incl. donations); catalog + order history;
> full checkout (Stripe).

---

## 0. TL;DR — current state (2026-08-06)

| Area | Status |
|---|---|
| **Two repos** | `glia-shopify-erpnext` = the sync toolkit (this repo). `helm-erpnext` = the ERPNext image/helm chart (infra). |
| **Custom image** | `ghcr.io/gliax/erpnext:v16.30.0-crm-v1.80.0-ecommerce2` (frappe/erpnext + crm + payments + webshop). **Deployed.** |
| **E Commerce** | Installed (webshop + payments apps). Storefront live at `/all-products`. |
| **Phase 1 Catalog** | 29 active products → 200 Items, 171 variants, 189 prices, 29 Website Items (23 with images). |
| **Phase 2 Shipping** | No-op (Shopify has 30 zones, 0 rates — merch is POD-fulfilled). |
| **Phase 3 Customers** | 4,265 customers-with-orders imported. |
| **Phase 4 Orders** | 580 Sales Orders (CAD $139,219.67), FY 2019–2026, 0 errors. |
| **Phase 5 Checkout** | Stripe wired + tested (live keys); `enable_checkout=1`. |
| **Phase 6 Ongoing sync** | **Not started** (webhooks / scheduled deltas). |
| **Tests** | 105 passing; ruff + format clean. |

### 🔴 The #1 pending action: **commit the glia-shopify-erpnext code**
Every line of shop-migration code (`src/glia_shopify_sync/shop/`, tests, config,
`AGENTS.md`, this file) is **uncommitted** locally. The infra repo
(`helm-erpnext`) is fully committed/pushed, but the sync toolkit is not. If the
workspace is lost, ~2,000 lines of work + all the live-run lessons vanish.
Commit it before doing anything else:
```bash
cd /home/orangey/workspace/glia-shopify-erpnext
git add -A && git commit -m "Shop migration: catalog + customers + orders + checkout (Phases 1,3,4,5)"
```

---

## 1. Timeline (what actually happened, in order)

1. **Orientation.** Read README/OPERATIONS/AGENTS + the donation-sync code
   (`shopify_client`, `frappe_client`, `transform`, `crm_mapping`, `sync`,
   `setup_erpnext`). Confirmed the repo only syncs *donations* → ERPNext CRM;
   the shop migration is a new effort.
2. **Scoping.** Asked 4 questions (goal / catalog scope / history / checkout).
   User chose the maximum: full replacement, all products, history, checkout.
3. **Phase 1 — Catalog (build + import).** Created `shop/` sub-package:
   `models`, `erpnext_catalog_mapping` (pure transforms), `setup`,
   `catalog_sync`, + GraphQL queries. Dry-run against live Shopify immediately
   caught `ProductVariant.weight` removal (API 2025-07) → moved to
   `inventoryItem.measurement.weight`. Live import then surfaced, in sequence:
   `Website Category` doctype absent → put collection id on `Item Group`; Item
   Group root is `All Item Groups` (not `All Items`); pre-existing manufacturing
   `Size`/`Color` attributes collide with Shopify abbreviations + mixed case →
   parse-time normalization + case-insensitive dedup + collision-safe abbrs;
   `Stethoscope` pre-exists as non-template → link + degrade; `website_image`
   rejects bare URLs → `File` doc per image.
4. **E Commerce install (the deep one).** Discovered `asset.glia.org` shipped
   with E Commerce stripped out (empty `shopping_cart/` shell, no
   `Website Item`). `apps/` is on the **ephemeral overlay** (only `sites/` is on
   the PVC) → enabling it needs an **image rebuild**, not in-place `install-app`.
   Bundled `frappe/webshop` (version-16). First build failed: webshop imports
   the extracted `payments` app → added `frappe/payments`. `install-app payments`
   hit a known bug (frappe/payments#87: Web Form `payment_gateway` Link validates
   before `Payment Gateway` doctype syncs) → fix: `bench migrate` first, then
   `bench execute payments.utils.make_custom_fields`, then `install-app webshop`.
5. **Blank-storefront asset saga (3 cycles).** Products existed but the grid was
   empty. Root causes, found one at a time: (a) `bench get-app` didn't register
   apps in `sites/apps.txt` → `bench build` skipped them; (b) `bench build`
   compiled webshop bundles but didn't write the `assets.json` manifest → bare
   `/web.bundle.js` 404'd; (c) the manifest entry pointed at the **raw source**
   (`web.bundle.js` = ES `import`s) instead of the **compiled** `dist/js/` bundle;
   (d) `bench build` re-hashed frappe/erpnext CSS but left the manifest on old
   hashes → those CSS 404'd; (e) `get_assets_json()` caches the manifest in
   **valkey-cache**, which `bench clear-cache` does NOT flush. Final fix: a
   Dockerfile step that rebuilds the manifest by scanning `assets/*/dist/` and
   remapping every `<name>.bundle.{js,css}` to its real hashed file, + restart
   valkey-cache after each asset-changing deploy.
6. **CI outage bypass.** GitHub Actions had no hosted runners for ~1h. Built the
   image **locally with docker** and pushed to ghcr.io directly (the `gh` token
   has `write:packages`).
7. **Phase 2 — Shipping.** 30 Shopify zones, all with **0 rates** (POD partner
   handles fulfillment). Nothing to migrate.
8. **Phase 3 — Customers.** GraphQL `customers` query; `ordersCount`/`totalSpent`
   also removed in 2025-07 (omitted). Default `orders_count:>0` skips ~16k
   zero-order accounts. Address requires `address_line1` (mandatory). 4,265
   imported.
9. **Phase 5 — Checkout + Stripe.** `checkout_setup` CLI configures Webshop
   Settings + Payment Gateway + Stripe Settings + Payment Gateway Account → CAD
   bank. Stripe test: account validated, live Checkout Session created. Keys are
   **live** — flagged for the user.
10. **Phase 4 — Orders (3 gotchas).** Built `order_sync` → Sales Orders for
    non-donation lines (donations already `Donation`s). Dry-run looked fine;
    full run errored 467/579 shop orders. Causes, found one at a time: (a)
    company default warehouse `Stores - Glia` is **disabled** → set
    `shop.warehouse`; (b) `financial_status:paid` Shopify query filter is
    unreliable → filter **client-side**; (c) **missing Fiscal Years** — Glia
    only had 2024 + 2026; orders from 2025/2023/pre-2020 failed with
    `FiscalYearError` → created FY 2015–2025. Final: 580 Sales Orders, 0 errors.

---

## 2. Architecture in one screen

```
glia-shopify-erpnext (sync toolkit, Python 3.11)        helm-erpnext (infra)
└─ src/glia_shopify_sync/                               └─ Dockerfile (custom image)
   ├─ (flat) donation sync — DONE, in production           FROM frappe/erpnext:v16.30.0
   └─ shop/  ← THE MIGRATION                              + bench get-app crm/payments/webshop
      ├─ models.py            frozen dataclasses          + register apps in sites/apps.txt
      ├─ shopify_queries.py    PRODUCTS/CUSTOMERS/         + bench build
      │                        COLLECTIONS/ORDERS          + rebuild assets.json from dist/
      ├─ shopify_client.py     iter_products/customers/   + assets/<app> symlinks
      │                        collections/orders         values.yaml  (image tag)
      ├─ erpnext_catalog_       Item/Attr/Price/Website   values.pin.yaml (MUST include)
      │   mapping.py            Item                      values.secret.yaml
      ├─ erpnext_customer_      Customer/Address         .github/workflows/build-image.yml
      │   mapping.py
      ├─ erpnext_order_         Sales Order
      │   mapping.py
      ├─ setup.py              glia-shop-setup
      ├─ catalog_sync.py       glia-shop-catalog-sync
      ├─ customer_sync.py      glia-shop-customer-sync
      ├─ order_sync.py         glia-shop-order-sync
      └─ checkout_setup.py     glia-shop-checkout-setup
```

**Data flow:** Shopify Admin API (GraphQL, 24h client-credentials token) →
frozen dataclasses → pure transforms → Frappe doc dicts → ERPNext REST (HTTP
Basic). Transports are injectable so tests use no real HTTP.

---

## 3. Tips for success (the hard-won lessons)

### Process / workflow
- **Dry-run against live FIRST, every time.** `--dry-run` against the real
  Shopify catches API-version field renames (2025-07 removed `weight`,
  `ordersCount`, `totalSpent`) before they bite mid-import. Tests use fixtures;
  only the live API reveals schema drift.
- **Read the actual rendered output, not just the API.** The storefront "looked
  empty" while the product API returned 29 items — the failure was in the
  asset pipeline, invisible from the data side. `curl` the page + check the
  browser console (MIME errors, 404s).
- **Each deploy can hide a new failure.** Iterate: deploy → verify the specific
  symptom → fix → redeploy. Don't assume one fix closes it.
- **When CI is down, build locally.** `DOCKER_BUILDKIT=1 docker build ... &&
  docker push` works if your `gh` token has `write:packages` (it does).
- **Take a DB backup before every ERPNext write phase.** `bench --site
  asset.glia.org backup --with-files`. Restorable from the sites PVC.

### ERPNext / Frappe specifics
- **`apps/` is ephemeral** on this helm chart (only `sites/` is on the PVC). Any
  new Frappe **app** must go in the Docker image; `bench get-app` in a running
  pod vanishes on restart, and `install-app` would persist DB migrations without
  the code → broken site.
- **`bench clear-cache` does NOT flush valkey-cache's `assets_json`.** After any
  deploy that changes assets, `kubectl -n erpnext rollout restart deploy/
  erpnext-valkey-cache` or the storefront reverts to blank/broken.
- **Attach fields (`website_image`, etc.) reject bare remote URLs** (silently
  blank them). Create a `File` doc referencing the URL, then set the field to
  its `file_url`.
- **Posting dates must fall in an active Fiscal Year.** Old backfills need FYs
  covering the whole history. Check `Fiscal Year` and create missing years
  before backfilling.
- **A template Item (`has_variants=1`) cannot be sold** on a Sales Order — only
  its variants. When resolving order lines, never fall back to a template
  item_code.
- **Doctype required-field surprises:** `Address.address_line1` is mandatory;
  Sales Order needs `naming_series` (`SAL-ORD-.YYYY.-`), a non-disabled
  warehouse, `order_type`, both exchange-rate fields.
- **`limit_page_start` (offset) is silently ignored** by Frappe REST — fetch in
  one big page. Filters/fields must be `json.dumps()`.

### Shopify API (2025-07)
- **Fields removed** from this API version: `ProductVariant.weight`/`weightUnit`
  (→ `inventoryItem.measurement.weight`); `Customer.ordersCount`/`totalSpent`
  (→ a `stats` sub-object, or omit).
- **The `financial_status:paid` GraphQL query filter is unreliable** → filter
  paid status client-side on `displayFinancialStatus` (the donation sync does
  this too).
- Auth is the **client-credentials grant** (Client ID + Secret → 24h token), not
  a static token. The app already grants every read scope the migration needs.

### Testing
- **`Settings()` loads `.env`** — so tests that need "no keys" must use
  `Settings(_env_file=None)` (e.g. the checkout-setup tests), or the real
  Stripe keys leak in and break the assertion.
- Keep transforms pure (no I/O) and feed them dicts in tests; that's why
  coverage on the mapping modules is ~100%.

---

## 4. Gotchas quick-reference (by phase)

| Phase | Gotcha | Fix |
|---|---|---|
| 1 | `weight` removed from ProductVariant | `inventoryItem.measurement.weight` |
| 1 | `Website Category` doctype absent | collection id on `Item Group` |
| 1 | Item Group root name | `All Item Groups` (config `item_group_parent`) |
| 1 | manufacturing Size/Color collisions | `_norm_option` + case-insensitive dedup + `_unique_abbr` |
| 1 | pre-existing non-template Item (Stethoscope) | link + `products_degraded` |
| 1 | `website_image` blanks bare URLs | `File` doc per image (`_ensure_website_image`) |
| E Commerce | E Commerce stripped from image | rebuild image w/ payments + webshop |
| E Commerce | `install-app payments` Web Form bug | `bench migrate` → `make_custom_fields` → `install-app webshop` |
| Assets | storefront blank | register apps in apps.txt; rebuild manifest from dist/; restart valkey-cache |
| 3 | `ordersCount`/`totalSpent` removed | omit from query |
| 3 | Address needs address_line1 | skip address if no street |
| 4 | disabled default warehouse | `shop.warehouse` = `Finished Goods - Canada - Glia` |
| 4 | `financial_status` filter unreliable | filter client-side |
| 4 | missing Fiscal Years | created 2015–2025 (cover order history) |

---

## 5. The "never forget" deploy checklist (image/helm changes)

```bash
# 0. BACKUP
kubectl -n erpnext exec deploy/erpnext-gunicorn -- bash -lc \
  'cd /home/frappe/frappe-bench && bench --site asset.glia.org backup --with-files'

# 1. Build the image (CI auto-builds on push; or locally if GitHub is down)
cd /home/orangey/workspace/helm-erpnext
git commit -am "..." && git push           # CI builds ghcr.io/gliax/erpnext:<tag>
# local fallback:
DOCKER_BUILDKIT=1 docker build --build-arg ... -t ghcr.io/gliax/erpnext:<tag> . && docker push ...

# 2. Bump values.yaml image.tag, then helm upgrade (ALWAYS include values.pin.yaml)
helm -n erpnext upgrade erpnext frappe/erpnext --version 8.0.21 \
  -f values.yaml -f values.secret.yaml -f values.pin.yaml
kubectl -n erpnext rollout status deploy/erpnext-gunicorn

# 3. If the change touched assets (JS/CSS/bundles) — flush the assets cache
kubectl -n erpnext rollout restart deploy/erpnext-valkey-cache

# 4. Verify the storefront actually renders (don't trust "deployed" status)
curl -sS https://asset.glia.org/all-products | grep -oiE 'src="[^"]*web\.bundle[^"]*"'
# all referenced .bundle.{js,css} must return 200
```

---

## 6. What's left & how to resume

### Phase 6 — Ongoing sync (the only remaining migration work)
The migration so far is a one-time backfill. For ERPNext to *replace* Shopify,
new Shopify activity needs to flow in (or, post-cutover, ERPNext becomes the
source of truth and Shopify is decommissioned). Options:
- **Inbound (Shopify → ERPNext):** scheduled job (reuse the CronJob pattern from
  the donation sync) or Shopify **webhooks** (`orders/create`, `product/update`).
  Webhooks need `read_subscriptions`/`write_subscriptions` scope + a public
  endpoint; the donation sync notes (OPERATIONS §9) discuss this.
- **Reconciliation:** a Payment Entry per paid Sales Order (currently deferred —
  the Sales Order is the order record only). Wire `STRIPE_WEBHOOK_SECRET` for
  Stripe→ERPNext payment matching.

### If resuming
1. **Commit the glia-shopify-erpnext code first** (§0).
2. `. .venv/bin/activate` — the venv + `.env` are already populated; CLIs and
   `load_config()` pick up live credentials.
3. Re-read `AGENTS.md` (auto-loaded) for the phase tracker + env, and
   `OPERATIONS.md §12` for per-phase commands.
4. Every sync CLI is idempotent + resumable (dedup by `shopify_*_id`), so
   re-running is always safe — but **back up first** for anything new.

### Decommissioning Shopify (the eventual goal)
Once Phase 6 + a storefront review confirm ERPNext handles the full flow
(browse → cart → Stripe → order → fulfillment), cut over DNS/checkout and
retire Shopify. The donation sync (CronJobs) and the shop CLIs would then
either stop (Shopify gone) or flip direction (ERPNext → nowhere). Out of scope
until Phase 6 lands.
