import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT = DATA_DIR / "snapshot.csv"
DETAIL_DIR = DATA_DIR / "details"

st.set_page_config(page_title="Stock Screener Lite", page_icon="📈", layout="wide")


# ----------------------------------------------------------------- data loading (cached = fast)
@st.cache_data(show_spinner=False)
def load_snapshot(_mtime: float) -> pd.DataFrame:
    return pd.read_csv(SNAPSHOT)


@st.cache_data(show_spinner=False)
def load_detail(symbol: str, _mtime: float) -> dict:
    return json.loads((DETAIL_DIR / f"{symbol}.json").read_text())


@st.cache_data(ttl=3600, show_spinner="Fetching live from Screener...")
def live_fetch(symbol: str) -> dict:
    from scraper import fetch_company  # imported lazily

    return fetch_company(symbol)


def split_to_df(t: dict) -> pd.DataFrame:
    return pd.DataFrame(t["data"], columns=t["columns"])


def pair_chart(t: dict, items: list[str], title: str):
    df = split_to_df(t).set_index("Item")
    rows = [i for i in items if i in df.index]
    if not rows:
        return
    wide = df.loc[rows].T
    periods = list(wide.index)
    long = wide.reset_index(names="Period").melt("Period", var_name="Item", value_name="Value")
    chart = (
        alt.Chart(long, title=title)
        .mark_bar()
        .encode(
            x=alt.X("Period:N", sort=periods, title=None),
            y=alt.Y("Value:Q", title="₹ Cr"),
            xOffset="Item:N",
            color="Item:N",
            tooltip=["Period", "Item", "Value"],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)


def show_company(d: dict):
    st.subheader(f"{d['name']}  ·  {d['symbol']}")
    if d.get("sector") or d.get("industry"):
        st.caption(" › ".join(x for x in (d.get("sector"), d.get("industry")) if x))

    r = d["ratios"]
    keys = ["Market Cap", "Current Price", "Stock P/E", "ROCE", "ROE", "Book Value", "Dividend Yield", "52w High", "52w Low"]
    shown = [k for k in keys if k in r]
    cols = st.columns(min(len(shown), 5) or 1)
    for i, k in enumerate(shown):
        cols[i % len(cols)].metric(k, f"{r[k]:,.2f}" if r[k] is not None else "–")

    tb = d["tables"]
    if "profit-loss" in tb:
        pair_chart(tb["profit-loss"], ["Sales", "Revenue", "Net Profit"], "Sales vs Net Profit")

    names = {
        "quarters": "Quarterly",
        "profit-loss": "Profit & Loss",
        "balance-sheet": "Balance Sheet",
        "cash-flow": "Cash Flow",
        "ratios": "Ratios",
        "shareholding": "Shareholding",
    }
    avail = [k for k in names if k in tb]
    if avail:
        tabs = st.tabs([names[k] for k in avail])
        for tab, k in zip(tabs, avail):
            with tab:
                st.dataframe(split_to_df(tb[k]).set_index("Item"), use_container_width=True)
    st.caption(f"Data fetched: {d['fetched_at'][:16].replace('T', ' ')} UTC · Source: screener.in")


# ----------------------------------------------------------------- UI
st.title("📈 Stock Screener Lite")

if not SNAPSHOT.exists():
    st.warning(
        "No data yet. Run `python scraper.py --limit 20` locally (or trigger the GitHub Action "
        "'Scrape Screener data'), commit the `data/` folder, then reload."
    )
    st.stop()

snap = load_snapshot(SNAPSHOT.stat().st_mtime)
tab_screen, tab_company = st.tabs(["🔎 Screener", "🏢 Company"])

# ---- Screener tab
with tab_screen:
    with st.sidebar:
        st.header("Filters")
        q = st.text_input("Search name / symbol")
        sectors = ["All"] + sorted(s for s in snap["Sector"].dropna().unique() if s)
        sector = st.selectbox("Sector", sectors)
        min_mcap = st.number_input("Min Market Cap (₹ Cr)", 0.0, step=500.0)
        max_pe = st.number_input("Max P/E (0 = off)", 0.0, step=1.0)
        min_roce = st.number_input("Min ROCE %", 0.0, step=1.0)
        min_roe = st.number_input("Min ROE %", 0.0, step=1.0)
        min_div = st.number_input("Min Dividend Yield %", 0.0, step=0.5)

    f = snap.copy()
    if q:
        f = f[
            f["Symbol"].str.contains(q, case=False, na=False)
            | f["Name"].str.contains(q, case=False, na=False)
        ]
    if sector != "All":
        f = f[f["Sector"] == sector]

    def at_least(df, col, v):
        return df[df[col] >= v] if v > 0 and col in df else df

    f = at_least(f, "Market Cap", min_mcap)
    f = at_least(f, "ROCE", min_roce)
    f = at_least(f, "ROE", min_roe)
    f = at_least(f, "Dividend Yield", min_div)
    if max_pe > 0 and "Stock P/E" in f:
        f = f[(f["Stock P/E"] > 0) & (f["Stock P/E"] <= max_pe)]

    st.write(f"**{len(f)}** of {len(snap)} companies match")
    show_cols = [
        c for c in ["Symbol", "Name", "Sector", "Market Cap", "Current Price", "Stock P/E",
                    "ROCE", "ROE", "Dividend Yield", "Sales", "Net Profit", "OPM %"]
        if c in f.columns
    ]
    st.dataframe(
        f[show_cols],
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={c: st.column_config.NumberColumn(format="%.1f") for c in show_cols
                       if c not in ("Symbol", "Name", "Sector")},
    )
    st.download_button("⬇️ Download as CSV", f.to_csv(index=False), "screen_results.csv", "text/csv")
    st.caption(f"Last data update: {snap['Updated'].max()} UTC")

# ---- Company tab
with tab_company:
    labels = {row.Symbol: f"{row.Symbol} – {row.Name}" for row in snap.itertuples()}
    choice = st.selectbox("Pick a company", list(labels), format_func=labels.get)
    p = DETAIL_DIR / f"{choice}.json"
    if p.exists():
        show_company(load_detail(choice, p.stat().st_mtime))

    with st.expander("Symbol not in the list? Fetch it live"):
        sym = st.text_input("Screener symbol (e.g. TATAMOTORS)").strip().upper()
        if st.button("Fetch") and sym:
            try:
                show_company(live_fetch(sym))
            except Exception as e:
                st.error(f"Could not fetch {sym}: {e}")
