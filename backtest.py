#!/usr/bin/env python3
"""
backtest.py — Crosstalk's prediction test, written in advance and gated.

Asks the one question the whole project rests on: when a phrase spreads across
sectors, does the sector's price move afterwards? And specifically, do BROAD
ignitions beat NARROW ones — because breadth is the distinctive claim.

    python3 backtest.py            check readiness, run if there is enough data
    python3 backtest.py --status   readiness only, never runs the test
    python3 backtest.py --force    run anyway (the result will not be readable)

WHY THIS EXISTS BEFORE THE DATA DOES

Written now so there is no setup delay later, and so the thresholds are fixed
before anyone has seen a number. Ten rounds of testing on public-attention data
produced two clean nulls; the version that matters — trade-press language against
prices — has never been testable. It will be, in roughly six months.

The gate is the point. An underpowered version of this test produces a figure
that looks meaningful, is driven by a handful of events, and can be read whichever
way the reader prefers. That already happened once, in an earlier round, and it
took a day to unpick. The script refuses rather than repeat it.
"""

import argparse, json, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))

# ---- pre-registered, before any result has been seen -----------------------
MIN_HISTORY_DAYS = 180     # a baseline needs this much behind it
MIN_EVENTS       = 100     # below this the answer is noise, whatever it says
BASE_DAYS        = 120     # trailing window each phrase is measured against
GAP_DAYS         = 7
SMOOTH_DAYS      = 7
REFRACTORY       = 30      # one story is not four ignitions
FORWARD_DAYS     = 60      # how long we wait for the price to respond
Z_THRESHOLD      = 3.0
MIN_LIFT         = 1.6
BROAD_SECTORS    = 3       # "broad" means at least this many sectors carry it

VERDICT = """
  Pre-registered reading, fixed before any number was seen:

    broad minus narrow, under +2 points   ->  breadth carries no price information
    +2 to +5 points                       ->  marginal, not a foundation
    above +5 points with p under 0.05      ->  the distinctive claim survives
"""

median = lambda a: (sorted(a)[len(a)//2] if len(a) % 2
                    else (sorted(a)[len(a)//2-1] + sorted(a)[len(a)//2]) / 2) if a else 0.0


def mad(a, med):
    return median([abs(x - med) for x in a])


def norm_cdf(z):
    import math
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p


def load(conn):
    """Daily article share per phrase per sector, and daily sector closes.

    Backdated articles are excluded outright. They were imported after the fact,
    so including them would let the test see coverage that did not exist on the
    day it is measuring — the classic way a backtest lies to you.
    """
    live_only = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE backfilled=0").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    if total and live_only < total:
        print(f"  excluding {total - live_only:,} backdated articles "
              f"({live_only:,} usable)")
    grams = defaultdict(lambda: defaultdict(dict))     # phrase -> sector -> day -> share
    totals = {}
    for day, sector, n in conn.execute("SELECT day, sector, articles FROM totals"):
        totals[(day, sector)] = n
    for day, sector, gram, arts in conn.execute(
            "SELECT day, sector, gram, articles FROM grams"):
        tot = totals.get((day, sector), 0)
        if tot:
            grams[gram][sector][day] = arts / tot
    prices = defaultdict(dict)
    try:
        for sector, day, close in conn.execute("SELECT sector, day, close FROM prices"):
            prices[sector][day] = close
    except sqlite3.OperationalError:
        pass
    return grams, prices


def readiness(conn, grams, prices):
    days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM totals ORDER BY day")]
    span = 0
    if days:
        span = (datetime.fromisoformat(days[-1]).date()
                - datetime.fromisoformat(days[0]).date()).days
    arts = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"days": span, "articles": arts, "phrases": len(grams),
            "priced_sectors": len(prices)}


def ignitions(grams, prices):
    """Every phrase-sector ignition with a forward price outcome."""
    events = []
    for gram, secs in grams.items():
        all_days = sorted({d for s in secs.values() for d in s})
        if len(all_days) < BASE_DAYS + GAP_DAYS + FORWARD_DAYS:
            continue
        breadth_by_day = {d: sum(1 for s in secs.values() if s.get(d, 0) > 0)
                          for d in all_days}
        for sector, series in secs.items():
            if sector not in prices:
                continue
            vals = [series.get(d, 0.0) for d in all_days]
            sm = [sum(vals[max(0, i-SMOOTH_DAYS+1):i+1]) /
                  len(vals[max(0, i-SMOOTH_DAYS+1):i+1]) for i in range(len(vals))]
            last = -10**9
            for i in range(BASE_DAYS + GAP_DAYS, len(sm) - FORWARD_DAYS):
                if i - last < REFRACTORY:
                    continue
                w = sm[i-GAP_DAYS-BASE_DAYS:i-GAP_DAYS]
                med = median(w)
                if med <= 0:
                    continue
                scale = 1.4826 * mad(w, med) or med * 0.05
                z, lift = (sm[i] - med) / scale, sm[i] / med
                if z < Z_THRESHOLD or lift < MIN_LIFT:
                    continue
                d0 = all_days[i]
                d1 = all_days[min(i + FORWARD_DAYS, len(all_days) - 1)]
                p0 = prices[sector].get(d0) or _nearest(prices[sector], d0)
                p1 = prices[sector].get(d1) or _nearest(prices[sector], d1)
                if not p0 or not p1:
                    continue
                events.append({"gram": gram, "sector": sector, "day": d0,
                               "breadth": breadth_by_day.get(d0, 1),
                               "ret": (p1 - p0) / p0})
                last = i
    return events


def _nearest(series, day, window=6):
    d = datetime.fromisoformat(day).date()
    for k in range(1, window + 1):
        for cand in ((d - timedelta(days=k)).isoformat(),
                     (d + timedelta(days=k)).isoformat()):
            if cand in series:
                return series[cand]
    return None


def report(events):
    broad = [e for e in events if e["breadth"] >= BROAD_SECTORS]
    narrow = [e for e in events if e["breadth"] < BROAD_SECTORS]
    if not broad or not narrow:
        print("  All events fell on one side of the breadth split. Nothing to compare.\n")
        return

    up = lambda g: 100 * sum(1 for e in g if e["ret"] > 0) / len(g)
    rb, rn = up(broad), up(narrow)
    prem = rb - rn
    p = (sum(1 for e in events if e["ret"] > 0)) / len(events)
    import math
    se = math.sqrt(p * (1 - p) * (1/len(broad) + 1/len(narrow))) or 1e-9
    pval = 1 - norm_cdf((prem / 100) / se)

    print(f"  events {len(events)}   broad {len(broad)}   narrow {len(narrow)}\n")
    print(f"  broad  ignitions followed by a rise : {rb:.1f}%")
    print(f"  narrow ignitions followed by a rise : {rn:.1f}%")
    print(f"  median forward return, broad        : {median([e['ret'] for e in broad])*100:+.2f}%")
    print(f"  median forward return, narrow       : {median([e['ret'] for e in narrow])*100:+.2f}%")
    print(f"\n  breadth premium: {prem:+.1f} points   p = {pval:.3f}\n")

    if prem >= 5 and pval < 0.05:
        print("  ABOVE THE LINE. Breadth carries price information. This is the first")
        print("  evidence for the claim the product was built on.\n")
    elif prem >= 2:
        print("  MARGINAL. Something may be there; it is not a foundation. Treat as")
        print("  unproven and keep collecting.\n")
    else:
        print("  BELOW THE LINE. Breadth carries no price information on this data.")
        print("  That is a real finding: the monitoring product stands on its own,")
        print("  and the prediction claim should not be revived without new evidence.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("No database yet."); return 1
    conn = sqlite3.connect(DB)
    grams, prices = load(conn)
    r = readiness(conn, grams, prices)

    print(f"\n  history        {r['days']} days   (need {MIN_HISTORY_DAYS})")
    print(f"  articles       {r['articles']}")
    print(f"  phrases        {r['phrases']}")
    print(f"  priced sectors {r['priced_sectors']}")

    ready_days = r["days"] >= MIN_HISTORY_DAYS
    events = ignitions(grams, prices) if (ready_days or args.force) else []
    print(f"  ignitions      {len(events)}   (need {MIN_EVENTS})\n")

    if args.status:
        return 0

    if not (ready_days and len(events) >= MIN_EVENTS) and not args.force:
        short = max(0, MIN_HISTORY_DAYS - r["days"])
        print(f"  NOT READY. About {short} more days of collection needed before the")
        print("  baseline exists, then enough ignitions on top of that.\n")
        print("  Running early is worse than not running: an underpowered result reads")
        print("  as meaningful and is impossible to un-see. Left alone, the collector")
        print("  reaches this threshold on its own.\n")
        print(VERDICT)
        return 0

    print(VERDICT)
    report(events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
