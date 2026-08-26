"""
Daily Unleashed -> Supabase sync.

Pulls:
  - StockOnHand (per-warehouse availability, allocated, on-order, product group)
  - Products (to exclude obsolete/discontinued SKUs)
  - Assemblies (the kanban)

Writes a fresh snapshot into the unleashed_stock and assemblies tables each run
(deletes old rows, inserts current ones), so the dashboard always reflects
today's Unleashed data exactly, with nothing stale lingering.

Required environment variables (set as GitHub Actions secrets, never hardcoded):
  UNLEASHED_API_ID
  UNLEASHED_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   <- the SECRET key, not the publishable one. Server-side only.
"""

import os
import re
import hmac
import hashlib
import base64
import requests
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from collections import defaultdict

UNLEASHED_API_ID = os.environ["UNLEASHED_API_ID"]
UNLEASHED_API_KEY = os.environ["UNLEASHED_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

UNLEASHED_BASE = "https://api.unleashedsoftware.com"
CLIENT_TYPE = "nyrahbeauty/dashboardsync"


def unleashed_signature(query_string: str) -> str:
    """HMAC-SHA256 of the query string only (no endpoint name, no leading '?'),
    using the API key as the secret, base64-encoded. Per Unleashed's auth docs."""
    digest = hmac.new(
        UNLEASHED_API_KEY.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def unleashed_get_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    """Fetches every page of a Unleashed list endpoint and returns the combined items."""
    params = dict(params or {})
    params.setdefault("pageSize", 200)
    items = []
    page = 1

    while True:
        page_params = dict(params, page=page) if page > 1 else params
        query_string = urlencode(page_params)
        url = f"{UNLEASHED_BASE}/{endpoint}?{query_string}" if query_string else f"{UNLEASHED_BASE}/{endpoint}"

        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "api-auth-id": UNLEASHED_API_ID,
                "api-auth-signature": unleashed_signature(query_string),
                "client-type": CLIENT_TYPE,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        # Unleashed wraps list results under a key matching the endpoint name, e.g. "Items"
        items_key = next((k for k in data if isinstance(data[k], list)), None)
        page_items = data.get(items_key, []) if items_key else []
        items.extend(page_items)

        pagination = data.get("Pagination", {})
        number_of_pages = pagination.get("NumberOfPages", 1)
        if page >= number_of_pages:
            break
        page += 1

    return items


def fetch_obsolete_codes() -> set[str]:
    products = unleashed_get_all_pages("Products", {"includeObsolete": "true"})
    return {p["ProductCode"] for p in products if p.get("Obsolete")}


def fetch_stock_rows(obsolete_codes: set[str]) -> list[dict]:
    raw = unleashed_get_all_pages("StockOnHand")

    by_sku = defaultdict(lambda: {
        "name": "", "product_group": None,
        "available": 0, "allocated": 0, "on_order": 0,
        "warehouses": [],
    })

    for row in raw:
        sku = row.get("ProductCode")
        if not sku or sku in obsolete_codes:
            continue
        entry = by_sku[sku]
        entry["name"] = row.get("ProductDescription") or entry["name"]
        entry["product_group"] = row.get("ProductGroupName") or entry["product_group"]
        entry["available"] += float(row.get("AvailableQty") or 0)
        entry["allocated"] += float(row.get("AllocatedQty") or 0)
        # OnPurchase appears to be an account-wide figure repeated per warehouse row,
        # not warehouse-specific — take the max seen rather than summing, to avoid
        # double-counting. Worth double-checking against your account's actual data.
        entry["on_order"] = max(entry["on_order"], float(row.get("OnPurchase") or 0))
        warehouse_name = row.get("Warehouse")
        if warehouse_name:
            entry["warehouses"].append({"name": warehouse_name, "qty": float(row.get("AvailableQty") or 0)})

    return [{"sku": sku, **data} for sku, data in by_sku.items()]


def parse_unleashed_date(raw: str | None) -> str | None:
    """Unleashed returns dates as '/Date(1669852800000)/' (milliseconds since epoch),
    a legacy .NET JSON convention — not a plain ISO date string. This converts it to
    a normal 'YYYY-MM-DD' string that Postgres's date column will accept."""
    if not raw:
        return None
    match = re.search(r"/Date\((-?\d+)", raw)
    if match:
        millis = int(match.group(1))
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date().isoformat()
    # Fallback, in case a future API response gives a plain ISO date instead
    return raw[:10] if len(raw) >= 10 else None


ASSEMBLY_STATUS_MAP = {
    "parked": "parked",
    "in progress": "progress",
    "completed": "done",
}


def fetch_assembly_rows() -> list[dict]:
    # Without a date filter, this pulls every assembly since the account began,
    # which is slow and unnecessary — the dashboard only needs recent/current ones.
    start_date = (date.today() - timedelta(days=30)).isoformat()
    raw = unleashed_get_all_pages("Assemblies", {"includeObsolete": "false", "startDate": start_date})
    rows = []
    for a in raw:
        status_raw = (a.get("AssemblyStatus") or "").strip().lower()
        status = ASSEMBLY_STATUS_MAP.get(status_raw)
        if not status:
            continue  # skip any status outside our three kanban columns
        rows.append({
            "assembly_number": a.get("AssemblyNumber"),
            "product_name": a.get("Product", {}).get("ProductDescription") or a.get("AssemblyNumber"),
            "qty": float(a.get("Quantity") or 0),
            "due_date": parse_unleashed_date(a.get("AssemblyDate")),  # see caveat in file header
            "status": status,
        })

    # Safety net: assembly_number is the table's primary key, so duplicates would
    # break the insert. Keep the last occurrence of each if any slip through.
    deduped = {r["assembly_number"]: r for r in rows if r["assembly_number"]}
    return list(deduped.values())


def supabase_replace_table(table: str, rows: list[dict], conflict_col: str):
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    # Clear the table, then insert the fresh snapshot — guarantees no stale/removed
    # SKUs or assemblies linger with outdated numbers.
    del_resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}?{conflict_col}=neq.__none__",
        headers=headers, timeout=60,
    )
    if not del_resp.ok:
        print(f"  Delete failed: {del_resp.status_code} {del_resp.text}")
    del_resp.raise_for_status()

    if not rows:
        return

    # Insert in batches to stay well under request size limits
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ins_resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers, json=batch, timeout=60,
        )
        if not ins_resp.ok:
            print(f"  Insert failed: {ins_resp.status_code} {ins_resp.text}")
        ins_resp.raise_for_status()


def main():
    print("Fetching obsolete product list...")
    obsolete_codes = fetch_obsolete_codes()
    print(f"  {len(obsolete_codes)} obsolete SKUs will be excluded")

    print("Fetching stock on hand...")
    stock_rows = fetch_stock_rows(obsolete_codes)
    print(f"  {len(stock_rows)} products (incl. packaging)")

    print("Fetching assemblies...")
    assembly_rows = fetch_assembly_rows()
    print(f"  {len(assembly_rows)} assemblies")

    print("Writing to Supabase: unleashed_stock...")
    supabase_replace_table("unleashed_stock", stock_rows, "sku")

    print("Writing to Supabase: assemblies...")
    supabase_replace_table("assemblies", assembly_rows, "assembly_number")

    print("Done.")


if __name__ == "__main__":
    main()
