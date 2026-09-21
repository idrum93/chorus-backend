"""Projected output per fighter. Two different jobs, two different models.

Why not one approach for everything: the four output stats have genuinely
different shapes, and forcing them into one mould produced nonsense.

  Control time and significant strikes are roughly continuous and always
  positive, so they get QUANTILE RANGES. A single fight's stat line is mostly
  noise (the median model explains ~11% of variance), so a point estimate would
  be false precision. An interval that covers what it claims to, plus a rank, is
  defensible: on ranking the model clearly beats a career average
  (Spearman 0.45 vs 0.28 on strikes).

  Takedowns and knockdowns are zero-inflated counts — most fighters record zero
  in most fights. Median quantile regression therefore predicts a constant zero
  for nearly everyone, which is arithmetically correct and completely useless
  (it scored R2 = -0.34). These get PROBABILITIES instead: "68% chance of
  landing at least one takedown" is both more honest and more readable than
  "1.4 takedowns per 15 minutes".

Interval width is calibrated on a validation slice rather than assumed. Asking
a model for its 10th and 90th percentile does not reliably produce 80%
coverage — untuned, control time covered 87% and strikes 75%.
"""
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import GradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, r2_score, roc_auc_score

from .features import PANEL_OWN, build_panel

FEATS = [f"own_{k}" for k in PANEL_OWN] + [f"opp_{k}" for k in PANEL_OWN]

# continuous -> ranges.  (label, unit, career-average baseline column, scale)
# (label, unit, career-average baseline column, scale, two_sided)
# Control time has a point mass at zero (17% of fighter-fights record none,
# 27% under a second per minute). A two-sided interval there is dishonest: the
# lower bound sits at 0 and excludes nothing, so all the coverage comes from
# the upper bound and the interval over-covers (86.6% when claiming 80%). It
# gets a calibrated ONE-SIDED bound instead — "80% of outcomes fall below X" —
# which is also the more natural thing to show a reader.
RANGE_TARGETS = {
    "y_ctrl_pm": ("control time", "sec/min", "own_ctrl_share", 60.0, False),
    "y_slpm": ("significant strikes", "per min", "own_adj_slpm", 1.0, True),
}
# zero-inflated counts -> probability of at least one
EVENT_TARGETS = {
    "y_td15": ("takedown", "own_adj_td15"),
    "y_kd15": ("knockdown", "own_kd15"),
}
TARGET_COVERAGE = 0.80
BASE_Q = (0.10, 0.90)


def _qmodel(tr, target, q, seed=7):
    return GradientBoostingRegressor(
        loss="quantile", alpha=q, n_estimators=200, learning_rate=0.06,
        max_depth=3, min_samples_leaf=40, random_state=seed
    ).fit(tr[FEATS], tr[target])


def fit_range(tr, val, target, two_sided=True, seed=7):
    """Conformalized quantile regression.

    Fit the 10th/50th/90th percentile on the training slice, then widen (or
    tighten) the interval by a single constant chosen on a held-out slice so
    that realised coverage hits TARGET_COVERAGE. Three model fits instead of
    searching over quantile pairs — and unlike a search, CQR comes with a
    finite-sample coverage guarantee that does not depend on the quantile
    models being any good.
    """
    models = {q: _qmodel(tr, target, q, seed) for q in (0.50, *BASE_Q)}
    lo = models[BASE_Q[0]].predict(val[FEATS])
    hi = models[BASE_Q[1]].predict(val[FEATS])
    mid = models[0.50].predict(val[FEATS])
    lo, hi = np.minimum(lo, mid), np.maximum(hi, mid)
    y = val[target].values
    # conformity score: how far outside the interval each point fell. For a
    # one-sided bound only the upper side counts, which lets k go negative and
    # tighten an interval that over-covers.
    score = np.maximum(lo - y, y - hi) if two_sided else (y - hi)
    k = float(np.quantile(score, TARGET_COVERAGE))
    return models, k


def predict_range(models, k, X, two_sided=True):
    mid = models[0.50].predict(X[FEATS])
    hi = np.maximum(models[BASE_Q[1]].predict(X[FEATS]), mid) + k
    if not two_sided:
        return np.zeros(len(X)), mid, np.maximum(hi, mid)
    lo = np.minimum(models[BASE_Q[0]].predict(X[FEATS]), mid) - k
    return np.maximum(lo, 0.0), mid, hi


def evaluate(fights, fighters, min_date="2012-01-01", test_frac=0.20, verbose=True):
    P = build_panel(fights, fighters)
    P = P[P.date >= pd.Timestamp(min_date)].sort_values("date")
    cut = P.date.quantile(1 - test_frac)
    trn, te = P[P.date <= cut], P[P.date > cut]
    vcut = trn.date.quantile(0.85)
    tr, val = trn[trn.date <= vcut], trn[trn.date > vcut]

    rng_rows, evt_rows, fitted = [], [], {}

    for t, (label, unit, base_col, scale, two_sided) in RANGE_TARGETS.items():
        models, k = fit_range(tr, val, t, two_sided)
        fitted[t] = (models, k, two_sided)
        lo, mid, hi = predict_range(models, k, te, two_sided)
        y = te[t].values
        base = te[base_col].values * scale
        rng_rows.append(dict(
            stat=label, unit=unit, kind="2-sided" if two_sided else "upper bound",
            mean_actual=y.mean(),
            r2=r2_score(y, mid), base_r2=r2_score(y, base),
            spearman=spearmanr(mid, y).statistic,
            base_spearman=spearmanr(base, y).statistic,
            conformal_k=k,
            coverage=((y >= lo) & (y <= hi)).mean(),
            med_width=np.median(hi - lo)))

    both = pd.concat([tr, val])
    for t, (label, base_col) in EVENT_TARGETS.items():
        yb = (both[t] > 0).astype(int)
        yte = (te[t] > 0).astype(int)
        m = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
            min_samples_leaf=40, l2_regularization=1.0,
            random_state=7).fit(both[FEATS], yb)
        fitted[t] = m
        pr = m.predict_proba(te[FEATS])[:, 1]
        evt_rows.append(dict(
            event=f"lands >=1 {label}", base_rate=yte.mean(),
            auc=roc_auc_score(yte, pr),
            base_auc=roc_auc_score(yte, te[base_col].values),
            brier=brier_score_loss(yte, pr),
            brier_const=brier_score_loss(yte, np.full(len(yte), yb.mean()))))

    R, E = pd.DataFrame(rng_rows), pd.DataFrame(evt_rows)
    if verbose:
        print(f"panel: {len(tr)} fit / {len(val)} calib / {len(te)} test "
              f"fighter-fights  ({te.date.min().date()}..{te.date.max().date()})\n")
        print("CONTINUOUS OUTPUTS -> calibrated ranges")
        print(R.round(3).to_string(index=False))
        print("\nZERO-INFLATED COUNTS -> probability of at least one")
        print(E.round(3).to_string(index=False))
        print("\nbase_* is the fighter's own shrunk career rate: what a site would")
        print("show with no modelling at all. coverage should be close to 0.80.")
    return R, E, fitted, (tr, val, te)


if __name__ == "__main__":
    from .loaders import load
    f, p, _ = load(verbose=False)
    evaluate(f, p)
