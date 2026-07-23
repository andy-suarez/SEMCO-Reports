#!/usr/bin/env python3
"""
SEMCO FL Cost Report exporter — location-aware line-item export.

Single-file FastAPI service. Serves a form (date range + location filter),
pulls orders via Shopify Admin GraphQL, splits line-item quantities by
fulfillment location, and returns a CSV download.

Env vars (set in Render dashboard):
  SHOPIFY_STORE   e.g. semco-florida.myshopify.com
  SHOPIFY_TOKEN   Admin API access token (shpat_...)
  APP_USER        Basic auth username for the page (e.g. "semco")
  APP_PASS        Basic auth password
  STORE_TZ_OFFSET Optional, default "-04:00" (store timezone for date bounds)
  API_VERSION     Optional, default "2026-01"

Run: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import csv
import io
import os
import secrets
import time
from collections import defaultdict
from datetime import date

import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

STORE = os.environ["SHOPIFY_STORE"]
TOKEN = os.environ["SHOPIFY_TOKEN"]
APP_USER = os.environ["APP_USER"]
APP_PASS = os.environ["APP_PASS"]
TZ_OFFSET = os.environ.get("STORE_TZ_OFFSET", "-04:00")
API_VERSION = os.environ.get("API_VERSION", "2026-01")

ENDPOINT = f"https://{STORE}/admin/api/{API_VERSION}/graphql.json"
HEADERS = {"X-Shopify-Access-Token": TOKEN, "Content-Type": "application/json"}

LOCATIONS = ["SEMCO Las Vegas Warehouse", "SEMCO Florida Warehouse"]

app = FastAPI(title="SEMCO FL Cost Report")
security = HTTPBasic()


def check_auth(creds: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(creds.username, APP_USER)
    ok_pass = secrets.compare_digest(creds.password, APP_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


QUERY = """
query($cursor: String, $q: String!) {
  orders(first: 50, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name
      createdAt
      cancelledAt
      subtotalPriceSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      totalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      shippingAddress { name company }
      lineItems(first: 100) {
        nodes {
          id
          sku
          name
          quantity
          discountedUnitPriceSet { shopMoney { amount } }
        }
      }
      fulfillments(first: 25) {
        status
        location { name }
        fulfillmentLineItems(first: 100) {
          nodes {
            quantity
            lineItem { id }
          }
        }
      }
      fulfillmentOrders(first: 25) {
        status
        assignedLocation { name }
        lineItems(first: 100) {
          nodes {
            remainingQuantity
            lineItem { id }
          }
        }
      }
    }
  }
}
"""


def gql(variables: dict) -> dict:
    """POST the query with simple throttle handling."""
    for attempt in range(6):
        resp = requests.post(
            ENDPOINT, headers=HEADERS,
            json={"query": QUERY, "variables": variables}, timeout=60,
        )
        if resp.status_code == 429:
            time.sleep(2.0)
            continue
        resp.raise_for_status()
        data = resp.json()
        errors = data.get("errors")
        if errors:
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2.0)
                continue
            raise RuntimeError(f"GraphQL errors: {errors}")
        return data["data"]
    raise RuntimeError("Throttled repeatedly; gave up after 6 attempts.")


def money(node: dict | None) -> str:
    if not node:
        return ""
    return node.get("shopMoney", {}).get("amount", "")


def fetch_rows(start: date, end: date, loc_filter: str) -> list[list]:
    """Pull orders in [start, end] inclusive (store time) and split by location."""
    q = (
        f"created_at:>='{start.isoformat()}T00:00:00{TZ_OFFSET}' "
        f"created_at:<='{end.isoformat()}T23:59:59{TZ_OFFSET}'"
    )
    rows: list[list] = []
    cursor = None

    while True:
        data = gql({"cursor": cursor, "q": q})
        conn = data["orders"]

        for order in conn["nodes"]:
            if order.get("cancelledAt"):
                continue

            # Line-item lookup: id -> (sku, name, unit_price, ordered_qty)
            items = {}
            for li in order["lineItems"]["nodes"]:
                items[li["id"]] = {
                    "sku": li.get("sku") or "",
                    "name": li.get("name") or "",
                    "unit_price": money(li.get("discountedUnitPriceSet")),
                    "qty": li.get("quantity") or 0,
                }

            # Shipped quantities per (line item, location)
            shipped = defaultdict(int)
            for f in order.get("fulfillments") or []:
                if f.get("status") != "SUCCESS":
                    continue
                loc = (f.get("location") or {}).get("name") or "UNKNOWN"
                for fli in f["fulfillmentLineItems"]["nodes"]:
                    li_id = fli["lineItem"]["id"]
                    shipped[(li_id, loc)] += fli.get("quantity") or 0

            # Unshipped remainder per (line item, assigned location)
            pending = defaultdict(int)
            for fo in order.get("fulfillmentOrders") or []:
                if fo.get("status") in ("CANCELLED", "INCOMPLETE"):
                    continue
                loc = (fo.get("assignedLocation") or {}).get("name") or "UNKNOWN"
                for foli in fo["lineItems"]["nodes"]:
                    rq = foli.get("remainingQuantity") or 0
                    if rq > 0:
                        pending[(foli["lineItem"]["id"], loc)] += rq

            # Assemble rows: shipped first, then pending
            order_rows = []
            for (li_id, loc), qty in sorted(shipped.items(), key=lambda k: k[0][1]):
                order_rows.append((li_id, loc, qty, "FULFILLED"))
            for (li_id, loc), qty in sorted(pending.items(), key=lambda k: k[0][1]):
                order_rows.append((li_id, loc, qty, "UNFULFILLED"))

            first = True
            for li_id, loc, qty, state in order_rows:
                if loc_filter != "All" and loc != loc_filter:
                    continue
                item = items.get(li_id)
                if item is None:
                    continue  # line item removed/edited out of the order
                unit = item["unit_price"]
                try:
                    line_total = f"{qty * float(unit):.2f}" if unit else ""
                except ValueError:
                    line_total = ""
                ship = order.get("shippingAddress") or {}
                rows.append([
                    order["name"] if first else "",
                    money(order["subtotalPriceSet"]) if first else "",
                    money(order["totalShippingPriceSet"]) if first else "",
                    money(order["totalTaxSet"]) if first else "",
                    money(order["totalPriceSet"]) if first else "",
                    money(order["totalDiscountsSet"]) if first else "",
                    money(order["totalRefundedSet"]) if first else "",
                    order["createdAt"] if first else "",
                    qty,
                    item["name"],
                    unit,
                    line_total,
                    item["sku"],
                    loc,
                    state,
                    (ship.get("name") or "") if first else "",
                    (ship.get("company") or "") if first else "",
                ])
                first = False

        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]

    return rows


HEADER = [
    "Name", "Subtotal (Net)", "Shipping", "Taxes", "Total", "Discount",
    "Refunded Amount", "Created at", "Item Amount", "Lineitem name",
    "Price Sold (Per unit)", "Price Sold (Total)", "SKU",
    "Fulfillment Location", "Fulfillment State",
    "Shipping Name", "Shipping Company",
]

FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>SEMCO FL Cost Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: Arial, sans-serif; max-width: 480px; margin: 60px auto; padding: 0 20px; color: #222; }}
  h1 {{ font-size: 1.3rem; }}
  label {{ display: block; margin: 14px 0 4px; font-weight: bold; font-size: 0.9rem; }}
  input, select {{ width: 100%; padding: 8px; font-size: 1rem; box-sizing: border-box; }}
  button {{ margin-top: 20px; width: 100%; padding: 12px; font-size: 1rem; font-weight: bold;
           background: #1a5632; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }}
  button:hover {{ background: #143f25; }}
  p.hint {{ font-size: 0.8rem; color: #666; }}
</style></head>
<body>
<h1>SEMCO Cost Report Export</h1>
<form action="/export" method="get">
  <label>Start date</label>
  <input type="date" name="start" required>
  <label>End date</label>
  <input type="date" name="end" required>
  <label>Location</label>
  <select name="location">
    <option>All</option>
    {options}
  </select>
  <button type="submit">Run &amp; Download CSV</button>
</form>
<p class="hint">Dates are inclusive, store time (ET). Rows tagged UNFULFILLED are
assigned to a warehouse but not yet shipped. Large ranges may take a minute.</p>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def form(_=Depends(check_auth)):
    opts = "\n".join(f"<option>{loc}</option>" for loc in LOCATIONS)
    return FORM_HTML.format(options=opts)


@app.get("/export")
def export(
    start: date = Query(...),
    end: date = Query(...),
    location: str = Query("All"),
    _=Depends(check_auth),
):
    if end < start:
        raise HTTPException(status_code=400, detail="End date is before start date.")
    if location != "All" and location not in LOCATIONS:
        raise HTTPException(status_code=400, detail="Unknown location.")

    rows = fetch_rows(start, end, location)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HEADER)
    writer.writerows(rows)
    buf.seek(0)

    loc_tag = "ALL" if location == "All" else ("FL" if "Florida" in location else "LV")
    fname = f"cost_report_{loc_tag}_{start.isoformat()}_{end.isoformat()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}
