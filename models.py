"""Compact, agent-facing data shapes + strict parsing of raw WooCommerce JSON.

Design goals:
  * Return only what an agent needs (small context footprint).
  * Data minimisation: customer email, phone, street address, customer notes and
    order meta_data are never copied into the output.
  * Fail loudly (MalformedResponseError) if WooCommerce returns something that
    does not look like an order/product, instead of passing garbage to the agent.
Raw WooCommerce JSON is validated here; unknown fields are ignored.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from woo_client import MalformedResponseError

MAX_DESCRIPTION_CHARS = 500
_TAG_RE = re.compile(r"<[^>]+>")


def _bad(kind: str, detail: str) -> MalformedResponseError:
    return MalformedResponseError(f"WooCommerce returned an unexpected {kind} format ({detail}).")


class _Reader:
    """Typed accessors over a raw JSON object; every failure names the field, never the value."""

    def __init__(self, raw: Any, kind: str):
        if not isinstance(raw, dict):
            raise _bad(kind, "expected a JSON object")
        self.raw, self.kind = raw, kind

    def _fail(self, key: str, expected: str):
        raise _bad(self.kind, f"field '{key}' is missing or not {expected}")

    def int(self, key: str) -> int:
        v = self.raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            self._fail(key, "an integer")
        return v

    def opt_int(self, key: str) -> Optional[int]:
        v = self.raw.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            self._fail(key, "an integer")
        return v

    def text(self, key: str, default: Optional[str] = None) -> str:
        v = self.raw.get(key)
        if v is None and default is not None:
            return default
        if not isinstance(v, str):
            self._fail(key, "a string")
        return v

    def money(self, key: str, default: Optional[str] = None) -> str:
        """WooCommerce sends amounts as strings; tolerate bare numbers."""
        v = self.raw.get(key)
        if v is None and default is not None:
            return default
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            self._fail(key, "a string or number")
        return str(v)

    def flag(self, key: str, default: bool = False) -> bool:
        v = self.raw.get(key)
        if v is None:
            return default
        if not isinstance(v, bool):
            self._fail(key, "a boolean")
        return v

    def obj(self, key: str) -> Dict[str, Any]:
        v = self.raw.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            self._fail(key, "an object")
        return v

    def items(self, key: str) -> List[Any]:
        v = self.raw.get(key)
        if v is None:
            return []
        if not isinstance(v, list):
            self._fail(key, "a list")
        return v


# --- Output shapes ----------------------------------------------------------
@dataclass(frozen=True)
class OrderItem:
    product_id: int
    name: str
    quantity: int
    total: str


@dataclass(frozen=True)
class OrderSummary:
    id: int
    status: str
    date_created: str          # store-local time, ISO 8601, no timezone suffix
    total: str                 # decimal string in `currency`
    currency: str
    customer_name: str
    item_count: int
    payment_method: str


@dataclass(frozen=True)
class OrderDetail(OrderSummary):
    customer_id: int           # 0 means guest checkout
    ship_to: Dict[str, str]    # city / state / country only
    items: List[OrderItem]


@dataclass(frozen=True)
class ProductSummary:
    id: int
    name: str
    sku: str
    status: str                # publish | draft | pending | private
    price: str                 # current price; "" for variable products
    regular_price: str
    sale_price: str
    on_sale: bool
    stock_status: str          # instock | outofstock | onbackorder
    stock_quantity: Optional[int]   # None when the store does not track quantity


@dataclass(frozen=True)
class ProductDetail(ProductSummary):
    type: str                  # simple | variable | grouped | external
    description: str           # HTML stripped, truncated


# --- Parsers ----------------------------------------------------------------
def _clean_text(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = " ".join(text.split())
    return text if len(text) <= MAX_DESCRIPTION_CHARS else text[: MAX_DESCRIPTION_CHARS - 1] + "…"


def _order_line_items(r: _Reader) -> List[OrderItem]:
    items = []
    for raw_item in r.items("line_items"):
        ir = _Reader(raw_item, "order line item")
        items.append(OrderItem(ir.int("product_id"), ir.text("name", ""), ir.int("quantity"), ir.money("total", "0")))
    return items


def _customer_name(billing: Dict[str, Any], shipping: Dict[str, Any]) -> str:
    for address in (billing, shipping):
        parts = [address.get("first_name"), address.get("last_name")]
        name = " ".join(p for p in parts if isinstance(p, str) and p).strip()
        if name:
            return name
    return ""


def parse_order_summary(raw: Any) -> OrderSummary:
    r = _Reader(raw, "order")
    items = _order_line_items(r)
    return OrderSummary(
        id=r.int("id"), status=r.text("status"), date_created=r.text("date_created"),
        total=r.money("total"), currency=r.text("currency", ""),
        customer_name=_customer_name(r.obj("billing"), r.obj("shipping")),
        item_count=sum(i.quantity for i in items),
        payment_method=r.text("payment_method_title", ""),
    )


def parse_order_detail(raw: Any) -> OrderDetail:
    s = parse_order_summary(raw)
    r = _Reader(raw, "order")
    shipping = r.obj("shipping")
    ship_to = {k: str(shipping.get(k) or "") for k in ("city", "state", "country")}
    return OrderDetail(**s.__dict__, customer_id=r.opt_int("customer_id") or 0,
                       ship_to=ship_to, items=_order_line_items(r))


def parse_product_summary(raw: Any) -> ProductSummary:
    r = _Reader(raw, "product")
    return ProductSummary(
        id=r.int("id"), name=r.text("name"), sku=r.text("sku", ""), status=r.text("status", ""),
        price=r.money("price", ""), regular_price=r.money("regular_price", ""),
        sale_price=r.money("sale_price", ""), on_sale=r.flag("on_sale"),
        stock_status=r.text("stock_status"), stock_quantity=r.opt_int("stock_quantity"),
    )


def parse_product_detail(raw: Any) -> ProductDetail:
    s = parse_product_summary(raw)
    r = _Reader(raw, "product")
    return ProductDetail(**s.__dict__, type=r.text("type", ""), description=_clean_text(r.text("description", "")))
