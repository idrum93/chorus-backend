"""Every projection recorded before the fight, graded after, scored per market.

The gap this closes. The site prints sixteen numbers per bout and only five of
them — winner, two takedowns, two knockdowns — have ever been measured against
reality. The other eleven are displayed with identical visual weight and no
path to earning trust, because nothing wrote down what they said. The odds
ledger records the moneyline rule and nothing else, so a prop projection
vanished the moment the card turned over.

A number that is never scored is an opinion. A number with a public hit rate
is a claim. This turns the first into the second, one card at a time.

Rows are written when predictions are generated (before the event, enforced by
the same date guard as odds capture), graded from the corpus afterwards, and
rolled up into a per-market calibration table the site shows beside each
projection.

Scoring is Brier against the market's own base rate, not accuracy. "Ends in
round 1" is right 80% of the time by always saying no; only calibration
distinguishes a useful projection from a confident one.
"""
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

LEDGER_DIR = "data/ledger"
OUT = "site/scorecard.json"
KEY = ["event_date", "bout", "market"]


def month_file(when=None, directory=LEDGER_DIR):
    ts = pd.Timestamp(when) if when is not None else pd.Timestamp.now(tz="UTC")
    return os.path.join(directory, f"proj-{ts.year:04d}-{ts.month:02d}.jsonl")


def files(directory=LEDGER_DIR):
    import glob
    return sorted(glob.glob(os.path.join(directory, "proj-*.jsonl")))


def markets_from_bout(b):
    """The full set of claims a single bout makes. Keys are stable strings so
    a market's history survives changes to the display."""
    a, bb = b["a"], b["b"]
    m = {
        "winner_a": (b.get("p_a"), f"{a} wins"),
        "inside_distance": (b.get("p_finish"), "ends inside the distance"),
        "decision": (b.get("m_decision"), "goes the distance"),
        "ko_a": (b.get("m_a_ko"), f"{a} by KO/TKO"),
        "ko_b": (b.get("m_b_ko"), f"{bb} by KO/TKO"),
        "sub_a": (b.get("m_a_sub"), f"{a} by submission"),
        "sub_b": (b.get("m_b_sub"), f"{bb} by submission"),
        "takedown_a": (b.get("a_p_takedown"), f"{a} lands a takedown"),
        "takedown_b": (b.get("b_p_takedown"), f"{bb} lands a takedown"),
        "knockdown_a": (b.get("a_p_knockdown"), f"{a} scores a knockdown"),
        "knockdown_b": (b.get("b_p_knockdown"), f"{bb} scores a knockdown"),
    }
    for k in range(1, 6):
        v = b.get(f"p_end_r{k}")
        if v is not None:
            m[f"end_r{k}"] = (v, f"ends in round {k}")
    for k, v in (b.get("totals") or {}).items():
        m[f"total_{k}"] = (v, f"over {k.replace('over_','').replace('_','.')} rounds")
    return {k: v for k, v in m.items() if v[0] is not None}


def log(payload, path=None, verbose=True):
    """Write one row per bout per market, for a card that has not happened."""
    ev_date = pd.to_datetime(payload.get("date"), errors="coerce")
    if pd.isna(ev_date):
        return 0
    if pd.Timestamp.now(tz="UTC").normalize() > ev_date.tz_localize("UTC") + pd.Timedelta(days=1):
        if verbose:
            print("projections: card already happened, nothing logged")
        return 0

    p = path or month_file()
    have = set()
    for f in files():
        for line in open(f, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                have.add(tuple(str(r.get(k)) for k in KEY))

    rows = []
    for b in payload.get("bouts", []):
        for mk, (prob, label) in markets_from_bout(b).items():
            k = (str(payload.get("date")), str(b["bout"]), mk)
            if k in have:
                continue
            rows.append({"logged_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
                         "event": payload.get("event"), "event_date": payload.get("date"),
                         "bout": b["bout"], "a": b["a"], "b": b["b"],
                         "market": mk, "label": label, "p": round(float(prob), 4),
                         "settled": False})
    if rows:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    if verbose:
        print(f"projections: logged {len(rows)} market claims for "
              f"{payload.get('event')}")
    return len(rows)


def _outcome(market, f, a_is_red):
    """Did it happen? f is the corpus row for that fight."""
    dec = f.method == "DEC"
    who_red = f.winner == "r"
    a_won = who_red == a_is_red
    rnd = min(5, int(math.ceil(f.total_sec / 300.0))) if f.total_sec else 1
    a_td = (f.r_td_landed if a_is_red else f.b_td_landed) > 0
    b_td = (f.b_td_landed if a_is_red else f.r_td_landed) > 0
    a_kd = (f.r_kd if a_is_red else f.b_kd) > 0
    b_kd = (f.b_kd if a_is_red else f.r_kd) > 0
    table = {
        "winner_a": a_won,
        "inside_distance": not dec,
        "decision": dec,
        "ko_a": a_won and f.method == "KO/TKO",
        "ko_b": (not a_won) and f.method == "KO/TKO",
        "sub_a": a_won and f.method == "SUB",
        "sub_b": (not a_won) and f.method == "SUB",
        "takedown_a": bool(a_td), "takedown_b": bool(b_td),
        "knockdown_a": bool(a_kd), "knockdown_b": bool(b_kd),
        "total_over_1_5": f.total_sec > 450,
        "total_over_2_5": f.total_sec > 750,
        "total_over_3_5": f.total_sec > 1050,
        "total_over_4_5": f.total_sec > 1350,
    }
    for k in range(1, 6):
        table[f"end_r{k}"] = (not dec) and rnd == k
    return table.get(market)


def settle(fights, verbose=True):
    """Grade every logged claim whose fight is now in the corpus."""
    from .upcoming import _key
    parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    idx = {}
    for i, (r_, b_, dt) in enumerate(zip(parts[0], parts[1], fights.date)):
        idx.setdefault(frozenset((_key(r_), _key(b_))), []).append((dt, _key(r_), i))

    n = 0
    for fp in files():
        rows = [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]
        out = []
        for r in rows:
            r = dict(r)
            cands = idx.get(frozenset((_key(r["a"]), _key(r["b"])))) or []
            ev = pd.to_datetime(r.get("event_date"), errors="coerce")
            best = None
            for dt, red, i in cands:
                if pd.isna(ev):
                    continue
                gap = abs((pd.Timestamp(dt) - ev).days)
                if gap <= 4 and (best is None or gap < best[0]):
                    best = (gap, red, i)
            if best is not None:
                _, red, i = best
                row = fights.iloc[i]
                if row.winner in ("r", "b"):
                    hit = _outcome(r["market"], row, _key(r["a"]) == red)
                    if hit is not None:
                        r["settled"], r["hit"] = True, bool(hit)
                        n += 1
            out.append(r)
        Path(fp).write_text("\n".join(json.dumps(x, sort_keys=True) for x in out) + "\n",
                            encoding="utf-8")
    if verbose:
        print(f"projections: settled {n} market claims")
    return n


def scorecard(path=OUT, verbose=True):
    """Per-market calibration from settled claims. This is the trust engine:
    a market only earns weight on the site once its own record says so."""
    rows = []
    for fp in files():
        rows += [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]
    D = pd.DataFrame(rows)
    out = {"built_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
           "markets": {}, "total_logged": int(len(D)),
           "total_settled": int(D.settled.sum()) if len(D) and "settled" in D else 0}
    if len(D) and "hit" in D.columns:
        S = D[(D.settled == True) & D.hit.notna()]  # noqa: E712
        for mk, g in S.groupby("market"):
            y = g.hit.astype(int).values
            p = g.p.astype(float).values
            base = float(y.mean())
            brier = float(np.mean((p - y) ** 2))
            brier_base = float(np.mean((base - y) ** 2))
            bands = []
            for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
                m = (p >= lo) & (p < hi)
                if m.sum() >= 5:
                    bands.append({"said": f"{int(lo*100)}-{int(hi*100)}%",
                                  "n": int(m.sum()),
                                  "avg_said": round(float(p[m].mean()), 3),
                                  "happened": round(float(y[m].mean()), 3)})
            out["markets"][mk] = {
                "n": int(len(g)), "base_rate": round(base, 4),
                "brier": round(brier, 4), "brier_base": round(brier_base, 4),
                "skill": round(brier_base - brier, 4), "bands": bands,
                "label": str(g.label.iloc[0])}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(out, indent=1), encoding="utf-8")
    if verbose:
        print(f"scorecard: {out['total_settled']} of {out['total_logged']} claims "
              f"settled across {len(out['markets'])} markets -> {path}")
    return out


def fair_odds(p):
    """Decimal and American price implied by a probability, so a reader can
    put the projection next to a book's number without doing arithmetic."""
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    dec = 1.0 / p
    us = -100 * p / (1 - p) if p >= 0.5 else 100 * (1 - p) / p
    return round(dec, 2), int(round(us))
