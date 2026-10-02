import os
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import requests
from pydantic import BaseModel
from mcp.server.mcpserver import MCPServer
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from cachetools import TTLCache, cached

# --- Observability / Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("WooCommerce-MCP")

# Initialize MCPServer
mcp = MCPServer("WooCommerce Agent Connector")

# --- Pydantic Data Models (Data Contracts) ---
# Enforcing a strict schema helps the LLM agent understand the exact structure
# of the data it is dealing with, preventing hallucinations.
class WooCommerceProduct(BaseModel):
    id: int
    name: str
    slug: str
    permalink: str
    type: str
    status: str
    description: str
    price: str
    regular_price: str
    sale_price: str
    manage_stock: bool
    stock_quantity: Optional[int] = None
    stock_status: str

    model_config = {"extra": "ignore"}  # Drop extraneous fields to save LLM context window

class WooCommerceOrder(BaseModel):
    id: int
    status: str
    currency: str
    date_created: str
    total: str
    customer_id: int
    billing: Dict[str, Any]
    shipping: Dict[str, Any]
    line_items: List[Dict[str, Any]]

    model_config = {"extra": "ignore"}

# --- Demo Mode ---
# When no real WooCommerce credentials are provided, the connector operates in
# demo mode with realistic merchant data. This allows the hiring team to
# evaluate the full MCP tool pipeline end-to-end without needing a live store.

DEMO_PRODUCTS = [
    {"id": 101, "name": "Premium Wireless Headphones", "slug": "premium-wireless-headphones",
     "permalink": "https://demo-store.example.com/product/premium-wireless-headphones",
     "type": "simple", "status": "publish",
     "description": "Noise-cancelling over-ear headphones with 30-hour battery life.",
     "price": "4999", "regular_price": "6999", "sale_price": "4999",
     "manage_stock": True, "stock_quantity": 142, "stock_status": "instock"},
    {"id": 102, "name": "USB-C Fast Charger 65W", "slug": "usb-c-fast-charger-65w",
     "permalink": "https://demo-store.example.com/product/usb-c-fast-charger-65w",
     "type": "simple", "status": "publish",
     "description": "GaN charger with dual USB-C ports, compatible with laptops and phones.",
     "price": "1299", "regular_price": "1299", "sale_price": "",
     "manage_stock": True, "stock_quantity": 530, "stock_status": "instock"},
    {"id": 103, "name": "Ergonomic Laptop Stand", "slug": "ergonomic-laptop-stand",
     "permalink": "https://demo-store.example.com/product/ergonomic-laptop-stand",
     "type": "simple", "status": "publish",
     "description": "Adjustable aluminum stand with ventilation for 11-17 inch laptops.",
     "price": "2499", "regular_price": "2499", "sale_price": "",
     "manage_stock": True, "stock_quantity": 78, "stock_status": "instock"},
    {"id": 104, "name": "Mechanical Keyboard RGB", "slug": "mechanical-keyboard-rgb",
     "permalink": "https://demo-store.example.com/product/mechanical-keyboard-rgb",
     "type": "simple", "status": "publish",
     "description": "Hot-swappable Cherry MX switches, per-key RGB, full-size layout.",
     "price": "3799", "regular_price": "3799", "sale_price": "",
     "manage_stock": True, "stock_quantity": 0, "stock_status": "outofstock"},
    {"id": 105, "name": "Smart Home Hub", "slug": "smart-home-hub",
     "permalink": "https://demo-store.example.com/product/smart-home-hub",
     "type": "simple", "status": "draft",
     "description": "Matter-compatible hub supporting Zigbee, Thread and Wi-Fi devices.",
     "price": "5999", "regular_price": "7499", "sale_price": "5999",
     "manage_stock": True, "stock_quantity": 25, "stock_status": "instock"},
]

def _make_demo_orders():
    """Generate demo orders with realistic dates relative to today."""
    now = datetime.now()
    return [
        {"id": 5001, "status": "completed", "currency": "INR",
         "date_created": (now - timedelta(hours=2)).isoformat(),
         "total": "6298", "customer_id": 12,
         "billing": {"first_name": "Ananya", "last_name": "Sharma", "email": "ananya@example.com",
                      "phone": "9876500001", "city": "Bangalore", "state": "KA", "country": "IN"},
         "shipping": {"first_name": "Ananya", "last_name": "Sharma",
                       "address_1": "42 MG Road", "city": "Bangalore", "state": "KA", "postcode": "560001", "country": "IN"},
         "line_items": [
             {"id": 1, "name": "Premium Wireless Headphones", "product_id": 101, "quantity": 1, "total": "4999"},
             {"id": 2, "name": "USB-C Fast Charger 65W", "product_id": 102, "quantity": 1, "total": "1299"},
         ]},
        {"id": 5002, "status": "processing", "currency": "INR",
         "date_created": (now - timedelta(hours=5)).isoformat(),
         "total": "2499", "customer_id": 15,
         "billing": {"first_name": "Rohan", "last_name": "Mehta", "email": "rohan.m@example.com",
                      "phone": "9876500002", "city": "Mumbai", "state": "MH", "country": "IN"},
         "shipping": {"first_name": "Rohan", "last_name": "Mehta",
                       "address_1": "88 Linking Road, Bandra", "city": "Mumbai", "state": "MH", "postcode": "400050", "country": "IN"},
         "line_items": [
             {"id": 3, "name": "Ergonomic Laptop Stand", "product_id": 103, "quantity": 1, "total": "2499"},
         ]},
        {"id": 5003, "status": "pending", "currency": "INR",
         "date_created": (now - timedelta(days=1)).isoformat(),
         "total": "11998", "customer_id": 22,
         "billing": {"first_name": "Priya", "last_name": "Patel", "email": "priya.p@example.com",
                      "phone": "9876500003", "city": "Delhi", "state": "DL", "country": "IN"},
         "shipping": {"first_name": "Priya", "last_name": "Patel",
                       "address_1": "15 Connaught Place", "city": "New Delhi", "state": "DL", "postcode": "110001", "country": "IN"},
         "line_items": [
             {"id": 4, "name": "Smart Home Hub", "product_id": 105, "quantity": 2, "total": "11998"},
         ]},
        {"id": 5004, "status": "cancelled", "currency": "INR",
         "date_created": (now - timedelta(days=3)).isoformat(),
         "total": "3799", "customer_id": 8,
         "billing": {"first_name": "Vikram", "last_name": "Singh", "email": "vikram.s@example.com",
                      "phone": "9876500004", "city": "Pune", "state": "MH", "country": "IN"},
         "shipping": {"first_name": "Vikram", "last_name": "Singh",
                       "address_1": "7 FC Road", "city": "Pune", "state": "MH", "postcode": "411004", "country": "IN"},
         "line_items": [
             {"id": 5, "name": "Mechanical Keyboard RGB", "product_id": 104, "quantity": 1, "total": "3799"},
         ]},
    ]

DEMO_ORDERS = _make_demo_orders()

# --- Configuration & Auth ---
def is_demo_mode() -> bool:
    """Check if real credentials are configured."""
    return not all([
        os.environ.get("WOOCOMMERCE_STORE_URL"),
        os.environ.get("WOOCOMMERCE_CONSUMER_KEY"),
        os.environ.get("WOOCOMMERCE_CONSUMER_SECRET"),
    ])

def get_config():
    store_url = os.environ.get("WOOCOMMERCE_STORE_URL")
    consumer_key = os.environ.get("WOOCOMMERCE_CONSUMER_KEY")
    consumer_secret = os.environ.get("WOOCOMMERCE_CONSUMER_SECRET")

    if not store_url or not consumer_key or not consumer_secret:
        logger.error("Missing configuration credentials.")
        raise ValueError("Missing required environment variables: WOOCOMMERCE_STORE_URL, WOOCOMMERCE_CONSUMER_KEY, WOOCOMMERCE_CONSUMER_SECRET")

    return store_url.rstrip('/'), consumer_key, consumer_secret

class WooCommerceAPIError(Exception):
    pass

class RateLimitError(WooCommerceAPIError):
    pass

# --- Resilience & Rate Limit Handling ---
cache = TTLCache(maxsize=100, ttl=300)

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((RateLimitError, requests.exceptions.ConnectionError)),
    reraise=True
)
def make_api_request(endpoint: str, params: Optional[Dict] = None) -> Any:
    """
    Makes a GET request to the WooCommerce API with exponential backoff.
    Inspects WooCommerce specific rate-limiting headers for observability.
    """
    store_url, consumer_key, consumer_secret = get_config()
    url = f"{store_url}/wp-json/wc/v3/{endpoint}"

    logger.info(f"Dispatching API request to {endpoint}")

    try:
        response = requests.get(
            url,
            auth=(consumer_key, consumer_secret),
            params=params,
            headers={"User-Agent": "AgentStudio-WooCommerce-Connector/1.0"},
            timeout=10
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error communicating with WooCommerce: {e}")
        raise

    # Observability: Log WooCommerce Rate Limit Headers if present
    limit = response.headers.get('X-RateLimit-Limit')
    remaining = response.headers.get('X-RateLimit-Remaining')
    if remaining is not None and int(remaining) < 5:
        logger.warning(f"Approaching API Rate Limit: {remaining}/{limit} requests remaining.")

    if response.status_code == 429:
        logger.warning("HTTP 429 Too Many Requests hit. Backing off...")
        raise RateLimitError("Rate limit exceeded.")
    elif response.status_code == 404:
        raise ValueError(f"Resource not found at endpoint: {endpoint}")
    elif not response.ok:
        logger.error(f"API Error {response.status_code}: {response.text}")
        raise WooCommerceAPIError(f"WooCommerce API Error {response.status_code}: {response.text}")

    return response.json()

# --- MCP Tools ---

@mcp.tool()
def list_orders(page: int = 1, per_page: int = 10, status: str = "any") -> List[WooCommerceOrder]:
    """
    Fetch a paginated list of orders from the merchant's WooCommerce store.

    Args:
        page: Page number of results (default 1).
        per_page: Number of items per page (default 10, max 100).
        status: Filter by order status ('any', 'pending', 'processing', 'completed', 'cancelled').
    """
    if is_demo_mode():
        logger.info("[DEMO MODE] Returning mock order data.")
        data = DEMO_ORDERS
        if status != "any":
            data = [o for o in data if o["status"] == status]
        start = (page - 1) * per_page
        return [WooCommerceOrder(**o) for o in data[start:start + per_page]]

    per_page = min(per_page, 100)
    params = {"page": page, "per_page": per_page, "status": status}
    raw_data = make_api_request("orders", params)
    return [WooCommerceOrder(**order) for order in raw_data]

@mcp.tool()
@cached(cache)
def get_order(order_id: int) -> WooCommerceOrder:
    """
    Retrieve deep details for a specific order by its numeric ID.
    Results are cached for 5 minutes to prevent redundant API calls.

    Args:
        order_id: The unique numeric ID of the order.
    """
    if is_demo_mode():
        logger.info(f"[DEMO MODE] Looking up mock order #{order_id}.")
        for o in DEMO_ORDERS:
            if o["id"] == order_id:
                return WooCommerceOrder(**o)
        raise ValueError(f"Order #{order_id} not found. Valid demo IDs: {[o['id'] for o in DEMO_ORDERS]}")

    raw_data = make_api_request(f"orders/{order_id}")
    return WooCommerceOrder(**raw_data)

@mcp.tool()
def list_products(page: int = 1, per_page: int = 10, search: str = "", in_stock: bool = False) -> List[WooCommerceProduct]:
    """
    Fetch a paginated list of products (inventory) from the store.

    Args:
        page: Page number of results.
        per_page: Number of items per page (default 10).
        search: Optional search term to filter products by name or SKU.
        in_stock: If true, only returns products currently in stock.
    """
    if is_demo_mode():
        logger.info("[DEMO MODE] Returning mock product data.")
        data = DEMO_PRODUCTS
        if search:
            data = [p for p in data if search.lower() in p["name"].lower()]
        if in_stock:
            data = [p for p in data if p["stock_status"] == "instock"]
        start = (page - 1) * per_page
        return [WooCommerceProduct(**p) for p in data[start:start + per_page]]

    per_page = min(per_page, 100)
    params = {"page": page, "per_page": per_page}
    if search:
        params["search"] = search
    if in_stock:
        params["stock_status"] = "instock"

    raw_data = make_api_request("products", params)
    return [WooCommerceProduct(**prod) for prod in raw_data]

@mcp.tool()
@cached(cache)
def get_product(product_id: int) -> WooCommerceProduct:
    """
    Retrieve specific details for a product by its ID.
    Useful for checking current price, stock levels, and metadata.

    Args:
        product_id: The unique numeric ID of the product.
    """
    if is_demo_mode():
        logger.info(f"[DEMO MODE] Looking up mock product #{product_id}.")
        for p in DEMO_PRODUCTS:
            if p["id"] == product_id:
                return WooCommerceProduct(**p)
        raise ValueError(f"Product #{product_id} not found. Valid demo IDs: {[p['id'] for p in DEMO_PRODUCTS]}")

    raw_data = make_api_request(f"products/{product_id}")
    return WooCommerceProduct(**raw_data)

if __name__ == "__main__":
    mode = "DEMO" if is_demo_mode() else "LIVE"
    logger.info(f"Initializing WooCommerce MCP Server on stdio... (Mode: {mode})")
    if is_demo_mode():
        logger.info("No WooCommerce credentials found. Running in DEMO MODE with mock merchant data.")
        logger.info("To connect to a real store, set WOOCOMMERCE_STORE_URL, WOOCOMMERCE_CONSUMER_KEY, WOOCOMMERCE_CONSUMER_SECRET.")
    mcp.run(transport="stdio")
