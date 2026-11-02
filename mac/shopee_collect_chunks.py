"""Collect (or request, then collect) specific Shopee report chunks for ONE shop.

Why: Shopee generates a shop's reports one after another, so a big chunk can need longer than the nightly
job's wait, and the chunks queued behind it time out too -- yet they keep generating on Shopee's side
(2026-11-02: Mall Oct 6-25). This picks them up by report_id instead of requesting them again.

    python3 shopee_collect_chunks.py scarlettofficialshop previous_month_20261006_20261010:2026-10-06:2026-10-10:63006232 \
                                                          previous_month_20261026_20261030:2026-10-26:2026-10-30
(no report_id after the dates = request it now). Never run while shopee_scheduled_export.py is running.
Afterwards run shopee_guard.py: it converts new xlsx to csv, uploads them to GCS and asks GitHub to rebuild.
"""
import sys
import traceback
from datetime import date

import shopee_scheduled_export as sse
from playwright.sync_api import sync_playwright

shop = sys.argv[1]
specs = []
for a in sys.argv[2:]:
    parts = a.split(":")
    specs.append((parts[0], parts[1], parts[2], int(parts[3]) if len(parts) > 3 else None))

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(sse.PROFILE_DIR, headless=True, viewport={"width": 1280, "height": 800})
    try:
        page = context.pages[0] if context.pages else context.new_page()
        sse.goto_with_retry(page, sse.ORDERS_PAGE, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url or not sse.get_spc_cds(context):
            sys.exit("not logged in -- run: python3 ~/shopee_login.py")
        sse.switch_shop(page, shop)
        spc_cds = sse.get_spc_cds(context)
        # request the new ones first (they queue server-side), then collect everything
        todo = []
        for label, start, end, rid in specs:
            if rid is None:
                created = sse.throttled_request_report(page, spc_cds, start, end, f"{shop}/{label}")
                rid = created["report_id"]
                sse.log(f"  report_id={rid} records~={created.get('record_numbers')}")
            todo.append((label, start, end, rid))
        ok = bad = 0
        for label, start, end, rid in todo:
            try:
                sse.poll_report(page, spc_cds, rid, max_wait_seconds=3600)
                saved = sse.download_flat_xlsx(page, spc_cds, rid, sse.SHOP_DESTINATIONS[shop],
                                               clear_stale_start=date.fromisoformat(start), shop=shop)
                sse.log(f"  {label}: saved {len(saved)} .xlsx file(s)")
                ok += 1
            except Exception:
                bad += 1
                sse.log(f"ERROR collecting {shop}/{label} (report {rid}):\n{traceback.format_exc()}")
        sse.log(f"collect done: {ok} chunk(s) saved, {bad} failed")
    finally:
        context.close()
