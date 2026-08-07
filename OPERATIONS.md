# Glia Donation Sync — Operations Document

> **Last updated:** 2026-07-31  
> **Status:** Shopify sync operational (every 2h). Patreon sync operational (daily). Both backfilled. Thank-you email Notification configured (needs SMTP). CRM UI functional.

---

## 1. System Architecture

```
Shopify (glia2.myshopify.com)                        Patreon (patreon.com/campaigns/2407963)
  │                                                   │
  ├─ Dev Dashboard custom app                          ├─ Creator API client (v1+v2)
  │  Client ID + Secret → 24h token                   │  Creator's Access Token (refreshable)
  │                                                   │
  ▼                                                   ▼
glia-shopify-erpnext (Python, k8s CronJobs)
  ├─ Shopify sync: every 2h (glia-sync CronJob)
  ├─ Patreon sync: daily 06:00 UTC (glia-patreon-sync CronJob)
  │
  ▼
ERPNext 16.30 / Frappe 16.29 / Frappe CRM 1.80
  asset.glia.org (k8s, DigitalOcean)
  ├─ Contact (donor records — core Frappe doctype, CRM UI)
  ├─ Donation (custom doctype, module "Glia")
  └─ Notification "Donation Thank You" (fires on Donation create)
```

### Repos
| Repo | Visibility | Purpose |
|---|---|---|
| `GliaX/glia-shopify-erpnext` | private | Sync code + Dockerfile + CI (builds `ghcr.io/gliax/sync`) |
| `GliaX/helm-erpnext` | private | ERPNext helm-as-code + CronJob manifests + secrets templates |

### Key files
- **glia-shopify-erpnext:**
  - `.env` — secrets (Shopify client ID/secret, Patreon tokens, ERPNext API key/secret). **NEVER commit.**
  - `config.yaml` — donation product allow-list (17 IDs), recurring set, tag skip-list, tip mode. Copied from `config.example.yaml`.
  - `src/glia_shopify_sync/` — all source.
  - `Dockerfile` — builds the sync container image.
  - `.github/workflows/build-sync-image.yml` — CI → `ghcr.io/gliax/sync:latest`.
- **helm-erpnext:**
  - `values.yaml` — ERPNext helm config (image: `ghcr.io/gliax/erpnext:v16.30.0-crm-v1.80.0-fix`).
  - `values.pin.yaml` — **REQUIRED** node-pin overlay (RWO PVC constraint).
  - `values.secret.yaml` — DB passwords (gitignored).
  - `Dockerfile` — ERPNext+CRM custom image.
  - `sync/cronjob.yaml` — Shopify 2h CronJob.
  - `sync/patreon-cronjob.yaml` — Patreon daily CronJob.
  - `sync/secret.example.yaml` — template for the k8s Secret.

---

## 2. ERPNext / Frappe CRM Infrastructure

### Versions
- **ERPNext:** 16.30.0 (upgraded from 16.4.1 on 2026-07-29)
- **Frappe Framework:** 16.29.0 (bundled in the image)
- **Frappe CRM:** 1.80.0 (from `tareko/crm:fix-assign-to-v1.x` fork, pending upstream PR #2581)
- **Chart:** upstream `frappe/erpnext` 8.0.21 (NOT upgraded to 8.0.69 — that restructures MariaDB templates)
- **Image:** `ghcr.io/gliax/erpnext:v16.30.0-crm-v1.80.0-fix` (public on ghcr.io)

### Deployment specifics
- **Cluster:** DigitalOcean k8s, namespace `erpnext`.
- **Site DB:** `_a4f5d93d31b414b8` (hash-named, on the MariaDB statefulset PVC).
- **Sites PVC:** `erpnext` (RWO, 8Gi, `do-block-storage`), attached to node `pool-f971mwbjk-372vst`.
- **Node pin (`values.pin.yaml`):** ALL erpnext pods pinned to node `pool-f971mwbjk-372vst` via `nodeSelector`. **CRITICAL** — the RWO sites PVC + the chart's default topology-spread cause Multi-Attach deadlocks without this pin. Never deploy without `values.pin.yaml`.
- **MariaDB:** in-cluster statefulset (NOT the managed DO DB — that's configured-but-disabled in values.yaml).

### CRM-specific fixes applied
1. **`assign_to._add` fix** (PR frappe/crm#2581): CRM Lead/Deal imported the private `_add` (removed in Frappe 16.5.0, restored in 16.29). Our fork uses the public `add()`. Merged on `tareko/crm:fix-assign-to-v1.x`.
2. **CRM frontend assets:** the `sites/assets/crm` symlink was missing from the PVC (the chart's setup job only created frappe/erpnext symlinks). Created manually on the PVC via nginx. **If CRM is reinstalled, run `bench setup assets` on the PVC.**
3. **Frappe CRM `CRM Contacts` is a CHILD TABLE** (istable=1), not a top-level doctype. Frappe CRM uses the **core `Contact`** doctype for people. Donors go into `Contact`, not `CRM Contacts`.

### Deploying helm changes
```bash
cd /home/orangey/workspace/helm-erpnext
helm repo update
helm -n erpnext upgrade erpnext frappe/erpnext --version 8.0.21 \
  -f values.yaml -f values.secret.yaml -f values.pin.yaml
# ALWAYS include values.pin.yaml or the rollout will deadlock.
```

---

## 3. The Donation Doctype

Custom doctype (module: `Glia`, `issingle: 0`, `custom: 1`). Created by `glia-sync-setup-erpnext` CLI.

### Fields (17)
| Field | Type | Notes |
|---|---|---|
| `contact` | Link→Contact | **Required**. The donor. |
| `source` | Select (Shopify/Patreon) | Origin platform. Filterable in list view. |
| `donor_name` | Data | Label like "#6074 · Glia4Gaza". |
| `donor_email` | Data, `options=Email` | Denormalized from Contact. Used by the Notification. **Must be `Email` (capital)** for the Notification dropdown. |
| `donation_date` | Date | **Required**. |
| `amount` | Currency | Shop money (CAD for Shopify, USD for Patreon). |
| `currency` | Link→Currency | |
| `amount_presentment` | Currency | Donor's original currency. |
| `currency_presentment` | Link→Currency | |
| `donation_type` | Select (One-time/Recurring) | |
| `campaign` | Data | Shopify product title or Patreon tier. |
| `tier` | Data | Variant title. |
| `includes_tip` | Check | Shopify tip folded into amount. |
| `financial_status` | Data | PAID / Declined / etc. |
| `shopify_order_id` | Data, `set_only_once` | Dedup key part 1. **NOT unique** (orders can have multiple donation lines). |
| `shopify_order_name` | Data | Human-readable (#6074, Patreon 2026-07). |
| `shopify_line_item_id` | Data, `set_only_once` | Dedup key part 2. |

### Role permissions
- System Manager: full (read/write/create/delete/share/set_user_permissions).
- Sales Manager: full (read/write/create/delete/share).

### Dedup key
`shopify_order_id|shopify_line_item_id` (composite). NOT a DB unique constraint (there's NO unique index — see pitfalls). Dedup is handled in the sync's in-memory set.

### The `Glia` Module
- Module Def: `Glia`, `app_name: "frappe"` (mandatory on newer Frappe for custom modules).
- Contains: `Donation` doctype, `Glia Sync State` singleton.

### The `Glia Sync State` singleton
- `issingle: 1`. Stores `last_processed_at` (the incremental cursor for the daily Shopify sync).
- Read/written by `FrappeState` class (stateless pod — no PVC needed for the CronJob).
- Fields: `last_processed_at` (Data).

---

## 4. Shopify Sync

### Auth (post-Jan 2026 changes)
- Custom apps are created in the **Dev Dashboard** (`dev.shopify.com`), not the store admin.
- **No static access token.** Client ID + Secret → 24h access token via OAuth client-credentials grant.
- Token minted at runtime by `TokenManager` (auto-refreshes before expiry).
- Scopes needed: `read_products`, `read_orders`, `read_all_orders` (for orders older than 60 days), `read_customers`.
- App URL: `https://shopify.dev/apps/default-app-home` (placeholder).

### Data model
- **Source:** Shopify Orders (GraphQL Admin API), filtered by a curated allow-list of 17 donation product IDs.
- **Donor:** `Contact` (dedup by `shopify_customer_id` custom field on Contact).
- **Donation:** one per donation-product line item (orders can have multiple).
- **Tip handling:** tip line items (`product: null`, name "Tip") are folded into the first donation's amount (`tip_mode: fold`).
- **Currency:** dual — `amount`/`currency` = shop money (CAD); `amount_presentment`/`currency_presentment` = donor's original currency.
- **Tags:** product tags + order tags (combined, deduped, minus skip-list `Donate/Contribute/Campaigns`) applied as native ERPNext tags.

### Donation product allow-list (config.yaml)
17 product IDs. The store has ~50 products total (merch, services, devices, education) — only donation products are synced. See `config.example.yaml` for the full list + the 3 explicitly-excluded products (Mobilizing Innovation course, Oakville admission, Glia Gift Card).

### Recurring detection
Products with selling plans (Shopify Subscriptions) are flagged `donation_type=Recurring`. The recurring product IDs are in `recurring_product_ids` in config.yaml. Each billing cycle creates a new Shopify Order → one Donation per cycle.

### CLIs
| Command | Purpose |
|---|---|
| `glia-sync-setup-erpnext` | Create Glia module, Contact.shopify_customer_id custom field, Donation doctype, Glia Sync State singleton. Idempotent. |
| `glia-sync-doctor [--with-write-test] [--no-shopify]` | Read-only health checks + optional write test. |
| `glia-sync-send-test [--keep]` | Push a synthetic Contact+Donation, then delete (unless `--keep`). |
| `glia-sync-backfill [--since YYYY] [--dry-run] [--limit N]` | Full/partial historical sync. Resumable (uses state cursor). |
| `glia-sync-daily [--dry-run]` | Incremental sync (uses Glia Sync State cursor). This is what the CronJob runs. |
| `glia-sync-patreon [--dry-run]` | Patreon members sync (see §5). |

### CronJob
- Name: `glia-sync`, namespace `erpnext`.
- Schedule: `0 */2 * * *` (every 2 hours).
- Image: `ghcr.io/gliax/sync:latest`.
- Secret: `glia-sync-secrets` (contains SHOPIFY_*, ERPNEXT_*, PATREON_* vars).
- Stateless (cursor in ERPNext `Glia Sync State` singleton).

---

## 5. Patreon Sync

### Auth
- **Creator's Access Token** + **Creator's Refresh Token** (from the Patreon developer portal).
- The access token is a long-lived bearer token. If it expires (401), the client auto-refreshes using the refresh token.
- No OAuth user flow needed (server-to-server).

### API strategy: v1 for emails, v2 for data
- **v2** `GET /campaigns/{id}/members` → detailed member data (amounts, charge status, lifetime_support_cents, pledge start, patron_status, tiers). **Does NOT return member emails** (a known v2 limitation — the `campaigns.members[email]` scope doesn't work even with the Creator's token).
- **v1** `GET /api/campaigns/{id}/pledges` → **returns user emails** (via included user resources). Less detailed pledge data.
- **Solution:** `PatreonClient.fetch_user_emails()` calls v1 → builds `{user_id: email}` map. The sync cross-references v2 member `relationships.user.data.id` against this map to enrich each member with their email.
- **Caveat:** v1 only returns pledges for patrons with active/recent pledge records. Former patrons (status=`former_patron`) whose data was purged by Patreon have NO email available from either API. 13 of 38 Patreon patrons have emails; the other 25 are former patrons without emails.

### Data model
- **Backfill:** one lifetime Donation per member (`amount = lifetime_support_cents / 100`, dated at `pledge_relationship_start`, dedup key `patreon:{member_id}:lifetime`).
- **Ongoing monthly:** one Donation per active patron's latest charge (`amount = currently_entitled_amount_cents / 100`, dated at `last_charge_date`, dedup key `patreon:{member_id}:{date}`). Re-running detects new charge dates automatically.
- **Source:** `Patreon` on all Patreon donations.
- **Tagged:** `Patreon` (native ERPNext tag).
- **Currency:** USD (Patreon default).
- **Donor:** `Contact` (dedup by `shopify_customer_id = patreon:{member_id}` — reusing the existing custom field).

### Campaign
- ID: `2407963` (auto-fetched via `GET /campaigns` if not set in config).
- 40 active patrons, 58 total members (including former/free).

### CronJob
- Name: `glia-patreon-sync`, namespace `erpnext`.
- Schedule: `0 6 * * *` (daily 06:00 UTC).
- Command: `glia-sync-patreon` (overrides the image's default `glia-sync-daily` entrypoint).
- Same image + Secret as the Shopify CronJob.

---

## 6. Thank-You Email Notification

### Setup
- **Notification:** "Donation Thank You" (`/app/notification/Donation Thank You`).
- **Event:** `New` (fires on Donation creation — both Shopify and Patreon).
- **Channel:** Email.
- **Recipient:** `donor_email` (document field; set on the Donation doctype with `options=Email` so it appears in the dropdown).
- **Body:** Jinja template (editable in the Notification's `message` field). Variables: `{{ doc.donor_name }}`, `{{ doc.amount }}`, `{{ doc.currency }}`, `{{ doc.campaign }}`, `{{ doc.donation_type }}`, `{{ doc.donation_date }}`, `{{ doc.source }}`.

### ⚠️ Prerequisite: outbound Email Account NOT configured
- **No Email Account with SMTP is configured** on `asset.glia.org`.
- The Notification fires correctly (creates a `Communication` record), but emails are **not sent** — they sit unsent because there's no outbound mail server.
- **To enable delivery:** add an Email Account (`/app/email-account`) with SMTP credentials → enable outgoing. The scheduler will then send queued emails.
- The Notification's `receiver_by_document_field` dropdown only shows fields with `options=Email` or Links to User/Customer. That's why `donor_email` needed `options=Email` (capital E) to appear.

---

## 7. Current Data State (as of 2026-07-31)

| Metric | Count |
|---|---|
| Total Donations | ~4,081 |
| ├─ Shopify | 4,031 (source=Shopify) |
| └─ Patreon | 50 (source=Patreon) |
| Total Contacts (donors) | ~3,224 (3,186 Shopify + 38 Patreon) |
| Donations with donor_email | ~4,054 (4,031 Shopify + 23 Patreon with email) |
| Donations tagged | ~2,792 (product + order campaign tags) |
| Contacts with shopify_customer_id | ~3,186 (Shopify) + 38 (patreon: prefix) |

---

## 8. Pitfalls & Gotchas (learned the hard way)

### Frappe / ERPNext
1. **`limit_page_start` (offset) is silently ignored** by Frappe's REST API. `get_list` must fetch in a single large page (`limit_page_length=100000`), not paginate by offset. Pagination by offset causes infinite loops on large tables.
2. **`str(filters)` breaks Frappe's JSON parser.** Filters must be `json.dumps()`'d, not Python `str()`. Same for `fields`.
3. **`_data()` envelope:** resource endpoints wrap as `{"data": ...}`; RPC method endpoints (`frappe.client.*`) wrap as `{"message": ...}`. Both must be unwrapped.
4. **Custom DocType creation via REST:** `Module Def` requires `app_name` (mandatory in newer Frappe). Custom modules use `app_name="frappe"`.
5. **DocType role permissions:** custom doctypes created via REST get **zero** role permissions by default. Must explicitly add System Manager + Sales Manager perms in the doctype def. Without them, only Administrator can create records.
6. **`shopify_order_id` must NOT be unique:** orders with multiple donation line items share an order ID. The unique constraint caused 154 duplicate-entry errors during backfill. Removed both the field property (`unique=0`) AND the DB index (`ALTER TABLE DROP INDEX`). **If the doctype is ever re-saved (e.g., via setup), verify the index hasn't been re-added.**
7. **CRM frontend assets:** `sites/assets/crm` symlink must exist on the PVC for `/crm` to render. The chart's setup job only creates frappe/erpnext symlinks. Missing symlink → blank `/crm` page (JS bundle 404s). Fix: `ln -s apps/crm/crm/public sites/assets/crm` on the PVC (via nginx pod, which mounts it).
8. **Notification recipient dropdown:** only shows fields with `options=Email` (capital E) or Link→User/Customer. Plain Data fields don't appear. "owner" is hard-coded as a universal fallback.
9. **The `disabled` field on Notification:** appears as `None` in the API even after setting to 0. None ≈ enabled (falsy). The Notification fires correctly.
10. **`CRM Contacts` is a child table** (istable=1). Don't try to insert it standalone — it requires a parent. Use core `Contact` for donors.
11. **System Health Report TypeError:** Frappe's `system_health_report.fetch_storage_details` throws `TypeError: NoneType / int` on k8s (disk stats quirk). Harmless but noisy in logs.
12. **Frappe cache:** newly created Notifications may not fire until `bench clear-cache` is run. Always clear cache after creating/modifying Notifications, doctype fields, or custom fields.

### Shopify
1. **Custom apps moved to Dev Dashboard** (Jan 2026). No static access token — use client-credentials grant for a 24h token.
2. **`read_all_orders` scope** is required for orders older than 60 days. Without it, backfill misses historical orders.
3. **`country_code`** doesn't exist on `MailingAddress` in API 2025-07. Removed from the GraphQL query.
4. **GraphQL field errors return HTTP 200** with an `errors` array. The client must check for errors in the response body.
5. **GraphQL cost throttling:** `throttleStatus.currentlyAvailable` and `restoreRate` in the `extensions.cost` response. If budget < requested cost, sleep `(cost - budget) / restoreRate` seconds.

### Patreon
1. **v2 API does NOT return member emails.** The `campaigns.members[email]` scope doesn't work even with the Creator's Access Token. Use the v1 `/api/campaigns/{id}/pledges` endpoint for emails.
2. **v1 user IDs match v2 user IDs** (both numeric). Cross-reference by `relationships.user.data.id`.
3. **v1 only returns pledges** (not all members). Former patrons whose data was purged have no email in either API.
4. **Token refresh:** the Creator's Access Token may expire. On 401, refresh via `POST /api/oauth2/token` with `grant_type=refresh_token`.
5. **Rate limits:** 100 req/2s (client), 100 req/min (token). Edge limit: 2000 bad requests in 10 min → 30 min block. Set a descriptive `User-Agent` header or calls may be dropped with 403.

### Kubernetes / helm
1. **Node pin is MANDATORY.** The RWO sites PVC + topology-spread cause Multi-Attach deadlocks. All erpnext pods must be pinned to the PVC's node (`pool-f971mwbjk-372vst`) via `values.pin.yaml`. Never deploy without it.
2. **Cluster autoscaler adds nodes** during rollouts, scattering pods. The node pin prevents this.
3. **`bench build` needs node.js** — the ERPNext runtime image has none. Can't build CRM frontend assets in-pod. Assets must be on the PVC (created once via nginx pod).
4. **Chart 8.0.69 restructures MariaDB templates** — upgrading the chart is deferred to avoid disrupting the running MariaDB.
5. **The `createSite` job is idempotent** (skips if site exists). Safe to leave enabled.
6. **Backups:** `bench --site asset.glia.org backup --with-files` produces a DB + files dump on the sites PVC. Copy off-cluster with `kubectl cp`. Also: DigitalOcean volume snapshots of the MariaDB PVC.

---

## 9. Things NOT Yet Done (Future Work)

### Email delivery
- **Email Account / SMTP not configured.** The "Donation Thank You" Notification fires but emails don't send. Add an Email Account (`/app/email-account`) with SMTP credentials.

### Frappe CRM PR
- PR [frappe/crm#2581](https://github.com/frappe/crm/pull/2581) (assign_to fix) is **open on `develop`** (maintainer asked to retarget from `main`). Once merged + released, the custom image can switch to vanilla `frappe/crm` (drop the fork).

### ERPNext chart upgrade
- Chart is pinned to 8.0.21. Upgrading to 8.0.69 would bring chart improvements but restructures MariaDB templates — needs careful testing (backup first, possible brief downtime).

### Cross-platform donor dedup
- Shopify and Patreon donors are deduped within their platform (by platform-specific IDs). Cross-platform merge (same person on both) is **not automatic** — a patron on both platforms gets two Contacts. Could be improved by matching on email (now available for both platforms). Manual merge is possible in the CRM UI.

### Webhooks
- Both Shopify and Patreon support webhooks (real-time). Currently using polling (CronJob every 2h / daily). Webhooks would give near-real-time sync. Shopify webhook setup: add `read_subscriptions` scope + configure webhooks via API. Patreon: `POST /api/oauth2/v2/webhooks` with triggers `members:pledge:create/update`.

### Donation PDF receipts
- ERPNext `Print Format` on Donation could generate a PDF receipt attached to the thank-you email. Not yet created.

### Glia Sync State for Patreon
- The Patreon sync doesn't use the `Glia Sync State` singleton (it relies on dedup keys). Could add a Patreon-specific cursor for efficiency (avoid re-scanning all members if none changed).

### Workspace / sidebar shortcut
- The Donation doctype doesn't appear in the CRM sidebar (it's under the Glia module). A CRM Workspace shortcut or menu item would make it more discoverable.

---

## 10. Backup & Recovery

### Backup locations
- `/home/orangey/backups/erpnext-20260728/` — pre-CRM (v16.4.1, before any CRM work).
- `/home/orangey/backups/erpnext-pre-crm/` — before CRM install.
- `/home/orangey/backups/erpnext-pre-v16.30/` — before the v16.30 upgrade.
- On the sites PVC: `sites/asset.glia.org/private/backups/` — `bench backup` dumps.

### Restore procedure
```bash
# Restore DB (on the gunicorn pod)
kubectl -n erpnext exec deploy/erpnext-gunicorn -- bash -lc \
  'cd /home/frappe/frappe-bench && bench --site asset.glia.org restore /path/to/dump.sql.gz'

# Or from a DO volume snapshot: snapshot the data-erpnext-mariadb-sts-0 PVC.
```

---

## 11. Emergency Contacts & Key Commands

### Check sync health
```bash
# Shopify CronJob
kubectl -n erpnext get cronjob glia-sync
kubectl -n erpnext logs job/<latest-sync-job>

# Patreon CronJob
kubectl -n erpnext get cronjob glia-patreon-sync
kubectl -n erpnext logs job/<latest-patreon-job>

# Donation counts by source
kubectl -n erpnext exec erpnext-mariadb-sts-0 -- mysql -uroot -p<PW> _a4f5d93d31b414b8 \
  -e "SELECT source, COUNT(*) FROM tabDonation GROUP BY source;"
```

### Manual sync trigger
```bash
kubectl -n erpnext create job --from=cronjob/glia-sync manual-shopify-$(date +%s)
kubectl -n erpnext create job --from=cronjob/glia-patreon-sync manual-patreon-$(date +%s)
```

### Rollback ERPNext
```bash
helm -n erpnext rollback erpnext <revision-number>
helm -n erpnext history erpnext
```

### Clear cache
```bash
kubectl -n erpnext exec deploy/erpnext-gunicorn -- bash -lc \
  'cd /home/frappe/frappe-bench && bench --site asset.glia.org clear-cache'
```

### Local development
```bash
cd /home/orangey/workspace/glia-shopify-erpnext
. .venv/bin/activate
pytest -q                    # 99 tests (donation sync + shop migration)
ruff check src tests         # lint
mypy                         # type check
glia-sync-doctor             # health check (reads .env)
glia-sync-backfill --dry-run --limit 10  # preview
```

---

## 12. Shop Migration (Shopify → ERPNext E Commerce)

Recreating the Shopify storefront on ERPNext's E Commerce module. The migration
CLIs live in `src/glia_shopify_sync/shop/`; architecture & phase tracker are in
`AGENTS.md` ("Shop migration"). This section is the **ops runbook**.

### 12.1 What's installed (custom image)
`asset.glia.org` originally shipped with E Commerce stripped out. The custom
image (`GliaX/helm-erpnext` Dockerfile, tag `v16.30.0-crm-v1.80.0-ecommerce2`)
bundles three apps on top of `frappe/erpnext:v16.30.0`:
- **`frappe/payments`** (version-16) — payment plumbing; webshop depends on it.
- **`frappe/webshop`** (version-16) — E Commerce app (`Website Item`,
  `Webshop Settings`, Shopping Cart).
- **Frappe CRM** (the Glia fork) — unchanged.

Apps are in the image (not on a PVC), so **enabling E Commerce required an image
rebuild**, not an in-place `bench install-app` (apps/ is ephemeral on this chart;
only `sites/` is persistent). The Shopify app grants every read scope the
migration needs (`read_inventory`, `read_locations`, `read_fulfillments`,
`read_shipping`).

### 12.2 Deploying an image change (read every time)
```bash
cd /home/orangey/workspace/helm-erpnext
git commit -am "..." && git push                             # CI builds ghcr.io/gliax/erpnext
# (or build locally if GitHub Actions runners are down:)
DOCKER_BUILDKIT=1 docker build --build-arg CRM_REPO=... -t ghcr.io/gliax/erpnext:<tag> . \
  && docker push ghcr.io/gliax/erpnext:<tag>                 # token needs write:packages
# bump values.yaml image.tag, then:
helm -n erpnext upgrade erpnext frappe/erpnext --version 8.0.21 \
  -f values.yaml -f values.secret.yaml -f values.pin.yaml    # ALWAYS include values.pin.yaml
kubectl -n erpnext rollout status deploy/erpnext-gunicorn
```
**Always take a DB backup first** (§10) — the migration writes to production.

### 12.3 Three asset gotchas (the storefront goes blank if you miss one)
The webshop frontend wouldn't render until all three were fixed in the Dockerfile:
1. **Register apps in `sites/apps.txt`** before `bench build` — `bench get-app`
   doesn't always do this in a siteless image build, so `bench build` skips them.
2. **Rebuild the manifest from `dist/`** after `bench build`. `bench build`
   recompiles bundle files (new content hashes) but does NOT keep
   `assets/assets.json` in sync — so manifest entries point at non-existent
   old-hash files (frappe/erpnext CSS 404s) and webshop's bundles go
   unregistered. The Dockerfile scans `assets/*/dist/{js,css}/*.bundle.*` and
   remaps every `<name>.bundle.{js,css}` to its real hashed file.
3. **Flush valkey-cache after every asset-changing deploy.** `get_assets_json()`
   caches the manifest in valkey-cache (`client_cache`, key `assets_json`), and
   `bench clear-cache` does NOT flush it. After each such deploy:
   ```bash
   kubectl -n erpnext rollout restart deploy/erpnext-valkey-cache
   ```
   Symptom if skipped: `/all-products` renders blank or CSS 404s.

### 12.4 Phase 1 — Catalog sync
```bash
glia-shop-setup                  # one-time schema prep (custom fields, Price List, Item Groups) — WRITES, backup first
glia-shop-catalog-sync --dry-run --limit 5    # read-only Shopify preview
glia-shop-catalog-sync                         # import (idempotent, resumable)
```
State (2026-08-06): 29 active products → 200 Items (15 templates + 171 variants
+ 14 simples), 189 prices, 29 Website Items (23 with images), 4 donation items.
1 degraded (`Stethoscope` — pre-existing non-template, linked, variants skipped).

Gotchas: Item Group root is `All Item Groups` (config: `shop.item_group_parent`);
existing `Donation` group is singular (`shop.item_group_donations: "Donation"`).
Pre-existing manufacturing `Size`/`Color` attributes use spelled-out values;
Shopify uses abbreviations + mixed case — handled by parse-time normalization
(`_norm_option`) + case-insensitive attribute dedup + collision-safe abbreviations.
`website_image` can't be set as a bare URL (ERPNext blanks it) — a `File` doc
referencing the Shopify URL is created per image (`_ensure_website_image`).

### 12.5 Phase 3 — Customer sync
```bash
glia-shop-customer-sync --dry-run          # preview (customers-with-orders only)
glia-shop-customer-sync                    # import (default: only orders_count > 0)
glia-shop-customer-sync --all-customers    # include ~16k zero-order accounts
```
State: 4,265 customers-with-orders → ERPNext Customers + Shipping Addresses.
Dedup via `Customer.shopify_customer_id`; donor `Contact`s (3,271) coexist —
Customer↔Contact linking is deferred to the order phase. Default filter
`orders_count:>0` skips zero-order noise; `--all-customers` includes everyone.

### 12.6 Phase 5 — Checkout + Stripe
```bash
# 1. Put Stripe keys in .env (gitignored): STRIPE_PUBLISHABLE_KEY, STRIPE_SECRET_KEY
#    (optional: STRIPE_WEBHOOK_SECRET for reconciliation)
# 2. Configure everything:
glia-shop-checkout-setup
```
Creates `Payment Gateway` (Stripe) + `Stripe Settings` (keys) + `Payment Gateway
Account` (`Stripe - CAD - Glia` → `03-743-20 … Chequing`), enables
`Webshop Settings.enable_checkout`, and wires the payment gateway account.
Idempotent — safe to re-run (skips Stripe if keys absent, so run setup before
keys, then again after).

**Stripe test (2026-08-06, live keys):** account `acct_1EXkQFEYuMS0KEUL` (CA),
`charges_enabled=True`; created a live Checkout Session `cs_live_…` ($1.00 CAD)
which returned a working `checkout.stripe.com` payment URL. Integration
confirmed end-to-end. ⚠️ Keys are **live** (`pk_live`/`sk_live`) — any checkout
test processes a real charge. For no-risk testing, swap in `pk_test`/`sk_test`
keys and re-run setup, then use card `4242 4242 4242 4242`.

### 12.7 Phase 4 — Order history backfill
```bash
glia-shop-order-sync --dry-run --limit 50    # preview (paid orders only)
glia-shop-order-sync                          # backfill paid shop orders (idempotent)
glia-shop-order-sync --all                    # include unpaid/non-paid
```
State (2026-08-06): **580 Sales Orders** (CAD $139,219.67), spanning FY 2019–2026.
Only **non-donation** line items are placed on a Sales Order (donation orders are
already `Donation` records); pure-donation orders and orders for un-imported
(archived) products are skipped ("no shop items"). Customer is the migrated
`Customer` (Phase 3); guest orders use the `Shopify Guest` customer. Payment
Entry creation is deferred (the Sales Order is the order record).

Gotchas (all hit + fixed during the live backfill):
- The company default warehouse `Stores - Glia` is **disabled** → Sales Order
  items must set a valid warehouse (`shop.warehouse = "Finished Goods - Canada - Glia"`).
- A template Item (has_variants) **can't be sold** on a Sales Order — only its
  variants. `load_item_code_map` keys the product-id fallback to simple items
  only, so an order whose variant wasn't imported is skipped (not errored).
- ERPNext rejects posting dates outside an **active Fiscal Year**. The Glia site
  only had FY 2024 + 2026; the backfill created **2015–2025** to cover order
  history. Re-run after adding a missing FY if old orders error with
  `FiscalYearError`.
- Shopify's GraphQL `financial_status` query filter is unreliable → paid status
  is filtered **client-side** (the donation sync uses the same approach).
