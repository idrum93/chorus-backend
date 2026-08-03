#!/usr/bin/env python3
"""
prices.py — free daily sector prices, no key, no account.

Fetches daily closes for a sector ETF per Chorus sector and stores them next to
the article data. Stooq serves plain CSV over HTTP with no authentication.

    python3 prices.py --probe      one request, is it reachable?
    python3 prices.py              fetch every mapped sector
    python3 prices.py --report     what's stored

WHY THIS EXISTS, AND WHAT IT IS NOT

Ten rounds of testing found no evidence that vocabulary spreading across sectors
predicts anything. Prices are here as CONTEXT, not as a forecast: "here is what
this sector's prices did while the phrase was spreading" — concurrent, never
leading. The dashboard labels it that way and must keep doing so.

The second reason is preparation. When there is enough narrative history to test
the prediction claim properly — on the order of a hundred ignition events, so
roughly six months — the price series will already be sitting here, and the test
becomes a script rather than a project. Collecting now costs nothing and removes
a dependency from that future decision.
"""

import argparse, csv, io, os, sqlite3, sys, time
import urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))
UA   = "chorus-monitor/3.0 (sector price context)"

# One liquid sector ETF per Chorus sector. Policy, Waste & water and Labour have
# no clean equivalent and are deliberately absent — a bad proxy is worse than
# none, because it invites reading meaning into noise.
SECTOR_ETF = {
    "Utilities":    ("xlu.us", "Utilities Select Sector SPDR"),
    "Energy":       ("xle.us", "Energy Select Sector SPDR"),
    "Technology":   ("xlk.us", "Technology Select Sector SPDR"),
    "Industrials":  ("xli.us", "Industrial Select Sector SPDR"),
    "Materials":    ("xlb.us", "Materials Select Sector SPDR"),
    "Healthcare":   ("xlv.us", "Health Care Select Sector SPDR"),
    "Finance":      ("xlf.us", "Financial Select Sector SPDR"),
    "Real estate":  ("xlre.us", "Real Estate Select Sector SPDR"),
    "Consumer":     ("xly.us", "Consumer Discretionary SPDR"),
    "Supply chain": ("iyt.us", "iShares Transportation Average"),
    "Autos":        ("carz.us", "First Trust Global Auto"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(
  day TEXT, symbol TEXT, sector TEXT, close REAL,
  PRIMARY KEY(day, symbol)
);
CREATE INDEX IF NOT EXISTS idx_prices_sector ON prices(sector, day);
"""


def fetch_csv(symbol, timeout=40):
    """Stooq daily history as CSV: Date,Open,High,Low,Close,Volume"""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    if not body.strip() or body.strip().lower().startswith("<"):
        raise RuntimeError("empty or non-CSV reply")
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows or "Close" not in (rows[0] or {}):
        raise RuntimeError(f"unexpected columns: {list((rows[0] or {}).keys())[:6]}")
    out = []
    for r_ in rows:
        try:
            day = r_["Date"][:10]
            close = float(r_["Close"])
            datetime.strptime(day, "%Y-%m-%d")
            out.append((day, close))
        except (ValueError, KeyError, TypeError):
            continue
    if not out:
        raise RuntimeError("no usable rows")
    return sorted(out)


def probe():
    print("\nTesting price data. One request.\n")
    try:
        rows = fetch_csv("xlu.us")
    except Exception as e:
        print(f"  FAILED — {str(e)[:160]}\n")
        print("  If this is a network error, prices simply stay unavailable and")
        print("  nothing else in Chorus is affected. Send me the message.\n")
        return 1
    first, last = rows[0], rows[-1]
    print(f"  OK — {len(rows)} daily closes for XLU (utilities)")
    print(f"       {first[0]} at {first[1]:.2f}  →  {last[0]} at {last[1]:.2f}")
    print(f"\n  {len(SECTOR_ETF)} sectors are mapped. Run without --probe to fetch them all.\n")
    return 0


def collect(conn, since_days=400, verbose=True):
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=since_days)).isoformat()
    total, ok = 0, 0
    for sector, (symbol, name) in sorted(SECTOR_ETF.items()):
        try:
            rows = [(d, c) for d, c in fetch_csv(symbol) if d >= cutoff]
            for day, close in rows:
                conn.execute("INSERT INTO prices VALUES(?,?,?,?) "
                             "ON CONFLICT(day,symbol) DO UPDATE SET close=?",
                             (day, symbol, sector, close, close))
            conn.commit()
            total += len(rows); ok += 1
            if verbose:
                print(f"  ok   {sector:<14} {symbol:<9} {len(rows):>5} days  {name}")
        except Exception as e:
            if verbose:
                print(f"  FAIL {sector:<14} {symbol:<9} {str(e)[:70]}")
        time.sleep(1.2)
    return total, ok, len(SECTOR_ETF)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()

    if args.probe:
        return probe()

    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    conn.commit()

    if not args.report:
        print(f"Fetching daily closes for {len(SECTOR_ETF)} sectors")
        total, ok, tried = collect(conn, args.days)
        print(f"\n  {total} price-days stored · {ok}/{tried} sectors")

    rows = list(conn.execute(
        "SELECT sector, COUNT(*), MIN(day), MAX(day) FROM prices GROUP BY sector ORDER BY sector"))
    if rows:
        print(f"\n  {'sector':<14} {'days':>6}  range")
        for sector, n, lo, hi in rows:
            print(f"  {sector:<14} {n:>6}  {lo} → {hi}")
    else:
        print("\n  No prices stored yet.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
