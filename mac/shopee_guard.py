#!/usr/bin/env python3
"""Independent verify-and-repair pass for the nightly Shopee pipeline.

Why this exists: when OneDrive misbehaves (Errno 11 "Resource deadlock avoided",
Errno 60 timeouts) the main script (shopee_scheduled_export.py) used to give up
on individual files silently -- a CSV never converted, a raw file never reached
GCS -- and the cloud parquet got built with holes (seen 2026-10-08). The main
script now retries in rounds, but this guard is a second, separate line of
defence that does not trust anything the main script reported. It re-checks
from scratch, per shop:

  1. every Seller Data .xlsx has an up-to-date .csv beside it (missing or older
     than the xlsx => re-convert),
  2. every local .xlsx/.csv exists in GCS raw with the same size (missing or
     different => re-upload),

retrying in rounds with pauses, and logs anything it could not fix. It never
deletes anything. If it repaired something it triggers one GitHub Actions
rebuild of Shopee_bq.parquet so the fix reaches BigQuery; with --stale-check it
also triggers a rebuild when the cloud parquet is much older than the newest raw
upload (e.g. the scheduled rebuild failed).

    python3 shopee_guard.py                 # standalone (LaunchAgent, 04:30)
    python3 shopee_guard.py --stale-check   # + rebuild if parquet is stale
    python3 shopee_guard.py --from-main     # called by the main script itself

Exit code 0 = everything consistent at the end, 1 = something still unresolved.
Status of the last run: ~/shopee_guard_status.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd  # noqa: E402
import shopee_scheduled_export as sse  # noqa: E402

REPO = "brianirsyad-scarlett/som-shopee-report-automation"
WORKFLOW = "shopee-bq-pipeline.yml"
STATUS_FILE = os.path.expanduser("~/shopee_guard_status.json")
ROUND_PAUSES = (0, 60, 180, 300)  # pause before each repair round
REBUILD_COOLDOWN = timedelta(minutes=90)
STALE_PARQUET_AFTER = timedelta(hours=3)


def glog(msg):
    sse.log(f"[guard] {msg}")


def retry(fn, what, attempts=4, pause=20):
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts:
                glog(f"{what} failed ({e}); retry {i}/{attempts - 1} in {pause * i}s")
                time.sleep(pause * i)
    raise last


def is_conflict_copy(name, all_names):
    stem, ext = os.path.splitext(name)
    return "-" in stem and (stem.rsplit("-", 1)[0] + ext) in all_names


def main_still_working():
    """True if the main script is mid-run (downloading/converting/uploading), in
    which case a standalone guard must not touch the same files."""
    if subprocess.run(["pgrep", "-f", "shopee_scheduled_export.py"],
                      capture_output=True).returncode != 0:
        return False
    try:
        with open(sse.LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return True
    start = tail.rfind("Scheduled Shopee export run starting")
    return start != -1 and "=== Run complete ===" not in tail[start:]


# ---------------------------------------------------------------- repair 1: csv
def stale_conversions(shop):
    src, dst = sse.SHOP_DESTINATIONS[shop], sse.CSV_DESTINATIONS[shop]
    if not os.path.isdir(src):
        return []
    needs = []
    for name in retry(lambda: os.listdir(src), f"list {src}"):
        if not name.lower().endswith(".xlsx"):
            continue
        csv_path = os.path.join(dst, name[:-5] + ".csv")
        try:
            if (not os.path.exists(csv_path)
                    or os.path.getmtime(csv_path) + 1 < os.path.getmtime(os.path.join(src, name))):
                needs.append(name)
        except OSError:
            needs.append(name)  # can't tell -> let the repair round try it
    return needs


def convert(shop, name):
    src, dst = sse.SHOP_DESTINATIONS[shop], sse.CSV_DESTINATIONS[shop]
    df = pd.read_excel(os.path.join(src, name), sheet_name=0, dtype=str)
    data = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    sse.write_bytes_with_retry(data, os.path.join(dst, name[:-5] + ".csv"))


# -------------------------------------------------------------- repair 2: upload
def gcs_sizes(bucket, shop):
    prefix = f"{sse.GCS_RAW_PREFIX}/{shop}/"
    return retry(lambda: {b.name.rsplit("/", 1)[-1]: b.size
                          for b in bucket.client.list_blobs(bucket, prefix=prefix)},
                 f"list gs://.../{shop}/")


def missing_in_gcs(bucket, shop):
    remote = gcs_sizes(bucket, shop)
    todo = []
    for d in (sse.SHOP_DESTINATIONS[shop], sse.CSV_DESTINATIONS[shop]):
        if not os.path.isdir(d):
            continue
        names = retry(lambda d=d: os.listdir(d), f"list {d}")
        nameset = set(names)
        for name in names:
            if not name.lower().endswith((".xlsx", ".csv")) or is_conflict_copy(name, nameset):
                continue
            path = os.path.join(d, name)
            try:
                if remote.get(name) != os.path.getsize(path):
                    todo.append((name, path))
            except OSError:
                todo.append((name, path))
    return todo


def repair_shop(shop, bucket):
    result = {"converted": 0, "uploaded": 0, "unresolved_convert": [], "unresolved_upload": []}

    pending = stale_conversions(shop)
    for pause in ROUND_PAUSES:
        if not pending:
            break
        if pause:
            glog(f"{shop}: {len(pending)} csv still stale; waiting {pause}s")
            time.sleep(pause)
        still = []
        for name in pending:
            try:
                convert(shop, name)
                result["converted"] += 1
            except Exception as e:
                glog(f"{shop}: convert {name} failed: {e}")
                still.append(name)
        pending = still
    result["unresolved_convert"] = pending

    todo = missing_in_gcs(bucket, shop)
    for pause in ROUND_PAUSES:
        if not todo:
            break
        if pause:
            glog(f"{shop}: {len(todo)} file(s) not in GCS; waiting {pause}s")
            time.sleep(pause)
        still = []
        for name, path in todo:
            try:
                sse.upload_blob_with_retry(bucket.blob(f"{sse.GCS_RAW_PREFIX}/{shop}/{name}"), path)
                result["uploaded"] += 1
            except Exception as e:
                glog(f"{shop}: upload {name} failed: {e}")
                still.append((name, path))
        todo = still
    result["unresolved_upload"] = [n for n, _ in todo]
    return result


# ------------------------------------------------------------ cloud rebuild trigger
def gh_bin():
    return shutil.which("gh") or next(
        (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)


def load_state():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def trigger_rebuild(reason):
    gh = gh_bin()
    if not gh:
        glog("cannot trigger rebuild: gh CLI not found")
        return False
    last = load_state().get("last_rebuild_trigger")
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < REBUILD_COOLDOWN:
        glog(f"rebuild wanted ({reason}) but one was triggered <{REBUILD_COOLDOWN} ago; skipping")
        return False
    try:
        runs = subprocess.run(
            [gh, "run", "list", "-R", REPO, "--workflow", WORKFLOW, "--limit", "5",
             "--json", "status"], capture_output=True, text=True, timeout=60)
        if any(r.get("status") in ("queued", "in_progress") for r in json.loads(runs.stdout or "[]")):
            glog(f"rebuild wanted ({reason}) but a run is already queued/in progress; skipping")
            return False
        out = subprocess.run([gh, "workflow", "run", WORKFLOW, "-R", REPO],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            glog(f"gh workflow run failed: {out.stderr.strip()}")
            return False
        glog(f"triggered Shopee_bq.parquet rebuild ({reason})")
        return True
    except Exception as e:
        glog(f"could not trigger rebuild: {e}")
        return False


def parquet_is_stale(bucket):
    parquet = bucket.get_blob(sse.GCS_BQ_PARQUET_PATH)
    newest = None
    for shop in sse.SHOPS:
        for b in bucket.client.list_blobs(bucket, prefix=f"{sse.GCS_RAW_PREFIX}/{shop}/"):
            if b.name.lower().endswith(".csv") and (newest is None or b.updated > newest):
                newest = b.updated
    if parquet is None:
        return "Shopee_bq.parquet missing in GCS"
    if newest and newest - parquet.updated > STALE_PARQUET_AFTER:
        return f"parquet ({parquet.updated:%m-%d %H:%M}Z) older than newest raw csv ({newest:%m-%d %H:%M}Z)"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-main", action="store_true")
    ap.add_argument("--stale-check", action="store_true")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="just ask GitHub Actions to rebuild Shopee_bq.parquet (cooldown/in-progress checks apply)")
    args = ap.parse_args()

    if args.rebuild_only:
        trigger_rebuild("Mac export finished")
        return 0

    if not args.from_main and main_still_working():
        glog("main export is still running; leaving files alone this time")
        return 0

    glog("verify-and-repair starting")
    bucket = sse._gcs_client().bucket(sse.GCS_BUCKET)
    results, unresolved = {}, []
    for shop in sse.SHOPS:
        try:
            results[shop] = repair_shop(shop, bucket)
        except Exception:
            glog(f"{shop}: guard crashed:\n{traceback.format_exc()}")
            results[shop] = {"error": "guard crashed"}
            unresolved.append(f"{shop}: guard crashed")
            continue
        r = results[shop]
        glog(f"{shop}: reconverted {r['converted']}, re-uploaded {r['uploaded']}, "
             f"unresolved csv={len(r['unresolved_convert'])} upload={len(r['unresolved_upload'])}")
        unresolved += r["unresolved_convert"] + r["unresolved_upload"]

    repaired = sum(r.get("converted", 0) + r.get("uploaded", 0) for r in results.values())
    reason = f"{repaired} file(s) repaired" if repaired else None
    if not reason and args.stale_check:
        try:
            reason = parquet_is_stale(bucket)
        except Exception as e:
            glog(f"stale check failed: {e}")
    triggered = trigger_rebuild(reason) if reason else False

    state = load_state()
    if triggered:
        state["last_rebuild_trigger"] = datetime.now(timezone.utc).isoformat()
    state.update({"last_run": datetime.now(timezone.utc).isoformat(), "results": results,
                  "unresolved": unresolved, "rebuild_triggered": triggered})
    with open(STATUS_FILE, "w") as f:
        json.dump(state, f, indent=2)

    if unresolved:
        glog(f"UNRESOLVED after all repair rounds ({len(unresolved)}): {unresolved[:10]}")
        return 1
    glog("all raw files consistent (local == GCS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
