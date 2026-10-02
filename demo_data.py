"""Demo / mock data. Shaped exactly like raw WooCommerce REST responses (including
fields the connector deliberately drops, such as email, phone and street address)
so demo mode exercises the same parsing and filtering code as live mode.

ALL DATA BELOW IS FICTIONAL. Names, e-mails and phone numbers are made up.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List

DEMO_NOTICE = ("DEMO DATA: these are fictional records served by the connector's built-in demo mode "
               "because no WooCommerce credentials are configured. They do not come from a real store.")

DEMO_PRODUCTS: List[Dict[str, Any]] = [
    {"id": 101, "name": "Premium Wireless Headphones", "sku": "AUD-HP-101", "type": "simple", "status": "publish",
     "description": "<p>Noise-cancelling over-ear headphones with <strong>30-hour</strong> battery life.</p>",
     "permalink": "https://demo-store.example.com/product/premium-wireless-headphones",
     "price": "4999", "regular_price": "6999", "sale_price": "4999", "on_sale": True,
     "manage_stock": True, "stock_quantity": 142, "stock_status": "instock"},
    {"id": 102, "name": "USB-C Fast Charger 65W", "sku": "PWR-CH-102", "type": "simple", "status": "publish",
     "description": "<p>GaN charger with dual USB-C ports, compatible with laptops and phones.</p>",
     "price": "1299", "regular_price": "1299", "sale_price": "", "on_sale": False,
     "manage_stock": True, "stock_quantity": 530, "stock_status": "instock"},
    {"id": 103, "name": "Ergonomic Laptop Stand", "sku": "ACC-LS-103", "type": "simple", "status": "publish",
     "description": "<p>Adjustable aluminium stand with ventilation for 11-17 inch laptops.</p>",
     "price": "2499", "regular_price": "2499", "sale_price": "", "on_sale": False,
     "manage_stock": True, "stock_quantity": 78, "stock_status": "instock"},
    {"id": 104, "name": "Mechanical Keyboard RGB", "sku": "KEY-MK-104", "type": "simple", "status": "publish",
     "description": "<p>Hot-swappable Cherry MX switches, per-key RGB, full-size layout.</p>",
     "price": "3799", "regular_price": "3799", "sale_price": "", "on_sale": False,
     "manage_stock": True, "stock_quantity": 0, "stock_status": "outofstock"},
    {"id": 105, "name": "Smart Home Hub", "sku": "HOM-SH-105", "type": "simple", "status": "publish",
     "description": "<p>Matter-compatible hub supporting Zigbee, Thread and Wi-Fi devices.</p>",
     "price": "5999", "regular_price": "7499", "sale_price": "5999", "on_sale": True,
     "manage_stock": True, "stock_quantity": 0, "stock_status": "onbackorder"},
]


def _order(oid: int, status: str, day: date, time: str, total: str, cid: int, first: str, last: str,
           city: str, state: str, pay: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    slug = f"{first}.{last}".lower()
    return {
        "id": oid, "status": status, "currency": "INR", "date_created": f"{day.isoformat()}T{time}",
        "total": total, "customer_id": cid, "payment_method_title": pay,
        "customer_note": "(dropped by the connector)",
        "billing": {"first_name": first, "last_name": last, "email": f"{slug}@example.com",
                    "phone": "0000000000", "address_1": "1 Demo Street", "city": city, "state": state, "country": "IN"},
        "shipping": {"first_name": first, "last_name": last, "address_1": "1 Demo Street",
                     "city": city, "state": state, "postcode": "000000", "country": "IN"},
        "line_items": items,
    }


def demo_orders(today: date) -> List[Dict[str, Any]]:
    """Orders dated relative to `today` so date filters always have something to show. Newest first."""
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    return [
        _order(5001, "completed", d(0), "10:30:00", "6298", 12, "Ananya", "Sharma", "Bangalore", "KA", "Razorpay",
               [{"id": 1, "name": "Premium Wireless Headphones", "product_id": 101, "quantity": 1, "total": "4999"},
                {"id": 2, "name": "USB-C Fast Charger 65W", "product_id": 102, "quantity": 1, "total": "1299"}]),
        _order(5002, "processing", d(1), "14:05:00", "2499", 15, "Rohan", "Mehta", "Mumbai", "MH", "Razorpay",
               [{"id": 3, "name": "Ergonomic Laptop Stand", "product_id": 103, "quantity": 1, "total": "2499"}]),
        _order(5003, "pending", d(2), "09:15:00", "11998", 22, "Priya", "Patel", "New Delhi", "DL", "Cash on delivery",
               [{"id": 4, "name": "Smart Home Hub", "product_id": 105, "quantity": 2, "total": "11998"}]),
        _order(5004, "pending", d(4), "17:40:00", "1299", 31, "Karthik", "Iyer", "Chennai", "TN", "Razorpay",
               [{"id": 5, "name": "USB-C Fast Charger 65W", "product_id": 102, "quantity": 1, "total": "1299"}]),
        _order(5005, "cancelled", d(5), "11:00:00", "3799", 8, "Vikram", "Singh", "Pune", "MH", "Razorpay",
               [{"id": 6, "name": "Mechanical Keyboard RGB", "product_id": 104, "quantity": 1, "total": "3799"}]),
    ]
