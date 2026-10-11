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

# The prices table holds two kinds of row since company prices were added:
# one per sector ETF, and one per listed company, both tagged with a sector.
# Reading it by sector alone silently mixed Vistra's share price into the
# Utilities series and whichever row landed last won. Only the ETFs stand for
# a sector, so only the ETFs are read here.
try:
    from prices import SECTOR_ETF
    ETF_SYMBOLS = {v[0] for v in SECTOR_ETF.values()}
except Exception:
    ETF_SYMBOLS = set()


def sector_closes(conn):
    """Daily closes per sector, from the sector ETFs only."""
    daily = defaultdict(dict)
    rows = conn.execute("SELECT sector, symbol, day, close FROM prices")
    for sec, symbol, day, close in rows:
        if ETF_SYMBOLS and symbol not in ETF_SYMBOLS:
            continue
        if not ETF_SYMBOLS and "." not in (symbol or ""):
            continue          # fallback: ETF symbols carry a suffix, tickers do not
        daily[sec][day] = close
    return daily



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

# ---- the coined test, fixed before any result was seen ----------------------
#
# Breadth failed. This asks a different question of the same events: does it
# matter whether the phrase had to be COINED? "grid operator" is an operator,
# of a grid — two ordinary words assembled on the spot, and two industries
# reaching for it means only that both described the same object. "digital
# twin" cannot be built from its parts; somebody had to carry the term across.
#
# The measure is the head word's productivity: how many distinct phrases that
# word appears in anywhere in the corpus. Low means bound, which means coined.
#
# SPLIT: sort the events by head productivity and cut at the halfway point —
# the lower half is coined, the upper half compositional. By rank rather than
# by median VALUE because head counts tie heavily: "energy", "power" and
# "technology" each carry dozens of phrases, so a value cut can land on a tie
# and leave one group empty. A rank cut gives two groups of equal size on any
# distribution, and nobody picks a number that flatters the result.
#
# CORRECTION: this is about the eighth variable tried against this dataset.
# Test enough of them and one passes by luck. The headline band keeps p < 0.05
# so the number is directly comparable with the breadth run above, but the
# claim only survives at the Bonferroni-corrected 0.05 / 8 = 0.00625.
#
# HOLDOUT: everything from 2025Q1 is withheld from the headline and reported
# separately. An effect that appears in the training quarters and vanishes in
# the held-out ones is a fitted artifact, and this is the only way to see that
# without being able to collect more history.
COINED_TESTS_TRIED = 8
COINED_ALPHA       = 0.05 / COINED_TESTS_TRIED
HOLDOUT_FROM       = "2025Q1"
HOLDOUT_MIN        = 40      # below this the held-out half says nothing

COINED_VERDICT = """
  Fixed before any number was seen:

    coined minus compositional, under +2 points  ->  no price information
    +2 to +6 points                              ->  marginal, not a foundation
    above +6 with p under 0.00625                ->  the coined reading survives

  0.00625 rather than 0.05 because this is the eighth variable tried on one
  dataset. The uncorrected p is printed too, so the correction can be judged
  rather than taken on trust. A result that passes in the training quarters
  and fails in the held-out ones has not survived.
"""


def head_productivity(conn):
    """How many distinct phrases each word appears in, from the news grams.

    The same figure collect.py ranks on, read from the same table, so the test
    and the site cannot drift into measuring different things.
    """
    seen = defaultdict(set)
    try:
        for (gram,) in conn.execute("SELECT DISTINCT gram FROM grams"):
            for w in gram.split():
                seen[w].add(gram)
    except sqlite3.OperationalError:
        return {}
    return {w: len(g) for w, g in seen.items()}


def report_coined(measured, prod):
    """Coined against compositional, on the same events breadth was tested on."""
    if not prod:
        print("  No gram vocabulary stored, so head productivity cannot be read.")
        print("  Run the collector once against this database first.\n")
        return

    scored = []
    for e in measured:
        words = e["phrase"].strip('"').split()
        if not words:
            continue
        n = prod.get(words[-1])
        if n:
            scored.append(dict(e, bound=n))
    if len(scored) < MIN_EVENTS:
        print(f"  Only {len(scored)} of {len(measured)} events have a head word in")
        print(f"  the vocabulary, below the {MIN_EVENTS} needed. Not run.\n")
        return

    # rank the phrases, not the events, so a phrase firing in six industries
    # cannot drag the cut toward itself
    phrase_bound = {e["phrase"]: e["bound"] for e in scored}
    ranked = sorted(phrase_bound, key=lambda ph: (phrase_bound[ph], ph))
    half = len(ranked) // 2
    coined_phrases = set(ranked[:half])
    cut = phrase_bound[ranked[half - 1]] if half else 0

    def split(group):
        coined = [e for e in group if e["phrase"] in coined_phrases]
        comp   = [e for e in group if e["phrase"] not in coined_phrases]
        return coined, comp

    def premium(coined, comp):
        if not coined or not comp:
            return None
        rate = lambda g: 100 * sum(1 for e in g if e["ret"] > 0) / len(g)
        rc, rk = rate(coined), rate(comp)
        prem = rc - rk
        both = coined + comp
        pbar = sum(1 for e in both if e["ret"] > 0) / len(both)
        se = math.sqrt(pbar * (1 - pbar) * (1/len(coined) + 1/len(comp))) or 1e-9
        return rc, rk, prem, 1 - norm_cdf((prem/100) / se)

    # Can this test see a difference at all? The price is per sector per
    # quarter, so when a coined phrase and a compositional one both ignite in
    # the same sector-quarter they are handed the identical forward return and
    # no split can separate them. Verified against a planted effect: with the
    # groups in different sectors the test returned +26 points at p<0.0001;
    # with the groups sharing sectors and the SAME effect planted, +0.2 points
    # and a null. So a null here has two readings and the reader needs to know
    # which one is in front of them.
    cells = defaultdict(lambda: [0, 0])
    for e in scored:
        cells[(e["sector"], e["q"])][0 if e["phrase"] in coined_phrases else 1] += 1
    contested = sum(1 for v in cells.values() if v[0] and v[1])
    overlap = contested / len(cells) if cells else 0.0
    print(f"  {len(cells)} sector-quarters hold an ignition; {contested} hold both "
          f"kinds ({overlap:.0%})")
    if overlap >= 0.5:
        print("  ** Over half the events share a sector-quarter with the other group,")
        print("     so both kinds are handed the same forward return. This test has")
        print("     little power to separate them and a null below means 'cannot")
        print("     tell', not 'no effect'. **")
    print()

    train = [e for e in scored if e["q"] < HOLDOUT_FROM]
    held  = [e for e in scored if e["q"] >= HOLDOUT_FROM]

    print(f"  {len(ranked)} phrases ranked by head productivity; the lower half "
          f"is coined\n  (boundary: a head in {cut:.0f} distinct phrases) · "
          f"{len(scored)} events scored\n")

    tc, tk = split(train)
    r = premium(tc, tk)
    if r is None:
        print("  Every training event fell on one side of the split.\n")
        return
    rc, rk, prem, pval = r
    print(f"  TRAINING  {train[0]['q'] if train else '?'} to {HOLDOUT_FROM}"
          f"   events {len(train)}   coined {len(tc)}   compositional {len(tk)}")
    print(f"    coined        followed by a rise : {rc:5.1f}%")
    print(f"    compositional followed by a rise : {rk:5.1f}%")
    print(f"    median forward return, coined        : "
          f"{median([e['ret'] for e in tc])*100:+6.2f}%")
    print(f"    median forward return, compositional : "
          f"{median([e['ret'] for e in tk])*100:+6.2f}%")
    print(f"    premium {prem:+.1f} points   p = {pval:.4f}  "
          f"(corrected threshold {COINED_ALPHA:.5f})\n")

    hc, hk = split(held)
    hr = premium(hc, hk)
    if hr:
        hrc, hrk, hprem, hp = hr
        print(f"  HELD OUT  {HOLDOUT_FROM} onward"
              f"   events {len(held)}   coined {len(hc)}   compositional {len(hk)}")
        print(f"    premium {hprem:+.1f} points   p = {hp:.4f}")
        # a handful of events will swing twenty points on noise alone, in
        # either direction, and reading that as confirmation is the trap this
        # whole script exists to avoid
        if len(held) < HOLDOUT_MIN:
            print(f"    (under {HOLDOUT_MIN} events — not informative either way)")
        print()
    else:
        print(f"  HELD OUT  {len(held)} events, too few or all on one side "
              f"to compare.\n")

    passes = prem >= 6 and pval < COINED_ALPHA
    # three states, not two: the held-out half can confirm, contradict, or be
    # too small to say. Reporting "fails out of sample" when the real answer is
    # "could not be checked" would throw away a result that had not been tested.
    checkable = bool(hr) and len(held) >= HOLDOUT_MIN
    confirms = checkable and hr[2] >= 2

    if passes and not checkable:
        print(f"  PASSES IN TRAINING, UNTESTED OUT OF SAMPLE. Only {len(held)} held-out")
        print(f"  events, under the {HOLDOUT_MIN} this needs, so the second half neither")
        print("  confirms nor contradicts. Treat the result as unverified rather than")
        print("  as a finding: more quarters, or more tracked phrases through the")
        print("  edgar workflow, would settle it.\n")
    elif passes and confirms:
        print("  ABOVE THE LINE, AND IT HELD OUT. Whether a phrase had to be coined")
        print("  carries price information that breadth did not, and the effect")
        print("  survives in quarters the split never saw. This is the first")
        print("  positive result the project has produced. It is quarterly filing")
        print("  language, not a trading rule, and the next step is the same test")
        print("  on the news corpus rather than a position.\n")
    elif passes:
        print("  PASSES IN TRAINING, CONTRADICTED OUT OF SAMPLE. That pattern is what")
        print("  fitting looks like. The honest read is no result.\n")
    elif prem >= 2:
        print("  MARGINAL. Something may be there and it is not a foundation —")
        print("  and at the eighth variable tried, marginal is what chance")
        print("  produces on its own.\n")
    elif overlap >= 0.5:
        print("  NO SEPARATION AVAILABLE. The two groups ignite in the same")
        print("  sector-quarters too often to be told apart by a sector price, so")
        print("  this is not evidence against the coined reading — it is evidence")
        print("  that filings cannot test it. The news corpus can, because a daily")
        print("  series and a company-level price would not collapse the two groups")
        print("  onto one number.\n")
    else:
        print("  BELOW THE LINE. Being coined carries no price information in")
        print("  filings either. That is the third null for the prediction claim,")
        print("  on the measure that fits the idea best. Filings are quarterly and")
        print("  lawyer-written, so this does not formally close the news question,")
        print("  but three nulls is the answer unless something changes.\n")


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
        daily = sector_closes(conn)
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
    # Say which database this is before saying what is wrong with it. A missing
    # table can mean the edgar build never ran, or it ran and this is a copy
    # from before it did — and those need opposite responses.
    import time
    print(f"\n  database  {DB}")
    print(f"            {os.path.getsize(DB)/1e6:.1f} MB · written "
          f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(os.path.getmtime(DB)))}")
    tables = {r[0]: 0 for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    for t in list(tables):
        try:
            tables[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.DatabaseError:
            tables[t] = -1
    print("            " + ", ".join(f"{t} {n:,}" for t, n in tables.items()))

    if "filings" not in tables or not tables["filings"]:
        print("\n  No filing history in THIS copy of the database.")
        if tables.get("articles"):
            print("  The articles are here, so this is the live database and the")
            print("  edgar build either has not run or did not store its result.")
        print("  If the edgar workflow reported rows stored, check that this test")
        print("  did not start while that workflow was still running — both now")
        print("  share a concurrency group, which prevents exactly that.\n")
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

    print("\n" + "=" * 68)
    print("  THE COINED TEST — same events, a different question of them")
    print("=" * 68)
    print(COINED_VERDICT)
    report_coined(measured, head_productivity(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
