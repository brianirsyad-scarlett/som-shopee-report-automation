#!/usr/bin/env python3
"""Faster continuation of shopee_fix_mall_gaps.py.

Jan and Mar 2026 are confirmed complete (31/31 days each, verified via
shopee_check_day_coverage.py) and are skipped here. The remaining periods
(Apr-Sep) are downloaded the same way (chunked, see run_chunked), but the
per-period xlsx->csv conversion and GCS raw upload are done ONCE at the end
instead of after every single period -- the original script's biggest time
sink was re-converting and re-verifying the *entire* Mall history on every
period, which only gets slower as more months are added.
"""
import sys
import traceback
from datetime import date

import shopee_scheduled_export as sse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from shopee_fix_mall_gaps import clear_old_family_in_range

SHOP = "scarlettofficialshop"

PERIODS = [
    (date(2026, 4, 1), date(2026, 4, 30)),
    (date(2026, 5, 1), date(2026, 5, 31)),
    (date(2026, 6, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 7, 31)),
    (date(2026, 8, 1), date(2026, 8, 31)),
    (date(2026, 9, 1), date.today()),
]


def main():
    sse.log("=== Shopee Mall gap-fix run 2 starting (Apr-Sep, deferred convert/upload) ===")
    sse.ensure_onedrive_running()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            sse.PROFILE_DIR, headless=True, viewport={"width": 1280, "height": 800}
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                sse.goto_with_retry(page, sse.ORDERS_PAGE, wait_until="domcontentloaded", timeout=30000)
            except PWTimeoutError:
                sse.log("ERROR: could not reach Shopee Seller Center after retries. Aborting.")
                sys.exit(1)

            if "/login" in page.url or not sse.get_spc_cds(context):
                sse.log("ERROR: not logged in (saved session expired).")
                sys.exit(1)

            try:
                sse.switch_shop(page, SHOP)
                spc_cds = sse.get_spc_cds(context)
            except Exception:
                sse.log(f"ERROR switching to shop {SHOP}, aborting:\n{traceback.format_exc()}")
                sys.exit(1)

            xlsx_dir = sse.SHOP_DESTINATIONS[SHOP]
            csv_dir = sse.CSV_DESTINATIONS[SHOP]

            for period_start, period_end in PERIODS:
                sse.log(f"--- {period_start} -> {period_end} ---")
                removed_xlsx = clear_old_family_in_range(xlsx_dir, period_start, period_end, ".xlsx")
                removed_csv = clear_old_family_in_range(csv_dir, period_start, period_end, ".csv")
                removed = removed_xlsx + removed_csv
                if removed:
                    sse.log(f"  cleared {len(removed)} old file(s) for this period before re-downloading")
                    sse.delete_stale_remote_files(SHOP, removed)

                try:
                    sse.run_chunked(page, spc_cds, SHOP,
                                     f"gapfix2_{period_start:%Y%m}", period_start, period_end,
                                     chunk_days=5)
                except Exception:
                    sse.log(f"ERROR chunked-downloading {period_start}-{period_end}:\n{traceback.format_exc()}")

            sse.log("=== All periods downloaded. Converting + uploading once ===")
            try:
                sse.convert_xlsx_to_csv(SHOP, PERIODS[-1][0])
            except Exception:
                sse.log(f"ERROR converting xlsx->csv:\n{traceback.format_exc()}")

            try:
                uploaded, skipped_gcs = sse.upload_raw_files(SHOP)
                sse.log(f"  uploaded {uploaded} raw file(s) ({skipped_gcs} already up to date)")
            except Exception:
                sse.log(f"ERROR uploading raw files:\n{traceback.format_exc()}")

            sse.log("=== Shopee Mall gap-fix run 2: downloads complete ===")
        finally:
            context.close()

    try:
        sse.request_github_parquet_rebuild()
        sse.log("Asked GitHub Actions to rebuild Shopee_bq.parquet")
    except Exception:
        sse.log(f"ERROR asking GitHub to rebuild Shopee_bq.parquet:\n{traceback.format_exc()}")

    sse.log("=== Shopee Mall gap-fix run 2 complete ===")


if __name__ == "__main__":
    main()
