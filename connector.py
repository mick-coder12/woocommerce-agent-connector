"""Business logic behind the MCP tools: validation -> (demo | live) -> compact result.

Kept free of any MCP import so it can be unit-tested with plain `unittest`.
All input validation happens FIRST, so demo mode and live mode reject exactly the
same inputs with exactly the same messages.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import asdict
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from demo_data import DEMO_NOTICE, DEMO_PRODUCTS, demo_orders
from models import (parse_order_detail, parse_order_summary, parse_product_detail,
                    parse_product_summary)
from woo_client import (InvalidInputError, MalformedResponseError, NotFoundError,
                        PageOutOfRangeError, WooClient)

DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 100
MAX_SEARCH_LENGTH = 100
ORDER_STATUSES = ("any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed")
STOCK_STATUSES = ("instock", "outofstock", "onbackorder")
ORDER_CACHE_TTL_S = 60       # order status changes often -> short TTL
PRODUCT_CACHE_TTL_S = 300
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- Input validation -------------------------------------------------------
def _check_int(name: str, value: Any, lo: int, hi: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(f"{name} must be an integer.")
    if value < lo or (hi is not None and value > hi):
        bound = f"between {lo} and {hi}" if hi is not None else f"at least {lo}"
        raise InvalidInputError(f"{name} must be {bound} (got {value}).")
    return value


def _check_choice(name: str, value: Any, allowed: Tuple[str, ...]) -> str:
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        raise InvalidInputError(f"Invalid {name} {value!r}. Valid values: {', '.join(allowed)}.")
    return value.strip().lower()


def _check_search(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError("search must be a string.")
    value = value.strip()
    if len(value) > MAX_SEARCH_LENGTH:
        raise InvalidInputError(f"search must be at most {MAX_SEARCH_LENGTH} characters.")
    return value or None


def _check_date(name: str, value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise InvalidInputError(f"{name} must be a real calendar date in YYYY-MM-DD format.")


# --- Small helpers ----------------------------------------------------------
class TTLCache:
    """Tiny thread-safe TTL cache (successful lookups only)."""

    def __init__(self, ttl: float, maxsize: int = 256, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._maxsize, self._clock = ttl, maxsize, clock
        self._data: Dict[Any, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._clock() >= entry[0]:
                del self._data[key]
                return None
            return entry[1]

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            if key not in self._data and len(self._data) >= self._maxsize:
                del self._data[min(self._data, key=lambda k: self._data[k][0])]
            self._data[key] = (self._clock() + self._ttl, value)


def _paginate(rows: List[Any], page: int, per_page: int) -> Tuple[List[Any], int, int]:
    total = len(rows)
    total_pages = math.ceil(total / per_page)
    if total > 0 and page > total_pages:      # same behaviour as WooCommerce
        raise PageOutOfRangeError("The requested page is beyond the last page of results.")
    start = (page - 1) * per_page
    return rows[start:start + per_page], total, total_pages


def _expect_list(data: Any, what: str) -> List[Any]:
    if not isinstance(data, list):
        raise MalformedResponseError(f"WooCommerce returned an unexpected {what} list format (expected a JSON array).")
    return data


def _order_not_found(order_id: int) -> NotFoundError:
    return NotFoundError(f"Order {order_id} was not found. Use list_orders to find valid order IDs.")


def _product_not_found(product_id: int) -> NotFoundError:
    return NotFoundError(f"Product {product_id} was not found. Use list_products to find valid product IDs.")


class Connector:
    """`client=None` means demo mode."""

    def __init__(self, client: Optional[WooClient] = None, *, demo_today: Optional[date] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._demo_orders = demo_orders(demo_today or date.today()) if client is None else []
        # Separate caches: order #101 and product #101 are different things.
        self._orders = TTLCache(ORDER_CACHE_TTL_S, clock=clock)
        self._products = TTLCache(PRODUCT_CACHE_TTL_S, clock=clock)

    @property
    def demo(self) -> bool:
        return self._client is None

    # -- response envelopes ----------------------------------------------
    def _with_flags(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body["demo_mode"] = self.demo
        if self.demo:
            body["notice"] = DEMO_NOTICE
        return body

    def _page(self, items: List[Dict[str, Any]], page: int, per_page: int,
              total: Optional[int], total_pages: Optional[int]) -> Dict[str, Any]:
        has_more = page < total_pages if total_pages is not None else len(items) >= per_page
        return self._with_flags({"items": items, "page": page, "per_page": per_page, "total": total,
                                 "total_pages": total_pages, "has_more": has_more})

    # -- orders ------------------------------------------------------------
    def list_orders(self, page: Any = 1, per_page: Any = DEFAULT_PER_PAGE, status: Any = "any",
                    created_after: Any = None, created_before: Any = None) -> Dict[str, Any]:
        page = _check_int("page", page, 1)
        per_page = _check_int("per_page", per_page, 1, MAX_PER_PAGE)
        status = _check_choice("status", status, ORDER_STATUSES)
        after, before = _check_date("created_after", created_after), _check_date("created_before", created_before)
        if after and before and after > before:
            raise InvalidInputError("created_after must not be later than created_before.")

        if self.demo:
            rows = [o for o in self._demo_orders
                    if (status == "any" or o["status"] == status)
                    and (after is None or date.fromisoformat(o["date_created"][:10]) >= after)
                    and (before is None or date.fromisoformat(o["date_created"][:10]) <= before)]
            chunk, total, total_pages = _paginate(rows, page, per_page)
            return self._page([asdict(parse_order_summary(o)) for o in chunk], page, per_page, total, total_pages)

        params: Dict[str, Any] = {"page": page, "per_page": per_page, "status": status,
                                  "orderby": "date", "order": "desc"}
        if after:
            params["after"] = f"{after.isoformat()}T00:00:00"
        if before:
            params["before"] = f"{before.isoformat()}T23:59:59"
        resp = self._client.get("orders", params)
        items = [asdict(parse_order_summary(o)) for o in _expect_list(resp.data, "order")]
        return self._page(items, page, per_page, resp.total, resp.total_pages)

    def get_order(self, order_id: Any) -> Dict[str, Any]:
        order_id = _check_int("order_id", order_id, 1)
        if self.demo:
            raw = next((o for o in self._demo_orders if o["id"] == order_id), None)
            if raw is None:
                raise _order_not_found(order_id)
            return self._with_flags({"order": asdict(parse_order_detail(raw))})
        order = self._orders.get(order_id)
        if order is None:
            try:
                resp = self._client.get(f"orders/{order_id}")
            except NotFoundError:
                raise _order_not_found(order_id) from None
            order = asdict(parse_order_detail(resp.data))
            self._orders.set(order_id, order)
        return self._with_flags({"order": order})

    # -- products ----------------------------------------------------------
    def list_products(self, page: Any = 1, per_page: Any = DEFAULT_PER_PAGE, search: Any = None,
                      stock_status: Any = None) -> Dict[str, Any]:
        page = _check_int("page", page, 1)
        per_page = _check_int("per_page", per_page, 1, MAX_PER_PAGE)
        search = _check_search(search)
        if stock_status is not None and stock_status != "":
            stock_status = _check_choice("stock_status", stock_status, STOCK_STATUSES)
        else:
            stock_status = None

        if self.demo:
            needle = search.lower() if search else None
            rows = [p for p in DEMO_PRODUCTS
                    if (needle is None or needle in p["name"].lower() or needle in p["description"].lower())
                    and (stock_status is None or p["stock_status"] == stock_status)]
            chunk, total, total_pages = _paginate(rows, page, per_page)
            return self._page([asdict(parse_product_summary(p)) for p in chunk], page, per_page, total, total_pages)

        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if search:
            params["search"] = search
        if stock_status:
            params["stock_status"] = stock_status
        resp = self._client.get("products", params)
        items = [asdict(parse_product_summary(p)) for p in _expect_list(resp.data, "product")]
        return self._page(items, page, per_page, resp.total, resp.total_pages)

    def get_product(self, product_id: Any) -> Dict[str, Any]:
        product_id = _check_int("product_id", product_id, 1)
        if self.demo:
            raw = next((p for p in DEMO_PRODUCTS if p["id"] == product_id), None)
            if raw is None:
                raise _product_not_found(product_id)
            return self._with_flags({"product": asdict(parse_product_detail(raw))})
        product = self._products.get(product_id)
        if product is None:
            try:
                resp = self._client.get(f"products/{product_id}")
            except NotFoundError:
                raise _product_not_found(product_id) from None
            product = asdict(parse_product_detail(resp.data))
            self._products.set(product_id, product)
        return self._with_flags({"product": product})
