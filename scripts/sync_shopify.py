"""
Daily Shopify -> Supabase sync.

Pulls every open (unfulfilled or partially fulfilled) order, then works out:
  - Overview stats: open order count, total units still outstanding, and how
    many orders have been sitting unfulfilled for 48+ hours.
  - A per-SKU breakdown: how many units are on order for each product, and
    across how many distinct orders.

Built on Shopify's GraphQL Admin API — Shopify has been moving all new
integrations here since REST was marked legacy in late 2024.

As of January 2026, Shopify requires apps created via the Dev Dashboard to
authenticate with a client credentials grant rather than a static token.
Tokens from this flow expire after 24 hours, so this script requests a fresh
one at the start of every run — a good fit since it only runs once a day.

Required environment variables (set as GitHub Actions secrets):
  SHOPIFY_STORE_DOMAIN     e.g. nyrah-beauty.myshopify.com
  SHOPIFY_CLIENT_ID        from your app's Settings page in the Dev Dashboard
  SHOPIFY_CLIENT_SECRET    from the same page
  SUPABASE_URL
  SUPABASE_SERVICE_KEY     <- the SECRET key, not the publishable one. Server-side only.
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

SHOPIFY_STORE_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]
SHOPIFY_CLIENT_ID = os.environ["SHOPIFY_CLIENT_ID"]
SHOPIFY_CLIENT_SECRET = os.environ["SHOPIFY_CLIENT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

API_VERSION = "2026-07"
GRAPHQL_URL = f"https://{SHOPIFY_STORE_DOMAIN}/admin/api/{API_VERSION}/graphql.json"


def get_access_token() -> str:
    """Exchanges the app's client ID + secret for a fresh access token, good
    for 24 hours. We just need it for the next few minutes, so no need to
    cache or refresh it mid-run."""
    resp = requests.post(
        f"https://{SHOPIFY_STORE_DOMAIN}/admin/oauth/access_token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]

ORDERS_QUERY = """
query OpenOrders($cursor: String) {
  orders(first: 50, after: $cursor, query: "fulfillment_status:unfulfilled OR fulfillment_status:partial") {
    edges {
      cursor
      node {
        id
        name
        createdAt
        lineItems(first: 100) {
          edges {
            node {
              sku
              name
              fulfillableQuantity
            }
          }
        }
      }
    }
    pageInfo { hasNextPage }
  }
}
"""


def shopify_graphql(access_token: str, query: str, variables: dict) -> dict:
    for attempt in range(5):
        resp = requests.post(
            GRAPHQL_URL,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        errors = data.get("errors", [])
        if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
            wait = 2 ** attempt
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue

        if errors:
            raise RuntimeError(f"Shopify GraphQL error: {errors}")

        return data["data"]

    raise RuntimeError("Gave up after repeated rate-limit retries")


def fetch_open_orders(access_token: str) -> list[dict]:
    orders = []
    cursor = None
    while True:
        data = shopify_graphql(access_token, ORDERS_QUERY, {"cursor": cursor})
        page = data["orders"]
        for edge in page["edges"]:
            node = edge["node"]
            line_items = [
                {
                    "sku": li["node"].get("sku"),
                    "name": li["node"].get("name"),
                    "fulfillable_qty": li["node"].get("fulfillableQuantity") or 0,
                }
                for li in node["lineItems"]["edges"]
            ]
            orders.append({
                "id": node["id"],
                "name": node["name"],
                "created_at": node["createdAt"],
                "line_items": line_items,
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["edges"][-1]["cursor"]
    return orders


def summarise(orders: list[dict]) -> tuple[dict, list[dict]]:
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)

    open_orders = len(orders)
    units_on_order = 0
    unfulfilled_48h = 0

    per_sku_units = defaultdict(float)
    per_sku_orders = defaultdict(set)
    per_sku_name = {}

    for order in orders:
        created = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        order_has_outstanding = False

        for li in order["line_items"]:
            qty = li["fulfillable_qty"]
            if qty <= 0:
                continue
            units_on_order += qty
            order_has_outstanding = True

            sku = li["sku"]
            if sku:  # skip line items with no SKU (custom/bundled items) for the per-product table
                per_sku_units[sku] += qty
                per_sku_orders[sku].add(order["id"])
                per_sku_name[sku] = li["name"]

        if order_has_outstanding and created < cutoff_48h:
            unfulfilled_48h += 1

    overview = {
        "open_orders": open_orders,
        "units_on_order": int(units_on_order),
        "unfulfilled_48h": unfulfilled_48h,
    }

    line_items = [
        {
            "sku": sku,
            "name": per_sku_name[sku],
            "units_on_order": int(units),
            "order_count": len(per_sku_orders[sku]),
        }
        for sku, units in per_sku_units.items()
    ]

    return overview, line_items


def supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def write_overview(overview: dict):
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/shopify_overview?id=eq.1",
        headers=supabase_headers(),
        json={**overview, "updated_at": datetime.now(timezone.utc).isoformat()},
        timeout=60,
    )
    if not resp.ok:
        print(f"  Overview update failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()


def write_line_items(line_items: list[dict]):
    headers = supabase_headers()
    del_resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/shopify_line_items?sku=neq.__none__",
        headers=headers, timeout=60,
    )
    if not del_resp.ok:
        print(f"  Delete failed: {del_resp.status_code} {del_resp.text}")
    del_resp.raise_for_status()

    if not line_items:
        return

    batch_size = 500
    for i in range(0, len(line_items), batch_size):
        batch = line_items[i:i + batch_size]
        ins_resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/shopify_line_items",
            headers=headers, json=batch, timeout=60,
        )
        if not ins_resp.ok:
            print(f"  Insert failed: {ins_resp.status_code} {ins_resp.text}")
        ins_resp.raise_for_status()


def main():
    print("Requesting a fresh access token...")
    access_token = get_access_token()

    print("Fetching open Shopify orders...")
    orders = fetch_open_orders(access_token)
    print(f"  {len(orders)} open orders")

    overview, line_items = summarise(orders)
    print(f"  Overview: {overview}")
    print(f"  {len(line_items)} distinct SKUs on order")

    print("Writing to Supabase: shopify_overview...")
    write_overview(overview)

    print("Writing to Supabase: shopify_line_items...")
    write_line_items(line_items)

    print("Done.")


if __name__ == "__main__":
    main()
