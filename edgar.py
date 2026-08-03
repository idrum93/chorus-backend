#!/usr/bin/env python3
"""
edgar.py — SEC filings as a sector-tagged language corpus for Crosstalk.

WHY THIS MIGHT MATTER MORE THAN NEWS

Every SEC filer carries an SEC-assigned SIC industry code, and full-text search
reaches back to 2001. That is the Crosstalk premise in a purer form: which
industries used a phrase, when, with the sector label assigned by the regulator
rather than inferred from which publication ran a story. If it works, years of
history arrive at once and the prediction test stops being six months away.

WHAT THIS FILE IS FOR TODAY

Only to find out what the API actually returns. Four integrations in the last
day failed because the response shape was assumed rather than checked — GDELT's
query syntax, Stooq's blocking, Yahoo's rate limit, Alpha Vantage's notes. This
script reports structure instead of guessing at it.

    python3 edgar.py --probe        one search, print exactly what came back
    python3 edgar.py --probe-sic    one company lookup, find the industry code

The SEC asks that automated requests identify a contact address. Set it:

    EDGAR_UA="crosstalk-research you@crosstalkwire.com"
"""

import argparse, json, os, sqlite3, sys, time, urllib.parse, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UA = os.environ.get("EDGAR_UA", "").strip()
FTS = "https://efts.sec.gov/LATEST/search-index"
SUB = "https://data.sec.gov/submissions/CIK{cik}.json"


def get(url, timeout=40):
    if not UA:
        raise RuntimeError(
            "EDGAR_UA not set. The SEC asks automated clients to identify "
            "themselves, e.g. EDGAR_UA=\"crosstalk-research you@crosstalkwire.com\"")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def describe(obj, path="", depth=0, out=None):
    """Print the structure of whatever came back, a few levels deep."""
    out = out if out is not None else []
    pad = "  " * (depth + 1)
    if depth > 3:
        return out
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:12]:
            kind = type(v).__name__
            if isinstance(v, (dict, list)):
                n = len(v)
                out.append(f"{pad}{k}  ({kind}, {n})")
                describe(v, f"{path}.{k}", depth + 1, out)
            else:
                s = str(v)
                out.append(f"{pad}{k} = {s[:70]}")
    elif isinstance(obj, list) and obj:
        out.append(f"{pad}[0] of {len(obj)}:")
        describe(obj[0], path + "[0]", depth + 1, out)
    return out


def probe_search(phrase='"data center"', start="2024-01-01", end="2024-12-31"):
    params = {"q": phrase, "dateRange": "custom", "startdt": start, "enddt": end}
    url = FTS + "?" + urllib.parse.urlencode(params)
    print(f"\n  GET {url}\n")
    body = get(url)
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        print(f"  Not JSON. First 400 characters:\n\n{body[:400]}\n")
        return None

    print("  Response structure:")
    for line in describe(doc):
        print(line)

    # try the shape the SEC's own search UI uses, without depending on it
    hits = (doc.get("hits") or {}).get("hits")
    total = ((doc.get("hits") or {}).get("total") or {}).get("value")
    if hits:
        print(f"\n  Interpreted: {total} total filings, {len(hits)} returned.")
        src = hits[0].get("_source", {})
        print("  First hit fields:")
        for k, v in list(src.items())[:14]:
            print(f"      {k} = {str(v)[:64]}")
        ciks = src.get("ciks") or []
        if ciks:
            print(f"\n  A CIK to test the industry lookup with: {ciks[0]}")
    else:
        print("\n  Could not find a hits list. The structure above is what to go on.")
    return doc


def probe_sic(cik="0000320193"):
    cik = str(cik).lstrip("CIK").zfill(10)
    url = SUB.format(cik=cik)
    print(f"\n  GET {url}\n")
    body = get(url)
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        print(f"  Not JSON. First 300 characters:\n\n{body[:300]}\n")
        return None
    for k in ("name", "sic", "sicDescription", "tickers", "exchanges"):
        if k in doc:
            print(f"      {k} = {str(doc[k])[:70]}")
    if "sic" in doc:
        print("\n  Industry codes are available. A CIK to SIC to sector mapping"
              "\n  is straightforward from here, and cacheable — a filer's code"
              "\n  rarely changes.")
    return doc




# ============================================================================
# Adapter, written against the structure the probe reported.
#
# The find: full-text search returns an `aggregations.sic_filter.buckets` list —
# which industries filed documents containing the phrase, counted, in the same
# response. One request per phrase per quarter yields a sector-tagged time
# series. Twenty years of it exists.
#
# Filings are not news. They are quarterly, formal, and lag events. But the
# industry label comes from the SEC rather than from which publication ran a
# story, and the history goes back to 2001 — which is the constraint the news
# corpus cannot solve for another six months.
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings(
  quarter TEXT, phrase TEXT, sic TEXT, sector TEXT, n INTEGER,
  PRIMARY KEY(quarter, phrase, sic)
);
CREATE INDEX IF NOT EXISTS idx_filings_phrase ON filings(phrase, quarter);
"""

# SIC ranges to Crosstalk sectors. Coarse on purpose: the point is comparability
# with the news sectors, not precision about any single filer.
SIC_RANGES = [
    ((1000, 1099), "Materials"), ((1200, 1299), "Materials"),
    ((1400, 1499), "Materials"), ((2800, 2829), "Materials"),
    ((2840, 2899), "Materials"), ((3300, 3399), "Materials"),
    ((1300, 1399), "Energy"),    ((2900, 2999), "Energy"),
    ((4600, 4699), "Energy"),
    ((4900, 4999), "Utilities"),
    ((3570, 3579), "Technology"), ((3600, 3699), "Technology"),
    ((7370, 7379), "Technology"), ((4800, 4899), "Technology"),
    ((3500, 3569), "Industrials"), ((3580, 3599), "Industrials"),
    ((3400, 3499), "Industrials"), ((1600, 1799), "Industrials"),
    ((3700, 3710), "Industrials"),
    ((3711, 3799), "Autos"), ((5500, 5599), "Autos"),
    ((4000, 4599), "Supply chain"), ((4700, 4799), "Supply chain"),
    ((2830, 2836), "Healthcare"), ((3840, 3899), "Healthcare"),
    ((8000, 8099), "Healthcare"),
    ((5200, 5499), "Consumer"), ((5600, 5999), "Consumer"),
    ((2000, 2199), "Consumer"), ((7000, 7099), "Consumer"),
    ((6000, 6499), "Finance"), ((6700, 6799), "Finance"),
    ((6500, 6599), "Real estate"),
    ((9100, 9199), "Policy"),      # public administration and finance
    # 9900+ is "non-classifiable establishments" - deliberately unmapped, since
    # filing it anywhere would attribute activity to an industry that does not
    # exist. Better a gap than a wrong label.
]


def sic_sector(sic):
    try:
        code = int(str(sic)[:4])
    except (TypeError, ValueError):
        return None
    for (lo, hi), sector in SIC_RANGES:
        if lo <= code <= hi:
            return sector
    return None


def quarters(from_year, to_year=None):
    to_year = to_year or datetime.now().year
    out = []
    for y in range(from_year, to_year + 1):
        for q, (s, e) in enumerate([("01-01", "03-31"), ("04-01", "06-30"),
                                    ("07-01", "09-30"), ("10-01", "12-31")], 1):
            if datetime.strptime(f"{y}-{s}", "%Y-%m-%d") <= datetime.now():
                out.append((f"{y}Q{q}", f"{y}-{s}", f"{y}-{e}"))
    return out


def search_quarter(phrase, start, end):
    """Total filings and the industry breakdown, in one request."""
    params = {"q": phrase, "dateRange": "custom", "startdt": start, "enddt": end}
    doc = json.loads(get(FTS + "?" + urllib.parse.urlencode(params)))
    hits = doc.get("hits") or {}
    total = (hits.get("total") or {}).get("value", 0)
    capped = (hits.get("total") or {}).get("relation") == "gte"
    buckets = (((doc.get("aggregations") or {}).get("sic_filter") or {})
               .get("buckets") or [])
    by_sic = [(str(b.get("key")), b.get("doc_count", 0)) for b in buckets]
    return total, capped, by_sic


BASELINE = "__all_filings__"


def build_baseline(conn, qs, sleep=0.15, verbose=True):
    """How many filings each industry makes per quarter, regardless of subject.

    Without this, every phrase looks like a finance phrase — there are simply
    more financial filers than industrial ones. The same trap as counting raw
    articles per sector instead of the share of that sector's coverage.

    A ubiquitous phrase stands in for "all filings"; the aggregation buckets are
    what matter, not the total.
    """
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT quarter FROM filings WHERE phrase=?", (BASELINE,))}
    todo = [q for q in qs if q[0] not in have]
    if not todo:
        return 0
    if verbose:
        print(f"  baseline: {len(todo)} quarters of total filings per industry")
    n = 0
    for label, start, end in todo:
        try:
            _t, _c, by_sic = search_quarter('"the Company"', start, end)
        except Exception:
            time.sleep(2)
            continue
        for sic, cnt in by_sic:
            sector = sic_sector(sic)
            if not sector:
                continue
            conn.execute("INSERT INTO filings VALUES(?,?,?,?,?) "
                         "ON CONFLICT(quarter,phrase,sic) DO UPDATE SET n=?",
                         (label, BASELINE, sic, sector, cnt, cnt))
            n += 1
        time.sleep(sleep)
    conn.commit()
    return n


def shares(conn, phrase):
    """Phrase filings as a share of that industry's filings, by quarter.

    This is the number worth reading. Raw counts say Finance leads everything;
    shares say which industries are unusually preoccupied with a phrase.
    """
    base = {}
    for q, sector, n in conn.execute(
            "SELECT quarter, sector, SUM(n) FROM filings WHERE phrase=? "
            "GROUP BY quarter, sector", (BASELINE,)):
        base[(q, sector)] = n
    out = []
    for q, sector, n in conn.execute(
            "SELECT quarter, sector, SUM(n) FROM filings WHERE phrase=? "
            "GROUP BY quarter, sector ORDER BY quarter", (phrase,)):
        b = base.get((q, sector))
        if b:
            out.append({"quarter": q, "sector": sector, "n": n,
                        "share": round(n / b, 5)})
    return out


def build(db, phrases, from_year=2016, sleep=0.15, verbose=True):
    """One request per phrase per quarter. The SEC allows ten a second; this
    stays well under, because a rate-limit block costs far more than patience."""
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    qs = quarters(from_year)
    build_baseline(conn, qs, sleep, verbose)
    if verbose:
        print(f"  {len(phrases)} phrases × {len(qs)} quarters "
              f"= {len(phrases)*len(qs)} requests, roughly "
              f"{len(phrases)*len(qs)*sleep/60:.0f} minutes\n")
    stored = 0
    for phrase in phrases:
        q = phrase if phrase.startswith('"') else f'"{phrase}"'
        hits_here = 0
        for label, start, end in qs:
            try:
                total, capped, by_sic = search_quarter(q, start, end)
            except Exception as e:
                if verbose:
                    print(f"    {phrase[:24]:<26} {label}  {str(e)[:50]}")
                time.sleep(2)
                continue
            for sic, n in by_sic:
                sector = sic_sector(sic)
                if not sector:
                    continue
                conn.execute(
                    "INSERT INTO filings VALUES(?,?,?,?,?) "
                    "ON CONFLICT(quarter,phrase,sic) DO UPDATE SET n=?",
                    (label, phrase, sic, sector, n, n))
                stored += 1
                hits_here += n
            time.sleep(sleep)
        conn.commit()
        if verbose:
            print(f"  {phrase[:30]:<32} {hits_here:>7,} filings across {len(qs)} quarters")
    conn.close()
    return stored


def phrases_from_data(path, limit=20):
    """Use whatever the news collector is already tracking."""
    if not os.path.exists(path):
        return []
    d = json.load(open(path, encoding="utf-8"))
    return [t["term"] for t in d.get("terms", [])[:limit]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-sic", action="store_true")
    ap.add_argument("--phrase", default='"data center"')
    ap.add_argument("--cik", default="0000320193")
    ap.add_argument("--build", action="store_true",
                    help="fetch the industry breakdown for tracked phrases")
    ap.add_argument("--from-year", type=int, default=2016)
    ap.add_argument("--limit", type=int, default=20, help="how many phrases")
    args = ap.parse_args()

    if args.build:
        db = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))
        data = os.path.join(HERE, "data.json")
        phrases = phrases_from_data(data, args.limit)
        if not phrases:
            print("  No data.json yet — run the collector first.")
            return 1
        print(f"\n  Building filing history for {len(phrases)} phrases "
              f"from {args.from_year}\n")
        n = build(db, phrases, args.from_year)
        conn = sqlite3.connect(db)
        print(f"\n  {n:,} phrase-industry-quarter rows stored\n")
        print("  Share of each industry's filings that mention the phrase.")
        print("  Raw counts would just rank industries by how many companies file.\n")
        for phrase in phrases[:6]:
            rows = shares(conn, phrase)
            if not rows:
                continue
            recent = [r for r in rows if r["quarter"] >= sorted(
                {x["quarter"] for x in rows})[-4:][0]]
            agg = {}
            for r in recent:
                agg.setdefault(r["sector"], []).append(r["share"])
            top = sorted(((s, sum(v)/len(v)) for s, v in agg.items()),
                         key=lambda x: -x[1])[:5]
            print(f"  {phrase}")
            for sector, sh in top:
                bar = "#" * max(1, int(sh * 120))
                print(f"      {sector:<14}{sh*100:6.2f}%  {bar}")
            print()
        return 0

    if not (args.probe or args.probe_sic):
        print(__doc__)
        return 0

    print("\n" + "=" * 64)
    print("  EDGAR structure probe — reporting what the API returns,")
    print("  so the parser can be written against fact rather than guesswork.")
    print("=" * 64)

    ok = True
    if args.probe:
        print("\n--- full-text search " + "-" * 42)
        try:
            probe_search(args.phrase)
        except Exception as e:
            ok = False
            print(f"  FAILED — {str(e)[:200]}")
            print("\n  A 403 usually means the User-Agent was rejected; set EDGAR_UA")
            print("  to something identifying with a contact address. A 429 means")
            print("  the shared address is rate limited, as with GDELT and Yahoo.")

    if args.probe_sic:
        print("\n--- industry code lookup " + "-" * 38)
        try:
            probe_sic(args.cik)
        except Exception as e:
            ok = False
            print(f"  FAILED — {str(e)[:200]}")

    print("\n" + "=" * 64)
    print("  Send this output back and the adapter gets written against the")
    print("  real structure. Nothing else in Crosstalk depends on this file.")
    print("=" * 64 + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
