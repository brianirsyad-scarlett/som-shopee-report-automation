#!/usr/bin/env python3
"""
Unattended daily export: for each Shopee shop on this account, downloads two
order reports:
  - "month_to_date": day 1 of the current month -> today
  - "previous_month": day 1 -> last day of the previous month

Each report is saved as plain .xlsx file(s) directly into that shop's
"Seller Data" destination folder (see SHOP_DESTINATIONS) -- if Shopee returns
a .zip (large shops get split into multiple part files), it's unzipped in
place and only the .xlsx entries are kept, flattened straight into the
destination (no extra subfolder, no leftover .zip).

After both shops are downloaded, every .xlsx in each "Seller Data" folder is
converted to .csv into the sibling "Shopee Star"/"Shopee Mall" folder (mirrors
the existing csv_converter.py in each of those folders), and finally GitHub
Actions (som-shopee-report-automation) is asked to rebuild Shopee_bq.parquet from the raw files in
GCS. This Mac builds no Parquet at all any more.

Stale month-to-date files (yesterday's date range, now superseded by today's)
are deleted before writing fresh ones -- on both the .xlsx and .csv side --
so they don't pile up daily. previous_month files aren't touched this way
since their filename is stable all month and simply overwrites in place.

Meant to be triggered once a day at 00:00 Asia/Jakarta by a LaunchAgent (see
com.brian.shopee-export.plist). Reuses the persisted login profile from
shopee_export_orders.py (~/.shopee_seller_profile) headlessly -- it does NOT
prompt for login, since nobody's watching at midnight. If the saved session
has expired, it logs an error and exits; run shopee_export_orders.py by hand
to log in again.

Shopee's report system rejects a new report request if the previous one was
started less than ~1 minute ago, so requests here are spaced with a minimum
gap (MIN_GAP_SECONDS) regardless of how many shops/periods are queued.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shopee_export_orders import (  # noqa: E402
    BASE_URL,
    ORDERS_PAGE,
    PROFILE_DIR,
    JS_FETCH_BINARY,
    get_spc_cds,
    request_order_report,
    poll_report,
)
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError  # noqa: E402
import pandas as pd  # noqa: E402

SHOPS = ["scarlett_whitening", "scarlettofficialshop"]
SHOP_LIST_PAGE = f"{BASE_URL}/portal/shop"
MIN_GAP_SECONDS = 65  # small buffer over Shopee's ~60s minimum between report requests

ANCHANTO_ROOT = (
    "/Users/salesops/Library/CloudStorage/OneDrive-PT.OptoLumbungSejahtera"
    "/SOM/Anchanto Report/E-Commerce Data"
)
SHOP_DESTINATIONS = {
    "scarlett_whitening": os.path.join(ANCHANTO_ROOT, "Shopee Star Seller Data"),
    "scarlettofficialshop": os.path.join(ANCHANTO_ROOT, "Shopee Mall Seller Data"),
}
CSV_DESTINATIONS = {
    "scarlett_whitening": os.path.join(ANCHANTO_ROOT, "Shopee Star"),
    "scarlettofficialshop": os.path.join(ANCHANTO_ROOT, "Shopee Mall"),
}

GCS_BUCKET = "bucket_som"
GCS_SA_KEY = os.path.expanduser("~/scrwms-automation/gcs-service-account.json")
GCS_RAW_PREFIX = "sales_parquet/raw/online/shopee"
GCS_BQ_PARQUET_PATH = "sales_parquet/raw/online/shopee/Shopee_bq.parquet"

LOG_FILE = os.path.expanduser("~/shopee_export_orders_scheduled.log")
JAKARTA = ZoneInfo("Asia/Jakarta")

_last_request_time = [0.0]


def log(msg):
    line = f"[{datetime.now(JAKARTA).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_current_shop_username(page):
    try:
        return page.locator(".subaccount-name").first.inner_text(timeout=5000).strip()
    except Exception:
        return None


def switch_shop(page, target_username):
    current = get_current_shop_username(page)
    if current == target_username:
        return page
    log(f"Switching shop: {current!r} -> {target_username!r}")
    page.goto(SHOP_LIST_PAGE, wait_until="domcontentloaded", timeout=60000)
    row = page.locator(f"tr:has-text('{target_username}')").first
    try:
        # The shop table populates asynchronously after the page loads, so wait
        # (up to 20s) for the row's own "Details" link rather than a fixed sleep.
        row.locator("text=Details").click(timeout=20000)
    except PWTimeoutError:
        raise RuntimeError(
            f"Shop '{target_username}' not found in the shop switcher list "
            "(timed out waiting for its row to appear)"
        )
    page.wait_for_timeout(2500)
    # Orders pages can carry 200k+ rows; don't wait for full "load" (all XHRs/images),
    # just DOM-ready is enough since we only need the header and then hit the API directly.
    page.goto(ORDERS_PAGE, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    new_current = get_current_shop_username(page)
    if new_current != target_username:
        raise RuntimeError(f"Switch to '{target_username}' failed (still on {new_current!r})")
    return page


def month_to_date_range(today: date):
    return today.replace(day=1), today


def previous_month_range(today: date):
    last_month_end = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start, last_month_end


def throttled_request_report(page, spc_cds, start_date, end_date, label):
    elapsed = time.time() - _last_request_time[0]
    if _last_request_time[0] and elapsed < MIN_GAP_SECONDS:
        wait = MIN_GAP_SECONDS - elapsed
        log(f"  waiting {wait:.0f}s before next report request (system rate limit)...")
        time.sleep(wait)
    log(f"Requesting {label}: {start_date} -> {end_date}")
    created = request_order_report(page, spc_cds, start_date, end_date, "id", "order_creation_date")
    _last_request_time[0] = time.time()
    return created


def clear_stale_period_files(destination_dir, period_start, ext=".xlsx"):
    """Delete existing exported files in destination_dir that belong to this
    period's series (same "Order.all.<start>_..." prefix) but an older
    end-date -- e.g. yesterday's month-to-date file, now superseded by today's.
    Only called for month_to_date, since that's the one whose filename (and
    thus staleness) changes every single day; previous_month keeps the same
    filename all month and simply overwrites itself. Historical months (a
    different start-date prefix entirely) are never touched by this."""
    if not os.path.isdir(destination_dir):
        return []
    prefix = f"Order.all.{period_start.strftime('%Y%m%d')}_"
    removed = []
    for name in os.listdir(destination_dir):
        if name.startswith(prefix) and name.lower().endswith(ext):
            try:
                os.remove(os.path.join(destination_dir, name))
                removed.append(name)
            except OSError as e:
                log(f"  WARNING: could not delete stale file {name}: {e}")
    return removed


def clean_sync_conflict_copies(directory):
    """OneDrive can resolve a write-write race (e.g. two near-simultaneous
    writes to the same previous_month file, which this script doesn't clear
    daily like it does month_to_date) by keeping both, naming the loser
    "<name>-<Device Name>.<ext>" -- seen for real on 2026-09-23: 11 such
    files sat in Shopee Mall silently double-counting those orders in every
    Parquet rebuild for a full day before being noticed. Delete any file
    whose name, with a trailing "-something" stripped before the extension,
    matches another file that actually exists here."""
    if not os.path.isdir(directory):
        return []
    names = set(os.listdir(directory))
    removed = []
    for name in list(names):
        stem, ext = os.path.splitext(name)
        if "-" not in stem:
            continue
        canonical = stem.rsplit("-", 1)[0] + ext
        if canonical != name and canonical in names:
            try:
                os.remove(os.path.join(directory, name))
                removed.append(name)
            except OSError as e:
                log(f"  WARNING: could not delete conflict-copy {name}: {e}")
    return removed


def write_bytes_with_retry(data: bytes, out_path: str, attempts=4):
    """Write to a local temp file first, then move it into place, retrying the
    move if OneDrive's sync daemon has the destination momentarily locked
    (OSError [Errno 11] "Resource deadlock avoided" -- seen both on the big
    Parquet write and on plain per-file xlsx writes into the OneDrive-synced
    Seller Data folders). A direct write straight onto the cloud mount holds
    it open for the whole transfer; a local write is instant and lock-free,
    shrinking the collision window to one quick move."""
    fd, tmp_path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        last_err = None
        for attempt in range(1, attempts + 1):
            try:
                shutil.move(tmp_path, out_path)
                return
            except OSError as e:
                last_err = e
                if attempt < attempts:
                    wait = 10 * attempt
                    log(f"    could not write {os.path.basename(out_path)} yet ({e}); "
                        f"waiting {wait}s and retrying...")
                    time.sleep(wait)
        raise last_err
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def download_flat_xlsx(page, spc_cds, report_id, destination_dir, clear_stale_start=None, shop=None):
    """Download the finished report and land only .xlsx file(s) directly in
    destination_dir. If Shopee returned a .zip, extract just the .xlsx entries
    (flattened, ignoring any internal folder structure) and discard the zip.

    If clear_stale_start is given, older files from this same period's series
    (see clear_stale_period_files) are deleted first -- but only after the new
    content has been successfully fetched, so a failed download never leaves
    the folder without a valid file. If shop is also given, the same files
    just removed locally are deleted from GCS raw too (see
    delete_stale_remote_files)."""
    url = f"{BASE_URL}/api/v3/settings/download_report/?SPC_CDS={spc_cds}&SPC_CDS_VER=2&report_id={report_id}"
    result = page.evaluate(JS_FETCH_BINARY, url)
    content = base64.b64decode(result["base64"])

    cd = result.get("contentDisposition", "")
    m = re.search(r'filename="([^"]+)"', cd)
    filename = m.group(1) if m else f"shopee_report_{report_id}.bin"

    os.makedirs(destination_dir, exist_ok=True)

    if clear_stale_start is not None:
        removed = clear_stale_period_files(destination_dir, clear_stale_start)
        if removed:
            log(f"  removed {len(removed)} stale file(s) from previous run: {removed[:3]}"
                f"{'...' if len(removed) > 3 else ''}")
            if shop:
                delete_stale_remote_files(shop, removed)

    saved = []

    if filename.lower().endswith(".zip"):
        fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            with zipfile.ZipFile(tmp_path) as zf:
                for info in zf.infolist():
                    if not info.filename.lower().endswith(".xlsx"):
                        continue
                    out_path = os.path.join(destination_dir, os.path.basename(info.filename))
                    with zf.open(info) as src:
                        write_bytes_with_retry(src.read(), out_path)
                    saved.append(out_path)
        finally:
            os.remove(tmp_path)
    else:
        out_path = os.path.join(destination_dir, filename)
        write_bytes_with_retry(content, out_path)
        saved.append(out_path)

    return saved


def run_one(page, spc_cds, shop, label, start_date, end_date, clear_stale_start=None):
    created = throttled_request_report(page, spc_cds, start_date, end_date, f"{shop}/{label}")
    report_id = created["report_id"]
    log(f"  report_id={report_id} records~={created.get('record_numbers')}")
    poll_report(page, spc_cds, report_id, max_wait_seconds=900)
    destination_dir = SHOP_DESTINATIONS[shop]
    removed_conflicts = clean_sync_conflict_copies(destination_dir)
    if removed_conflicts:
        log(f"  removed {len(removed_conflicts)} OneDrive sync-conflict duplicate(s) from "
            f"{destination_dir}: {removed_conflicts[:3]}{'...' if len(removed_conflicts) > 3 else ''}")
    saved = download_flat_xlsx(page, spc_cds, report_id, destination_dir,
                                clear_stale_start=clear_stale_start, shop=shop)
    log(f"  saved {len(saved)} .xlsx file(s) to: {destination_dir}")
    return saved


def convert_xlsx_to_csv(shop, mtd_start):
    """Mirrors the existing csv_converter.py in each Shopee Star/Mall folder:
    reads every .xlsx in the shop's "Seller Data" folder and writes a same-name
    .csv into the sibling "Shopee Star"/"Shopee Mall" folder. Stale
    month-to-date CSVs (previous day's date range) are cleared first, same as
    on the .xlsx side -- historical months are never touched."""
    source_dir = SHOP_DESTINATIONS[shop]
    dest_dir = CSV_DESTINATIONS[shop]
    os.makedirs(dest_dir, exist_ok=True)

    removed = clear_stale_period_files(dest_dir, mtd_start, ext=".csv")
    if removed:
        log(f"  removed {len(removed)} stale CSV file(s) from {dest_dir}: {removed[:3]}"
            f"{'...' if len(removed) > 3 else ''}")
        delete_stale_remote_files(shop, removed)

    removed_conflicts = clean_sync_conflict_copies(dest_dir)
    if removed_conflicts:
        log(f"  removed {len(removed_conflicts)} OneDrive sync-conflict duplicate(s) from "
            f"{dest_dir}: {removed_conflicts[:3]}{'...' if len(removed_conflicts) > 3 else ''}")

    if not os.path.isdir(source_dir):
        return []

    converted = []

    def convert_one(filename):
        try:
            source_path = os.path.join(source_dir, filename)
            df = pd.read_excel(source_path, sheet_name=0, dtype=str)
            csv_name = filename[: -len(".xlsx")] + ".csv"
            dest_path = os.path.join(dest_dir, csv_name)
            csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
            write_bytes_with_retry(csv_bytes, dest_path)
            converted.append(dest_path)
            return True
        except Exception as e:
            log(f"  ERROR converting {filename} to CSV: {e}")
            return False

    failed = [f for f in os.listdir(source_dir)
              if f.lower().endswith(".xlsx") and not convert_one(f)]

    # Repair rounds for transient OneDrive lock errors (Errno 11), which hit
    # hundreds of files on 2026-10-08 and were never retried.
    for round_no, pause in enumerate((60, 180), start=1):
        if not failed:
            break
        log(f"  {len(failed)} conversion(s) failed; repair round {round_no} in {pause}s...")
        time.sleep(pause)
        failed = [f for f in failed if not convert_one(f)]

    if failed:
        log(f"  WARNING: {len(failed)} file(s) still unconverted after repair rounds "
            f"(shopee_guard.py will retry): {failed[:5]}{'...' if len(failed) > 5 else ''}")

    log(f"  converted {len(converted)} .xlsx -> .csv into {dest_dir}")
    return converted


def iter_chunks(period_start: date, period_end: date, chunk_days=5):
    """Split [period_start, period_end] into <= chunk_days-wide windows."""
    cur = period_start
    while cur <= period_end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), period_end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def run_chunked(page, spc_cds, shop, label, period_start, period_end, chunk_days=5):
    """Like run_one, but requests period_start..period_end as several small
    windows instead of one big report.

    Found 2026-09-28: Shopee Mall's report generation silently truncates for
    large date ranges -- the later days in the range come back as
    header-only empty files instead of an error, with no signal that
    anything went wrong (e.g. a 30-day April request only had real data for
    days 1-4; the rest of the month was empty placeholders). Requesting in
    small chunks keeps each individual report well under whatever size
    triggers the truncation. Each chunk gets its own clear_stale_start, so a
    partially-run backfill can safely be re-run from the top -- chunks
    already saved just get overwritten with the same content."""
    all_saved = []
    for chunk_start, chunk_end in iter_chunks(period_start, period_end, chunk_days):
        chunk_label = f"{label}_{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}"
        try:
            saved = run_one(page, spc_cds, shop, chunk_label,
                             chunk_start.isoformat(), chunk_end.isoformat(),
                             clear_stale_start=chunk_start)
            all_saved.extend(saved)
        except Exception:
            log(f"ERROR downloading {shop}/{chunk_label}:\n{traceback.format_exc()}")
    return all_saved


def ensure_onedrive_running():
    """The whole pipeline writes into an OneDrive-synced folder. If OneDrive's
    main app isn't running (its updater daemon alone doesn't count), reads/
    writes to any not-already-locally-cached file hang and time out instead
    of erroring cleanly -- launch it and give it a moment to connect."""
    result = subprocess.run(["pgrep", "-f", "OneDrive.app/Contents/MacOS/OneDrive"], capture_output=True)
    if result.returncode == 0:
        return
    log("OneDrive app not running -- launching it (needed for the Anchanto Report folder)...")
    subprocess.run(["open", "-a", "OneDrive"])
    time.sleep(20)


def goto_with_retry(page, url, attempts=3, **kwargs):
    """The very first navigation of the run (right at 00:00) has no
    per-shop error handling around it, so a transient network hiccup --
    Mac just waking up, Wi-Fi reconnecting, etc. -- used to kill the whole
    day's run before anything happened (as happened on 2026-09-19). Retry a
    few times before giving up."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, **kwargs)
            return
        except PWTimeoutError as e:
            last_err = e
            if attempt < attempts:
                log(f"  navigation to {url} timed out (attempt {attempt}/{attempts}), retrying...")
                time.sleep(10)
    raise last_err


def _gcs_client():
    from google.cloud import storage
    return storage.Client.from_service_account_json(GCS_SA_KEY)


def upload_blob_with_retry(blob, local_path, attempts=4, timeout=120):
    """upload_from_filename() has no timeout by default and can hang
    indefinitely on a stalled connection (seen for real: a GCS upload sat at
    0% CPU for minutes with no error). An explicit timeout turns that into a
    normal exception instead, which we then retry with backoff."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            blob.upload_from_filename(local_path, timeout=timeout)
            return
        except Exception as e:
            last_err = e
            if attempt < attempts:
                wait = 15 * attempt
                log(f"    GCS upload of {os.path.basename(local_path)} failed ({e}); "
                    f"waiting {wait}s and retrying...")
                time.sleep(wait)
    raise last_err


def delete_stale_remote_files(shop, names):
    """Delete these exact, already-superseded files from GCS too, so a stale
    month-to-date snapshot (just removed locally by clear_stale_period_files)
    doesn't linger in gs://bucket_som/sales_parquet/raw/online/shopee/<shop>/
    forever and get double-counted by every downstream Parquet rebuild.
    Bounded to the exact names clear_stale_period_files just removed locally
    -- never a bucket scan or bulk reconciliation -- so this can only ever
    touch the handful of files (at most one per period per extension) that a
    completed local deletion already identified."""
    if not names:
        return
    try:
        client = _gcs_client()
        bucket = client.bucket(GCS_BUCKET)
        prefix = f"{GCS_RAW_PREFIX}/{shop}/"
        for name in names:
            try:
                bucket.blob(prefix + name).delete()
            except Exception as e:
                log(f"    (remote {name} not cleaned up: {e})")
    except Exception as e:
        log(f"    WARNING: could not clean up stale remote file(s) for {shop}: {e}")


def upload_raw_files(shop):
    """Mirror this shop's raw .xlsx (Seller Data) and .csv (staging) files
    into gs://bucket_som/sales_parquet/raw/online/shopee/<shop>/, matching
    the existing raw-zone convention used by the other channels in this
    bucket (e.g. sales_parquet/raw/minimarket/alfamart/...). Skips any file
    whose size already matches what's in GCS, so re-running doesn't re-upload
    the full multi-year history every single day -- only new/changed files."""
    client = _gcs_client()
    bucket = client.bucket(GCS_BUCKET)
    prefix = f"{GCS_RAW_PREFIX}/{shop}/"
    existing = {b.name.rsplit("/", 1)[-1]: b.size for b in client.list_blobs(bucket, prefix=prefix)}

    uploaded, skipped = 0, 0
    pending = []  # (name, local_path) still needing an upload

    def attempt(name, local_path):
        # getsize() itself can raise on a stuck OneDrive file (Errno 11/60), so it
        # must live inside the try -- outside it, one bad file aborted the whole
        # shop's upload loop.
        try:
            if existing.get(name) == os.path.getsize(local_path):
                return "skipped"
            upload_blob_with_retry(bucket.blob(prefix + name), local_path)
            return "uploaded"
        except Exception as e:
            log(f"    ERROR: could not upload {name} yet: {e}")
            return "failed"

    for local_dir in (SHOP_DESTINATIONS[shop], CSV_DESTINATIONS[shop]):
        if not os.path.isdir(local_dir):
            continue
        for name in os.listdir(local_dir):
            if not (name.lower().endswith(".xlsx") or name.lower().endswith(".csv")):
                continue
            local_path = os.path.join(local_dir, name)
            result = attempt(name, local_path)
            if result == "uploaded":
                uploaded += 1
            elif result == "skipped":
                skipped += 1
            else:
                pending.append((name, local_path))

    # Repair rounds: OneDrive lock errors are transient, so give failed files
    # more chances after a pause instead of leaving a hole in GCS until someone
    # notices (seen 2026-10-08: one CSV part never made it, silently).
    for round_no, pause in enumerate((60, 180), start=1):
        if not pending:
            break
        log(f"  {len(pending)} file(s) failed to upload; repair round {round_no} in {pause}s...")
        time.sleep(pause)
        still = []
        for name, local_path in pending:
            result = attempt(name, local_path)
            if result == "uploaded":
                uploaded += 1
            elif result == "skipped":
                skipped += 1
            else:
                still.append((name, local_path))
        pending = still

    if pending:
        log(f"  WARNING: {len(pending)} file(s) still not in GCS after repair rounds "
            f"(shopee_guard.py will retry): {[n for n, _ in pending]}")
    return uploaded, skipped


def request_github_parquet_rebuild():
    """Shopee_bq.parquet in GCS is built ONLY by the GitHub Actions workflow
    (som-shopee-report-automation), from the raw files this script uploads. The Mac
    used to rebuild and upload the same object itself -- two writers for one file --
    so it now just asks GitHub for a rebuild (shopee_guard applies the cooldown and
    skips if a run is already queued)."""
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopee_guard.py"),
                        "--rebuild-only"], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"shopee_guard --rebuild-only exit {r.returncode}: {r.stderr.strip()[:300]}")


def main():
    today = datetime.now(JAKARTA).date()
    mtd_start, mtd_end = month_to_date_range(today)
    pm_start, pm_end = previous_month_range(today)

    log("=== Scheduled Shopee export run starting ===")
    ensure_onedrive_running()
    log(f"month_to_date period: {mtd_start} -> {mtd_end}")
    log(f"previous_month period: {pm_start} -> {pm_end}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=True, viewport={"width": 1280, "height": 800}
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                goto_with_retry(page, ORDERS_PAGE, wait_until="domcontentloaded", timeout=30000)
            except PWTimeoutError:
                log("ERROR: could not reach Shopee Seller Center after retries "
                    "(network down?). Aborting this run; will try again next scheduled time.")
                sys.exit(1)

            if "/login" in page.url or not get_spc_cds(context):
                log(
                    "ERROR: not logged in (saved session expired). Run "
                    "'python3 ~/shopee_export_orders.py --start-date ... --end-date ...' "
                    "interactively to log in again, then this schedule will resume working."
                )
                sys.exit(1)

            for shop in SHOPS:
                try:
                    switch_shop(page, shop)
                    spc_cds = get_spc_cds(context)
                except Exception:
                    log(f"ERROR switching to shop {shop}, skipping it entirely this run:\n"
                        f"{traceback.format_exc()}")
                    continue

                # Each step gets its own try/except: a failure downloading one
                # period (e.g. the OneDrive lock hit mid-write on 2026-09-23)
                # must not skip the CSV conversion for whatever *did* download
                # successfully -- that's what left "Shopee Mall" a full day
                # stale even though the raw xlsx side was mostly fine.
                try:
                    run_chunked(page, spc_cds, shop, "month_to_date", mtd_start, mtd_end)
                except Exception:
                    log(f"ERROR downloading {shop}/month_to_date:\n{traceback.format_exc()}")

                try:
                    run_chunked(page, spc_cds, shop, "previous_month", pm_start, pm_end)
                except Exception:
                    log(f"ERROR downloading {shop}/previous_month:\n{traceback.format_exc()}")

                try:
                    convert_xlsx_to_csv(shop, mtd_start)
                except Exception:
                    log(f"ERROR converting {shop} xlsx -> csv:\n{traceback.format_exc()}")

                try:
                    uploaded, skipped_gcs = upload_raw_files(shop)
                    log(f"  uploaded {uploaded} raw file(s) to gs://{GCS_BUCKET}/{GCS_RAW_PREFIX}/{shop}/ "
                        f"({skipped_gcs} already up to date)")
                except Exception:
                    log(f"ERROR uploading {shop} raw files to GCS:\n{traceback.format_exc()}")

            log("=== Run complete ===")
        finally:
            context.close()

    # Separate verify-and-repair pass (own process, so a bug here can't break it):
    # re-checks every local file against GCS and re-converts/re-uploads whatever
    # the steps above missed, then triggers a cloud rebuild if it fixed anything.
    try:
        guard = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopee_guard.py"),
             "--from-main"], timeout=5400)
        log(f"shopee_guard finished with exit code {guard.returncode}")
    except Exception:
        log(f"ERROR running shopee_guard:\n{traceback.format_exc()}")

    try:
        request_github_parquet_rebuild()
        log("Asked GitHub Actions to rebuild Shopee_bq.parquet from the raw files in GCS "
            "(the Mac no longer builds or uploads it).")
    except Exception:
        log(f"ERROR asking GitHub to rebuild Shopee_bq.parquet:\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
