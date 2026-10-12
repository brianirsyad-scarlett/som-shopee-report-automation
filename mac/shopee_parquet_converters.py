#!/usr/bin/env python3
"""
Mac-path port of parquet_converter_shopee.py / parquet_converter_shopee_bq.py
(both live in the Anchanto Report/E-Commerce Data folder, hardcoded to a
Windows D:\\ path for the machine that folder is normally run from).

Logic here is kept verbatim from those two scripts -- only the path constants
are swapped for the equivalent Mac/OneDrive path -- so this can run from this
Mac without touching the originals (which stay untouched for Windows use).
"""
import glob
import os
import re
import shutil
import tempfile
import time

import numpy as np
import pandas as pd

ANCHANTO_ROOT = (
    "/Users/salesops/Library/CloudStorage/OneDrive-PT.OptoLumbungSejahtera"
    "/SOM/Anchanto Report/E-Commerce Data"
)
MALL_FOLDER = os.path.join(ANCHANTO_ROOT, "Shopee Mall")
STAR_FOLDER = os.path.join(ANCHANTO_ROOT, "Shopee Star")
OUTPUT_PARQUET = os.path.join(ANCHANTO_ROOT, "Shopee.parquet")
OUTPUT_PARQUET_BQ = os.path.join(ANCHANTO_ROOT, "Shopee_bq.parquet")

SELECTED_YEAR = None  # Set to e.g. 2026 to filter to one year; None = all years

SKIPPED_FILES = []  # reset per _build_combined() call; files that failed to read after retries
MAX_TOLERABLE_SKIPPED = 5  # abort the write rather than silently drop more data than this

DESIRED_COLUMNS = [
    "No. Pesanan", "Status Pesanan", "Alasan Pembatalan", "Status Pembatalan/ Pengembalian",
    "No. Resi", "Opsi Pengiriman", "Antar ke counter/ pick-up",
    "Pesanan Harus Dikirimkan Sebelum (Menghindari keterlambatan)", "Waktu Pengiriman Diatur",
    "Waktu Pesanan Dibuat", "Waktu Pembayaran Dilakukan", "Metode Pembayaran",
    "SKU Induk", "Nama Produk", "Nomor Referensi SKU", "Nama Variasi",
    "Harga Awal", "Harga Setelah Diskon", "Jumlah", "Returned quantity",
    "Dibayar Pembeli", "Total Diskon", "Diskon Dari Penjual", "Diskon Dari Shopee",
    "Berat Produk", "Jumlah Produk di Pesan", "Total Berat", "Nama Gudang",
    "Voucher Ditanggung Penjual", "Cashback Koin", "Voucher Ditanggung Shopee",
    "Paket Diskon", "Paket Diskon (Diskon dari Shopee)", "Paket Diskon (Diskon dari Penjual)",
    "Potongan Koin Shopee", "Diskon Kartu Kredit", "Ongkos Kirim Dibayar oleh Pembeli",
    "Estimasi Potongan Biaya Pengiriman", "Ongkos Kirim Pengembalian Barang",
    "Total Pembayaran", "Perkiraan Ongkos Kirim", "Catatan dari Pembeli", "Catatan",
    "Username (Pembeli)", "Nama Penerima", "No. Telepon", "Alamat Pengiriman",
    "Kota/Kabupaten", "Provinsi", "Waktu Pesanan Selesai",
    "Marketplace"
]

DATE_COLUMNS = [
    "Waktu Pesanan Selesai", "Waktu Pesanan Dibuat", "Waktu Pembayaran Dilakukan",
    "Waktu Pengiriman Diatur", "Pesanan Harus Dikirimkan Sebelum (Menghindari keterlambatan)"
]

NUMERIC_COLUMNS = [
    "Perkiraan Ongkos Kirim", "Total Pembayaran", "Ongkos Kirim Pengembalian Barang",
    "Estimasi Potongan Biaya Pengiriman", "Ongkos Kirim Dibayar oleh Pembeli",
    "Diskon Kartu Kredit", "Potongan Koin Shopee", "Paket Diskon (Diskon dari Penjual)",
    "Paket Diskon (Diskon dari Shopee)", "Voucher Ditanggung Shopee", "Cashback Koin",
    "Voucher Ditanggung Penjual", "Jumlah Produk di Pesan", "Diskon Dari Shopee",
    "Diskon Dari Penjual", "Total Diskon", "Dibayar Pembeli", "Returned quantity",
    "Jumlah", "Harga Setelah Diskon", "Harga Awal"
]


def clean_column(name):
    """Replace any character that is not a letter, digit, or underscore with '_'."""
    return re.sub(r'[^a-zA-Z0-9_]', '_', name)


def read_csv_with_retry(file, attempts=4, base_delay=5):
    """OneDrive "Files On-Demand" can evict rarely-touched historical files to
    cloud-only placeholders; reading one triggers a fetch that can time out
    (ETIMEDOUT) instead of just blocking until ready. Retry with backoff
    rather than aborting the whole rebuild over one slow file."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return pd.read_csv(file, encoding='utf-8', dtype=str, low_memory=False)
        except (TimeoutError, OSError) as e:
            last_err = e
            if attempt < attempts:
                wait = base_delay * attempt
                print(f"    (retry {attempt}/{attempts - 1}) {os.path.basename(file)} not ready yet "
                      f"({e}); waiting {wait}s for OneDrive to fetch it...")
                time.sleep(wait)
    raise last_err


def is_sync_conflict_copy(path, all_basenames):
    """OneDrive occasionally resolves a write-write race by keeping both
    files, naming the loser "<name>-<Device Name>.<ext>" (seen for real on
    2026-09-23: 11 "Order.all...-Sales's iMac.csv" files sitting alongside
    the canonical ones, silently double-counting those rows in every Parquet
    rebuild since). Detect that pattern generically: if stripping a trailing
    "-something" before the extension yields another file that actually
    exists here, this one is the conflict copy."""
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    if "-" not in stem:
        return False
    canonical = stem.rsplit("-", 1)[0] + ext
    return canonical != base and canonical in all_basenames


def process_shopee_folder(folder_path, marketplace_name, add_missing_columns=None):
    csv_files = glob.glob(os.path.join(folder_path, "*.csv"))
    csv_files = [f for f in csv_files if "$" not in os.path.basename(f)]
    all_basenames = {os.path.basename(f) for f in csv_files}
    conflict_copies = [f for f in csv_files if is_sync_conflict_copy(f, all_basenames)]
    if conflict_copies:
        print(f"  WARNING: {len(conflict_copies)} OneDrive sync-conflict duplicate(s) found in "
              f"{os.path.basename(folder_path)}, excluding from this build (consider deleting them): "
              f"{[os.path.basename(f) for f in conflict_copies]}")
        csv_files = [f for f in csv_files if f not in conflict_copies]
    if not csv_files:
        print(f"  No CSV files found in {folder_path}")
        return pd.DataFrame()

    print(f"  Found {len(csv_files)} CSV files in {os.path.basename(folder_path)}")

    all_dfs = []
    skipped = []
    for file in csv_files:
        try:
            df = read_csv_with_retry(file)
        except (TimeoutError, OSError) as e:
            print(f"    WARNING: skipping unreadable file after retries: {os.path.basename(file)} ({e})")
            skipped.append(os.path.basename(file))
            continue

        existing_desired = [col for col in DESIRED_COLUMNS if col in df.columns and col != "Marketplace"]
        df = df[existing_desired]

        if "No. Pesanan" in df.columns:
            df = df[df["No. Pesanan"] != "No. Pesanan"]

        for col in DESIRED_COLUMNS:
            if col not in df.columns and col != "Marketplace":
                default = add_missing_columns.get(col, "") if add_missing_columns else ""
                df[col] = default

        for col in DATE_COLUMNS:
            if col in df.columns:
                df[col] = df[col].replace("-", np.nan)
                df[col] = pd.to_datetime(df[col], errors='coerce').dt.date

        for col in NUMERIC_COLUMNS:
            if col in df.columns:
                df[col] = df[col].astype(str).str.replace('.', '', regex=False)
                df[col] = df[col].replace("", "0").replace("nan", "0")
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype('int64')

        text_cols = [c for c in df.columns if c not in DATE_COLUMNS and c not in NUMERIC_COLUMNS]
        for col in text_cols:
            df[col] = df[col].fillna("").astype(str)

        df["Marketplace"] = marketplace_name
        all_dfs.append(df)

    if skipped:
        SKIPPED_FILES.extend(skipped)
        print(f"  WARNING: {len(skipped)} file(s) in {os.path.basename(folder_path)} were skipped "
              f"(stuck OneDrive sync?) -- rebuild continued without them.")

    if not all_dfs:
        return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True)


def _build_combined():
    SKIPPED_FILES.clear()
    print("\nReading Shopee Mall...")
    mall_df = process_shopee_folder(MALL_FOLDER, marketplace_name="Shopee Mall")
    print(f"  Mall rows after cleaning: {len(mall_df):,}")

    print("\nReading Shopee Star...")
    star_missing = {"Nama Gudang": ""}
    star_df = process_shopee_folder(STAR_FOLDER, marketplace_name="Shopee Star", add_missing_columns=star_missing)
    print(f"  Star rows after cleaning: {len(star_df):,}")

    print("\nAppending Mall and Star...")
    combined = pd.concat([mall_df, star_df], ignore_index=True)
    print(f"  Total rows before any filter: {len(combined):,}")

    for col in NUMERIC_COLUMNS:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors='coerce').fillna(0).astype('int64')

    if SELECTED_YEAR is not None and "Waktu Pesanan Dibuat" in combined.columns:
        combined["Waktu Pesanan Dibuat_dt"] = pd.to_datetime(combined["Waktu Pesanan Dibuat"], errors='coerce')
        combined = combined[combined["Waktu Pesanan Dibuat_dt"].dt.year == SELECTED_YEAR]
        combined = combined.drop(columns=["Waktu Pesanan Dibuat_dt"])
        print(f"  Rows after filtering to year {SELECTED_YEAR}: {len(combined):,}")
    elif SELECTED_YEAR is not None:
        print("  Warning: 'Waktu Pesanan Dibuat' column missing - skipping year filter.")
    else:
        print("  No year filter applied (including all years).")

    return combined


def convert_shopee():
    print("Processing Shopee data (standard)...")
    combined = _build_combined()
    final_cols = [c for c in DESIRED_COLUMNS if c in combined.columns]
    combined = combined[final_cols]

    print(f"\nSaving to Parquet: {OUTPUT_PARQUET}")
    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)
    combined.to_parquet(OUTPUT_PARQUET, index=False, engine="pyarrow", compression="zstd", coerce_timestamps="us")

    file_size_gb = os.path.getsize(OUTPUT_PARQUET) / (1024 ** 3)
    print(f"\nSUCCESS: Parquet file created")
    print(f"   Total rows: {len(combined):,}")
    print(f"   Parquet size: {file_size_gb:.2f} GB")
    print(f"   Columns: {len(final_cols)}")


def convert_shopee_bq():
    print("Processing Shopee data (BigQuery-compatible)...")
    combined = _build_combined()
    final_cols = [c for c in DESIRED_COLUMNS if c in combined.columns]
    combined = combined[final_cols]

    print("\nRenaming columns for BigQuery compatibility...")
    new_names = {col: clean_column(col) for col in combined.columns}
    combined.rename(columns=new_names, inplace=True)

    print(f"\nSaving to Parquet: {OUTPUT_PARQUET_BQ}")
    os.makedirs(os.path.dirname(OUTPUT_PARQUET_BQ), exist_ok=True)
    combined.to_parquet(OUTPUT_PARQUET_BQ, index=False, engine="pyarrow", compression="zstd", coerce_timestamps="us")

    file_size_gb = os.path.getsize(OUTPUT_PARQUET_BQ) / (1024 ** 3)
    print(f"\nSUCCESS: Parquet file created")
    print(f"   Total rows: {len(combined):,}")
    print(f"   Parquet size: {file_size_gb:.2f} GB")
    print(f"   Columns: {len(combined.columns)}")


def _write_parquet_atomically(df, final_path, attempts=3):
    """Write to a local (non-cloud) temp file first, then move it into place.

    Writing a ~300MB parquet file directly onto the OneDrive mount holds it
    open for the whole (slow) serialization; if OneDrive's sync daemon is
    mid-upload of the *previous* version at that moment, opening it again can
    raise OSError [Errno 11] "Resource deadlock avoided". A local write is
    fast and lock-free, and shutil.move() into the cloud folder is a single
    quick operation, shrinking that collision window to near-nothing. Retries
    the move step too, in case the destination is still momentarily locked.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".parquet", dir=tempfile.gettempdir())
    os.close(fd)
    try:
        df.to_parquet(tmp_path, index=False, engine="pyarrow", compression="zstd", coerce_timestamps="us")
        last_err = None
        for attempt in range(1, attempts + 1):
            try:
                shutil.move(tmp_path, final_path)
                return
            except OSError as e:
                last_err = e
                if attempt < attempts:
                    wait = 15 * attempt
                    print(f"    could not move into place yet ({e}); waiting {wait}s and retrying...")
                    time.sleep(wait)
        raise last_err
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def convert_both():
    """Build the combined dataframe once and write both parquet outputs from
    it -- functionally identical to calling convert_shopee() then
    convert_shopee_bq(), just without re-reading every CSV a second time."""
    print("Processing Shopee data (standard + BigQuery-compatible)...")
    combined = _build_combined()

    if len(SKIPPED_FILES) > MAX_TOLERABLE_SKIPPED:
        raise RuntimeError(
            f"Refusing to overwrite Shopee.parquet/Shopee_bq.parquet: {len(SKIPPED_FILES)} "
            f"CSV file(s) could not be read (OneDrive sync unhealthy?), which is more than "
            f"the {MAX_TOLERABLE_SKIPPED} straggler(s) this is willing to silently drop. "
            f"Writing anyway would silently delete that data from both Parquet files. "
            f"Leaving the existing Parquet files untouched -- retry once OneDrive has "
            f"caught up. Skipped: {SKIPPED_FILES[:10]}{'...' if len(SKIPPED_FILES) > 10 else ''}"
        )

    final_cols = [c for c in DESIRED_COLUMNS if c in combined.columns]
    combined = combined[final_cols]

    print(f"\nSaving to Parquet: {OUTPUT_PARQUET}")
    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)
    _write_parquet_atomically(combined, OUTPUT_PARQUET)
    size_gb = os.path.getsize(OUTPUT_PARQUET) / (1024 ** 3)
    print(f"   SUCCESS: {len(combined):,} rows, {size_gb:.2f} GB, {len(final_cols)} columns")

    bq = combined.rename(columns={col: clean_column(col) for col in combined.columns})
    print(f"\nSaving to Parquet: {OUTPUT_PARQUET_BQ}")
    os.makedirs(os.path.dirname(OUTPUT_PARQUET_BQ), exist_ok=True)
    _write_parquet_atomically(bq, OUTPUT_PARQUET_BQ)
    size_gb_bq = os.path.getsize(OUTPUT_PARQUET_BQ) / (1024 ** 3)
    print(f"   SUCCESS: {len(bq):,} rows, {size_gb_bq:.2f} GB, {len(bq.columns)} columns")

    if SKIPPED_FILES:
        print(f"\nWARNING: {len(SKIPPED_FILES)} file(s) could not be read (likely stuck OneDrive "
              f"sync) and were excluded from both Parquet files -- may need a manual re-sync:")
        for name in SKIPPED_FILES:
            print(f"   - {name}")

    return list(SKIPPED_FILES)


if __name__ == "__main__":
    convert_both()
