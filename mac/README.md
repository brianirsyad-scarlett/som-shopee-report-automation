# Mac side of the Shopee pipeline

Runs on the office Mac (launchd). It exists because Shopee only trusts the real, already-logged-in browser
session on that Mac -- the login/scrape cannot run on a GitHub runner.

## Who owns what (no overlap)

| Step | Owner |
|---|---|
| Log in, download month-to-date + previous-month reports, convert to CSV | **Mac** `shopee_scheduled_export.py` (launchd 00:00) |
| Copy raw xlsx/csv to `gs://bucket_som/sales_parquet/raw/online/shopee/<shop>/` | **Mac** (same script) |
| Verify the raw files all reached GCS, repair gaps, then ask for a rebuild | **Mac** `shopee_guard.py` (launchd 04:30, also called by the export) |
| Build `Shopee_bq.parquet` and upload it to GCS | **GitHub Actions only** (`../build_bq_parquet.py`, workflow `shopee-bq-pipeline.yml`) |

Since 2026-10-12 the Mac no longer uploads `Shopee_bq.parquet`: `request_github_parquet_rebuild()` in
`shopee_scheduled_export.py` runs `shopee_guard.py --rebuild-only`, which dispatches the workflow
(90-minute cooldown, skipped if a run is already queued). The Mac still writes local `Shopee.parquet` /
`Shopee_bq.parquet` into OneDrive for local Power BI use only.

`launchd/` holds copies of the LaunchAgent plists (install to ~/Library/LaunchAgents/).
