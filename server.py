"""WooCommerce MCP server (read-only). Thin layer: tool schemas/descriptions + error mapping.

All logic lives in connector.py / woo_client.py / models.py.
Run:  python server.py        (stdio transport; demo mode if no credentials are set)
"""
import logging
import sys
import threading
from typing import Any, Callable, Dict, Optional

try:                                              # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:                               # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server

from connector import Connector
from woo_client import ConnectorError, WooClient, load_config

logger = logging.getLogger("woocommerce-connector")
mcp = _Server("WooCommerce Agent Connector")

_connector: Optional[Connector] = None
_lock = threading.Lock()


def get_connector() -> Connector:
    """Build the connector once. Credentials are read from environment variables only."""
    global _connector
    with _lock:
        if _connector is None:
            config = load_config()
            _connector = Connector(WooClient(config) if config else None)
        return _connector


def _run(tool: str, fn: Callable[[Connector], Dict[str, Any]]) -> Dict[str, Any]:
    """Run a tool; agents only ever see ConnectorError messages, never stack traces or raw HTTP bodies."""
    try:
        return fn(get_connector())
    except ConnectorError as exc:
        logger.info("%s -> %s: %s", tool, type(exc).__name__, exc)
        raise
    except Exception:
        logger.exception("Unexpected error in %s", tool)       # full detail stays in the server log
        raise ConnectorError(f"Unexpected internal error in {tool}. See the connector's server logs.") from None


def _read_only_tool():
    """Register as a tool and, where the SDK supports it, advertise readOnlyHint to clients."""
    try:
        from mcp.types import ToolAnnotations
        return mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    except (ImportError, TypeError):
        return mcp.tool()


@_read_only_tool()
def list_orders(page: int = 1, per_page: int = 10, status: str = "any",
                created_after: Optional[str] = None, created_before: Optional[str] = None) -> Dict[str, Any]:
    """List orders from the WooCommerce store, newest first. Read-only.

    Use this to answer "show me pending orders", "what sold this week?", or to find an
    order ID before calling get_order. Returns compact summaries only (no line items or
    addresses) - call get_order(order_id) for those.

    Args:
        page: 1-based page number. Default 1.
        per_page: Results per page, 1-100. Default 10. Values above 100 are rejected, not clamped.
        status: any (default), pending, processing, on-hold, completed, cancelled, refunded, failed.
        created_after: Optional YYYY-MM-DD. Only orders created on or after this date (store timezone).
        created_before: Optional YYYY-MM-DD. Only orders created on or before this date.

    Returns: {items, page, per_page, total, total_pages, has_more, demo_mode}. Each item has
    id, status, date_created, total (decimal string, in `currency`), currency, customer_name,
    item_count, payment_method. If has_more is true, call again with page+1.
    Customer email, phone and street addresses are never returned.
    """
    return _run("list_orders", lambda c: c.list_orders(page, per_page, status, created_after, created_before))


@_read_only_tool()
def get_order(order_id: int) -> Dict[str, Any]:
    """Get full details of ONE order by its numeric ID. Read-only.

    Use this after list_orders, or when the user names an order number ("details for order 1234").

    Args:
        order_id: Positive integer order ID.

    Returns: {order, demo_mode}. order contains the summary fields plus customer_id (0 = guest),
    ship_to {city, state, country} and items [{product_id, name, quantity, total}].
    Not included: customer email/phone, street address, customer notes, payment transaction data.
    Fails with "Order N was not found" if the ID does not exist. Results are cached for 60 seconds.
    """
    return _run("get_order", lambda c: c.get_order(order_id))


@_read_only_tool()
def list_products(page: int = 1, per_page: int = 10, search: Optional[str] = None,
                  stock_status: Optional[str] = None) -> Dict[str, Any]:
    """Search and list products / inventory. Read-only.

    Use this for "find products containing 'shoes'", "which products are out of stock?" or
    to find a product ID before calling get_product. Variations of variable products are not listed.

    Args:
        page: 1-based page number. Default 1.
        per_page: Results per page, 1-100. Default 10. Values above 100 are rejected, not clamped.
        search: Optional keyword (max 100 chars), matched against product name/description.
        stock_status: Optional: instock, outofstock or onbackorder. Omit for all products.

    Returns: {items, page, per_page, total, total_pages, has_more, demo_mode}. Each item has
    id, name, sku, status (publish/draft/...), price, regular_price, sale_price, on_sale,
    stock_status, stock_quantity (null when the store does not track quantity).
    Prices are decimal strings in the store currency. Descriptions are omitted here (use get_product).
    """
    return _run("list_products", lambda c: c.list_products(page, per_page, search, stock_status))


@_read_only_tool()
def get_product(product_id: int) -> Dict[str, Any]:
    """Get details of ONE product by its numeric ID, including a plain-text description. Read-only.

    Use this to check the current price or stock of a specific product.

    Args:
        product_id: Positive integer product ID.

    Returns: {product, demo_mode}. product contains the list_products fields plus type
    (simple/variable/...) and description (HTML removed, truncated to 500 characters).
    Fails with "Product N was not found" if the ID does not exist. Results are cached for 5 minutes.
    """
    return _run("get_product", lambda c: c.get_product(product_id))


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,        # stdout is reserved for MCP stdio
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        connector = get_connector()                                    # fail fast on bad configuration
    except ConnectorError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)
    if connector.demo:
        logger.warning("DEMO MODE: no WooCommerce credentials set; serving fictional data. "
                       "Set WOOCOMMERCE_STORE_URL, WOOCOMMERCE_CONSUMER_KEY and WOOCOMMERCE_CONSUMER_SECRET for live data.")
    else:
        logger.info("LIVE MODE: connected to the configured WooCommerce store (read-only).")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
