#!/usr/bin/env python3
"""Fix Shopee Mall's truncated reports: Shopee's report generation silently
drops the later days of a large date range (confirmed 2026-09-28 via
shopee_check_day_coverage.py -- e.g. April's report only had real data for
days 1-4 of 30). Re-downloads each affected period in small chunks (see
run_chunked in shopee_scheduled_export.py) instead of one big request, after
clearing out the old, gap-ridden whole-period file family (which used a
different filename prefix than the new chunks will, so it won't get
overwritten automatically).

Covers every month Jan-Aug 2026 plus the current Sept month-to-date range
(Feb was already clean and is left alone). Star is not affected (small,
single-file reports) and is not touched.
"""
import calendar
import sys
import traceback
from datetime import date

import shopee_scheduled_export as sse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

SHOP = "scarlettofficialshop"  # Mall only -- Star confirmed unaffected

# (period_start, period_end) -- whole months Jan/Mar-Aug, plus Sept month-to-date
PERIODS = [
    (date(2026, 1, 1), date(2026, 1, 31)),
    (date(2026, 3, 1), date(2026, 3, 31)),
    (date(2026, 4, 1), date(2026, 4, 30)),
    (date(2026, 5, 1), date(2026, 5, 31)),
    (date(2026, 6, 1), date(2026, 6, 30)),
    (date(2026, 7, 1), date(2026, 7, 31)),
    (date(2026, 8, 1), date(2026, 8, 31)),
    (date(2026, 9, 1), date.today()),
]


def clear_old_family_in_range(dest_dir, range_start, range_end, ext):
    """Delete any existing Order.all.<start>_<end>[...] file (any old
    granularity -- whole-month, half-month, whatever) whose start-date falls
    inside [range_start, range_end]. The new chunked files use 5-day-window
    start dates that won't collide with these old, larger-window names, so
    they need to be cleared explicitly rather than naturally overwritten."""
    import os
    if not os.path.isdir(dest_dir):
        return []
    removed = []
    for name in os.listdir(dest_dir):
        if not name.startswith("Order.all.") or not name.lower().endswith(ext):
            continue
        try:
            start_str = name[len("Order.all."):].split("_", 1)[0]
            start_d = date(int(start_str[:4]), int(start_str[4:6]), int(start_str[6:8]))
        except (ValueError, IndexError):
            continue
        if range_start <= start_d <= range_end:
            try:
                os.remove(os.path.join(dest_dir, name))
                removed.append(name)
            except OSError as e:
                sse.log(f"  WARNING: could not delete old file {name}: {e}")
    return removed


def main():
    sse.log("=== Shopee Mall gap-fix run starting (chunked re-download) ===")
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
                                     f"gapfix_{period_start:%Y%m}", period_start, period_end,
                                     chunk_days=5)
                except Exception:
                    sse.log(f"ERROR chunked-downloading {period_start}-{period_end}:\n{traceback.format_exc()}")
                    continue

                try:
                    sse.convert_xlsx_to_csv(SHOP, period_start)
                except Exception:
                    sse.log(f"ERROR converting xlsx->csv after {period_start}:\n{traceback.format_exc()}")

                try:
                    uploaded, skipped_gcs = sse.upload_raw_files(SHOP)
                    sse.log(f"  uploaded {uploaded} raw file(s) ({skipped_gcs} already up to date)")
                except Exception:
                    sse.log(f"ERROR uploading raw files after {period_start}:\n{traceback.format_exc()}")

            sse.log("=== Shopee Mall gap-fix: downloads complete ===")
        finally:
            context.close()

    try:
        sse.request_github_parquet_rebuild()
        sse.log("Asked GitHub Actions to rebuild Shopee_bq.parquet")
    except Exception:
        sse.log(f"ERROR asking GitHub to rebuild Shopee_bq.parquet:\n{traceback.format_exc()}")

    sse.log("=== Shopee Mall gap-fix run complete ===")


if __name__ == "__main__":
    main()
