"""Fit, validate honestly, calibrate, export.

Design notes worth knowing:

  * Splits are by DATE, never random. Random CV on fight data lets the model
    see a fighter's future when predicting their past and inflates everything.
  * Training uses both mirror orientations; reporting uses orient==0 only, so
    each fight counts once.
  * The linear model is fit WITHOUT an intercept on antisymmetric features, so
    P(A beats B) = 1 - P(B beats A) holds exactly. The GBM has no such
    guarantee, so it is symmetrised explicitly at predict time.
  * Every number is shown against baselines. An accuracy figure with nothing to
    compare it to is not a result.
"""
import json
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from .config import MIN_PRIOR_FIGHTS, N_WALK_FOLDS, TEST_FRACTION
from .features import (CORE_FEATURES, DROPPED, FEATURE_NAMES,
                       WIN_FEATURES, build)
from .loaders import load as load_real
from .synth import make_corpus

warnings.filterwarnings("ignore")


# --------------------------------------------------------------- helpers
def symmetrise(predict_fn, Xf, Xr):
    """p = (p(A,B) + 1 - p(B,A)) / 2 — forces antisymmetry on any model."""
    return 0.5 * (predict_fn(Xf) + 1.0 - predict_fn(Xr))


def report(name, y, p):
    return dict(model=name, n=len(y),
                acc=accuracy_score(y, p > 0.5),
                auc=roc_auc_score(y, p),
                logloss=log_loss(y, np.clip(p, 1e-6, 1 - 1e-6)),
                brier=brier_score_loss(y, p))


def fmt(rows):
    df = pd.DataFrame(rows)
    df["acc"] = (df.acc * 100).round(2)
    for c in ("auc", "logloss", "brier"):
        df[c] = df[c].round(4)
    return df.to_string(index=False)


# --------------------------------------------------------------- main
def run(source="real", seed=7, n_fighters=760, n_events=520, cols=None,
        min_date="2012-01-01", upload_dir=None):
    """source="real" loads the scraped UFCStats CSVs; "synth" generates a
    simulated corpus (the only mode with an ORACLE ceiling to compare to).

    min_date defaults to 2012. Older fights are still used to BUILD each
    fighter's history — they just aren't trained or tested on. The sport's
    striking volume roughly tripled between 1995 and 2015, so 1990s bouts are
    a different game; including them costs about 1.7 points of accuracy.
    """
    cols = cols or WIN_FEATURES
    print(f"building corpus ({source}) ...")
    if source == "real":
        fights, fighters, _ = load_real(upload_dir, verbose=False)
        truth = None
    else:
        fights, fighters, truth = make_corpus(
            n_fighters=n_fighters, n_events=n_events, seed=seed)
    X, states = build(fights, fighters)
    d = X[(X.min_prior >= MIN_PRIOR_FIGHTS)
          & (X.date >= pd.Timestamp(min_date))].copy().sort_values("date")
    print(f"  {len(fights)} fights, {len(d)//2} usable "
          f"(both corners with {MIN_PRIOR_FIGHTS}+ prior bouts)\n")

    # ---- time split -----------------------------------------------------
    cut = d.date.quantile(1 - TEST_FRACTION)
    trn, tst = d[d.date <= cut], d[d.date > cut]
    print(f"train  {trn.date.min().date()} .. {trn.date.max().date()}  ({len(trn)//2} fights)")
    print(f"test   {tst.date.min().date()} .. {tst.date.max().date()}  ({len(tst)//2} fights)\n")

    # inner calibration slice, carved off the END of train (never the middle)
    cal_cut = trn.date.quantile(0.85)
    fit, cal = trn[trn.date <= cal_cut], trn[trn.date > cal_cut]

    sc = StandardScaler().fit(fit[cols])
    Tr = sc.transform(fit[cols])

    lr = LogisticRegression(max_iter=4000, C=0.5, fit_intercept=False).fit(Tr, fit.y)
    gb = HistGradientBoostingClassifier(
        max_iter=260, learning_rate=0.045, max_leaf_nodes=15,
        min_samples_leaf=40, l2_regularization=1.0, random_state=seed
    ).fit(Tr, fit.y)

    def p_lr(df):
        return lr.predict_proba(sc.transform(df[cols]))[:, 1]

    def p_gb_raw(df):
        return gb.predict_proba(sc.transform(df[cols]))[:, 1]

    def p_gb(df):
        f0 = df
        f1 = df.copy()
        f1[cols] = -f1[cols].values
        return 0.5 * (p_gb_raw(f0) + 1.0 - p_gb_raw(f1))

    # ---- calibration ----------------------------------------------------
    # Platt, not isotonic. The calibration slice here is a few hundred fights;
    # isotonic fits a free-form step function and overfits that badly (it cost
    # ~2 points of accuracy in testing). A 2-parameter sigmoid cannot.
    class Platt:
        def fit(self, p, y):
            z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
            self.m = LogisticRegression(max_iter=1000).fit(z.reshape(-1, 1), y)
            self.a = float(self.m.coef_[0][0])
            self.b = float(self.m.intercept_[0])
            return self

        def predict(self, p):
            z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
            return 1 / (1 + np.exp(-(self.a * z + self.b)))

    iso = {nm: Platt().fit(fn(cal), cal.y.values) for nm, fn in (("lr", p_lr), ("gb", p_gb))}

    # ---- evaluate on one row per fight ---------------------------------
    ev = tst[tst.orient == 0]
    y = ev.y.values
    rows = []

    # baselines
    # Note on baselines: the evaluation uses one row per fight in UFCStats'
    # native ordering, where the winner is listed first ~63% of the time. So
    # the majority-class rate below is an artifact of the source file, NOT
    # something achievable at prediction time — at which point there is no
    # "first listed" fighter. The operational floor is 50%.
    maj = max(y.mean(), 1 - y.mean())
    print(f"test-block majority class: {100*maj:.2f}%  "
          f"(source-ordering artifact, not a real baseline)\n")
    rows.append(report("baseline: coin flip", y, np.full(len(y), 0.5)))
    rows.append(report("baseline: higher Elo", y,
                       1 / (1 + 10 ** (-ev.d_elo.values / 400))))
    rows.append(report("baseline: better win %", y,
                       np.clip(0.5 + ev.d_win_pct.values, 0.02, 0.98)))

    scF = StandardScaler().fit(fit[FEATURE_NAMES])
    lrF = LogisticRegression(max_iter=4000, C=0.5, fit_intercept=False)
    lrF.fit(scF.transform(fit[FEATURE_NAMES]), fit.y)
    rows.append(report(f"logistic (all {len(FEATURE_NAMES)} feats)", y,
                       lrF.predict_proba(scF.transform(ev[FEATURE_NAMES]))[:, 1]))
    rows.append(report(f"logistic (core {len(cols)})", y, p_lr(ev)))
    rows.append(report("logistic core + platt", y, iso["lr"].predict(p_lr(ev))))
    rows.append(report("gbm (symmetrised)", y, p_gb(ev)))
    rows.append(report("gbm + platt", y, iso["gb"].predict(p_gb(ev))))

    # ---- oracle: only exists when the latents are known (synthetic) ------
    if truth is not None:
      t = truth.set_index("fighter_id")
      dims = ["so", "sd", "go", "gd", "dur", "pace", "skill"]
      def latent(df):
          A = t.loc[df.a_id.values, dims].values
          B = t.loc[df.b_id.values, dims].values
          return A - B
      orc = LogisticRegression(max_iter=4000, fit_intercept=False).fit(latent(fit), fit.y)
      rows.append(report("ORACLE (true latents)", y, orc.predict_proba(latent(ev))[:, 1]))

    print("HELD-OUT TEST BLOCK")
    print(fmt(rows), "\n")

    # ---- walk-forward: does it hold up across eras? ---------------------
    print("WALK-FORWARD (expanding window, logistic)")
    qs = np.linspace(0.45, 0.95, N_WALK_FOLDS + 1)
    wf = []
    for i in range(N_WALK_FOLDS):
        a, b = d.date.quantile(qs[i]), d.date.quantile(qs[i + 1])
        tr_i, te_i = d[d.date <= a], d[(d.date > a) & (d.date <= b)]
        te_i = te_i[te_i.orient == 0]
        if len(te_i) < 40:
            continue
        s_i = StandardScaler().fit(tr_i[cols])
        m_i = LogisticRegression(max_iter=4000, C=0.5, fit_intercept=False)
        m_i.fit(s_i.transform(tr_i[cols]), tr_i.y)
        p_i = m_i.predict_proba(s_i.transform(te_i[cols]))[:, 1]
        wf.append(dict(fold=i + 1, train_n=len(tr_i) // 2, test_n=len(te_i),
                       through=str(b.date()),
                       acc=round(100 * accuracy_score(te_i.y, p_i > 0.5), 2),
                       logloss=round(log_loss(te_i.y, np.clip(p_i, 1e-6, 1 - 1e-6)), 4)))
    print(pd.DataFrame(wf).to_string(index=False), "\n")

    # ---- calibration table ---------------------------------------------
    p_final = iso["lr"].predict(p_lr(ev))
    bins = pd.cut(p_final, [0, .35, .45, .55, .65, .75, 1.0])
    ct = pd.DataFrame({"p": p_final, "y": y}).groupby(bins, observed=True).agg(
        n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    ct[["predicted", "actual"]] = (ct[["predicted", "actual"]] * 100).round(1)
    print("CALIBRATION (logistic + platt)")
    print(ct.to_string(), "\n")

    # ---- collinearity ---------------------------------------------------
    # Correlated features split a shared effect between them, and the split can
    # land on a nonsense sign. Read any flagged coefficient as a pair, not alone.
    Cm = pd.DataFrame(Tr, columns=cols).corr().abs().copy()
    for c in cols:
        Cm.loc[c, c] = 0.0
    pairs = (Cm.stack().sort_values(ascending=False)
             .loc[lambda s: s > 0.75].iloc[::2].head(6))
    print(f"pruned {len(DROPPED)} redundant features: " +
          ", ".join(f"{k} ({v})" for k, v in list(DROPPED.items())[:3]) + ", ...\n")
    print("COLLINEARITY (|r| > 0.75 — these coefficients are not independently readable)")
    if len(pairs) == 0:
        print("  none\n")
    else:
        for (u, v), r in pairs.items():
            print(f"  {u:<16} ~ {v:<16} r={r:.3f}")
        print(f"  design matrix condition number: "
              f"{np.linalg.cond(Tr):.0f}  (>30 means unstable signs)\n")

    # ---- coefficients ---------------------------------------------------
    coef = sorted(zip(cols, lr.coef_[0]), key=lambda kv: -abs(kv[1]))
    print("FITTED WEIGHTS (standardised units — comparable to each other)")
    for k, v in coef:
        bar = "#" * int(round(abs(v) / max(abs(c) for _, c in coef) * 34))
        print(f"  {k:<18} {v:+.4f}  {bar}")

    export = {
        "features": cols,
        "mean": sc.mean_.tolist(),
        "scale": sc.scale_.tolist(),
        "coef": lr.coef_[0].tolist(),
        "intercept": 0.0,
        "calibration": {"type": "platt", "a": iso["lr"].a, "b": iso["lr"].b},
        "trained_through": str(fit.date.max().date()),
        "n_train_fights": len(fit) // 2,
        "source": source,
        "min_date": min_date,
        "test_acc": round(float(rows[-1]["acc"]), 4),
    }
    with open("model.json", "w") as f:
        json.dump(export, f, indent=1)
    print("\nwrote model.json")
    return export


if __name__ == "__main__":
    import sys
    run(source="synth" if "--synth" in sys.argv else "real")
