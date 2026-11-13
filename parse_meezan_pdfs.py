"""
Parse Al Meezan "Fund Prices and Performance" emails (saved as PDF from Gmail)
into a clean, tabular NAV dataset - one output file per input PDF.

Usage
-----
    python parse_meezan_pdfs.py                      # parse everything in "Document Samples"
    python parse_meezan_pdfs.py path/to/file.pdf     # parse specific file(s)

Outputs (in "Performance Output/"):
    fund_prices_YYYY-MM-DD.csv   machine-readable table
    fund_prices_YYYY-MM-DD.txt   fixed-width preview for eyeballing against the PDF

Layout notes (verified against the sample PDFs)
-----------------------------------------------
The email body is a single HTML table. Physical column order, left to right, is:

    Funds Category | Launch Date | Validity Date | Repurchase (Rs.) | Offer (Rs.)
    | NAV (Rs.) | MTD | FYTD | CYTD | FY<n> | FY<n-1> | Since Inception

Row kinds are distinguished by the styling of the first cell:
    bold + grey (#ABABAB), no data cells  -> category header   (e.g. "Equity Funds")
    regular black, no data cells          -> fund-family header (e.g. "Meezan Tahaffuz Pension Fund")
    regular black, with data cells        -> an actual fund row

Al Meezan only prints a price in the columns that differ. Most funds show
Repurchase + Offer (front-end load) and leave NAV blank; money-market funds show
NAV only; Meezan Gold shows all three (it carries a back-end load, so
Repurchase < NAV < Offer). `nav_rs` therefore falls back to the repurchase price
when the NAV column is blank, and `nav_source` records which column it came from.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "Document Samples"
OUTPUT_DIR = BASE_DIR / "Performance Output"

# Header text -> output column name. Matched after whitespace collapsing.
HEADER_MAP = {
    "funds category": "fund_name",
    "launch date": "launch_date",
    "validity date": "validity_date",
    "repurchase (rs.)": "repurchase_rs",
    "offer (rs.)": "offer_rs",
    "nav (rs.)": "nav_rs",
    "mtd": "return_mtd_pct",
    "fytd": "return_fytd_pct",
    "cytd": "return_cytd_pct",
    "since inception": "return_since_inception_pct",
}
# FY columns are dated (FY25/FY24 in 2025-26 mails, FY26/FY25 later), so they are
# mapped dynamically: "FY25" -> "return_fy25_pct".
FY_HEADER = re.compile(r"^fy\d{2}$")

PRICE_COLS = ("repurchase_rs", "offer_rs", "nav_rs")
GREY = 0.6706  # non-stroking colour Al Meezan uses for category headers


# --------------------------------------------------------------------------- #
# cleaning helpers
# --------------------------------------------------------------------------- #
def squash(text: str | None) -> str:
    """Collapse the newlines pdfplumber inserts for wrapped cells into spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip()


def squash_name(text: str | None) -> str:
    """
    Like squash(), but respects hyphenated names broken across wrapped lines.

    The HTML cell holds "MFPF-\nAggressive\nAllocation\nPlan"; joining every
    newline with a space would yield "MFPF- Aggressive ...". A newline directly
    after a hyphen is a wrap inside one token, so it is joined with nothing.
    """
    if not text:
        return ""
    joined = re.sub(r"-\n\s*", "-", text)
    return squash(joined)


def split_markers(text: str) -> tuple[str, str]:
    """'Meezan Cash Fund***' -> ('Meezan Cash Fund', '***')."""
    m = re.search(r"([*^]+)\s*$", text)
    if not m:
        return text, ""
    return text[: m.start()].strip(), m.group(1)


def parse_price(text: str) -> tuple[float | None, str]:
    value, marker = split_markers(squash(text))
    value = value.replace(",", "")
    if not value:
        return None, marker
    try:
        return float(value), marker
    except ValueError:
        raise ValueError(f"unparseable price cell: {text!r}")


def parse_pct(text: str) -> float | None:
    """'-12.28%' -> -12.28 (kept in percent units, not fractions)."""
    value = squash(text).replace(",", "")
    if not value or value in {"-", "N/A", "NA"}:
        return None
    if not value.endswith("%"):
        raise ValueError(f"unparseable percentage cell: {text!r}")
    return float(value[:-1])


def parse_date(text: str) -> str | None:
    """'Apr 01, 2026' / '28 Aug, 2024' -> '2026-04-01'. Both spellings occur."""
    value = squash(text)
    if not value:
        return None
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date cell: {text!r}")


def statement_date_from_name(path: Path) -> str | None:
    """'Gmail - Fund Prices and Performance- April 01, 2026.pdf' -> '2026-04-01'."""
    m = re.search(r"([A-Za-z]+ \d{1,2}, \d{4})", path.stem)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()


# --------------------------------------------------------------------------- #
# pdf -> rows
# --------------------------------------------------------------------------- #
def build_column_map(header_cells: list[str | None]) -> dict[int, str]:
    columns: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        key = squash(cell).lower()
        if key in HEADER_MAP:
            columns[idx] = HEADER_MAP[key]
        elif FY_HEADER.match(key):
            columns[idx] = f"return_{key}_pct"
    missing = {"fund_name", "validity_date", "repurchase_rs", "offer_rs", "nav_rs"} - set(columns.values())
    if missing:
        raise ValueError(f"header row is missing expected columns: {sorted(missing)}")
    return columns


def first_cell_style(page, row) -> tuple[bool, bool]:
    """(is_bold, is_grey) for the text in the row's first cell."""
    cell = row.cells[0]
    if cell is None:
        return False, False
    x0, top, x1, bottom = cell
    chars = [
        c
        for c in page.chars
        if top <= c["top"] < bottom and x0 <= c["x0"] < x1 and c["text"].strip()
    ]
    if not chars:
        return False, False
    bold = Counter("Bold" in c["fontname"] for c in chars).most_common(1)[0][0]
    grey = Counter(
        bool(c["non_stroking_color"]) and abs(c["non_stroking_color"][0] - GREY) < 0.02
        for c in chars
    ).most_common(1)[0][0]
    return bold, grey


def parse_pdf(path: Path) -> pd.DataFrame:
    statement_date = statement_date_from_name(path)
    records: list[dict] = []

    with pdfplumber.open(path) as pdf:
        category = ""
        fund_group = ""
        columns: dict[int, str] | None = None

        for page in pdf.pages:
            for table in page.find_tables():
                rows = table.rows
                grid = table.extract()

                for row, cells in zip(rows, grid):
                    texts = [squash(c) for c in cells]

                    # 1. locate/refresh the header row
                    if texts and texts[0].lower() == "funds category":
                        columns = build_column_map(cells)
                        continue
                    if columns is None:
                        continue  # greeting/preamble above the header

                    data_cells = [t for i, t in enumerate(texts) if i > 0 and t]
                    label = squash_name(cells[0]) if cells else ""

                    # 2. section rows carry a label and nothing else
                    if not data_cells:
                        if not label:
                            continue
                        bold, grey = first_cell_style(page, row)
                        if bold and grey:
                            category, fund_group = label, ""
                        elif len(label) < 80 and not label.startswith("("):
                            fund_group = label  # e.g. "Meezan Tahaffuz Pension Fund"
                        continue  # footnotes / disclaimers fall through here

                    # 3. a real fund row
                    name, name_marker = split_markers(label)
                    record: dict = {
                        "statement_date": statement_date,
                        "category": category,
                        "fund_group": fund_group,
                        "fund_name": name,
                        "fund_note_marker": name_marker,
                    }
                    price_markers: list[str] = []

                    for idx, column in columns.items():
                        raw = cells[idx] if idx < len(cells) else None
                        if column == "fund_name":
                            continue
                        if column.endswith("_date"):
                            record[column] = parse_date(raw)
                        elif column in PRICE_COLS:
                            value, marker = parse_price(raw)
                            record[column] = value
                            if marker:
                                price_markers.append(f"{column}{marker}")
                        else:
                            record[column] = parse_pct(raw)

                    # NAV column is only printed when it differs from the
                    # repurchase price; otherwise repurchase *is* the NAV.
                    if record.get("nav_rs") is not None:
                        record["nav_source"] = "NAV (Rs.)"
                    elif record.get("repurchase_rs") is not None:
                        record["nav_rs"] = record["repurchase_rs"]
                        record["nav_source"] = "Repurchase (Rs.)"
                    else:
                        record["nav_source"] = None

                    record["load_marker"] = ";".join(price_markers)
                    records.append(record)

    if not records:
        raise ValueError(f"no fund rows found in {path.name}")

    df = pd.DataFrame(records)
    ordered = [
        "statement_date",
        "category",
        "fund_group",
        "fund_name",
        "launch_date",
        "validity_date",
        "repurchase_rs",
        "offer_rs",
        "nav_rs",
        "nav_source",
        "load_marker",
        "fund_note_marker",
    ]
    returns = [c for c in df.columns if c.startswith("return_")]
    return df[[c for c in ordered if c in df.columns] + returns]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate(df: pd.DataFrame, path: Path) -> list[str]:
    problems: list[str] = []

    if df["fund_name"].eq("").any():
        problems.append("blank fund name")
    if df["nav_rs"].isna().any():
        bad = df.loc[df["nav_rs"].isna(), "fund_name"].tolist()
        problems.append(f"no usable NAV for: {bad}")
    if df["validity_date"].isna().any():
        problems.append("missing validity date")
    if df["category"].eq("").any():
        bad = df.loc[df["category"].eq(""), "fund_name"].tolist()
        problems.append(f"no category assigned to: {bad}")

    # Offer >= NAV >= Repurchase whenever the PDF prints both sides.
    both = df["offer_rs"].notna() & df["nav_rs"].notna()
    if (df.loc[both, "offer_rs"] < df.loc[both, "nav_rs"] - 1e-9).any():
        bad = df.loc[both & (df["offer_rs"] < df["nav_rs"] - 1e-9), "fund_name"].tolist()
        problems.append(f"offer price below NAV for: {bad}")
    printed_nav = df["repurchase_rs"].notna() & df["nav_rs"].notna() & df["nav_source"].eq("NAV (Rs.)")
    if (df.loc[printed_nav, "repurchase_rs"] > df.loc[printed_nav, "nav_rs"] + 1e-9).any():
        bad = df.loc[
            printed_nav & (df["repurchase_rs"] > df["nav_rs"] + 1e-9), "fund_name"
        ].tolist()
        problems.append(f"repurchase price above NAV for: {bad}")

    dupes = df[df.duplicated(["fund_name"], keep=False)]["fund_name"].unique().tolist()
    if dupes:
        problems.append(f"duplicate fund names: {dupes}")

    if df["statement_date"].isna().all():
        problems.append(f"could not read a statement date from filename {path.name}")

    return problems


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def write_preview(df: pd.DataFrame, target: Path) -> None:
    with pd.option_context(
        "display.max_rows", None, "display.max_columns", None, "display.width", 250
    ):
        body = df.to_string(index=False, na_rep="")
    header = f"{df['statement_date'].iloc[0]}  |  {len(df)} funds\n"
    target.write_text(header + "=" * 120 + "\n" + body + "\n", encoding="utf-8")


def process(path: Path) -> pd.DataFrame:
    df = parse_pdf(path)
    OUTPUT_DIR.mkdir(exist_ok=True)

    stamp = df["statement_date"].iloc[0] or path.stem
    csv_path = OUTPUT_DIR / f"fund_prices_{stamp}.csv"
    txt_path = OUTPUT_DIR / f"fund_prices_{stamp}.txt"
    df.to_csv(csv_path, index=False)
    write_preview(df, txt_path)

    print(f"\n{path.name}")
    print(f"  -> {csv_path.relative_to(BASE_DIR)}  ({len(df)} funds, {len(df.columns)} columns)")
    print(f"  -> {txt_path.relative_to(BASE_DIR)}")
    print(f"     categories: {df['category'].nunique()}  |  "
          f"nav from NAV col: {(df['nav_source'] == 'NAV (Rs.)').sum()}  |  "
          f"from repurchase col: {(df['nav_source'] == 'Repurchase (Rs.)').sum()}")

    for problem in validate(df, path):
        print(f"     ! {problem}")
    return df


def main(argv: list[str]) -> int:
    if argv:
        paths = [Path(a) for a in argv]
    else:
        paths = sorted(INPUT_DIR.glob("*.pdf"))
    if not paths:
        print(f"no PDFs found in {INPUT_DIR}")
        return 1
    for path in paths:
        process(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
