"""
Meezan NAV tracker -- line chart of NAV over time, one line per fund.

    streamlit run app.py
"""

from pathlib import Path

import pandas as pd
import streamlit as st

DATA_FILE = Path(__file__).resolve().parent / "Performance Output" / "nav_history.csv"

DEFAULT_FUNDS = [
    "Al Meezan Mutual Fund",
    "Meezan Islamic Fund",
    "Meezan Gold Fund",
]

st.set_page_config(page_title="Meezan NAV Tracker", layout="wide")
st.title("Meezan NAV Tracker")


@st.cache_data
def load_nav() -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE, parse_dates=["date"])
    return df.sort_values(["fund_name", "date"])


nav = load_nav()
fund_names = sorted(nav["fund_name"].unique())

selected = st.multiselect(
    "Funds",
    options=fund_names,
    default=[f for f in DEFAULT_FUNDS if f in fund_names] or fund_names[:3],
)

if not selected:
    st.info("Pick at least one fund.")
    st.stop()

chart_data = nav[nav["fund_name"].isin(selected)]

# Long format with color=fund_name, not a wide pivot: funds are priced on
# slightly different days (Gold lags the equity funds by one), so a pivot would
# be mostly gaps. This way each line is drawn on its own points.
st.line_chart(
    chart_data,
    x="date",
    y="nav_rs",
    color="fund_name",
    x_label="Date",
    y_label="NAV (Rs.)",
)

with st.expander("Data"):
    st.dataframe(
        chart_data.pivot_table(index="date", columns="fund_name", values="nav_rs"),
        use_container_width=True,
    )
