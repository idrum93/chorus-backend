"""Prop markets: method, distance, rounds.

Why bother when the win model already loses to the line: the moneyline is the
most liquid and most efficient market in the sport, and we measured that 51% of
it is reproducible from this data. Prop markets are thinner and priced more by
formula than by flow. They are also where this project's measured strengths
actually live — control time ranks at Spearman 0.43 and takedown probability at
AUC 0.742, neither of which is a moneyline input.

The key design difference from the win model: prop targets are SYMMETRIC. Whether
a fight goes to decision does not depend on which fighter you call the red
corner, so the features must be symmetric too. Signed differentials (a - b) are
wrong here and would let the model learn corner artifacts. Instead every
attribute enters twice:

    sum feature   = a + b        (how much of this quality is in the cage)
    gap feature   = |a - b|      (how mismatched the two are)

Both are invariant under swapping the fighters, so the prediction is as well.

No prop odds are available in the datasets used here, so this module measures
whether the props are PREDICTABLE. It cannot tell you whether they beat a prop
market. That distinction matters and is not glossed over anywhere in the output.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from .features import PANEL_OWN, build_panel

# Rounds are 5 minutes; "over 1.5 rounds" means past 7:30 of elapsed fight time.
ROUND_LINES = {"over_1_5_rounds": 450.0, "over_2_5_rounds": 750.0}


def symmetric_features(fights, fighters):
    """One row per fight, with sum/gap features that are invariant to corner."""
    P = build_panel(fights, fighters)
    if P.empty:
        return P
    g = P.groupby("fight_id")
    rows = {"date": g.date.first()}
    for k in PANEL_OWN:
        col = f"own_{k}"
        rows[f"sum_{k}"] = g[col].sum()
        rows[f"gap_{k}"] = g[col].max() - g[col].min()
    return pd.DataFrame(rows)


def prop_targets(fights):
    f = fights.set_index("fight_id")
    out = pd.DataFrame(index=f.index)
    out["decision"] = (f.method == "DEC").astype(int)
    out["inside_distance"] = (f.method != "DEC").astype(int)
    out["ko_tko"] = (f.method == "KO/TKO").astype(int)
    out["submission"] = (f.method == "SUB").astype(int)
    for name, secs in ROUND_LINES.items():
        out[name] = (f.total_sec > secs).astype(int)
    out["method3"] = f.method
    return out


BINARY = ["decision", "ko_tko", "submission",
          "over_1_5_rounds", "over_2_5_rounds"]


def evaluate(fights, fighters, min_date="2012-01-01", test_frac=0.20, verbose=True):
    S = symmetric_features(fights, fighters)
    T = prop_targets(fights)
    D = S.join(T, how="inner").dropna(subset=["date"])
    D = D[D.date >= pd.Timestamp(min_date)].sort_values("date")
    FE = [c for c in D.columns if c.startswith(("sum_", "gap_"))]
    cut = D.date.quantile(1 - test_frac)
    tr, te = D[D.date <= cut], D[D.date > cut]

    rows, fitted = [], {}
    for t in BINARY:
        m = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
            min_samples_leaf=40, l2_regularization=1.0,
            random_state=7).fit(tr[FE], tr[t])
        fitted[t] = m
        pr = m.predict_proba(te[FE])[:, 1]
        base = np.full(len(te), tr[t].mean())
        rows.append(dict(
            prop=t, base_rate=te[t].mean(),
            auc=roc_auc_score(te[t], pr),
            brier=brier_score_loss(te[t], pr),
            brier_base=brier_score_loss(te[t], base),
            logloss=log_loss(te[t], np.clip(pr, 1e-6, 1 - 1e-6)),
            logloss_base=log_loss(te[t], np.clip(base, 1e-6, 1 - 1e-6))))

    R = pd.DataFrame(rows)
    R["brier_gain"] = R.brier_base - R.brier
    R["ll_gain"] = R.logloss_base - R.logloss

    m3 = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
        min_samples_leaf=40, l2_regularization=1.0,
        random_state=7).fit(tr[FE], tr.method3)
    fitted["method3"] = m3
    p3 = m3.predict_proba(te[FE])
    labs = sorted(D.method3.unique())
    prior = tr.method3.value_counts(normalize=True).reindex(labs).values
    m3_ll = log_loss(te.method3, p3, labels=list(m3.classes_))
    m3_base = log_loss(te.method3, np.tile(prior, (len(te), 1)), labels=labs)

    if verbose:
        print(f"{len(tr)} train / {len(te)} test fights "
              f"({te.date.min().date()}..{te.date.max().date()}), "
              f"{len(FE)} symmetric features\n")
        print("BINARY PROPS  (gain > 0 means better than always predicting the base rate)")
        print(R[["prop", "base_rate", "auc", "brier", "brier_base",
                 "brier_gain", "ll_gain"]].round(4).to_string(index=False))
        print(f"\n3-WAY METHOD  log loss {m3_ll:.4f} vs {m3_base:.4f} for the base rates "
              f"(gain {m3_base - m3_ll:+.4f})")
        print("\nNo prop odds in the source data: this shows the props are predictable,")
        print("NOT that they beat a prop market. Those are different claims.")
    return R, fitted, (tr, te, FE)


if __name__ == "__main__":
    from .loaders import load
    f, p, _ = load(verbose=False)
    evaluate(f, p)
