"""Frozen dataclasses for the shop migration + Shopify JSON parsers.

Decoupled from Frappe doctype field names. The pure transforms in
`erpnext_catalog_mapping` map these to Frappe doc dicts. Money is held as
`Decimal`.

`product_from_node` / `collection_from_node` parse raw GraphQL Admin API *node*
dicts (shape defined in `shopify_queries.PRODUCTS_QUERY` / `COLLECTIONS_QUERY`)
into these dataclasses. They are pure (no I/O).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# Shopify's placeholder option name on single-variant products.
TEMPLATE_OPTION_NAME = "Title"


@dataclass(frozen=True)
class ShopImage:
    url: str
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class ShopVariantOption:
    name: str  # "Size", "Color", "Title"
    value: str  # "M", "Red", "Default Title"


@dataclass(frozen=True)
class ShopVariant:
    id: str
    sku: str | None
    title: str
    price: Decimal
    currency: str
    compare_at_price: Decimal | None = None
    barcode: str | None = None
    grams: Decimal | None = None
    available: bool = True
    options: tuple[ShopVariantOption, ...] = ()


@dataclass(frozen=True)
class ShopProduct:
    id: str
    title: str
    handle: str
    description_html: str = ""
    vendor: str | None = None
    product_type: str | None = None
    tags: tuple[str, ...] = ()
    status: str = "ACTIVE"  # ACTIVE | ARCHIVED | DRAFT
    options: tuple[str, ...] = ()  # option names, Shopify order
    variants: tuple[ShopVariant, ...] = ()
    images: tuple[ShopImage, ...] = ()
    collections: tuple[str, ...] = ()  # collection titles this product is in
    selling_plan_group_count: int = 0
    is_donation: bool = False
    is_recurring: bool = False

    @property
    def has_real_variants(self) -> bool:
        """True when the product has at least one genuine option (not the
        'Title/Default Title' placeholder) AND more than one variant."""
        if not self.variants or len(self.variants) <= 1:
            return False
        return any(o != TEMPLATE_OPTION_NAME for o in self.options)


@dataclass(frozen=True)
class ShopCollection:
    id: str
    title: str
    handle: str
    description_html: str = ""
    image: ShopImage | None = None
    sort_order: str | None = None


# --- Customers (Phase 3) -------------------------------------------------


@dataclass(frozen=True)
class ShopCustomerAddress:
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    province: str | None = None
    country: str | None = None
    zip: str | None = None
    phone: str | None = None
    company: str | None = None


@dataclass(frozen=True)
class ShopCustomer:
    id: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str = ""
    phone: str | None = None
    tags: tuple[str, ...] = ()
    state: str = ""  # "enabled" | "disabled"
    orders_count: int = 0
    total_spent: Decimal = Decimal("0")
    currency: str = ""
    created_at: str = ""
    default_address: ShopCustomerAddress | None = None

    @property
    def customer_name(self) -> str:
        joined = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return joined or self.display_name or self.email or f"Customer {_gid_num(self.id)}"

    @property
    def is_company(self) -> bool:
        return bool(self.default_address and self.default_address.company)


# --- Orders (Phase 4) ----------------------------------------------------


@dataclass(frozen=True)
class ShopOrderLine:
    id: str
    name: str
    quantity: Decimal
    product_id: str
    variant_id: str | None
    rate: Decimal  # shop-money unit price (discounted line total / qty)
    currency: str
    is_donation: bool


@dataclass(frozen=True)
class ShopOrder:
    id: str
    name: str  # Shopify order name e.g. "#1001"
    processed_at: str  # ISO datetime
    financial_status: str  # "PAID" | "PARTIALLY_PAID" | "REFUNDED" | ...
    test: bool = False
    currency: str = "CAD"
    total: Decimal = Decimal("0")
    customer_id: str | None = None
    customer_email: str | None = None
    lines: tuple[ShopOrderLine, ...] = ()

    @property
    def shop_lines(self) -> list[ShopOrderLine]:
        """Non-donation lines — the ones that belong on a Sales Order.

        Donation lines are already represented as `Donation` records (donation
        sync); creating Sales Order items for them would double-count revenue.
        """
        return [ln for ln in self.lines if not ln.is_donation]

    @property
    def is_shop_order(self) -> bool:
        return bool(self.shop_lines)


# --- Shopify JSON -> dataclass --------------------------------------------


def product_from_node(
    node: dict[str, Any],
    *,
    currency: str = "CAD",
    donation_gids: set[str] | None = None,
    recurring_gids: set[str] | None = None,
) -> ShopProduct:
    donation_gids = donation_gids or set()
    recurring_gids = recurring_gids or set()
    pid = node.get("id", "")
    return ShopProduct(
        id=pid,
        title=node.get("title", ""),
        handle=node.get("handle", ""),
        description_html=node.get("descriptionHtml") or "",
        vendor=_clean(node.get("vendor")),
        product_type=_clean(node.get("productType")),
        tags=tuple(node.get("tags") or ()),
        status=node.get("status", "ACTIVE"),
        options=tuple((o.get("name", "") or "") for o in (node.get("options") or [])),
        variants=tuple(
            _variant_from_node(e["node"], currency)
            for e in _edges(node, "variants")
            if e.get("node")
        ),
        images=tuple(_image_from_node(e["node"]) for e in _edges(node, "images") if e.get("node")),
        collections=tuple(
            (e.get("node") or {}).get("title", "")
            for e in _edges(node, "collections")
            if e.get("node")
        ),
        selling_plan_group_count=int(node.get("sellingPlanGroupCount") or 0),
        is_donation=pid in donation_gids,
        is_recurring=pid in recurring_gids,
    )


def collection_from_node(node: dict[str, Any]) -> ShopCollection:
    img = node.get("image") or None
    return ShopCollection(
        id=node.get("id", ""),
        title=node.get("title", ""),
        handle=node.get("handle", ""),
        description_html=node.get("descriptionHtml") or "",
        image=_image_from_node(img) if img else None,
        sort_order=_clean(node.get("sortOrder")),
    )


def customer_from_node(node: dict[str, Any], *, currency: str = "CAD") -> ShopCustomer:
    spent = node.get("totalSpent") or {}
    return ShopCustomer(
        id=node.get("id", ""),
        email=_clean(node.get("email")),
        first_name=_clean(node.get("firstName")),
        last_name=_clean(node.get("lastName")),
        display_name=node.get("displayName", ""),
        phone=_clean(node.get("phone")),
        tags=tuple(node.get("tags") or ()),
        state=node.get("state", ""),
        orders_count=int(node.get("ordersCount") or 0),
        total_spent=_dec(spent.get("amount")) if spent else Decimal("0"),
        currency=(spent.get("currencyCode") or currency) if spent else currency,
        created_at=node.get("createdAt", ""),
        default_address=_address_from_node(node.get("defaultAddress")),
    )


def _address_from_node(addr: dict[str, Any] | None) -> ShopCustomerAddress | None:
    if not addr:
        return None
    return ShopCustomerAddress(
        address1=_clean(addr.get("address1")),
        address2=_clean(addr.get("address2")),
        city=_clean(addr.get("city")),
        province=_clean(addr.get("provinceCode") or addr.get("province")),
        country=_clean(addr.get("country")),
        zip=_clean(addr.get("zip")),
        phone=_clean(addr.get("phone")),
        company=_clean(addr.get("company")),
    )


# --- order parsing --------------------------------------------------------


def order_from_node(
    node: dict[str, Any], *, currency: str = "CAD", donation_gids: set[str] | None = None
) -> ShopOrder:
    donation_gids = donation_gids or set()
    shop_money = node.get("totalPriceSet", {}).get("shopMoney", {}) or {}
    cur = shop_money.get("currencyCode") or node.get("currencyCode") or currency
    customer = node.get("customer") or {}

    lines: list[ShopOrderLine] = []
    for e in _edges(node, "lineItems"):
        ln = e.get("node") or {}
        product = ln.get("product") or {}
        variant = ln.get("variant") or {}
        product_id = product.get("id", "") or ""
        qty = _dec(ln.get("quantity") or 1)
        line_total = _dec((ln.get("discountedTotalSet") or {}).get("shopMoney", {}).get("amount"))
        lines.append(
            ShopOrderLine(
                id=ln.get("id", ""),
                name=ln.get("name", ""),
                quantity=qty,
                product_id=product_id,
                variant_id=variant.get("id"),
                rate=(line_total / qty) if qty else Decimal("0"),
                currency=cur,
                is_donation=product_id in donation_gids,
            )
        )

    return ShopOrder(
        id=node.get("id", ""),
        name=node.get("name", ""),
        processed_at=node.get("processedAt") or node.get("createdAt", ""),
        financial_status=node.get("displayFinancialStatus") or "",
        test=bool(node.get("test")),
        currency=cur,
        total=_dec(shop_money.get("amount")),
        customer_id=customer.get("id"),
        customer_email=_clean(customer.get("email")) or _clean(node.get("email")),
        lines=tuple(lines),
    )


# --- parse helpers --------------------------------------------------------


def _edges(node: dict[str, Any], key: str) -> list[dict[str, Any]]:
    conn = node.get(key) or {}
    return conn.get("edges") or []


def _variant_from_node(v: dict[str, Any], currency: str) -> ShopVariant:
    compare = v.get("compareAtPrice")
    return ShopVariant(
        id=v.get("id", ""),
        sku=_clean(v.get("sku")),
        title=v.get("title", ""),
        price=_dec(v.get("price")),
        currency=currency,
        compare_at_price=_dec(compare) if compare else None,
        barcode=_clean(v.get("barcode")),
        grams=_measurement_weight(v),
        available=bool(v.get("availableForSale", True)),
        options=tuple(
            ShopVariantOption(
                name=(o.get("name", "") or ""),
                value=_norm_option(o.get("value", "")),
            )
            for o in (v.get("selectedOptions") or [])
        ),
    )


def _measurement_weight(v: dict[str, Any]) -> Decimal | None:
    """Pull weight from `inventoryItem.measurement.weight.{value,unit}`.

    Shopify moved variant weight off the top-level `weight` field (removed in
    newer API versions) into the inventory item's measurement. Unit is ignored
    here — assumed grams; downstream (Shipping Rule) can convert later.
    """
    inv = v.get("inventoryItem") or {}
    measurement = inv.get("measurement") or {}
    weight_block = measurement.get("weight") or {}
    value = weight_block.get("value")
    return _dec(value) if value is not None else None


def _image_from_node(img: dict[str, Any]) -> ShopImage:
    return ShopImage(
        url=img.get("url", ""),
        alt_text=_clean(img.get("altText")),
        width=img.get("width"),
        height=img.get("height"),
    )


def _dec(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _gid_num(gid: str) -> str:
    """Trailing numeric segment of a Shopify GID."""
    import re

    m = re.search(r"(\d+)$", gid or "")
    return m.group(1) if m else ""


def _norm_option(value: Any) -> str:
    """Canonicalize a Shopify option value so casing variants collapse.

    ERPNext's Item Attribute uniqueness is case-insensitive, so "Black" and
    "black" can't coexist. We keep already-uppercase codes (S, M, XL, 2XL, XS)
    as-is and Title-case everything else (black -> Black, RED -> Red). Applied
    at parse time so both the attribute definitions and the variant attribute
    values use the same canonical form.
    """
    v = (str(value) if value is not None else "").strip()
    if not v:
        return ""
    return v if v == v.upper() else v.title()


__all__ = [
    "TEMPLATE_OPTION_NAME",
    "ShopCollection",
    "ShopCustomer",
    "ShopCustomerAddress",
    "ShopImage",
    "ShopOrder",
    "ShopOrderLine",
    "ShopProduct",
    "ShopVariant",
    "ShopVariantOption",
    "collection_from_node",
    "customer_from_node",
    "order_from_node",
    "product_from_node",
]
