"""Join closing betting odds onto the corpus and turn them into probabilities.

The market is the benchmark that matters. Beating "pick the better record" only
shows the features carry signal; beating the closing line is what would mean the
model knows something the betting public doesn't.

Two details that decide whether the comparison is honest:

DEVIGGING. Raw American odds imply probabilities that sum to more than 1 — the
bookmaker's margin. Comparing a model's calibrated probability against a vigged
implied probability would flatter the model on log loss for no good reason, so
both sides are normalised to sum to 1.

ORIENTATION. The odds file and UFCStats don't agree on which fighter is the red
corner, so fights are matched on the unordered pair of names plus the date, and
the odds are then flipped to match the corpus's own corner assignment.
"""
import re

import numpy as np
import pandas as pd


def _norm(s):
    return re.sub(r"[^a-z ]", "", re.sub(r"\s+", " ", str(s)).strip().lower())


def american_to_prob(o):
    """-150 -> 0.600, +130 -> 0.435. Returns NaN for missing odds."""
    o = pd.to_numeric(o, errors="coerce")
    return np.where(o < 0, -o / (-o + 100.0), 100.0 / (o + 100.0))


def load_odds(path, fights):
    """Returns `fights` with p_market added (probability the RED corner of the
    corpus wins), NaN where no line was found."""
    m = pd.read_csv(path, low_memory=False)
    m["p_r_raw"] = american_to_prob(m.R_odds)
    m["p_b_raw"] = american_to_prob(m.B_odds)
    tot = m.p_r_raw + m.p_b_raw
    m["p_r"] = m.p_r_raw / tot                      # devigged
    m["rk"] = m.R_fighter.map(_norm)
    m["bk"] = m.B_fighter.map(_norm)
    m["dk"] = pd.to_datetime(m.date, errors="coerce").dt.strftime("%Y-%m-%d")

    book = {}
    for dk, rk, bk, pr in zip(m.dk, m.rk, m.bk, m.p_r):
        if pd.isna(pr) or pd.isna(dk):
            continue
        book[(dk, frozenset((rk, bk)))] = (rk, float(pr))

    parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    rn = parts[0].str.strip().map(_norm)
    bn = parts[1].str.strip().map(_norm)
    dk = fights.date.dt.strftime("%Y-%m-%d")

    out = []
    for d, a, b in zip(dk, rn, bn):
        hit = book.get((d, frozenset((a, b))))
        if hit is None:
            out.append(np.nan)
        else:
            odds_red, p = hit
            # flip if the odds file called the other fighter the red corner
            out.append(p if odds_red == a else 1.0 - p)

    f = fights.copy()
    f["p_market"] = out
    return f


def vig_of(path):
    m = pd.read_csv(path, low_memory=False)
    t = american_to_prob(m.R_odds) + american_to_prob(m.B_odds)
    return float(np.nanmedian(t) - 1.0)
