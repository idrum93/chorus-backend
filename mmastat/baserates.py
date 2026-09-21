"""Conditional base rates: what happened in past fights that looked like this.

Not a model. Counts. For each registered condition the corpus is split into
quartiles and the outcome rate is reported with n and a 95% interval.

The condition list is FIXED in PREREGISTRATION.md addendum 10 and every entry
is rendered every time, including the ones that come out flat. The risk this
guards against is not false discovery, it is selective display: with a dozen
candidate conditions, showing whichever looks strongest turns a lookup table
into a machine for rendering noise attractively. C5 (combined control share
against inside-the-distance) measured flat and stays in the panel for exactly
that reason — a display that only ever shows strong relationships is
indistinguishable from one that selects them.

Two granularities, because the outcomes live at different levels:
  fighter  one row per fighter per fight  (did HE land a takedown)
  fight    one row per fight              (did IT end inside the distance)
"""
import numpy as np
import pandas as pd

from .features import build_panel

MIN_CELL = 200          # quartiles thinner than this are greyed, never dropped

# (id, label, level, condition column, outcome column, why it is here)
CONDITIONS = [
    ("C1", "Own takedown rate", "fighter", "own_adj_td15", "y_td",
     "the rate is the propensity"),
    ("C2", "Opponent's takedown defence", "fighter", "opp_td_def", "y_td",
     "the thing standing in the way"),
    ("C3", "Own knockdown rate", "fighter", "own_kd15", "y_kd",
     "direct"),
    ("C4", "Combined knockdown rate", "fight", "kd_sum", "itd",
     "two heavy hitters end fights"),
    ("C5", "Combined control share", "fight", "ctrl_sum", "itd",
     "grappling-heavy fights grind out"),
    ("C6", "Reach advantage", "fighter", "reach_diff", "y_td",
     "shooting under a longer fighter"),
    ("C7", "Combined strike volume", "fight", "slpm_sum", "itd",
     "pace as a proxy for damage"),
    ("C8", "Age gap", "fight", "age_gap", "itd",
     "the older fighter as the one who breaks"),
    ("C9", "Combined submission-attempt rate", "fight", "sub_sum", "dec",
     "grapplers hunting finishes"),
    ("C10", "Own clinch + ground share", "fighter", "pos_share", "y_td",
     "position-dependent offense needs the takedown first"),
]

OUTCOME_LABEL = {"y_td": "lands a takedown", "y_kd": "scores a knockdown",
                 "itd": "ends inside the distance", "dec": "goes the distance"}


def _frames(fights, fighters, min_date="2012-01-01"):
    P = build_panel(fights, fighters)
    P = P[P.date >= pd.Timestamp(min_date)].copy()
    P["y_td"] = (P.y_td15 > 0).astype(int)
    P["y_kd"] = (P.y_kd15 > 0).astype(int)
    P["reach_diff"] = P.own_reach - P.opp_reach
    P["pos_share"] = P.own_clinch_share + P.own_ground_share

    P["side"] = P.groupby("fight_id").cumcount()
    w = P.pivot_table(index="fight_id", columns="side",
                      values=["own_kd15", "own_ctrl_share", "own_adj_slpm",
                              "own_sub15", "own_age"])
    w.columns = [f"{a}{b}" for a, b in w.columns]
    F = fights.set_index("fight_id")
    F = F[F.date >= pd.Timestamp(min_date)].join(w, how="inner")
    F["itd"] = (F.method != "DEC").astype(int)
    F["dec"] = (F.method == "DEC").astype(int)
    F["kd_sum"] = F.own_kd150 + F.own_kd151
    F["ctrl_sum"] = F.own_ctrl_share0 + F.own_ctrl_share1
    F["slpm_sum"] = F.own_adj_slpm0 + F.own_adj_slpm1
    F["sub_sum"] = F.own_sub150 + F.own_sub151
    F["age_gap"] = (F.own_age0 - F.own_age1).abs()
    return P, F


def build_table(fights, fighters, min_date="2012-01-01", n_q=4):
    """Every registered condition, every quartile, always."""
    P, F = _frames(fights, fighters, min_date)
    out = []
    for cid, label, level, col, outcome, why in CONDITIONS:
        d = P if level == "fighter" else F
        if col not in d.columns or outcome not in d.columns:
            continue
        s = d[col].astype(float)
        try:
            q = pd.qcut(s, n_q, duplicates="drop", labels=False)
        except ValueError:
            continue
        base = float(d[outcome].mean())
        edges = [float(x) for x in np.nanquantile(s, np.linspace(0, 1, n_q + 1))]
        cells = []
        for i in range(int(np.nanmax(q)) + 1):
            m = q == i
            n = int(m.sum())
            if n == 0:
                continue
            r = float(d.loc[m, outcome].mean())
            se = float(np.sqrt(max(r * (1 - r), 1e-9) / n))
            cells.append({"q": i + 1, "n": n, "rate": round(r, 4),
                          "lift": round(r - base, 4), "ci": round(1.96 * se, 4),
                          "thin": n < MIN_CELL,
                          "lo": round(edges[i], 3), "hi": round(edges[i + 1], 3)})
        spread = max(c["rate"] for c in cells) - min(c["rate"] for c in cells)
        out.append({"id": cid, "label": label, "level": level,
                    "outcome": outcome, "outcome_label": OUTCOME_LABEL[outcome],
                    "why": why, "base": round(base, 4), "n": int(len(d)),
                    "spread": round(spread, 4), "edges": edges, "cells": cells})
    return out


def band_for(value, table_entry):
    """Which quartile a given fight or fighter falls in, 1-indexed."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    e = table_entry["edges"]
    for i in range(len(e) - 1):
        if value <= e[i + 1] or i == len(e) - 2:
            return i + 1
    return None


def write_json(fights, fighters, path="site/baserates.json", verbose=True):
    import json
    from pathlib import Path
    tbl = build_table(fights, fighters)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(
        {"built_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
         "min_cell": MIN_CELL, "conditions": tbl}, indent=1), encoding="utf-8")
    if verbose:
        flat = sum(1 for t in tbl if t["spread"] < 0.03)
        print(f"base rates: {len(tbl)} conditions written to {path} "
              f"({flat} flat, kept deliberately)")
    return tbl


if __name__ == "__main__":
    from .loaders import load
    f, p, _ = load(verbose=False)
    for t in write_json(f, p):
        cells = "  ".join(f"Q{c['q']} {c['rate']:.3f}" for c in t["cells"])
        print(f"  {t['id']:<4} {t['label']:<34} base {t['base']:.3f}  {cells}"
              f"   spread {t['spread']:+.3f}")
