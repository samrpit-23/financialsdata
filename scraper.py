"""
Screener.in scraper
-------------------
Reads symbols.txt, fetches each company page, and saves:
  data/details/<SYMBOL>.json   -> ratios + financial tables for one company
  data/snapshot.csv            -> one row per company (used by the app for fast screening)

Usage:
  python scraper.py                      # scrape everything in symbols.txt (skips fresh data)
  python scraper.py --limit 20           # test with 20 symbols
  python scraper.py --force              # ignore freshness, re-scrape all
  python scraper.py --workers 2 --delay 1.5   # be gentler
"""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DETAIL_DIR = DATA_DIR / "details"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SECTIONS = ["quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios", "shareholding"]


# ----------------------------------------------------------------- parsing helpers
def to_num(text):
    """'â¹ 1,23,456' -> 123456.0 ; '12.5 %' -> 12.5 ; junk -> None"""
    if text is None:
        return None
    t = re.sub(r"[â¹,%\s]|Cr\.", "", text)
    try:
        return float(t)
    except ValueError:
        return None


def table_to_split(table_tag):
    """HTML <table> -> {'columns': [...], 'data': [[...], ...]} with numeric cells."""
    df = pd.read_html(StringIO(str(table_tag)))[0]
    df.columns = ["Item"] + [str(c) for c in df.columns[1:]]
    df["Item"] = df["Item"].astype(str).str.replace(r"\s*\+$", "", regex=True).str.strip()
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace(r"[,%]", "", regex=True), errors="coerce"
        )
    df = df.dropna(how="all", subset=list(df.columns[1:]))
    df = df.astype(object).where(df.notna(), None)
    d = df.to_dict(orient="split")
    d.pop("index", None)
    return d


def parse_company(symbol, html):
    soup = BeautifulSoup(html, "lxml")

    h1 = soup.select_one("h1")
    name = h1.get_text(strip=True) if h1 else symbol

    ratios = {}
    for li in soup.select("#top-ratios li"):
        name_tag = li.select_one(".name")
        if not name_tag:
            continue
        key = name_tag.get_text(strip=True)
        nums = [to_num(n.get_text()) for n in li.select(".number")]
        if key == "High / Low" and len(nums) == 2:
            ratios["52w High"], ratios["52w Low"] = nums
        elif nums:
            ratios[key] = nums[0]

    peers = [a.get_text(strip=True) for a in soup.select("#peers a[href^='/market/']")]
    sector = peers[0] if peers else ""
    industry = peers[-1] if len(peers) > 1 else ""

    tables = {}
    for sec_id in SECTIONS:
        sec = soup.select_one(f"section#{sec_id}")
        t = sec.select_one("table.data-table") if sec else None
        if t is None:
            continue
        try:
            tables[sec_id] = table_to_split(t)
        except Exception:
            pass

    return {
        "symbol": symbol,
        "name": name,
        "sector": sector,
        "industry": industry,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "ratios": ratios,
        "tables": tables,
    }


# ----------------------------------------------------------------- network
def fetch_html(symbol, session=None):
    s = session or requests.Session()
    s.headers.update(HEADERS)
    r = None
    for suffix in ("consolidated/", ""):
        url = f"https://www.screener.in/company/{symbol}/{suffix}"
        for attempt in range(4):
            r = s.get(url, timeout=25)
            if r.status_code == 429:  # rate limited -> back off
                time.sleep(15 * (attempt + 1))
                continue
            break
        if r.status_code == 200 and "top-ratios" in r.text:
            return r.text
    code = r.status_code if r is not None else "?"
    raise RuntimeError(f"{symbol}: not found or blocked (HTTP {code})")


def fetch_company(symbol):
    """Fetch + parse one company (also used by the app for live look-ups)."""
    return parse_company(symbol.upper().strip(), fetch_html(symbol.upper().strip()))


# ----------------------------------------------------------------- storage
def detail_path(symbol):
    return DETAIL_DIR / f"{symbol}.json"


def is_fresh(symbol, max_age_hours):
    p = detail_path(symbol)
    if not p.exists():
        return False
    try:
        ts = datetime.fromisoformat(json.loads(p.read_text())["fetched_at"])
        return datetime.now(timezone.utc) - ts < timedelta(hours=max_age_hours)
    except Exception:
        return False


def latest_value(tables, table, labels):
    t = tables.get(table)
    if not t:
        return None
    for row in t["data"]:
        if row[0] in labels:
            for v in reversed(row[1:]):
                if v is not None:
                    return v
    return None


def _series(table, labels):
    for row in table["data"]:
        if row[0] in labels:
            return row[1:]
    return []


def _at(series, k):
    """k=1 latest quarter, k=2 previous quarter, k=5 same quarter last year."""
    return series[-k] if len(series) >= k else None


def _pct(cur, prev):
    """% change; uses abs(prev) so a loss that shrinks is not shown as a fall."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev) * 100, 1)


def _streak(vals):
    """How many quarters in a row the value has increased, counting back from the latest."""
    n = 0
    for i in range(len(vals) - 1, 0, -1):
        a, b = vals[i], vals[i - 1]
        if a is None or b is None or a <= b:
            break
        n += 1
    return n


def quarter_metrics(tables):
    t = tables.get("quarters")
    if not t:
        return {}
    cols = t["columns"][1:]
    npf = _series(t, ("Net Profit",))
    sales = _series(t, ("Sales", "Revenue"))
    opm = _series(t, ("OPM %", "Financing Margin %"))

    np0, np1, np4 = _at(npf, 1), _at(npf, 2), _at(npf, 5)
    s0, s1, s4 = _at(sales, 1), _at(sales, 2), _at(sales, 5)
    o0, o1 = _at(opm, 1), _at(opm, 2)

    def flag(cond):
        return int(bool(cond))

    return {
        "Qtr": cols[-1] if cols else None,
        "NP Latest Qtr": np0,
        "NP Prev Qtr": np1,
        "NP Year Ago Qtr": np4,
        "NP QoQ %": _pct(np0, np1),
        "NP YoY %": _pct(np0, np4),
        "Sales QoQ %": _pct(s0, s1),
        "Sales YoY %": _pct(s0, s4),
        "OPM Chg QoQ": round(o0 - o1, 1) if o0 is not None and o1 is not None else None,
        "Turnaround QoQ": flag(np0 is not None and np1 is not None and np1 < 0 < np0),
        "Turnaround YoY": flag(np0 is not None and np4 is not None and np4 < 0 < np0),
        "Profit To Loss QoQ": flag(np0 is not None and np1 is not None and np1 > 0 > np0),
        "NP Up Streak": _streak(npf[-8:]),
        "Sales Up Streak": _streak(sales[-8:]),
    }


def build_snapshot():
    rows = []
    for p in sorted(DETAIL_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        tb = d.get("tables", {})
        row = {
            "Symbol": d["symbol"],
            "Name": d.get("name"),
            "Sector": d.get("sector"),
            "Industry": d.get("industry"),
            **d.get("ratios", {}),
            "Sales": latest_value(tb, "profit-loss", ("Sales", "Revenue")),
            "Net Profit": latest_value(tb, "profit-loss", ("Net Profit",)),
            "OPM %": latest_value(tb, "profit-loss", ("OPM %", "Financing Margin %")),
            **quarter_metrics(tb),
            "Updated": d.get("fetched_at", "")[:16].replace("T", " "),
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty and "Market Cap" in df:
        df = df.sort_values("Market Cap", ascending=False)
    df.to_csv(DATA_DIR / "snapshot.csv", index=False)
    return len(df)


# ----------------------------------------------------------------- main
def read_symbols(path):
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if line and line not in out:
            out.append(line)
    return out


def scrape_one(symbol, delay):
    time.sleep(random.uniform(delay * 0.5, delay * 1.2))
    data = fetch_company(symbol)
    detail_path(symbol).write_text(json.dumps(data, default=str))
    return symbol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=str(ROOT / "symbols.txt"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--delay", type=float, default=1.0, help="avg seconds between requests per worker")
    ap.add_argument("--max-age-hours", type=float, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    symbols = read_symbols(args.symbols)
    if args.limit:
        symbols = symbols[: args.limit]

    todo = symbols if args.force else [s for s in symbols if not is_fresh(s, args.max_age_hours)]
    print(f"{len(symbols)} symbols, {len(todo)} to scrape")

    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(scrape_one, s, args.delay): s for s in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            sym = futures[fut]
            try:
                fut.result()
                ok += 1
            except Exception as e:
                failed.append(sym)
                print(f"  FAIL {sym}: {e}")
            if i % 25 == 0:
                print(f"  ...{i}/{len(todo)}")

    n = build_snapshot()
    print(f"Done. scraped={ok} failed={len(failed)} snapshot_rows={n}")
    if failed:
        print("Failed:", ", ".join(failed))
    if todo and ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
