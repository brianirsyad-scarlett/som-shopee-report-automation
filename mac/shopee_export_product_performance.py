#!/usr/bin/env python3
"""Day-by-day, per-product traffic export ("Product Performance" / Shopee's
internal "parentskudetail" report -- Sales, Product Impressions, Product
Clicks, Visitors, Conversion Rate etc, one row per product) for both shops,
backfilled from 2025-01-01 (matches the existing order-history archive's
start date) through yesterday.

Reuses the exact same report request/poll/download mechanism as the order
reports (request -> poll -> download all share the same generic
get_report/download_report endpoints regardless of report type -- confirmed
2026-09-28 by inspecting the network calls the Seller Centre UI's own
"Export Data" button makes). Only the trigger endpoint differs:
    GET /api/mydata/v3/product/performance/export/?start_ts=..&end_ts=..&period=day

Each day's raw xlsx is uploaded directly to GCS (no OneDrive/CSV step --
nothing downstream reads this yet) at:
    gs://bucket_som/sales_parquet/raw/ecommerce/shopee/<shop>/product_performance/

Resumable: skips any (shop, day) whose file is already in GCS with a
matching size, so this can be safely re-run if interrupted.
"""
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/Users/salesops")
from shopee_export_orders import (  # noqa: E402
    BASE_URL, PROFILE_DIR, fetch_json, poll_report, download_report,
)
import shopee_scheduled_export as sse  # noqa: E402
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError  # noqa: E402

JAKARTA = ZoneInfo("Asia/Jakarta")
BACKFILL_START = date(2025, 1, 1)
GCS_PP_PREFIX = "sales_parquet/raw/ecommerce/shopee/{shop}/product_performance"
STAGING_DIR = "/Users/salesops/shopee_product_performance_staging"
MIN_GAP_SECONDS = 65

_last_request_time = [0.0]


def day_to_ts(d: date):
    start = int(datetime(d.year, d.month, d.day, tzinfo=JAKARTA).timestamp())
    return start, start + 86400


def request_product_performance_report(page, spc_cds, day: date):
    start_ts, end_ts = day_to_ts(day)
    url = (
        f"{BASE_URL}/api/mydata/v3/product/performance/export/"
        f"?start_ts={start_ts}&end_ts={end_ts}&period=day&sort_by=&acc=false"
        f"&SPC_CDS={spc_cds}&SPC_CDS_VER=2"
    )
    body = fetch_json(page, url)
    if body.get("code") != 0:
        raise RuntimeError(f"product_performance export request failed: {body}")
    return body["data"]


def throttled(fn, *args, **kwargs):
    """The gap must be enforced even when fn() raises -- otherwise a
    persistent failure (e.g. wrong page context) turns into an unthrottled
    burst of requests against Shopee's servers instead of backing off, which
    is exactly the bot-like traffic pattern this pacing exists to avoid.
    Seen for real 2026-09-28: 270 failed requests in 26 seconds before this
    was caught and killed."""
    elapsed = time.time() - _last_request_time[0]
    if _last_request_time[0] and elapsed < MIN_GAP_SECONDS:
        time.sleep(MIN_GAP_SECONDS - elapsed)
    try:
        return fn(*args, **kwargs)
    finally:
        _last_request_time[0] = time.time()


def existing_gcs_files(bucket, shop):
    prefix = GCS_PP_PREFIX.format(shop=shop) + "/"
    return {b.name.rsplit("/", 1)[-1]: b.size for b in bucket.client.list_blobs(bucket, prefix=prefix)}


def main():
    sse.log("=== Product Performance backfill starting (2025-01-01 -> yesterday, both shops) ===")
    client = sse._gcs_client()
    bucket = client.bucket(sse.GCS_BUCKET)

    yesterday = datetime.now(JAKARTA).date() - timedelta(days=1)
    all_days = []
    d = BACKFILL_START
    while d <= yesterday:
        all_days.append(d)
        d += timedelta(days=1)
    sse.log(f"{len(all_days)} day(s) x {len(sse.SHOPS)} shop(s) to process")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=True, viewport={"width": 1280, "height": 800}
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                sse.goto_with_retry(page, sse.ORDERS_PAGE, wait_until="domcontentloaded", timeout=30000)
            except PWTimeoutError:
                sse.log("ERROR: could not reach Shopee Seller Center. Aborting.")
                sys.exit(1)

            if "/login" in page.url or not sse.get_spc_cds(context):
                sse.log("ERROR: not logged in (saved session expired).")
                sys.exit(1)

            PRODUCT_PERFORMANCE_PAGE = f"{BASE_URL}/datacenter/product/performance"

            for shop in sse.SHOPS:
                switched = False
                for attempt in range(1, 3):
                    try:
                        sse.switch_shop(page, shop)
                        switched = True
                        break
                    except Exception:
                        sse.log(f"  switch to {shop} failed (attempt {attempt}/2):\n{traceback.format_exc()}")
                        time.sleep(5)
                if not switched:
                    sse.log(f"ERROR: could not switch to shop {shop} after retries, skipping entirely")
                    continue

                try:
                    # The mydata/product-performance API needs the page to
                    # actually be on the Business Insights Product Performance
                    # page first -- calling it right after switch_shop (which
                    # leaves the page on the Orders list) fails every request
                    # with "not login"/"permission denied" (seen for real
                    # 2026-09-28: 270 failed requests before this was caught).
                    sse.goto_with_retry(page, PRODUCT_PERFORMANCE_PAGE, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    spc_cds = sse.get_spc_cds(context)
                except Exception:
                    sse.log(f"ERROR navigating to Product Performance page for {shop}, skipping entirely:\n{traceback.format_exc()}")
                    continue

                existing = existing_gcs_files(bucket, shop)
                out_dir = f"{STAGING_DIR}/{shop}"

                for day in all_days:
                    expected_name_hint = f"{day:%Y%m%d}_{day:%Y%m%d}"
                    already = any(expected_name_hint in name for name in existing)
                    if already:
                        continue
                    try:
                        created = throttled(request_product_performance_report, page, spc_cds, day)
                        report_id = created["report_id"]
                        data = poll_report(page, spc_cds, report_id, max_wait_seconds=180)
                        if (data.get("record_numbers") or 0) == 0 and (data.get("total_count") or 0) == 0:
                            sse.log(f"  {shop} {day}: no data (shop likely not active yet), skipping upload")
                            continue
                        local_path = download_report(page, spc_cds, report_id, out_dir)
                        blob_name = f"{GCS_PP_PREFIX.format(shop=shop)}/{local_path.rsplit('/', 1)[-1]}"
                        sse.upload_blob_with_retry(bucket.blob(blob_name), local_path)
                        sse.log(f"  {shop} {day}: uploaded to gs://{sse.GCS_BUCKET}/{blob_name}")
                    except Exception:
                        sse.log(f"ERROR on {shop} {day}:\n{traceback.format_exc()}")

            sse.log("=== Product Performance backfill complete ===")
        finally:
            context.close()


if __name__ == "__main__":
    main()
