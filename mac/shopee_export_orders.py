#!/usr/bin/env python3
"""
Automatically generates and downloads a Shopee Seller Center order report.

Reproduces the "Export" button on https://seller.shopee.co.id/portal/sale/order
(request report -> poll until ready -> download), but drives a real Chrome
window via Playwright so you never have to manually copy cookies. The first
time you run it, a browser window opens and you log in normally; your session
is saved to a local profile folder, so future runs are already logged in.

ONE-TIME SETUP
--------------
    pip3 install playwright
    playwright install chromium

USAGE
-----
    python3 shopee_export_orders.py --start-date 2026-09-01 --end-date 2026-09-16

A Chrome window opens. If you're not already logged in, log in like normal
(the script waits for you). Once logged in, it requests the report, waits
for it to finish generating, and downloads it to ./shopee_exports (a .zip,
auto-extracted). Your login is remembered for next time in a local profile
folder (~/.shopee_seller_profile), so you usually won't need to log in again.
"""
import argparse
import os
import re
import sys
import time
import zipfile
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
except ImportError:
    sys.exit(
        "Playwright isn't installed. Run:\n"
        "    pip3 install playwright\n"
        "    playwright install chromium\n"
        "then try again."
    )

BASE_URL = "https://seller.shopee.co.id"
ORDERS_PAGE = f"{BASE_URL}/portal/sale/order"
PROFILE_DIR = os.path.expanduser("~/.shopee_seller_profile")


def ensure_logged_in(context, timeout_seconds=300):
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(ORDERS_PAGE, wait_until="domcontentloaded")

    if "/login" in page.url or not get_spc_cds(context):
        print("\nPlease log in to Shopee Seller Center in the browser window that opened.")
        print("Waiting for you to finish logging in...")
        try:
            page.wait_for_url(re.compile(r".*/portal/.*"), timeout=timeout_seconds * 1000)
        except PWTimeoutError:
            sys.exit(f"Timed out after {timeout_seconds}s waiting for login.")
        page.wait_for_timeout(1500)  # let post-login cookies settle

    return page


def get_spc_cds(context):
    for c in context.cookies(BASE_URL):
        if c["name"] == "SPC_CDS":
            return c["value"]
    return None


JS_FETCH_JSON = """
async (url) => {
    const res = await fetch(url, { credentials: 'include' });
    const text = await res.text();
    try {
        return { ok: res.ok, status: res.status, json: JSON.parse(text) };
    } catch (e) {
        return { ok: res.ok, status: res.status, json: null, raw: text.slice(0, 500) };
    }
}
"""

JS_FETCH_BINARY = """
async (url) => {
    const res = await fetch(url, { credentials: 'include' });
    const buf = await res.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return {
        status: res.status,
        contentDisposition: res.headers.get('content-disposition') || '',
        base64: btoa(binary),
    };
}
"""


def fetch_json(page, url):
    result = page.evaluate(JS_FETCH_JSON, url)
    if result.get("json") is None:
        raise RuntimeError(f"non-JSON response from {url}: {result}")
    return result["json"]


def request_order_report(page, spc_cds, start_date, end_date, language, screening_condition):
    url = (
        f"{BASE_URL}/api/v3/order/request_order_report"
        f"?SPC_CDS={spc_cds}&SPC_CDS_VER=2"
        f"&start_date={start_date}&end_date={end_date}"
        f"&language={language}&screening_condition={screening_condition}"
        f"&parcel_level_filter=0"
    )
    body = fetch_json(page, url)
    if body.get("code") != 0:
        raise RuntimeError(f"request_order_report failed: {body}")
    return body["data"]


def poll_report(page, spc_cds, report_id, max_wait_seconds=600):
    url = f"{BASE_URL}/api/v3/settings/get_report/?SPC_CDS={spc_cds}&SPC_CDS_VER=2&report_id={report_id}"
    deadline = time.time() + max_wait_seconds
    interval = 1.0
    while time.time() < deadline:
        body = fetch_json(page, url)
        if body.get("code") != 0:
            raise RuntimeError(f"get_report failed: {body}")
        data = body["data"]
        status = data.get("status")
        total = data.get("total_count") or 0
        done = data.get("success_count") or 0
        print(f"  report {report_id}: status={status} progress={done}/{total or '?'}")
        if status == 2:
            return data
        time.sleep(interval)
        interval = min(interval + 1.0, 5.0)
    raise TimeoutError(f"report {report_id} did not finish within {max_wait_seconds}s")


def download_report(page, spc_cds, report_id, output_dir):
    import base64

    url = f"{BASE_URL}/api/v3/settings/download_report/?SPC_CDS={spc_cds}&SPC_CDS_VER=2&report_id={report_id}"
    result = page.evaluate(JS_FETCH_BINARY, url)
    content = base64.b64decode(result["base64"])

    filename = None
    m = re.search(r'filename="([^"]+)"', result.get("contentDisposition", ""))
    if m:
        filename = m.group(1)
    if not filename:
        filename = f"shopee_report_{report_id}.bin"

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename)
    with open(out_path, "wb") as f:
        f.write(content)
    print(f"  saved {out_path} ({len(content):,} bytes)")

    if out_path.lower().endswith(".zip"):
        extract_dir = out_path[: -len(".zip")]
        with zipfile.ZipFile(out_path) as zf:
            zf.extractall(extract_dir)
        print(f"  extracted to {extract_dir}/")

    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--language", default="id")
    parser.add_argument("--screening-condition", default="order_creation_date")
    parser.add_argument("--output-dir", default="./shopee_exports")
    parser.add_argument("--max-wait", type=int, default=600)
    parser.add_argument("--keep-open", action="store_true", help="leave the browser window open when done")
    parser.add_argument("--headless", action="store_true", help="run without a visible window (only safe once already logged in)")
    args = parser.parse_args()

    datetime.strptime(args.start_date, "%Y-%m-%d")
    datetime.strptime(args.end_date, "%Y-%m-%d")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=args.headless,
            viewport={"width": 1280, "height": 800},
        )
        try:
            page = ensure_logged_in(context)
            spc_cds = get_spc_cds(context)
            if not spc_cds:
                sys.exit("Still couldn't find a valid session after login. Try running again.")

            print(f"Requesting order report {args.start_date} -> {args.end_date} ...")
            created = request_order_report(
                page, spc_cds, args.start_date, args.end_date, args.language, args.screening_condition
            )
            report_id = created["report_id"]
            print(f"  report_id={report_id} expected_file={created.get('report_file_name')} "
                  f"records~={created.get('record_numbers')}")

            print("Waiting for report to finish generating...")
            poll_report(page, spc_cds, report_id, max_wait_seconds=args.max_wait)

            print("Downloading finished report...")
            path = download_report(page, spc_cds, report_id, args.output_dir)

            print(f"\nDone: {path}")
        finally:
            if not args.keep_open:
                context.close()


if __name__ == "__main__":
    main()
