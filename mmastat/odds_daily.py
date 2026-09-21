"""The jerzyszocik daily-snapshot odds feed.

This file is two datasets in one, and treating it as one thing produces
nonsense. Splitting them is the first thing this module does.

REGIME A  source == "zewnetrzne", 2010-2024, 6,299 fights, one row each.
          Carries the UFCStats fight-details hash (100%), so it joins to the
          corpus directly with no name matching, and 99% of those hashes are
          present in our corpus. 4,867 fights carry a COMPLETE six-leg
          method-of-victory market (each fighter by KO / submission /
          decision), which is the only prop-price source found anywhere free.
          It is a single price of unknown timing — all rows were stamped in one
          bulk import on 2025-07-27 — so it cannot be called a closing line.

REGIME B  37 live bookmakers, 2025-03-08 onward, 1,778 bouts, ~202k rows.
          Real repeated snapshots: 79% of bouts have 2+ and 41% have 5+, with a
          median of 7 books quoting each bout. No fight hash after 2025 (0% in
          2026), so it joins by normalised name plus event date, which reaches
          64% of corpus fights.

TIMING, which decides what this feed may be used for. The last snapshot before
an event lands a median of 27 hours out, with the 25th percentile at 7 hours.
That is a Friday price for a Saturday card, NOT a closing line. A pre-close
price is a less efficient market, so scoring a model against it flatters the
model — the bias runs toward whatever you are hoping to prove. Use it for
description and for line-movement work; do not substitute it for the
prospective capture the pre-registration requires.

Regime B also contains non-UFC promotions (the feed is "UFC/MMA" and the
source is Polish, so KSW bouts appear). Joining to the corpus filters them.
"""
import re
import unicodedata

import numpy as np
import pandas as pd

PROP_COLS = ["f1_ko_odds", "f2_ko_odds", "f1_sub_odds",
             "f2_sub_odds", "f1_dec_odds", "f2_dec_odds"]
BACKFILL_SOURCE = "zewnetrzne"


def _nm(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def load_raw(path):
    d = pd.read_csv(path, low_memory=False)
    d["event_date"] = pd.to_datetime(d.event_date, errors="coerce")
    d["snap"] = pd.to_datetime(d.adding_date, errors="coerce", utc=True,
                               format="mixed")
    return d


def moneyline_probs(d, fights, prefer="best"):
    """Devigged P(red corner wins) joined onto `fights`.

    prefer="best" takes the best available price across books, which is what a
    bettor actually gets and is worth more than most model edges; "median"
    takes the consensus. They are different quantities — do not mix them within
    one analysis.
    """
    B = d[(d.source != BACKFILL_SOURCE) & d.odds_1.notna() & d.odds_2.notna()].copy()
    B["k1"], B["k2"] = B.fighter_1.map(_nm), B.fighter_2.map(_nm)
    B["dk"] = B.event_date.dt.strftime("%Y-%m-%d")

    agg = "max" if prefer == "best" else "median"
    # keep only the last snapshot per book per bout before aggregating across books
    B = B.sort_values("snap").groupby(["dk", "k1", "k2", "source"], as_index=False).last()
    g = B.groupby(["dk", "k1", "k2"]).agg(o1=("odds_1", agg), o2=("odds_2", agg),
                                          books=("source", "nunique"),
                                          last_snap=("snap", "max")).reset_index()
    book = {}
    for r in g.itertuples():
        p1 = 1.0 / r.o1
        p2 = 1.0 / r.o2
        book[(r.dk, frozenset((r.k1, r.k2)))] = (r.k1, p1 / (p1 + p2), r.books,
                                                 r.last_snap)

    parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    rk, bk = parts[0].str.strip().map(_nm), parts[1].str.strip().map(_nm)
    dk = fights.date.dt.strftime("%Y-%m-%d")
    p, nb, ls = [], [], []
    for d_, a, b in zip(dk, rk, bk):
        hit = book.get((d_, frozenset((a, b))))
        if hit is None:
            p.append(np.nan); nb.append(np.nan); ls.append(pd.NaT)
        else:
            src, pr, n, snap = hit
            p.append(pr if src == a else 1 - pr)
            nb.append(n); ls.append(snap)
    out = fights.copy()
    out["p_daily"] = p
    out["n_books"] = nb
    out["last_snap"] = ls
    # hours between the final snapshot and roughly the event's bell
    out["snap_lead_h"] = ((out.date.dt.tz_localize("UTC") + pd.Timedelta(hours=23)
                           - out.last_snap).dt.total_seconds() / 3600)
    return out


def method_market(d, min_legs=6):
    """Fight-level devigged P(KO/TKO), P(submission), P(decision).

    The six legs are mutually exclusive and exhaustive, so they devig together.
    Note the overround: a median of 22.3% across the six, versus 3.7% on the
    moneyline. Method markets are roughly six times more expensive to bet, which
    matters more than any modelling edge you are likely to find in them.
    """
    A = d[d.source == BACKFILL_SOURCE].dropna(subset=PROP_COLS).copy()
    A = A[(A[PROP_COLS] > 1.0).all(axis=1)]
    A["fight_id"] = A.fight_url.str.rsplit("/", n=1).str[-1]
    imp = 1.0 / A[PROP_COLS]
    tot = imp.sum(axis=1)
    A["overround"] = tot - 1.0
    q = imp.div(tot, axis=0)
    A["mk_ko"] = q.f1_ko_odds + q.f2_ko_odds
    A["mk_sub"] = q.f1_sub_odds + q.f2_sub_odds
    A["mk_dec"] = q.f1_dec_odds + q.f2_dec_odds
    return A.set_index("fight_id")[["mk_ko", "mk_sub", "mk_dec", "overround",
                                    "event_date"]]


def line_movement(d, min_snaps=2):
    """Per-bout drift between the first and last snapshot, in probability
    points. Repeated snapshots are the one thing this feed has that no other
    free source does."""
    B = d[(d.source != BACKFILL_SOURCE) & d.odds_1.notna()
          & d.odds_2.notna() & d.snap.notna()].copy()
    B["k1"], B["k2"] = B.fighter_1.map(_nm), B.fighter_2.map(_nm)
    B["dk"] = B.event_date.dt.strftime("%Y-%m-%d")
    B["p1"] = (1 / B.odds_1) / (1 / B.odds_1 + 1 / B.odds_2)
    g = B.sort_values("snap").groupby(["dk", "k1", "k2"])
    out = g.agg(first_p=("p1", "first"), last_p=("p1", "last"),
                n_snap=("snap", "nunique"), books=("source", "nunique"),
                first_snap=("snap", "first"), last_snap=("snap", "last"))
    out = out[out.n_snap >= min_snaps]
    out["drift"] = out.last_p - out.first_p
    out["span_d"] = (out.last_snap - out.first_snap).dt.total_seconds() / 86400
    return out.reset_index()
