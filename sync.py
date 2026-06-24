"""
Shopify <-> Trendyol Two-Way Sync
----------------------------------
Direction 1 - Shopify -> Trendyol  (every run)
  - Matches products by barcode
  - Pushes live Shopify stock + price (x 1.25 + 8) to Trendyol

Direction 2 - Trendyol -> Shopify  (every run)
  - Fetches new Trendyol orders since last check
  - Deducts sold quantities from Shopify inventory
  - Stores processed order IDs to avoid double-counting

Required environment variables (set as GitHub Secrets):
  SHOPIFY_STORE_URL      e.g. my-store.myshopify.com
  SHOPIFY_ACCESS_TOKEN   Admin API access token
  TRENDYOL_SUPPLIER_ID   Numeric supplier ID from Trendyol Seller Center
  TRENDYOL_API_KEY       API key from Trendyol Seller Center
  TRENDYOL_API_SECRET    API secret from Trendyol Seller Center
"""

import os
import sys
import time
import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SHOPIFY_STORE_URL     = os.environ["SHOPIFY_STORE_URL"].rstrip("/")
SHOPIFY_ACCESS_TOKEN  = os.environ["SHOPIFY_ACCESS_TOKEN"]
TRENDYOL_SUPPLIER_ID  = os.environ["TRENDYOL_SUPPLIER_ID"]
TRENDYOL_API_KEY      = os.environ["TRENDYOL_API_KEY"]
TRENDYOL_API_SECRET   = os.environ["TRENDYOL_API_SECRET"]

PRICE_MULTIPLIER = 1.25
PRICE_ADDEND     = 8.0
TRENDYOL_BATCH   = 100
TRENDYOL_BASE    = "https://api.trendyol.com/sapigw"
STATUS_FILE      = "sync_status.json"


def shopify_headers():
    return {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}


def trendyol_auth():
    return HTTPBasicAuth(TRENDYOL_API_KEY, TRENDYOL_API_SECRET)


def trendyol_headers():
    return {
        "Content-Type": "application/json",
        "User-Agent": f"{TRENDYOL_SUPPLIER_ID} - SelfIntegration",
    }


def calc_trendyol_price(shopify_price):
    return round(shopify_price * PRICE_MULTIPLIER + PRICE_ADDEND, 2)


def fetch_shopify_variants():
    with_barcode = []
    without_barcode = []
    params = {
        "limit": 250,
        "fields": "id,barcode,price,inventory_item_id,sku,product_id,title",
    }
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/variants.json"
    while url:
        resp = requests.get(url, headers=shopify_headers(), params=params, timeout=30)
        resp.raise_for_status()
        for v in resp.json().get("variants", []):
            barcode = (v.get("barcode") or "").strip()
            entry = {
                "product_id":        v.get("product_id"),
                "variant_id":        v["id"],
                "variant_title":     v.get("title", ""),
                "sku":               v.get("sku", ""),
                "price":             float(v["price"]),
                "inventory_item_id": v["inventory_item_id"],
                "barcode":           barcode,
            }
            (with_barcode if barcode else without_barcode).append(entry)
        link = resp.headers.get("Link", "")
        url, params = None, None
        for part in link.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break
    log.info("Variants - with barcode: %d | without barcode: %d", len(with_barcode), len(without_barcode))
    return with_barcode, without_barcode


def fetch_product_titles(product_ids):
    titles = {}
    ids = list(set(str(x) for x in product_ids if x))
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        resp = requests.get(
            f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/products.json",
            headers=shopify_headers(),
            params={"ids": ",".join(chunk), "fields": "id,title"},
            timeout=30,
        )
        resp.raise_for_status()
        for p in resp.json().get("products", []):
            titles[p["id"]] = p["title"]
    return titles


def fetch_inventory_levels(inventory_item_ids):
    levels = {}
    ids = list(inventory_item_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        resp = requests.get(
            f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels.json",
            headers=shopify_headers(),
            params={"inventory_item_ids": ",".join(str(x) for x in chunk), "limit": 250},
            timeout=30,
        )
        resp.raise_for_status()
        for lvl in resp.json().get("inventory_levels", []):
            iid = lvl["inventory_item_id"]
            qty = max(0, lvl.get("available") or 0)
            levels[iid] = levels.get(iid, 0) + qty
    return levels


def fetch_shopify_location_id():
    resp = requests.get(
        f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/locations.json",
        headers=shopify_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    locations = resp.json().get("locations", [])
    active = [loc for loc in locations if loc.get("active", True)]
    if not active:
        raise RuntimeError("No active Shopify locations found")
    log.info("Using Shopify location: %s (id=%s)", active[0]["name"], active[0]["id"])
    return active[0]["id"]


def adjust_shopify_inventory(location_id, inventory_item_id, adjustment):
    resp = requests.post(
        f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels/adjust.json",
        headers={**shopify_headers(), "Content-Type": "application/json"},
        json={
            "location_id":          location_id,
            "inventory_item_id":    inventory_item_id,
            "available_adjustment": adjustment,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("inventory_level", {}).get("available")


def push_to_trendyol(items):
    url = f"{TRENDYOL_BASE}/suppliers/{TRENDYOL_SUPPLIER_ID}/products/price-and-inventory"
    success, fail, batch_ids = 0, 0, []
    for i in range(0, len(items), TRENDYOL_BATCH):
        batch = items[i : i + TRENDYOL_BATCH]
        try:
            resp = requests.post(
                url,
                json={"items": batch},
                auth=trendyol_auth(),
                headers=trendyol_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            bid = resp.json().get("batchRequestId", "N/A")
            batch_ids.append(bid)
            log.info("Batch %d-%d -> batchId=%s", i + 1, i + len(batch), bid)
            success += len(batch)
        except requests.HTTPError as exc:
            log.error("Batch %d-%d failed: %s", i + 1, i + len(batch), exc.response.text[:300])
            fail += len(batch)
        time.sleep(0.15)
    return success, fail, batch_ids


def fetch_trendyol_orders(start_ms, end_ms):
    url = f"{TRENDYOL_BASE}/suppliers/{TRENDYOL_SUPPLIER_ID}/orders"
    all_orders = []
    page = 0
    while True:
        params = {
            "startDate":        start_ms,
            "endDate":          end_ms,
            "page":             page,
            "size":             200,
            "orderByField":     "PackageLastModifiedDate",
            "orderByDirection": "ASC",
        }
        try:
            resp = requests.get(
                url,
                auth=trendyol_auth(),
                headers=trendyol_headers(),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.HTTPError as exc:
            log.error("Failed to fetch Trendyol orders: %s", exc.response.text[:300])
            break
        content = data.get("content", [])
        all_orders.extend(content)
        total_pages = data.get("totalPages", 1)
        log.info("Orders page %d/%d - got %d orders", page + 1, total_pages, len(content))
        page += 1
        if page >= total_pages:
            break
        time.sleep(0.2)
    return all_orders


def run_shopify_to_trendyol(with_barcode, without_barcode):
    unsynced_pids = [v["product_id"] for v in without_barcode if v.get("product_id")]
    product_titles = fetch_product_titles(unsynced_pids) if unsynced_pids else {}

    inv_ids = {v["inventory_item_id"] for v in with_barcode}
    log.info("Fetching inventory for %d items...", len(inv_ids))
    levels = fetch_inventory_levels(inv_ids)

    trendyol_items = [
        {
            "barcode":   v["barcode"],
            "quantity":  levels.get(v["inventory_item_id"], 0),
            "salePrice": calc_trendyol_price(v["price"]),
            "listPrice": calc_trendyol_price(v["price"]),
        }
        for v in with_barcode
    ]

    log.info("Pushing %d items to Trendyol...", len(trendyol_items))
    ok, fail, batch_ids = push_to_trendyol(trendyol_items)
    log.info("Shopify->Trendyol complete - Sent: %d | Failed: %d", ok, fail)

    unsynced_list = []
    for v in without_barcode[:500]:
        title = product_titles.get(v["product_id"], "Unknown Product")
        unsynced_list.append({
            "title":          title,
            "variant":        v["variant_title"],
            "sku":            v["sku"] or "-",
            "shopify_price":  v["price"],
            "trendyol_price": calc_trendyol_price(v["price"]),
            "reason":         "No barcode",
        })

    return ok, fail, batch_ids, unsynced_list


def run_trendyol_to_shopify(with_barcode, previous_status):
    barcode_map = {v["barcode"]: v["inventory_item_id"] for v in with_barcode if v["barcode"]}
    log.info("Barcode map: %d entries", len(barcode_map))

    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)

    last_check_iso = previous_status.get("last_order_check")
    if last_check_iso:
        start_dt = datetime.fromisoformat(last_check_iso.replace("Z", "+00:00"))
        start_dt = start_dt - timedelta(minutes=10)
    else:
        start_dt = now_dt - timedelta(hours=24)
    start_ms = int(start_dt.timestamp() * 1000)

    log.info(
        "Fetching Trendyol orders from %s to %s",
        start_dt.strftime("%Y-%m-%d %H:%M UTC"),
        now_dt.strftime("%Y-%m-%d %H:%M UTC"),
    )

    orders = fetch_trendyol_orders(start_ms, now_ms)
    log.info("Total Trendyol orders fetched: %d", len(orders))

    processed_ids = set(previous_status.get("processed_order_ids", []))

    try:
        location_id = fetch_shopify_location_id()
    except Exception as exc:
        log.error("Could not get Shopify location: %s", exc)
        return 0, 0, list(previous_status.get("trendyol_adjustment_log", [])), list(processed_ids)

    orders_processed = 0
    items_adjusted = 0
    adjustment_log = list(previous_status.get("trendyol_adjustment_log", []))

    ACTIVE_STATUSES = {
        "Created", "Picking", "Invoiced", "Shipped", "Delivered",
        "PickingDelay", "Invoicing",
    }

    for order in orders:
        order_id = str(order.get("orderNumber") or order.get("id") or "")
        if not order_id or order_id in processed_ids:
            continue

        order_status = order.get("status", "")
        processed_ids.add(order_id)

        if order_status not in ACTIVE_STATUSES:
            log.info("Skipping order %s - status: %s", order_id, order_status)
            continue

        lines = order.get("lines", [])
        order_adjusted = 0

        for line in lines:
            barcode = (line.get("barcode") or "").strip()
            quantity = int(line.get("quantity") or 1)
            if not barcode or barcode not in barcode_map:
                log.warning("Order %s: barcode '%s' not in Shopify - skipping", order_id, barcode)
                continue

            inv_item_id = barcode_map[barcode]
            try:
                new_qty = adjust_shopify_inventory(location_id, inv_item_id, -quantity)
                log.info(
                    "Order %s: barcode=%s qty=-%d -> Shopify stock now %s",
                    order_id, barcode, quantity, new_qty,
                )
                adjustment_log.append({
                    "ts":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "order_id":   order_id,
                    "barcode":    barcode,
                    "adjustment": -quantity,
                    "new_stock":  new_qty,
                })
                items_adjusted += 1
                order_adjusted += 1
                time.sleep(0.15)
            except requests.HTTPError as exc:
                log.error(
                    "Order %s: failed to adjust barcode=%s: %s",
                    order_id, barcode, exc.response.text[:200],
                )

        if order_adjusted > 0:
            orders_processed += 1

    adjustment_log = adjustment_log[-200:]
    all_ids = list(processed_ids)[-2000:]

    log.info(
        "Trendyol->Shopify complete - Orders processed: %d | Items adjusted: %d",
        orders_processed, items_adjusted,
    )
    return orders_processed, items_adjusted, adjustment_log, all_ids


def main():
    log.info("=== Shopify <-> Trendyol two-way sync started ===")
    sync_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    previous_status = {}
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            previous_status = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log.info("No previous %s found - starting fresh", STATUS_FILE)

    with_barcode, without_barcode = fetch_shopify_variants()

    log.info("--- Direction 1: Shopify -> Trendyol ---")
    ok, fail, batch_ids, unsynced_list = run_shopify_to_trendyol(with_barcode, without_barcode)

    log.info("--- Direction 2: Trendyol -> Shopify ---")
    orders_processed, items_adjusted, adjustment_log, processed_ids = run_trendyol_to_shopify(
        with_barcode, previous_status
    )

    status = {
        "last_sync":             sync_time,
        "synced_count":          ok,
        "failed_count":          fail,
        "total_with_barcode":    len(with_barcode),
        "total_without_barcode": len(without_barcode),
        "batch_ids":             batch_ids,
        "unsynced_products":     unsynced_list,
        "status":                "success" if fail == 0 else "partial_failure",
        "last_order_check":          sync_time,
        "trendyol_orders_processed": orders_processed,
        "trendyol_items_adjusted":   items_adjusted,
        "trendyol_adjustment_log":   adjustment_log,
        "processed_order_ids":       processed_ids,
    }

    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    log.info("%s saved.", STATUS_FILE)

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
