"""Glia Shopify -> ERPNext donation sync.

Extracts donation Orders from Shopify (one-time and recurring) and pushes them
into ERPNext's Nonprofit module as Donor + Donation documents.

Phase 1: config, Shopify client (token + pagination), transform, dedup, state.
"""

__version__ = "0.1.0"
