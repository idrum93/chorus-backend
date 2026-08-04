#!/usr/bin/env python3
"""
filingtest.py — the cross-industry test, runnable now.

    python3 filingtest.py            run it
    python3 filingtest.py --status   readiness only

WHY THIS ONE DOES NOT HAVE TO WAIT

The news backtest is 130-odd days away because backfilled articles are excluded:
GDELT's index today is not necessarily what it held in 2019, so anything imported
after the fact might carry hindsight.

SEC filings have none of that problem. A filing dated 2019-03-14 was public on
2019-03-14, the index is complete rather than curated, and nothing is added
retroactively. So the same question can be asked over forty-plus quarters today.

THE QUESTION

When a phrase starts appearing unusually often in an industry's filings, and
several industries pick it up at once, does that industry's share price move
afterwards? And specifically — do BROAD spreads beat NARROW ones? Breadth is the
distinctive claim, so breadth is what gets tested.

WHAT A NULL HERE WOULD AND WOULD NOT MEAN

Quarterly disclosure language is not daily trade press, so a null would not
close the news question. A positive result would be strong, because filings are
the harder test: they lag events and are written by lawyers.
"""

import argparse, math, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))
BASELINE = "__all_filings__"

# ---- fixed before any result was seen ---------------------------------------
MIN_EVENTS   = 60      # below this the answer is noise whatever it says
BASE_Q       = 8       # trailing quarters each phrase is measured against
Z_THRESHOLD  = 2.0
MIN_LIFT     = 1.5     # share must be this multiple of its own trailing median
FORWARD_Q    = 2       # how long we wait for the price to respond
BROAD        = 3       # industries lit at once to count as a broad spread

VERDICT = """
  Fixed before any number was seen:

    broad minus narrow, under +2 points  ->  breadth carries no price information
    +2 to +6 points                      ->  marginal, not a foundation
    above +6 with p under 0.05           ->  the distinctive claim survives
"""


def median(a):
    if not a: return 0.0
    s = sorted(a); m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m-1] + s[m]) / 2


def mad(a, med):
    return median([abs(x - med) for x in a])


def norm_cdf(z):
    t = 1 / (1 + 0.2316419 * abs(z)); d = 0.3989423 * math.exp(-z*z/2)
    p = d*t*(0.3193815 + t*(-0.3565638 + t*(1.781478 + t*(-1.821256 + t*1.330274))))
    return 1 - p if z > 0 else p


def qend(q):
    """Last calendar day of a quarter label like 2019Q3."""
    y, n = int(q[:4]), int(q[-1])
    m = n * 3
    d = 31 if m in (3, 12) else 30
    return f"{y}-{m:02d}-{d:02d}"


def load(conn):
    """Phrase share of each industry's filings, by quarter; and quarterly closes."""
    base = {}
    for q, sec, n in conn.execute(
            "SELECT quarter, sector, SUM(n) FROM filings WHERE phrase=? "
            "GROUP BY quarter, sector", (BASELINE,)):
        base[(q, sec)] = n

    shares = defaultdict(dict)     # (phrase, sector) -> quarter -> share
    for phrase, q, sec, n in conn.execute(
            "SELECT phrase, quarter, sector, SUM(n) FROM filings WHERE phrase!=? "
            "GROUP BY phrase, quarter, sector", (BASELINE,)):
        b = base.get((q, sec))
        if b:
            shares[(phrase, sec)][q] = n / b

    prices = defaultdict(dict)     # sector -> quarter -> close nearest quarter end
    try:
        daily = defaultdict(dict)
        for sec, day, close in conn.execute("SELECT sector, day, close FROM prices"):
            daily[sec][day] = close
        quarters = sorted({q for d in shares.values() for q in d})
        for sec, series in daily.items():
            days = sorted(series)
            for q in quarters:
                target = qend(q)
                near = [d for d in days if d <= target]
                if near and (datetime.fromisoformat(target)
                             - datetime.fromisoformat(near[-1])).days <= 12:
                    prices[sec][q] = series[near[-1]]
    except sqlite3.OperationalError:
        pass
    return shares, prices


def ignitions(shares):
    """Quarters where a phrase's share in an industry breaks from its own past."""
    events = []
    for (phrase, sector), series in shares.items():
        qs = sorted(series)
        if len(qs) < BASE_Q + 2:
            continue
        for i in range(BASE_Q, len(qs)):
            window = [series[q] for q in qs[i-BASE_Q:i]]
            med = median(window)
            if med <= 0:
                continue
            scale = 1.4826 * mad(window, med) or med * 0.25
            z = (series[qs[i]] - med) / scale
            lift = series[qs[i]] / med
            if z >= Z_THRESHOLD and lift >= MIN_LIFT:
                events.append({"phrase": phrase, "sector": sector,
                               "q": qs[i], "z": z, "lift": lift})
    return events


def measure(events, prices):
    """Attach breadth and the forward price move to each ignition."""
    by_phrase_q = defaultdict(set)
    for e in events:
        by_phrase_q[(e["phrase"], e["q"])].add(e["sector"])

    out = []
    for e in events:
        e["breadth"] = len(by_phrase_q[(e["phrase"], e["q"])])
        series = prices.get(e["sector"], {})
        qs = sorted(series)
        if e["q"] not in series:
            continue
        i = qs.index(e["q"])
        if i + FORWARD_Q >= len(qs):
            continue
        p0, p1 = series[e["q"]], series[qs[i + FORWARD_Q]]
        if not p0:
            continue
        e["ret"] = (p1 - p0) / p0
        out.append(e)
    return out


def report(measured):
    broad = [e for e in measured if e["breadth"] >= BROAD]
    narrow = [e for e in measured if e["breadth"] < BROAD]
    if not broad or not narrow:
        print("  Every event fell on one side of the breadth split. Nothing to compare.\n")
        return

    rate = lambda g: 100 * sum(1 for e in g if e["ret"] > 0) / len(g)
    rb, rn = rate(broad), rate(narrow)
    prem = rb - rn
    p = sum(1 for e in measured if e["ret"] > 0) / len(measured)
    se = math.sqrt(p * (1-p) * (1/len(broad) + 1/len(narrow))) or 1e-9
    pval = 1 - norm_cdf((prem/100) / se)

    print(f"  events {len(measured)}   broad {len(broad)}   narrow {len(narrow)}\n")
    print(f"  broad  spreads followed by a rise : {rb:5.1f}%")
    print(f"  narrow spreads followed by a rise : {rn:5.1f}%")
    print(f"  median forward return, broad      : {median([e['ret'] for e in broad])*100:+6.2f}%")
    print(f"  median forward return, narrow     : {median([e['ret'] for e in narrow])*100:+6.2f}%")
    print(f"\n  breadth premium: {prem:+.1f} points   p = {pval:.3f}\n")

    if prem >= 6 and pval < 0.05:
        print("  ABOVE THE LINE. Breadth carries price information in filings — the")
        print("  first evidence for the claim the product was built on. Worth testing")
        print("  again on the news corpus when it is deep enough, since a second")
        print("  independent confirmation is what would make this solid.\n")
    elif prem >= 2:
        print("  MARGINAL. Something may be there; it is not a foundation. The honest")
        print("  read is that filings hint at an effect too weak to sell on.\n")
    else:
        print("  BELOW THE LINE. Breadth carries no price information here.")
        print("  Filings are the harder test — quarterly, formal, lawyer-written — so")
        print("  this does not close the news question. But it is the second null in")
        print("  a row for the prediction claim, and the monitoring product stands on")
        print("  its own either way.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("No database yet."); return 1
    conn = sqlite3.connect(DB)
    try:
        conn.execute("SELECT 1 FROM filings LIMIT 1")
    except sqlite3.OperationalError:
        print("\n  No filing history yet. Run the edgar workflow in build mode first.\n")
        return 1

    shares, prices = load(conn)
    phrases = len({p for p, _ in shares})
    quarters = sorted({q for d in shares.values() for q in d})
    priced_q = sorted({q for s in prices.values() for q in s})

    print(f"\n  phrases          {phrases}")
    print(f"  filing quarters  {len(quarters)}"
          + (f"   {quarters[0]} → {quarters[-1]}" if quarters else ""))
    print(f"  priced quarters  {len(priced_q)}"
          + (f"   {priced_q[0]} → {priced_q[-1]}" if priced_q else ""))

    ev = ignitions(shares)
    measured = measure(ev, prices)
    print(f"  ignitions        {len(ev)}")
    print(f"  with a price     {len(measured)}   (need {MIN_EVENTS})\n")

    if args.status:
        return 0
    if len(measured) < MIN_EVENTS:
        print("  NOT ENOUGH EVENTS. More phrases would fix this — the edgar workflow")
        print("  in build mode with a higher phrase limit costs nothing but time.")
        print("  Running early is worse than not running: an underpowered result")
        print("  reads as meaningful and cannot be un-seen.\n")
        print(VERDICT)
        return 0

    print(VERDICT)
    report(measured)
    return 0


if __name__ == "__main__":
    sys.exit(main())
