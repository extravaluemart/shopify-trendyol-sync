"""
Shopify → Trendyol Stock & Price Sync
--------------------------------------
- Matches products by barcode
- Stock: mirrors Shopify live inventory
- Price: (Shopify price × 1.25) + 8  →  rounded to 2 decimal places

Required environment variables (set as GitHub Secrets):
  SHOPIFY_STORE_URL      e.g. my-store.myshopify.com
  SHOPIFY_ACCESS_TOKEN   Admin API access token
  TRENDYOL_SUPPLIER_ID   Numeric supplier ID from Trendyol Seller Center
  TRENDYOL_API_KEY       API key from Trendyol Seller Center
  TRENDYOL_API_SECRET    API secret from Trendyol Seller Center
"""

import os
import sys
import math
import time
import logging
import requests
from requests.auth import HTTPBasicAuth

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Config from env
SHOPIFY_STORE_URL     = os.environ["SHOPIFY_STORE_URL"].rstrip("/")
SHOPIFY_ACCESS_TOKEN  = os.environ["SHOPIFY_ACCESS_TOKEN"]
TRENDYOL_SUPPLIER_ID  = os.environ["TRENDYOL_SUPPLIER_ID"]
TRENDYOL_API_KEY      = os.environ["TRENDYOL_API_KEY"]
TRENDYOL_API_SECRET   = os.environ["TRENDYOL_API_SECRET"]

PRICE_MULTIPLIER = 1.25
PRICE_ADDEND     = 8.0
TRENDYOL_BATCH   = 100   # max items per Trendyol request


# Shopify helpers

def shopify_get(path, params=None):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/{path}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp


def fetch_shopify_variants():
    """
    Returns a list of dicts:
      { barcode, price (float), inventory_item_id }
    Handles Shopify pagination automatically.
    """
    variants = []
    params = {"limit": 250, "fields": "id,barcode,price,inventory_item_id"}
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/variants.json"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("variants", [])
        for v in data:
            barcode = (v.get("barcode") or "").strip()
            if barcode:
                variants.append({
                    "barcode": barcode,
                    "price": float(v["price"]),
                    "inventory_item_id": v["inventory_item_id"],
                })

        # Follow Link header for next page
        link = resp.headers.get("Link", "")
        url = None
        params = None
        for part in link.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break

    log.info("Fetched %d Shopify variants with barcodes.", len(variants))
    return variants


def fetch_inventory_levels(inventory_item_ids):
    """
    Returns dict: { inventory_item_id (int): available_qty (int) }
    Shopify allows up to 100 IDs per request.
    """
    levels = {}
    ids = list(inventory_item_ids)
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {
            "inventory_item_ids": ",".join(str(x) for x in chunk),
            "limit": 250,
        }
        url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/inventory_levels.json"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        for lvl in resp.json().get("inventory_levels", []):
            iid = lvl["inventory_item_id"]
            qty = lvl.get("available") or 0
            levels[iid] = levels.get(iid, 0) + max(0, qty)

    return levels


# Trendyol helpers

TRENDYOL_BASE = "https://api.trendyol.com/sapigw"


def trendyol_auth():
    return HTTPBasicAuth(TRENDYOL_API_KEY, TRENDYOL_API_SECRET)


def push_to_trendyol(items):
    """
    items: list of { barcode, quantity, salePrice, listPrice }
    Sends in batches of TRENDYOL_BATCH.
    Returns (success_count, fail_count).
    """
    url = (
        f"{TRENDYOL_BASE}/suppliers/{TRENDYOL_SUPPLIER_ID}"
        f"/products/price-and-inventory"
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"{TRENDYOL_SUPPLIER_ID} - SelfIntegration",
    }

    success = 0
    fail = 0

    for i in range(0, len(items), TRENDYOL_BATCH):
        batch = items[i:i + TRENDYOL_BATCH]
        payload = {"items": batch}

        try:
            resp = requests.post(
                url,
                json=payload,
                auth=trendyol_auth(),
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            batch_id = result.get("batchRequestId", "N/A")
            log.info(
                "Batch %d-%d Trendyol batchId=%s",
                i + 1, i + len(batch), batch_id,
            )
            success += len(batch)
        except requests.HTTPError as exc:
            log.error(
                "Batch %d-%d failed: %s %s",
                i + 1, i + len(batch), exc, exc.response.text[:300],
            )
            fail += len(batch)

        # Respect Trendyol rate limits
        time.sleep(0.15)

    return success, fail


# Price formula

def calc_trendyol_price(shopify_price: float) -> float:
    return round(shopify_price * PRICE_MULTIPLIER + PRICE_ADDEND, 2)


# Main

def main():
    log.info("=== Shopify Trendyol sync started ===")

    # 1. Fetch all Shopify variants with barcodes
    variants = fetch_shopify_variants()
    if not variants:
        log.warning("No variants with barcodes found. Nothing to sync.")
        return

    # 2. Fetch inventory levels
    inv_item_ids = {v["inventory_item_id"] for v in variants}
    log.info("Fetching inventory levels for %d inventory items...", len(inv_item_ids))
    levels = fetch_inventory_levels(inv_item_ids)

    # 3. Build Trendyol update payload
    items = []
    skipped = 0
    for v in variants:
        qty = levels.get(v["inventory_item_id"], 0)
        sale_price = calc_trendyol_price(v["price"])
        items.append({
            "barcode":    v["barcode"],
            "quantity":   qty,
            "salePrice":  sale_price,
            "listPrice":  sale_price,
        })

    log.info(
        "Prepared %d items to push (skipped %d without inventory data).",
        len(items), skipped,
    )

    # 4. Push to Trendyol
    ok, fail = push_to_trendyol(items)
    log.info("Sync complete. Sent=%d  Failed=%d", ok, fail)

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
