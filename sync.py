"""
Shopify -> Trendyol Stock & Price Sync
---------------------------------------
- Matches products by barcode
- Stock: mirrors Shopify live inventory
- Price: (Shopify price x 1.25) + 8  ->  rounded to 2 decimal places
- Saves sync_status.json after every run (read by the dashboard)

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
from datetime import datetime, timezone
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


def shopify_headers():
    return {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}


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


def push_to_trendyol(items):
    url = f"{TRENDYOL_BASE}/suppliers/{TRENDYOL_SUPPLIER_ID}/products/price-and-inventory"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"{TRENDYOL_SUPPLIER_ID} - SelfIntegration",
    }
    auth = HTTPBasicAuth(TRENDYOL_API_KEY, TRENDYOL_API_SECRET)
    success, fail, batch_ids = 0, 0, []
    for i in range(0, len(items), TRENDYOL_BATCH):
        batch = items[i : i + TRENDYOL_BATCH]
        try:
            resp = requests.post(url, json={"items": batch}, auth=auth, headers=headers, timeout=30)
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


def main():
    log.info("=== Shopify -> Trendyol sync started ===")
    sync_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with_barcode, without_barcode = fetch_shopify_variants()

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
    log.info("Sync complete - Sent: %d | Failed: %d", ok, fail)

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

    status = {
        "last_sync":             sync_time,
        "synced_count":          ok,
        "failed_count":          fail,
        "total_with_barcode":    len(with_barcode),
        "total_without_barcode": len(without_barcode),
        "batch_ids":             batch_ids,
        "unsynced_products":     unsynced_list,
        "status":                "success" if fail == 0 else "partial_failure",
    }
    with open("sync_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    log.info("sync_status.json saved.")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
