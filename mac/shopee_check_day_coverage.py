#!/usr/bin/env python3
"""Check which calendar days actually have order data in Shopee Mall's local
CSVs for a given month, by reading every part file for that month's date
prefix and looking at "Waktu Pesanan Dibuat" (order creation time)."""
import calendar
import glob
import os
import sys
from datetime import date

import pandas as pd

DIR = ("/Users/salesops/Library/CloudStorage/OneDrive-PT.OptoLumbungSejahtera"
       "/SOM/Anchanto Report/E-Commerce Data/Shopee Mall")


def check_month(year, month):
    start = date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    end = date(year, month, days_in_month)
    files = []
    for f in glob.glob(os.path.join(DIR, "Order.all.*.csv")):
        base = os.path.basename(f)
        if "$" in base:
            continue
        try:
            start_str = base[len("Order.all."):].split("_", 1)[0]
            file_start = date(int(start_str[:4]), int(start_str[4:6]), int(start_str[6:8]))
        except (ValueError, IndexError):
            continue
        if start <= file_start <= end:
            files.append(f)
    seen_days = set()
    total_rows = 0
    empty_parts = 0
    for f in sorted(files):
        try:
            df = pd.read_csv(f, dtype=str, low_memory=False, usecols=["Waktu Pesanan Dibuat"])
        except Exception as e:
            print(f"  ERROR reading {os.path.basename(f)}: {e}")
            continue
        if len(df) == 0:
            empty_parts += 1
            continue
        total_rows += len(df)
        dt = pd.to_datetime(df["Waktu Pesanan Dibuat"], errors="coerce")
        seen_days |= set(dt.dt.date.dropna().unique())

    expected_days = {date(year, month, d) for d in range(1, days_in_month + 1)}
    missing_days = sorted(expected_days - seen_days)
    print(f"{year}-{month:02d}: {len(files)} parts ({empty_parts} empty), {total_rows:,} rows, "
          f"{len(seen_days)}/{days_in_month} days covered")
    if missing_days:
        print(f"  MISSING DAYS: {[d.isoformat() for d in missing_days]}")
    return len(missing_days) == 0


if __name__ == "__main__":
    months = [(2026, m) for m in range(1, 9)]  # Jan..Aug 2026
    for y, m in months:
        check_month(y, m)
