#!/usr/bin/env python3
"""One-off: replace Shopee Star / Shopee Mall historical data for Jan-Jul 2026
(both shops) with freshly re-downloaded monthly reports, then re-upload to GCS
and rebuild both combined Parquet files.

Reuses every download/cleanup/upload function from shopee_scheduled_export.py
as-is (run_one, clear_stale_period_files, delete_stale_remote_files,
convert_xlsx_to_csv, upload_raw_files) so this gets the same stale-file
cleanup (both local and GCS) that the nightly pipeline already has -- a
replaced month's old file(s) (xlsx and csv, whatever part-count they had
before) are deleted before the fresh ones are saved, both locally and in GCS,
so no orphaned old parts linger the way the month-to-date ones did.
"""
import calendar
import sys
import traceback
from datetime import date

import shopee_scheduled_export as sse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

MONTHS = [date(2026, m, 1) for m in range(1, 8)]  # Jan .. Jul 2026


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def main():
    sse.log("=== Historical replace run starting (Jan-Jul 2026, both shops) ===")
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

            for shop in sse.SHOPS:
                try:
                    sse.switch_shop(page, shop)
                    spc_cds = sse.get_spc_cds(context)
                except Exception:
                    sse.log(f"ERROR switching to shop {shop}, skipping it entirely:\n{traceback.format_exc()}")
                    continue

                for m_start in MONTHS:
                    m_end = month_end(m_start)
                    label = f"replace_{m_start:%Y-%m}"
                    try:
                        sse.run_one(page, spc_cds, shop, label,
                                    m_start.isoformat(), m_end.isoformat(),
                                    clear_stale_start=m_start)
                    except Exception:
                        sse.log(f"ERROR downloading {shop}/{label}:\n{traceback.format_exc()}")
                        continue

                    # Old CSV (any prior part-count) for this same month, both
                    # locally and in GCS -- same mechanism run_one just used
                    # on the xlsx side, applied here to the staging folder.
                    dest_dir = sse.CSV_DESTINATIONS[shop]
                    try:
                        removed_csv = sse.clear_stale_period_files(dest_dir, m_start, ext=".csv")
                        if removed_csv:
                            sse.log(f"  removed {len(removed_csv)} stale CSV file(s) for {m_start:%Y-%m}: "
                                     f"{removed_csv[:3]}{'...' if len(removed_csv) > 3 else ''}")
                            sse.delete_stale_remote_files(shop, removed_csv)
                    except Exception:
                        sse.log(f"ERROR clearing stale CSVs for {shop}/{m_start:%Y-%m}:\n{traceback.format_exc()}")

                try:
                    # One full re-conversion pass covers every replaced month's
                    # xlsx at once; its own stale-clear (keyed to MONTHS[-1])
                    # is a no-op here since each month was already cleared above.
                    sse.convert_xlsx_to_csv(shop, MONTHS[-1])
                except Exception:
                    sse.log(f"ERROR converting {shop} xlsx -> csv:\n{traceback.format_exc()}")

                try:
                    uploaded, skipped_gcs = sse.upload_raw_files(shop)
                    sse.log(f"  uploaded {uploaded} raw file(s) to gs://{sse.GCS_BUCKET}/{sse.GCS_RAW_PREFIX}/{shop}/ "
                             f"({skipped_gcs} already up to date)")
                except Exception:
                    sse.log(f"ERROR uploading {shop} raw files to GCS:\n{traceback.format_exc()}")

            sse.log("=== Historical replace: downloads complete ===")
        finally:
            context.close()

    try:
        sse.request_github_parquet_rebuild()
        sse.log("Asked GitHub Actions to rebuild Shopee_bq.parquet")
    except Exception:
        sse.log(f"ERROR asking GitHub to rebuild Shopee_bq.parquet:\n{traceback.format_exc()}")

    sse.log("=== Historical replace run complete ===")


if __name__ == "__main__":
    main()
