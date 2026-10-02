"""Walk through realistic agent questions against the DEMO data. No store, no credentials, no MCP client needed.

    python demo.py

This calls the same Connector code the MCP tools call (validation, filtering, compact
output), minus the MCP transport. To see the real MCP tools, use MCP Inspector (see README).
It always runs on fictional demo data, even if WooCommerce env vars are set.
"""
import json

from connector import Connector
from demo_data import DEMO_NOTICE
from woo_client import ConnectorError

connector = Connector()   # no client -> demo mode


def ask(question: str, call: str, fn):
    print(f"\n{'=' * 78}\nUser : {question}\nAgent: -> {call}")
    try:
        result = fn()
        result.pop("notice", None)          # printed once below
        print(json.dumps(result, indent=2))
    except ConnectorError as exc:
        print(f"Tool error returned to the agent: {exc}")


print(DEMO_NOTICE)
ask("Show me my pending orders.", 'list_orders(status="pending")',
    lambda: connector.list_orders(status="pending"))
ask("Which products are out of stock?", 'list_products(stock_status="outofstock")',
    lambda: connector.list_products(stock_status="outofstock"))
ask("Show me the details for order 5001.", "get_order(order_id=5001)",
    lambda: connector.get_order(5001))
ask("Find products containing 'keyboard'.", 'list_products(search="keyboard")',
    lambda: connector.list_products(search="keyboard"))
ask("Show order 9999 (does not exist).", "get_order(order_id=9999)", lambda: connector.get_order(9999))
ask("Give me 500 orders at once.", "list_orders(per_page=500)", lambda: connector.list_orders(per_page=500))
