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


# All published + draft Products (cursor-paginated). We pull the fields the
# catalog transform needs:
#   - identity / title / handle / descriptionHtml / vendor / productType / status
#   - tags (list of strings) and option names
#   - variants: id, sku, title, price, compareAtPrice, barcode, weight(+unit),
#     availability, and selectedOptions (the concrete option values per variant)
#   - images (url + altText + dimensions)
#   - collections the product belongs to (title + handle)
#   - sellingPlanGroupCount (> 0 means subscription-capable; flags recurring)
PRODUCTS_QUERY = """
query ProductsPage($first: Int!, $after: String, $query: String, $sortKey: ProductSortKeys, $reverse: Boolean) {
  products(first: $first, after: $after, query: $query, sortKey: $sortKey, reverse: $reverse) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        descriptionHtml
        vendor
        productType
        tags
        status
        options { name }
        variants(first: 100) {
          edges {
            node {
              id
              sku
              title
              price
              compareAtPrice
              barcode
              availableForSale
              inventoryItem { measurement { weight { value unit } } }
              selectedOptions { name value }
            }
          }
        }
        images(first: 10) {
          edges {
            node { url altText width height }
          }
        }
        collections(first: 10) {
          edges { node { title handle } }
        }
        sellingPlanGroupCount
      }
    }
  }
}
""".strip()


# All Collections (manual + smart). Used to build ERPNext Website Categories.
COLLECTIONS_QUERY = """
query CollectionsPage($first: Int!, $after: String, $sortKey: CollectionSortKeys, $reverse: Boolean) {
  collections(first: $first, after: $after, sortKey: $sortKey, reverse: $reverse) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        descriptionHtml
        sortOrder
        image { url altText }
      }
    }
  }
}
""".strip()


# All Customers (cursor-paginated). Supports a `query` filter, e.g.
# "orders_count:>0" to migrate only customers who actually bought something.
# Requires the `read_customers` scope.
#
# NOTE: `ordersCount`/`totalSpent` were removed from `Customer` in API 2025-07
# (moved under a `stats` sub-object). We don't need them to build Customer/Address
# docs, so they're omitted. The `orders_count:>0` *query filter* still works as a
# search parameter on the connection regardless.
CUSTOMERS_QUERY = """
query CustomersPage($first: Int!, $after: String, $query: String, $sortKey: CustomerSortKeys, $reverse: Boolean) {
  customers(first: $first, after: $after, query: $query, sortKey: $sortKey, reverse: $reverse) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        firstName
        lastName
        displayName
        email
        phone
        tags
        state
        createdAt
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
    }
  }
}
""".strip()


__all__ = [
    "COLLECTIONS_QUERY",
    "CUSTOMERS_QUERY",
    "ORDERS_QUERY",
    "PRODUCTS_QUERY",
    "TOKEN_ENDPOINT_TEMPLATE",
]
