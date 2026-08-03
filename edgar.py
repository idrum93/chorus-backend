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

import argparse, json, os, sys, urllib.parse, urllib.request
from datetime import datetime

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-sic", action="store_true")
    ap.add_argument("--phrase", default='"data center"')
    ap.add_argument("--cik", default="0000320193")
    args = ap.parse_args()

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
