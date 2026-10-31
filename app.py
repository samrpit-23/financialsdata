import json
import re
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT = DATA_DIR / "snapshot.csv"
DETAIL_DIR = DATA_DIR / "details"

st.set_page_config(page_title="Stock Screener Lite", page_icon="📈", layout="wide")

FLAG_COLS = ["Turnaround QoQ", "Turnaround YoY", "Profit To Loss QoQ", "NP Up Streak", "Sales Up Streak"]
RENAME = {"Stock P/E": "pe", "Current Price": "price", "52w High": "high_52w", "52w Low": "low_52w", "OPM %": "opm", "P/B": "pb"}


def col_id(c: str) -> str:
    """'NP QoQ %' -> 'np_qoq' (safe names to use inside filters)."""
    return RENAME.get(c) or re.sub(r"[^0-9a-zA-Z]+", "_", c).strip("_").lower()


GLOSSARY = {
    "symbol": "Screener symbol, text",
    "name": "Company name, text",
    "sector": "Sector, text (compare with == only)",
    "market_cap": "Market cap in ₹ Crore",
    "price": "Current price in ₹",
    "pe": "Stock P/E",
    "pb": "Price to book value",
    "roce": "ROCE %",
    "roe": "ROE %",
    "dividend_yield": "Dividend yield %",
    "book_value": "Book value per share ₹",
    "from_52w_high": "% distance from 52-week high (0 = at high, -20 = 20% below)",
    "sales": "Latest TTM/annual sales ₹ Cr",
    "net_profit": "Latest TTM/annual net profit ₹ Cr",
    "opm": "Operating profit margin % (TTM)",
    "qtr": "Latest reported quarter, text like 'Sep 2026'",
    "np_latest_qtr": "Net profit in latest quarter ₹ Cr",
    "np_prev_qtr": "Net profit in previous quarter ₹ Cr",
    "np_year_ago_qtr": "Net profit in same quarter last year ₹ Cr",
    "np_qoq": "Net profit % change vs previous quarter (30 means +30%)",
    "np_yoy": "Net profit % change vs same quarter last year",
    "sales_qoq": "Sales % change vs previous quarter",
    "sales_yoy": "Sales % change vs same quarter last year",
    "opm_chg_qoq": "Change in operating margin vs previous quarter, in percentage points",
    "turnaround_qoq": "1 if net profit was negative last quarter and is positive now, else 0",
    "turnaround_yoy": "1 if net profit was negative in the year-ago quarter and is positive now, else 0",
    "profit_to_loss_qoq": "1 if net profit was positive last quarter and is negative now, else 0",
    "np_up_streak": "Number of consecutive quarters net profit has increased (latest backwards)",
    "sales_up_streak": "Number of consecutive quarters sales has increased",
}

# name -> (filter expression, sort column, ascending)
PRESETS = {
    "Loss → Profit turnaround (vs last quarter)": ("turnaround_qoq == 1", "market_cap", False),
    "Loss → Profit turnaround (vs year-ago quarter)": ("turnaround_yoy == 1", "market_cap", False),
    "Net profit up > 30% QoQ (profit-making base)": ("np_qoq > 30 and np_prev_qtr > 0", "np_qoq", False),
    "Net profit up > 30% YoY (profit-making base)": ("np_yoy > 30 and np_year_ago_qtr > 0", "np_yoy", False),
    "Sales and profit both up > 20% QoQ": ("sales_qoq > 20 and np_qoq > 20 and np_prev_qtr > 0", "np_qoq", False),
    "Net profit rising 3+ quarters in a row": ("np_up_streak >= 3", "np_up_streak", False),
    "Slipped from profit into loss (QoQ)": ("profit_to_loss_qoq == 1", "market_cap", False),
    "Quality + growth: ROCE>20, PE<25, profit up YoY": ("roce > 20 and pe > 0 and pe < 25 and np_yoy > 0", "roce", False),
}


# ----------------------------------------------------------------- data loading (cached = fast)
@st.cache_data(show_spinner=False)
def load_snapshot(mtime: float) -> pd.DataFrame:
    df = pd.read_csv(SNAPSHOT)
    if {"Current Price", "Book Value"} <= set(df.columns):
        df["P/B"] = (df["Current Price"] / df["Book Value"]).where(df["Book Value"] > 0).round(2)
    if {"Current Price", "52w High"} <= set(df.columns):
        df["From 52w High %"] = ((df["Current Price"] / df["52w High"] - 1) * 100).round(1)
    for c in FLAG_COLS:
        if c in df.columns:
            df[c] = df[c].fillna(0).astype(int)
    return df


@st.cache_data(show_spinner=False)
def load_detail(symbol: str, mtime: float) -> dict:
    return json.loads((DETAIL_DIR / f"{symbol}.json").read_text())


@st.cache_data(ttl=3600, show_spinner="Fetching live from Screener...")
def live_fetch(symbol: str) -> dict:
    from scraper import fetch_company

    return fetch_company(symbol)


def get_secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


# ----------------------------------------------------------------- safe filter engine
SAFE_WORDS = {"and", "or", "not", "True", "False"}


def validate_expr(expr: str, cols: set):
    if not expr.strip():
        raise ValueError("Filter is empty")
    e = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', "0", expr)  # ignore text literals
    for w in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", e):
        if w not in cols and w not in SAFE_WORDS:
            raise ValueError(f"Unknown column or word: '{w}'")
    rest = re.sub(r"[A-Za-z_][A-Za-z_0-9]*|\d+(\.\d+)?", " ", e)
    if re.search(r"[^\s+\-*/<>=!&|()]", rest) or "**" in rest:
        raise ValueError("Unsupported characters in filter")


def run_filter(q: pd.DataFrame, expr: str, sort_by=None, ascending=False):
    validate_expr(expr, set(q.columns))
    res = q.query(expr, engine="python")
    if sort_by in q.columns:
        res = res.sort_values(sort_by, ascending=ascending, na_position="last")
    return res.index


def ask_claude(question: str, q: pd.DataFrame) -> dict:
    key = get_secret("ANTHROPIC_API_KEY")
    model = get_secret("ANTHROPIC_MODEL") or "claude-haiku-5-5"
    cols = "\n".join(f"- {c}: {GLOSSARY.get(c, '')}" for c in q.columns)
    sectors = ", ".join(sorted(s for s in q["sector"].dropna().unique() if s)[:80]) if "sector" in q else ""
    system = f"""You turn a user's question about Indian stocks into a filter for a pandas DataFrame.
Columns you may use (one row per company):
{cols}

Valid sector values: {sectors}

Rules:
- Use ONLY the column names above. Allowed syntax: numbers, > < >= <= == !=, and, or, not, parentheses.
- Text columns (sector, symbol, name) may only be compared with == to a double-quoted string.
- Percentages are plain numbers (30 means 30%). Money is in ₹ Crore.
- When the user asks for profit GROWTH of x%, also require the base to be positive (e.g. np_prev_qtr > 0) so loss-narrowing is not counted as growth, unless they ask about turnarounds or loss reduction.
- 'Turned positive from negative' -> turnaround_qoq == 1 (quarter on quarter) or turnaround_yoy == 1 (year on year).
- If the question cannot be answered with these columns, return {{"error": "short reason"}}.
Reply with ONLY a JSON object: {{"query": "...", "sort_by": "<column or null>", "ascending": false, "explain": "one short sentence"}}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 400, "system": system, "messages": [{"role": "user", "content": question}]},
        timeout=45,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI reply was not understood")
    return json.loads(m.group(0))


# ----------------------------------------------------------------- display helpers
def split_to_df(t: dict) -> pd.DataFrame:
    return pd.DataFrame(t["data"], columns=t["columns"])


def pair_chart(t: dict, items, title: str):
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
    st.altair_chart(chart, width="stretch")


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
    if "quarters" in tb:
        pair_chart(tb["quarters"], ["Sales", "Revenue", "Net Profit"], "Quarterly Sales vs Net Profit")
    if "profit-loss" in tb:
        pair_chart(tb["profit-loss"], ["Sales", "Revenue", "Net Profit"], "Yearly Sales vs Net Profit")

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
                st.dataframe(split_to_df(tb[k]).set_index("Item"), width="stretch")
    st.caption(f"Data fetched: {d['fetched_at'][:16].replace('T', ' ')} UTC · Source: screener.in")


def show_results(snap: pd.DataFrame, idx, expr: str, id2label: dict, key: str):
    base = ["Symbol", "Name", "Sector", "Market Cap", "Current Price", "Stock P/E", "ROCE",
            "Qtr", "NP Prev Qtr", "NP Latest Qtr", "NP QoQ %", "Sales QoQ %"]
    used = [id2label[w] for w in re.findall(r"[A-Za-z_]\w*", expr) if w in id2label]
    labels = list(dict.fromkeys([c for c in base if c in snap.columns] + used))
    out = snap.loc[idx, labels]
    st.success(f"**{len(out)}** companies match (out of {len(snap)} tracked)")
    st.dataframe(
        out,
        width="stretch",
        hide_index=True,
        height=min(600, 80 + 35 * max(len(out), 3)),
        column_config={c: st.column_config.NumberColumn(format="%.1f") for c in out.columns
                       if pd.api.types.is_numeric_dtype(out[c])},
    )
    if len(out):
        st.download_button("⬇️ Download CSV", out.to_csv(index=False), "results.csv", "text/csv", key=f"dl_{key}")


# ----------------------------------------------------------------- UI
st.title("📈 Stock Screener Lite")

if not SNAPSHOT.exists():
    st.warning(
        "No data yet. Run the GitHub Action 'Scrape Screener data' (or `python scraper.py --limit 20` "
        "locally), commit the `data/` folder, then reload."
    )
    st.stop()

snap = load_snapshot(SNAPSHOT.stat().st_mtime)
q = snap.copy()
q.columns = [col_id(c) for c in snap.columns]
id2label = {col_id(c): c for c in snap.columns}

tab_ask, tab_screen, tab_company = st.tabs(["💬 Ask", "🔎 Screener", "🏢 Company"])

# ---------------------------------------------------------------- Ask tab
with tab_ask:
    if "np_qoq" not in q.columns:
        st.warning(
            "Growth columns are not in your data yet. Run the GitHub Action 'Scrape Screener data' "
            "once more (it rebuilds the table), then reload this page."
        )
    else:
        mode = st.radio("How do you want to search?", ["⚡ Quick screens", "🤖 Ask in plain English", "🧮 Custom filter"],
                        horizontal=True)

        if mode.startswith("⚡"):
            name = st.selectbox("Choose a screen", list(PRESETS))
            expr, sort_by, asc = PRESETS[name]
            st.code(expr, language="text")
            try:
                show_results(snap, run_filter(q, expr, sort_by, asc), expr, id2label, "preset")
            except Exception as e:
                st.error(str(e))

        elif mode.startswith("🤖"):
            if not get_secret("ANTHROPIC_API_KEY"):
                st.info(
                    "AI search needs an Anthropic API key. Add `ANTHROPIC_API_KEY` in your Streamlit app → "
                    "Settings → Secrets (steps are in the chat). Until then, use Quick screens or Custom filter."
                )
            else:
                question = st.text_input(
                    "Your question",
                    placeholder="e.g. companies whose net profit turned positive from negative last quarter",
                )
                if st.button("Search", type="primary") and question.strip():
                    st.session_state["ai_calls"] = st.session_state.get("ai_calls", 0) + 1
                    if st.session_state["ai_calls"] > 30:
                        st.error("Question limit reached for this session. Refresh the page to reset.")
                    else:
                        try:
                            with st.spinner("Thinking..."):
                                st.session_state["ai_result"] = ask_claude(question, q)
                        except Exception as e:
                            st.session_state["ai_result"] = {"error": f"AI request failed: {e}"}

                res = st.session_state.get("ai_result")
                if res:
                    if res.get("error"):
                        st.warning(res["error"])
                    else:
                        st.caption(f"💡 {res.get('explain', '')}")
                        st.code(res.get("query", ""), language="text")
                        try:
                            idx = run_filter(q, res["query"], res.get("sort_by"), bool(res.get("ascending", False)))
                            show_results(snap, idx, res["query"], id2label, "ai")
                        except Exception as e:
                            st.error(f"Could not run that filter: {e}")

        else:
            st.write("Combine columns with `and`, `or`, `not`, and `> < >= <= == !=`.")
            expr = st.text_input("Filter", value="np_qoq > 30 and np_prev_qtr > 0 and roce > 15")
            c1, c2 = st.columns(2)
            sort_by = c1.selectbox("Sort by", [c for c in q.columns if pd.api.types.is_numeric_dtype(q[c])],
                                   index=list(c for c in q.columns if pd.api.types.is_numeric_dtype(q[c])).index("np_qoq"))
            asc = c2.checkbox("Ascending", value=False)
            try:
                show_results(snap, run_filter(q, expr, sort_by, asc), expr, id2label, "custom")
            except Exception as e:
                st.error(str(e))
            with st.expander("📖 Column names you can use"):
                st.dataframe(
                    pd.DataFrame({"column": list(q.columns), "meaning": [GLOSSARY.get(c, "") for c in q.columns]}),
                    hide_index=True, width="stretch",
                )

# ---------------------------------------------------------------- Screener tab
with tab_screen:
    with st.expander("Filters", expanded=True):
        a, b, c = st.columns(3)
        qtext = a.text_input("Search name / symbol")
        sectors = ["All"] + sorted(s for s in snap["Sector"].dropna().unique() if s)
        sector = b.selectbox("Sector", sectors)
        min_mcap = c.number_input("Min Market Cap (₹ Cr)", 0.0, step=500.0)
        d, e, f_, g = st.columns(4)
        max_pe = d.number_input("Max P/E (0 = off)", 0.0, step=1.0)
        min_roce = e.number_input("Min ROCE %", 0.0, step=1.0)
        min_roe = f_.number_input("Min ROE %", 0.0, step=1.0)
        min_div = g.number_input("Min Dividend Yield %", 0.0, step=0.5)

    f = snap.copy()
    if qtext:
        f = f[f["Symbol"].str.contains(qtext, case=False, na=False) | f["Name"].str.contains(qtext, case=False, na=False)]
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
    show_cols = [c for c in ["Symbol", "Name", "Sector", "Market Cap", "Current Price", "Stock P/E", "ROCE", "ROE",
                             "Dividend Yield", "Sales", "Net Profit", "OPM %", "NP QoQ %", "NP YoY %"] if c in f.columns]
    st.dataframe(
        f[show_cols], width="stretch", hide_index=True, height=560,
        column_config={c: st.column_config.NumberColumn(format="%.1f") for c in show_cols
                       if c not in ("Symbol", "Name", "Sector")},
    )
    st.download_button("⬇️ Download as CSV", f.to_csv(index=False), "screen_results.csv", "text/csv")
    st.caption(f"Last data update: {snap['Updated'].max()} UTC")

# ---------------------------------------------------------------- Company tab
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
            except Exception as ex:
                st.error(f"Could not fetch {sym}: {ex}")
