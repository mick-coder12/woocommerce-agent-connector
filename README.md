# WooCommerce Agent Connector

A **read-only MCP (Model Context Protocol) server** that lets an AI agent list, search and inspect **orders** and **products/inventory** in a WooCommerce store. It ships with a built-in **demo mode** (fictional data), so you can evaluate it without a store or credentials.

> Scope note: this repository demonstrates an MCP-compatible connector. It has **not** been wired into Razorpay Agent Studio itself; any MCP-capable host can launch it over stdio.

## Why I built it

Merchants on WooCommerce want agents that can answer "which orders are still pending?" or "what's out of stock?" without a human opening wp-admin. Putting an LLM in front of merchant data raises four practical questions, and the design follows from them:

| Concern | What the connector does |
|---|---|
| Agent can't use a raw REST API well | 4 small tools with explicit enums, limits and descriptions, instead of mirroring every WooCommerce query parameter |
| Customer data in an LLM context | Compact responses; email, phone, street address, customer notes and meta_data are **never** returned |
| Flaky / rate-limited merchant hosting | Bounded retry with exponential backoff, `Retry-After` support, explicit timeouts |
| "Is this real data?" | Demo responses carry `demo_mode: true` plus a notice; partial credentials are an error, never a silent fallback to demo |

## Architecture

```
 Agent / MCP host (e.g. Agent Studio, MCP Inspector)
          |
          |  MCP over stdio
          v
 +------------------------------------------------------------+
 | server.py          tool schemas + descriptions, error mapping |
 |   v                                                          |
 | connector.py       input validation -> demo | live           |
 |                    TTL cache (orders 60s, products 5 min)     |
 |   |                                    |                      |
 |   v (live)                             v (demo)               |
 | woo_client.py                       demo_data.py              |
 |  API-key auth, timeouts,            fictional, WooCommerce-   |
 |  retry/backoff, error mapping       shaped records            |
 |   |                                    |                      |
 |   +------------------+-----------------+                      |
 |                      v                                        |
 | models.py          strict parsing of raw JSON -> compact      |
 |                    response shapes (drops unneeded fields)    |
 +------------------------------------------------------------+
          |  HTTPS, GET only, HTTP Basic (consumer key/secret)
          v
 WooCommerce REST API  (/wp-json/wc/v3/orders, /products)
```

Demo mode runs the **same** validation, filtering, pagination and parsing code as live mode; only the data source differs.

## MCP Tools

| Tool | Purpose | Key inputs |
|---|---|---|
| `list_orders` | List orders newest-first; summaries only | `page`, `per_page` (1-100), `status`, `created_after`, `created_before` (YYYY-MM-DD) |
| `get_order` | Full detail of one order (line items, ship-to city/state/country) | `order_id` (positive int) |
| `list_products` | Search / list inventory; summaries only | `page`, `per_page` (1-100), `search` (max 100 chars), `stock_status` (`instock` / `outofstock` / `onbackorder`) |
| `get_product` | Detail of one product incl. plain-text description | `product_id` (positive int) |

Valid `status` values: `any` (default), `pending`, `processing`, `on-hold`, `completed`, `cancelled`, `refunded`, `failed`.

Behaviour agents can rely on:

* **Search** = `list_products(search=...)` (keyword against product name/description). **Filtering** = order status, order date range, product stock status. **Pagination** = every list tool returns `{items, page, per_page, total, total_pages, has_more, demo_mode}`; when `has_more` is true, call again with `page + 1`.
* **Validation happens before any data access**, identically in demo and live mode. Invalid input returns an actionable message (`per_page must be between 1 and 100 (got 500).`), not an API error. `per_page > 100` is **rejected, not silently clamped**.
* Tools are registered with the MCP `readOnlyHint` annotation where the installed SDK supports it.

## Agent Examples

Real output from `python demo.py` (demo data; dates are relative to the day you run it, long lists trimmed).

**1. "Show me my pending orders."** → `list_orders(status="pending")`
The agent gets a small, ready-to-summarise page (2 orders) instead of a raw WooCommerce payload:

```json
{"items": [{"id": 5003, "status": "pending", "date_created": "2026-09-30T09:15:00", "total": "11998",
            "currency": "INR", "customer_name": "Priya Patel", "item_count": 2, "payment_method": "Cash on delivery"},
           {"id": 5004, "status": "pending", "...": "..."}],
 "page": 1, "per_page": 10, "total": 2, "total_pages": 1, "has_more": false, "demo_mode": true}
```

**2. "Which products are out of stock?"** → `list_products(stock_status="outofstock")`

```json
{"items": [{"id": 104, "name": "Mechanical Keyboard RGB", "sku": "KEY-MK-104", "status": "publish",
            "price": "3799", "regular_price": "3799", "sale_price": "", "on_sale": false,
            "stock_status": "outofstock", "stock_quantity": 0}],
 "total": 1, "has_more": false, "demo_mode": true}
```

**3. "Show me the details for order 5001."** → `get_order(order_id=5001)`
Note what is *absent*: no email, phone or street address. The agent still gets what it needs to answer "what did they buy and where is it going?":

```json
{"order": {"id": 5001, "status": "completed", "total": "6298", "currency": "INR",
           "customer_name": "Ananya Sharma", "payment_method": "Razorpay", "customer_id": 12,
           "ship_to": {"city": "Bangalore", "state": "KA", "country": "IN"},
           "items": [{"product_id": 101, "name": "Premium Wireless Headphones", "quantity": 1, "total": "4999"},
                     {"product_id": 102, "name": "USB-C Fast Charger 65W", "quantity": 1, "total": "1299"}]},
 "demo_mode": true}
```

**4. "Find products containing 'keyboard'."** → `list_products(search="keyboard")` returns product 104. The agent can chain to `get_product(104)` or to example 2's stock check.

**Errors are written for the agent to act on**, e.g. `get_order(9999)` →
`Order 9999 was not found. Use list_orders to find valid order IDs.`, and `list_orders(per_page=500)` →
`per_page must be between 1 and 100 (got 500).`

## Authentication

WooCommerce **REST API keys** (consumer key + consumer secret) over HTTPS with HTTP Basic auth. OAuth is not used or claimed.

1. WordPress admin → **WooCommerce → Settings → Advanced → REST API → Add key**.
2. Set **Permissions: Read**. The connector only issues `GET` requests, so a Read key is all it needs and limits the blast radius if the key leaks.
3. Provide the three values as **environment variables** (see [Configuration](#configuration)). Credentials are read from the environment only, never from files in the repo or from tool arguments.

Startup checks: all-or-nothing configuration (a partially set config is an error that names the missing *variable names*, never values); `https://` is required except for `localhost`; URLs with embedded credentials are rejected; redirects are not followed, so credentials are never forwarded to another host.

## Rate Limits & Reliability

Implemented in `woo_client.py` (simple loop, no retry library, easy to read):

| Situation | Behaviour |
|---|---|
| HTTP **429**, **502**, **503**, **504** | Retried |
| Request **timeout** (connect 5s / read 15s) | Retried |
| **Connection error** | Retried |
| Backoff | Exponential: 1s, 2s, 4s |
| `Retry-After` header | Honoured (seconds or HTTP-date) in place of the computed backoff |
| `Retry-After` > 30s | Fails fast with a message telling the agent to try later, rather than blocking a tool call |
| Maximum | **4 attempts** per call; the loop is bounded, so it cannot retry forever |
| 400 / 401 / 403 / 404 / other 5xx | **Not retried**; reported once with a specific message |

After retries are exhausted the agent gets a short message (e.g. `WooCommerce rate limit exceeded; gave up after 4 attempt(s). Try again later.`). Technical details (status, endpoint, a truncated body) go to the server log on stderr only.

Note: WooCommerce core's REST API has no built-in rate limiter. A 429 typically comes from the host, a CDN/WAF or a security plugin, so the headers and limits vary per store.

## Caching

In-memory, per-process TTL cache for **successful** `get_order` (60 s) and `get_product` (5 min) lookups, so an agent re-checking the same record while reasoning doesn't repeat API calls. Orders and products use **separate** caches (an order and a product can share the same numeric ID). Errors are never cached. Lists are not cached. Demo mode doesn't use the cache. Trade-off: data can be up to the TTL stale.

## Safety Boundaries

* **Read-only by construction.** The client exposes only `GET`; there is no create/update/delete code path.
* **Data minimisation.** Never returned: customer email, phone, street address, postcode, customer notes, order `meta_data`, payment transaction details, API credentials. Still returned: customer **name**, city/state/country and order contents, because agents need them to be useful, so treat the agent as having access to that.
* **Untrusted text.** Product descriptions are merchant-authored free text and customer names are customer-authored; both reach the model. Customer notes (a common prompt-injection vector) are deliberately excluded and descriptions are stripped of HTML and truncated to 500 chars.
* **Secrets.** Never hardcoded, logged, returned in tool output or included in error messages (covered by tests). `.env` is git-ignored; `.env.example` has placeholders only.
* **Errors.** Agents see short curated messages; stack traces and raw HTTP bodies stay in the server log.

## Can / Cannot

**Can**

* List orders, filtered by status and creation date, with pagination
* Inspect one order (items, ship-to city/state/country, payment method label)
* Search products by keyword, filter by stock status, paginate
* Inspect one product (price, sale price, stock status/quantity, description)

**Cannot**

* Create, update or delete anything (orders, products, stock, customers)
* List or look up customers, coupons, refunds, shipping, or reports (no such tools)
* See product variations (variable products show as one product; their price may be empty)
* Search orders by customer or free text (only status and date range)
* Receive webhooks / real-time events (request-response only)
* Take payments or talk to Razorpay
* See anything the configured API key's WordPress user can't read

## Implemented vs demo vs not implemented

| | Status |
|---|---|
| API-key auth, 4 read tools, validation, pagination, filters, retry/backoff, timeouts, caching, compact responses, error mapping, secret hygiene | **Implemented** (verified by unit tests with mocked HTTP; see Testing) |
| Demo mode: fictional orders/products with WooCommerce-shaped records, relative dates | **Simulated** (clearly labelled `demo_mode: true` + notice in every response) |
| Centralised secrets, metrics/tracing, shared cache, webhooks, OAuth, sandbox integration tests | **Recommended for production, not implemented** (see below) |

## Running Locally

Requires Python 3.10+ (Node.js only for MCP Inspector).

```bash
git clone https://github.com/mick-coder12/woocommerce-agent-connector.git
cd woocommerce-agent-connector
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python server.py          # starts the MCP server on stdio (demo mode if no credentials are set)
```

Live mode:

```bash
cp .env.example .env      # edit .env, then:
set -a; source .env; set +a
python server.py
```

Example MCP host config (do not commit real keys; `mcp_config.json` is git-ignored):

```json
{
  "mcpServers": {
    "woocommerce": {
      "command": "python",
      "args": ["/absolute/path/to/woocommerce-agent-connector/server.py"],
      "env": {
        "WOOCOMMERCE_STORE_URL": "https://your-store.example.com",
        "WOOCOMMERCE_CONSUMER_KEY": "ck_...",
        "WOOCOMMERCE_CONSUMER_SECRET": "cs_..."
      }
    }
  }
}
```

## Demo Mode

No store or credentials needed. Demo mode activates when **none** of the three variables is set.

```bash
# A) Quick walk-through of realistic agent questions (no MCP client needed)
python demo.py

# B) The real MCP tools in a browser UI (needs Node.js)
npx @modelcontextprotocol/inspector python server.py
# open the URL printed in the terminal -> Connect -> Tools -> List Tools -> pick a tool -> Run Tool
```

`demo.py` calls the same connector code the tools use, without the MCP transport; MCP Inspector exercises the actual MCP server. Demo data: 5 orders (IDs 5001-5005; statuses completed, processing, pending x2, cancelled) and 5 products (IDs 101-105, including one `outofstock` and one `onbackorder`). Try `list_orders(per_page=2, page=2)`, `list_orders(status="pending")`, `list_products(stock_status="outofstock")`, `get_order(9999)`, `list_orders(per_page=500)`.

## Testing

```bash
python -m unittest discover -s tests -v
```

Standard library only; no store, network or credentials needed (HTTP is mocked). The suite covers: list/get for orders and products, search and filters, pagination (including past-the-end page), invalid parameters (rejected identically in demo and live, with zero HTTP calls), `per_page` maximum, 429 retry + backoff + `Retry-After`, retry bound, timeouts and connection errors, non-retryable HTTP errors, malformed API responses, secret/PII leakage, cache behaviour and demo-mode labelling. One test checks the four tools are registered with the MCP SDK and is skipped if the `mcp` package isn't installed. `pytest` also works if you prefer it.

## Configuration

`.env.example`:

```bash
WOOCOMMERCE_STORE_URL=https://your-store.example.com
WOOCOMMERCE_CONSUMER_KEY=ck_replace_me
WOOCOMMERCE_CONSUMER_SECRET=cs_replace_me
```

The server does not parse `.env` itself; export the variables (as above) or let your MCP host inject them.

## Assumptions & Limitations

* Store runs WooCommerce REST API v3 (`/wp-json/wc/v3`) with REST enabled and pretty permalinks.
* Order dates and the `created_after`/`created_before` filters use the **store's timezone**; `created_*` bounds are inclusive.
* Amounts are decimal strings in the store currency (the `currency` field is returned for orders); the connector does no currency conversion or arithmetic.
* Product search relies on WooCommerce's own keyword search; relevance ranking is WooCommerce's.
* Cache and demo data live in process memory; there is no persistence.
* Single store per server process (one set of credentials).
* Live-mode behaviour is verified against mocked HTTP responses shaped like WooCommerce's, not a production store.

## Production Considerations

Not implemented here, but recommended for a real deployment:

* Centralised secrets management / key rotation instead of env vars
* Observability: structured logs, metrics (retry counts, latency, error rates), tracing
* A shared cache (e.g. Redis) if several connector instances run
* Webhook/event ingestion so agents can react instead of poll
* OAuth or per-merchant credential flows if the merchant environment requires them
* Integration tests against a WooCommerce sandbox store, and contract tests for API-version changes
* Per-tool audit logging of which agent queried which records

## Repository layout

| File | Role |
|---|---|
| `server.py` | MCP tool definitions/descriptions, error mapping, entrypoint |
| `connector.py` | Input validation, demo/live switch, pagination envelope, TTL cache |
| `woo_client.py` | Config from env, API-key auth, timeouts, retry/backoff, error mapping |
| `models.py` | Strict parsing of raw WooCommerce JSON into compact response shapes |
| `demo_data.py` | Fictional WooCommerce-shaped demo records |
| `demo.py` | Runnable walk-through of agent scenarios on demo data |
| `tests/` | Unit tests (mocked HTTP) |
