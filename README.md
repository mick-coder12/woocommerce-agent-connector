# WooCommerce Private Connector for Agent Studio

A production-ready MCP (Model Context Protocol) connector that enables an Agent Studio agent to read orders and products (inventory) from a WooCommerce store. Built with resilience, observability, and strict data contracts.

**Includes a built-in demo mode** — runs out of the box with realistic mock merchant data. No WooCommerce store needed to evaluate.

## Quick Start (Demo Mode)

```bash
# 1. Clone and setup
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Launch with MCP Inspector (visual testing tool)
npx @modelcontextprotocol/inspector python server.py

# 3. Open the URL shown in terminal, click Connect, then click any tool → Run Tool
```

No API keys needed. Demo mode activates automatically and returns realistic Indian merchant data (orders from Bangalore, Mumbai, Delhi with INR pricing).

## Connecting to a Real Store

Set these environment variables to switch from demo mode to a live WooCommerce store:

```bash
export WOOCOMMERCE_STORE_URL="https://your-store.com"
export WOOCOMMERCE_CONSUMER_KEY="ck_your_consumer_key"
export WOOCOMMERCE_CONSUMER_SECRET="cs_your_consumer_secret"
```

Generate API keys in WordPress Admin → **WooCommerce** → **Settings** → **Advanced** → **REST API** → **Add Key** (set permissions to **Read**).

## Architecture & Design Decisions (FDE Focus)

As a Forward-Deployed Engineer, building reliable, observable, and strictly typed integrations is critical when putting AI agents in front of merchant data. This connector was built with those principles in mind:

1. **Strict Data Contracts (Pydantic)**: Instead of returning raw, massive JSON blobs from the WooCommerce API, this connector uses `pydantic` models (`WooCommerceOrder`, `WooCommerceProduct`). This drops extraneous fields, strictly types the data, and dramatically saves the LLM's context window, preventing hallucination.
2. **Resilience & Rate Limiting (`tenacity`)**: Network calls fail, and merchants hit API limits. This connector uses exponential backoff to automatically retry requests on HTTP 429s or connection timeouts.
3. **Observability (Structured Logging)**: The connector intercepts WooCommerce's native `X-RateLimit-Remaining` headers and logs warnings to `stderr` when limits approach. This ensures the MCP `stdio` transport remains uncorrupted while providing critical telemetry to the host system.
4. **Caching (`cachetools`)**: The `get_order` and `get_product` endpoints utilize a Time-To-Live (TTL) LRU cache. If an agent loops over a product multiple times during reasoning, it won't spam the merchant's API.
5. **Fail-Fast Defensiveness**: Pagination parameters are strictly clamped (`per_page` maxes at 100) to prevent an LLM from accidentally requesting 10,000 records and crashing the merchant's PHP backend.
6. **Graceful Demo Mode**: When no credentials are configured, the server automatically falls back to realistic mock data instead of crashing — making it safe and easy to evaluate.

## MCP Tool Specification

### Tools Exposed

| Tool | Description | Key Args |
|------|-------------|----------|
| `list_orders` | Paginated list of orders, filterable by status | `page`, `per_page`, `status` |
| `get_order` | Retrieve a single order by ID (cached 5 min) | `order_id` |
| `list_products` | Paginated product inventory, filterable by search/stock | `page`, `per_page`, `search`, `in_stock` |
| `get_product` | Retrieve a single product by ID (cached 5 min) | `product_id` |

### Agent Configuration (`mcp_config.json`)

```json
{
  "mcpServers": {
    "woocommerce": {
      "command": "python",
      "args": ["/path/to/woocommerce-mcp-connector/server.py"],
      "env": {
        "WOOCOMMERCE_STORE_URL": "https://your-store.com",
        "WOOCOMMERCE_CONSUMER_KEY": "ck_...",
        "WOOCOMMERCE_CONSUMER_SECRET": "cs_..."
      }
    }
  }
}
```

## What the Agent Can and Cannot Do

### ✅ The Agent Can:
- View all orders (or filter by status: pending, processing, completed, cancelled).
- Read detailed information about a specific order (customer details, line items, shipping info).
- View all products in the store's inventory, and filter by in-stock status or search term.
- Read detailed information about a specific product (stock status, price, descriptions).

### ❌ The Agent Cannot (Intentional Limitations):
- **Create, Update, or Delete**: Read-only primitives only (`GET` endpoints). Cannot create orders, modify stock, or delete records. This is a deliberate safety decision for a read-only agent.
- **Manage Customers Directly**: No standalone customer primitives (customer data is embedded in order payloads).
- **Webhook Subscriptions**: The agent polls the API; it does not receive real-time push events.

### Long-term Enhancements:
1. Add `POST`/`PUT` endpoints governed by OAuth2 scopes for write operations (inventory updates, order status changes).
2. Integrate a webhook receiver with pub/sub to make the agent reactive instead of poll-based.
3. Add `list_customers` and `search_orders_by_date_range` primitives for richer merchant queries.

## Tech Stack
- **Python 3.10+** with type hints
- **MCP SDK** (`mcp` v2.x) — Model Context Protocol server
- **Pydantic** — Data validation and strict schemas
- **Tenacity** — Retry logic with exponential backoff
- **Cachetools** — In-memory TTL caching
- **Requests** — HTTP client
