"""GraphQL query strings for the Shopify Admin API.

Kept separate from the client so they're easy to read and adjust when Shopify
ships API-version field renames. All queries are read-only (no mutations).
"""

from __future__ import annotations

# Exchange Client ID + Secret for a 24-hour access token (client credentials
# grant). See https://shopify.dev/docs/apps/build/authentication-authorization/
# access-tokens/client-credentials-grant
# (POST form-encoded body, not JSON.)
TOKEN_ENDPOINT_TEMPLATE = "/admin/oauth/access_token"

# One page of donation-relevant Orders, newest-first, optionally filtered by
# processed_at >= $since. We pull the fields the transform needs:
#   - identity / dates / financial status / test flag
#   - customer (donor) + default address
#   - per-line-item product + variant + the money the donor actually paid
#     (shopMoney = accounting currency e.g. CAD; presentmentMoney = donor's
#     original currency).
#
# `discountedTotalSet` is the line's final charged amount (after discounts),
# which is what we want for the donation amount. The "Tip" pseudo line item has
# product == null and name "Tip"; it still carries a discountedTotalSet.
ORDERS_QUERY = """
query OrdersPage($first: Int!, $after: String, $query: String, $sortKey: OrderSortKeys, $reverse: Boolean) {
  orders(first: $first, after: $after, query: $query, sortKey: $sortKey, reverse: $reverse) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        processedAt
        createdAt
        displayFinancialStatus
        test
        tags
        email
        currencyCode
        presentmentCurrencyCode
        customer {
          id
          firstName
          lastName
          email
          phone
          defaultAddress {
            address1
            address2
            city
            provinceCode
            province
            country
            zip
            phone
            company
          }
        }
        totalPriceSet {
          shopMoney { amount currencyCode }
          presentmentMoney { amount currencyCode }
        }
        lineItems(first: 25) {
          edges {
            node {
              id
              name
              quantity
              product { id title tags }
              variant { id title }
              discountedTotalSet {
                shopMoney { amount currencyCode }
                presentmentMoney { amount currencyCode }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


__all__ = ["ORDERS_QUERY", "TOKEN_ENDPOINT_TEMPLATE"]
