# Container image for the Shopify -> ERPNext donation sync (daily CronJob).
# Stateless: the incremental cursor lives in ERPNext (Glia Sync State), so no
# PVC is needed. Secrets (SHOPIFY_*, ERPNEXT_*) come from a k8s Secret at runtime.
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# Bake the donation allow-list / config as config.yaml (GLIA_SYNC_CONFIG default).
COPY config.example.yaml ./config.yaml

RUN pip install --no-cache-dir .

ENTRYPOINT ["glia-sync-daily"]
