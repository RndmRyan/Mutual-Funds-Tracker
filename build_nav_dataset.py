"""
Merge the per-statement CSVs in "Performance Output/" into one long-format NAV
history: date | fund_name | nav_rs.

`date` is the per-row *validity date* (the day the price actually applies), not
the statement date -- some funds lag the email by a day or two.

Usage:
    python build_nav_dataset.py
"""

from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "Performance Output"
OUTPUT_FILE = INPUT_DIR / "nav_history.csv"


def build() -> pd.DataFrame:
    files = sorted(p for p in INPUT_DIR.glob("fund_prices_*.csv"))
    if not files:
        raise SystemExit(f"no fund_prices_*.csv found in {INPUT_DIR} -- run parse_meezan_pdfs.py first")

    frames = []
    for path in files:
        df = pd.read_csv(path, usecols=["validity_date", "fund_name", "nav_rs"])
        frames.append(df.rename(columns={"validity_date": "date"}))

    nav = pd.concat(frames, ignore_index=True)
    nav["date"] = pd.to_datetime(nav["date"])
    nav = nav.dropna(subset=["date", "fund_name", "nav_rs"])

    # Same fund + same price date can arrive in two mails; keep one.
    before = len(nav)
    nav = nav.drop_duplicates(subset=["date", "fund_name"], keep="last")
    duplicates = before - len(nav)

    nav = nav.sort_values(["fund_name", "date"]).reset_index(drop=True)
    nav.to_csv(OUTPUT_FILE, index=False, date_format="%Y-%m-%d")

    print(f"{len(files)} statements -> {OUTPUT_FILE.relative_to(BASE_DIR)}")
    print(f"  {len(nav)} rows | {nav.fund_name.nunique()} funds | "
          f"{nav.date.min():%Y-%m-%d} to {nav.date.max():%Y-%m-%d}")
    if duplicates:
        print(f"  dropped {duplicates} duplicate (date, fund) rows")
    return nav


if __name__ == "__main__":
    build()
