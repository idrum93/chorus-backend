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

import argparse, csv, io, json, os, sqlite3, sys, time
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


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get(url, timeout=40, browser=True):
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA if browser else UA,
                      "Accept": "text/csv,application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def from_yahoo(sym, timeout=40):
    """Yahoo's chart endpoint. JSON, no key, no account."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=2y&interval=1d")
    body = _get(url, timeout)
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"not JSON: {body[:70]}")
    res = ((doc.get("chart") or {}).get("result") or [None])[0]
    if not res:
        err = ((doc.get("chart") or {}).get("error") or {})
        raise RuntimeError(str(err)[:90] or "no result")
    stamps = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    out = []
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), float(c)))
    if not out:
        raise RuntimeError("no usable rows")
    return sorted(out)


def from_stooq(sym, timeout=40):
    """Stooq CSV. Free, but blocks many datacentre addresses."""
    body = _get(f"https://stooq.com/q/d/l/?s={sym}&i=d", timeout)
    if not body.strip() or body.strip().lower().startswith("<"):
        raise RuntimeError("empty or non-CSV reply (address likely blocked)")
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows or "Close" not in (rows[0] or {}):
        raise RuntimeError(f"unexpected columns: {list((rows[0] or {}).keys())[:6]}")
    out = []
    for r_ in rows:
        try:
            day = r_["Date"][:10]
            datetime.strptime(day, "%Y-%m-%d")
            out.append((day, float(r_["Close"])))
        except (ValueError, KeyError, TypeError):
            continue
    if not out:
        raise RuntimeError("no usable rows")
    return sorted(out)


PROVIDERS = [("yahoo", from_yahoo, lambda s: s.replace(".us", "").upper()),
             ("stooq", from_stooq, lambda s: s)]


def fetch_csv(symbol, timeout=40):
    """Try each provider in turn. One working source is enough."""
    errors = []
    for name, fn, conv in PROVIDERS:
        try:
            return fn(conv(symbol), timeout)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:60]}")
    raise RuntimeError(" | ".join(errors))


def probe():
    print("\nTesting price data. Each provider in turn.\n")
    working = None
    for name, fn, conv in PROVIDERS:
        try:
            rows = fn(conv("xlu.us"))
            print(f"  ok    {name:<8} {len(rows)} daily closes for XLU")
            working = working or (name, rows)
        except Exception as e:
            print(f"  FAIL  {name:<8} {str(e)[:90]}")
    print()
    try:
        if not working:
            raise RuntimeError("no provider answered")
        rows = working[1]
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
