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
| **Custom image** | `ghcr.io/gliax/erpnext:v16.30.0-crm-v1.80.0-emailbuilder5-guestcheckout6-donations8-ops7` (frappe + erpnext + crm + payments + webshop + email builder + guest checkout + donations + ops). **Deployed.** |
| **E Commerce** | Installed (webshop + payments apps). Storefront live at `/all-products`. |
| **Guest checkout** | `glia_guest_checkout` 0.0.2 — <https://github.com/GliaX/glia-guest-checkout>. Anonymous cart + checkout at `/guest_cart`. **Deployed & tested.** |
| **Recurring gifts** | `glia_donations` 0.3.1 — <https://github.com/GliaX/glia-donations>. Stripe Billing subscriptions, webhook → `Donation` + optional ledger booking, `/recurring_donation` cancel page. **Deployed & tested.** |
| **Ops** | `glia_ops` 0.1.6 — <https://github.com/GliaX/glia-ops>. Backup alerting, Shopify→ERPNext redirect generator, cutover readiness report. **Deployed.** |
| **Backups** | CronJob `erpnext-backup`, 03:30 daily, off-cluster to Nextcloud, restore proven. See `helm-erpnext/backup/README.md`. **Live.** |
| **Phase 1 Catalog** | 29 active products → 200 Items, 171 variants, 189 prices, 29 Website Items (23 with images). |
| **Phase 2 Shipping** | No-op (Shopify has 30 zones, 0 rates — merch is POD-fulfilled). |
| **Phase 3 Customers** | 4,265 customers-with-orders imported. |
| **Phase 4 Orders** | 580 Sales Orders (CAD $139,219.67), FY 2019–2026, 0 errors. |
| **Phase 5 Checkout** | Stripe wired + tested (live keys); `enable_checkout=1`. |
| **Phase 6 Ongoing sync** | **Not started** (webhooks / scheduled deltas). |
| **Tests** | 105 passing; ruff + format clean. |

### ✅ Code commit — DONE (verified 2026-11-13)

The old "commit before doing anything else" warning is resolved: the working
tree is clean and Phases 1/3/4/5 (+ LaunchGood) are on `main` (shop-migration
work through `364d1db`), remote
`git@github.com:GliaX/glia-shopify-erpnext.git`. The image tag is now
`ghcr.io/gliax/erpnext:v16.30.0-crm-v1.80.0-emailbuilder5`.

### Next phase: staging + cutover plan

The actual move of `glia.org` off Shopify is staged in
**`~/workspace/glia-server/glia-org-shopify-to-erpnext-staging-plan.md`**
(staging site, content/theme parity, the donation/recurring/account gaps, DNS
cutover, rollback). Read it before starting Phase 6 — it supersedes this file's
§6 "What's left" as the execution checklist. Credentials/DNS/TLS access are
documented in `~/workspace/glia-server/ACCESS.md` §7–§11.

### Content/theme bootstrap — DONE (2026-11-13)

The storefront look and content scaffolding are live on `asset.glia.org`:
custom `Blog` / `Blog Category` / `Blog Post` doctypes, 38 published articles,
83 Web Pages, a Glia navbar/footer/donation bar, and a rebuilt homepage.
Artifacts, re-apply commands and rollback live in
`~/workspace/glia-server/glia-theme/` (README there). All remote Shopify assets
were localised on 2026-11-13 (260 unique assets → 287 `/files/shopify-*` docs,
~92 MB, 0 remote refs); audit scripts are in `glia-theme/localization/`.

### Guest checkout — DONE (2026-11-14)

Upstream webshop has no guest checkout (open issue
<https://github.com/frappe/webshop/issues/122>): its cart is a `Quotation` keyed
on `contact_email = frappe.session.user`, so anonymous shoppers are redirected
to `/login`. We wrote our own app instead of patching webshop:

- Repo: <https://github.com/GliaX/glia-guest-checkout> (public, Frappe app
  `glia_guest_checkout` 0.0.2), baked into the image by
  `helm-erpnext/Dockerfile` (`GUEST_CHECKOUT_REPO` / `_BRANCH`) — a new commit
  there needs a **new image tag**, not just a bench reload.
- Cart = `Guest Cart` doc behind an HttpOnly `guest_cart_id` cookie (30 days);
  shop pages `/guest_cart` and `/guest_order?token=…`; `guest_checkout.js`
  rewrites `/cart` links and intercepts add-to-cart for guests.
- Order path: Customer/Contact/Address → Quotation (Shopping Cart) → Sales Order
  → Payment Request → existing `/stripe_checkout`. Document creation runs as
  Administrator (a Guest cannot insert Items/Quotations); the request itself
  stays Guest and `allow_guest` APIs refuse logged-in users.
- Guards: variant templates (`has_variants` without `variant_of`) and any line
  that prices at ≤ 0 are rejected at add-to-cart *and* again before the
  quotation is submitted — a template added directly used to produce a 0 CAD
  order, which is exactly the bug 0.0.2 fixes.
- Verified end-to-end on the deployed image: `/guest_cart` 200, add-to-cart 200,
  checkout created `SAL-ORD`+`ACC-PRQ` and a working Stripe URL; the test
  documents were then deleted.
- Open gaps: recurring donations, donation records/hooks, tax + shipping rules,
  fulfilment, reconciliation, and cart merging on login.

### One-off donations, redirects, accounts, backups — DONE (2026-11-15)

- **One-off gifts now reach the donor CRM.** `glia_donations` hooks Payment
  Entry submit and writes a `Donation` per donation line of the paid Sales
  Invoice (or an advance against the order), linked back to
  order/invoice/payment entry. Verified with full, partial and advance payments
  plus a merch control that correctly creates nothing; idempotent per
  (payment entry, invoice, item). Add `run_backfill` reports historical paid
  invoices that predate the hook.
- **Recurring charges can be booked** (opt-in in Donation Settings): a Sales
  Invoice + Payment Entry per paid charge using `misc-monthly-donation` and
  `Individual Contributions - Glia`; verified double-entry (bank +25, income
  +25) with no duplicate Donation from the shop hook.
- **Account claim:** `/claim_account` emails Frappe's password-reset link to an
  imported customer (matched via Contact or `Customer.shopify_email`, 4,220 of
  4,275 have one). Rate-limited per email, branded reset template installed,
  footer links to it.
- **Shopify URL redirects:** 139 entries in `Website Settings.route_redirects`,
  generated by `glia_ops.redirects.generate_redirects`. Every published Shopify
  URL (125: products, pages, collections, articles) returns 301; zero 404s.
- **Backups:** every 6 hours, Nextcloud + DigitalOcean Spaces, status marker,
  and `glia_ops` emails `info@glia.org` when a backup stops delivering.
- **`glia_ops` also carries `readiness.report()`** — one JSON answer to "what
  still blocks cutover" (currently: tax). Backups, redirects, accounts and both
  donation paths report OK; fulfilment warns on missing tracking fields.

### Review round: Adam's QA list (2026-11-15)

Verified each item against the live site before touching anything; several were
already fine. Fixed:

- **Broken article link** (`/blog/news/glia.org/glia4gaza` → 500): the content
  had `href="glia.org/glia4gaza"` (relative). Rewritten to `/glia4gaza`; both
  ExFix articles now return 200 with zero broken internal links.
- **Newsletter accepted anything**: `notanemail` (and "darpa") were stored and
  thanked. New `glia_guest_checkout.api.subscribe_newsletter` validates the
  address server-side, de-duplicates, and is rate-limited per email; the footer
  validates client-side too. Invalid → 417 with a clear message.
- **Add-to-cart UX**: modal alert replaced with a toast, header badge updates
  immediately (Frappe + webshop counters), no forced redirect to the cart, and
  adding again increments instead of sticking at one.
- **Favicon** was a near-blank white PNG; replaced with a dark "G" on cream
  (readable light/dark). `push_design.py` now carries the new path so design
  pushes stop reverting it.
- **Empty shop flash**: products render client-side, so `/all-products` looked
  broken until JS finished. Added "Loading products…" + an image skeleton.
- **Duplicate blog**: "Engagement Hub – Opportunities at Glia" had 0 posts and
  duplicated "Get Involved"; unpublished. Blog index now lists 4 blogs.
- **Steth of Hope had no image**: pulled `SOH-EricImage.png` from Shopify and
  attached it to the Website Item and Item.

Already fine (no action needed): donation product pages (they do have variant
pickers and add-to-cart), R&D / 3D-printing section donate links, press-kit
contact link, tourniquet/kidney-cap now using ERPNext product cards, contact-form
wording, and no unexplained "donating*" asterisk anywhere in content.

Still open — **content, not code**: `meet-the-team` (13 chars), `faqs` (4),
`glia-x-rajaeen` (14), `support-glia-medical-solidarity-with-palestine` and
`restoring-care-in-gaza` (236) are near-empty. Shopify's own pages for the first
three have **empty bodies too** (they are section-built), so their copy has to be
reconstructed from the rendered Shopify pages or written fresh. Also needs a human
call: what to do with Steth-of-Hope and funds raised for Eric, and whether the
currency label becomes a real multi-currency selector.

### Thin pages rebuilt from the rendered Shopify site (2026-11-15)

Shopify's Admin API returns an **empty `body`** for section-built pages (Meet the
Team, FAQs, Glia X Rajaeen, Run For Palestine, Medical Solidarity, Restoring
Care) — their content lives in theme sections. `scrape_pages.py` therefore
rebuilds them from the rendered storefront page:

| page | before | after |
|---|---|---|
| `/meet-the-team` | 13 chars | 21 people with portraits, Canada/Gaza groups |
| `/faqs` | 4 chars | full FAQ incl. donation-methods PDF copied to `/files` |
| `/glia-x-rajaeen` | 14 chars | partnership copy + 3 images |
| `/restoring-care-in-gaza` | 236 chars | 10k chars of campaign copy + progress figures |
| `/glia4gaza` | 0 chars of body copy | 7k chars + own donate CTAs |
| `/run-for-palestine-2026` | 90 chars | event details + donate CTAs |

How it works: keep only semantic content inside `<main>`, lift lazyloaded
`<noscript>`/`data-bgset` images, upload every Shopify CDN image and document to
ERPNext `/files`, rewrite `/pages/*`, `/products/*`, `/collections/*` and
`/blogs/*` onto the new routes, strip Shopify's donation-app widgets and inject
CTA links to our own flows. The duplicate `/the-glia-x-rajaeen-collection` page
was unpublished and 301'd to `/glia-x-rajaeen`.

### SEO redirects — corrected (2026-11-15)

The earlier "125/125 return 301" claim was measured without following the
redirect. Targets were stored relative, and Frappe interpolates them verbatim,
so nested paths looped (50 hops) or landed on 404s. Targets are now normalised to
a leading slash (`glia_ops` v0.1.7 + a migration of the 139 existing rows), the
truncated press-release route got its own hop, and the honest check — follow to
the end — gives **125/125 final 200, one hop, zero loops**. Frappe caches
redirects in-process, so bulk edits need a `gunicorn` restart.

### Visual regressions measured against the live site (2026-11-15)

Visual claims were measured rather than eyeballed: `measure_visual.py` loads both
sites in headless Chromium at 1440x900 and compares computed font sizes, element
boxes, clipping, scrollbars and button styles.

| finding | before | now | live |
|---|---|---|---|
| titles cut off | 3/12 home h2 clipped (51.8px text in 46px box) | 0 clipped | 0 |
| fonts smaller | hero 17 / body 17 / article 16 / product 13 / press 14 px | 20 / 18 / 17 / 16 px | 20 / 16-17 |
| logo smaller | 64px, 82px header | 100px, 112px header | ~100px, ~112px |
| selected buttons solid | grid/list toggle solid grey + white text | cream + teal outline, unselected transparent | outlined |
| scrollbars always visible | empty `#product-scroll` with inline `overflow:scroll` | 0 useless scroll containers | 0 |
| contact form | Send 51x29 black chip | 530x61 cream button, 19px, white fields | 540x61, 20px |
| article column | centred, 980px cover | left-aligned 780px (live 793) | left-aligned + sidebar |

Also found and fixed while measuring: the homepage had **no `<h1>`**, the shop
listing rendered its heading as a `<div>` (now a real `<h1>`), and
`/press-releases` + `/pages/press` 404'd — both now 301 to the press-release list.

Not closed: the live article page has a right-hand sidebar (tag list + "Read
more"); ours does not. That is template work, not CSS.

### Cutover plumbing (2026-11-15, later)

- **robots.txt** was a 0-byte 200 response (the `Website Settings.robots_txt`
  field was empty). It now disallows `/desk`, `/app`, `/api`, `/login`,
  `/guest_cart`, `/guest_order`, `/search` and points at `/sitemap.xml`
  (86 URLs, all sampled ones 200).
- **Newsletter consent (CASL):** the footer posted `consent=1` implicitly. It now
  has an explicit checkbox with a privacy-policy link, validated client side
  *and* server side (`subscribe_newsletter` refuses without consent). A
  `/privacy-policy` page was written (there was none on Shopify either).
- **Readiness check now verifies redirects by following them.** Presence was
  never proof — that is how a looping redirect passed as healthy. It samples
  product/page/collection/blog sources, follows up to 6 hops with urllib's
  auto-redirect disabled, and requires a final 200 in at most three hops. Pages
  that exist only on the new site (`privacy-policy`) no longer count as gaps.
- **Article sidebar** (categories + "Read more") rendered client-side from
  `glia_ops.api.related_posts`, because the article template is Frappe's own.
- **Tests:** `bench run-tests --app ...` for all three apps (redirect normaliser,
  donation-item classification incl. the Gray Area trap, donation amount →
  variant mapping). The redirect test caught a real bug: `absolute()` was
  referenced by the generator but never defined, so `generate_redirects` would
  have raised `NameError`.
- **Restore drill:** newest backup was pulled back from the **dedicated** Spaces
  bucket, archives verified, and the dump restored into a scratch database:
  4,275 customers, 14,927 contacts, 17,200 donations, 580 sales orders, 29
  website items, 38 blog posts, 83 web pages — matching production. Scratch DB
  dropped afterwards.

### Sales tax — DONE (2026-11-15)

Tax was the last cutover blocker and is now configured and proven:

* **Rates** (verified against CRA/2026 references): ON 13% HST, NS 14% HST (cut from
  15% on 2025-04-01), NB/NL/PE 15% HST, AB/YT/NT/NU 5% GST, BC 5%+7% PST,
  MB 5%+7% RST, SK 5%+6% PST, QC 5%+9.975% QST, exports 0%. The pre-existing
  templates were wrong for the two busiest provinces (no 13% or 14% at all).
* **Mechanism**: ERPNext cannot vary rates by destination *and* exempt individual
  lines in one document, so the guest checkout resolves the destination template
  itself (`glia_guest_checkout.taxes`), requires a province for Canadian orders,
  and refuses to mix merchandise with donations. Donations stay untaxed; a
  donation order strips any template inherited from the cart quotation (found a
  50 gift being billed 56.50 before the fix).
* **Data**: 8 rate templates, 3 new tax accounts (QST 9.975, RST 7, PST 6),
  13 province Tax Rules, BN `807831391` on the company. No template is a company
  default — a default leaked onto quotations and therefore donations.
* **Checkout form**: province became a required selector (13 provinces), with a
  client-side guard and a note that tax is calculated at checkout.
* **Proof**: $40 stethoscope → 45.20 ON, 45.60 NS, 44.80 BC, 45.99 QC, 40.00 US;
  $50 donation → 50.00. Tests: 11 in `glia_guest_checkout` (mapping, taxability,
  module imports) plus 4 in `glia_ops`.

Open items: confirm the **RT** suffix of the BN for invoice printing (the folder
shows the RC account), and shipping is taxable once shipping charges exist
(0 Shipping Rules today).

### Recurring gifts — DONE (2026-11-15)

Monthly donations bill through Stripe Billing via a second app,
`glia_donations` (<https://github.com/GliaX/glia-donations>, tag `v0.1.3`):

- Doctypes `Recurring Donation` (status/Stripe ids/totals/cancel token) and
  `Donation Settings` (single). The app only *adds* fields to the DB-level
  `Donation` doctype (`stripe_invoice_id`, `stripe_payment_intent`,
  `recurring_donation`), relaxes the Shopify-only required fields and adds the
  `Website` source — it never redefines `Donation`.
- `POST glia_donations.api.start_recurring_donation` → hosted Stripe Checkout
  (`mode=subscription`), price reused by lookup key `glia_monthly_cad_<cents>`.
- `POST glia_donations.webhooks.stripe_webhook` (signed; endpoint
  `we_1Ubx4lEYuMS0KEULfw5VLirp`): checkout completed → Active, `invoice.paid` →
  `Donation`, payment failed → Past Due, subscription deleted → Cancelled.
  Idempotent on `stripe_invoice_id`.
- `/recurring_donation?token=…` → donor sees status/totals and can cancel
  (immediate Stripe cancellation). Receipts queue to the worker (an SMTP failure
  must not fail the webhook — it once did, hence `enqueue_after_commit`).
- **The `Stripe Settings` record did not exist at all**, so one-off checkout had
  no credentials; it now holds the live keys (record name `Stripe`, autonamed on
  gateway_name — that is what the ERPNext gateway looks up).
- Verified: full webhook lifecycle with signed events, bad signature → 401, a
  live subscription Checkout session created then expired (no charge), and
  `/stripe_checkout` now renders with `pk_live_…`.
- Still open: one-off cart donations create only a Sales Order (gap: Sales Order
  → `Donation` hook); no ledger/Payment Entry booking for recurring charges.

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
