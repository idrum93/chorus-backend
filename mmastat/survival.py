"""Competing-risks hazard model: one structure, every outcome.

The props in `props.py` are independent binary classifiers. P(KO), P(decision)
and P(over 2.5 rounds) are fitted separately, so nothing forces them to agree —
they can imply a method distribution that does not sum to one, or a round line
inconsistent with the finish probability. That is a modelling error even when
each individual number is fine.

A fight ending is really a race between hazards: A can stop B, B can stop A,
either can submit the other, and the clock runs out on all of it. Modelled as
competing risks in discrete time, one fit produces a coherent joint
distribution and everything else is arithmetic on it:

    P(method), P(ends in round N), P(goes the distance),
    P(fighter X wins by method M in round N), expected duration,
    and P(X wins) as the sum over all the ways X can win.

Structure. Each fight is expanded into one row per minute it survives. In each
interval the outcome is one of five classes:

    0 survive   1 A by KO   2 B by KO   3 A by submission   4 B by submission

A decision is not a class — it is what happens when the fight survives every
scheduled interval, so it falls out of the survival curve rather than being
predicted directly. Fights are censored at their OWN scheduled length (25
minutes for five-rounders, 15 for three), which is why `sched_sec` exists.

Antisymmetry is preserved the same way as in the win model: every fight is
expanded in both orientations, with the A/B classes swapped, so the model
cannot learn a corner bias and P(A by KO) in one orientation equals P(B by KO)
in the other.

Given the distance, who wins is a separate question — judges score what
happened, which this model does not observe — so P(A | decision) is a small
second model on the same pre-fight features.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .features import PANEL_OWN, WIN_FEATURES, build, build_panel

INTERVAL = 60.0          # seconds per discrete interval
MAX_INTERVALS = 25
CLASSES = ["survive", "a_ko", "b_ko", "a_sub", "b_sub"]


def _fight_frame(fights, fighters):
    """Per-fight features: directional differentials decide WHO, symmetric
    sums decide WHETHER and WHEN."""
    X, _ = build(fights, fighters)
    X = X[X.orient == 0]
    P = build_panel(fights, fighters)
    g = P.groupby("fight_id")
    sym = pd.DataFrame({f"sum_{k}": g[f"own_{k}"].sum() for k in PANEL_OWN})
    base = X.set_index("fight_id")[WIN_FEATURES + ["date", "min_prior"]]
    return base.join(sym, how="inner")


def expand(fights, fighters, min_prior=2, min_date="2012-01-01", full=False):
    """One row per fight per interval, both orientations.

    full=False (TRAINING): intervals up to when the fight actually ended, the
    last one labelled with the terminal event. This is the likelihood.

    full=True (PREDICTION): every SCHEDULED interval, unlabelled. This
    distinction is not cosmetic. Rolling the survival curve over a fight's
    observed length uses the outcome to decide how long to roll for, so a
    first-round knockout gets one interval to decay over and comes out looking
    like the likeliest decision on the card. That inverts the model — it scored
    AUC 0.19 on P(decision), i.e. reliably backwards — and it is the survival
    analogue of ordinary temporal leakage.
    """
    F = _fight_frame(fights, fighters)
    F = F[(F.min_prior >= min_prior) & (F.date >= pd.Timestamp(min_date))]
    meta = fights.set_index("fight_id")[["total_sec", "sched_sec", "method", "winner"]]
    F = F.join(meta, how="inner")
    feats = [c for c in F.columns if c in WIN_FEATURES or c.startswith("sum_")]
    diff = [c for c in feats if c in WIN_FEATURES]

    rows = []
    for r in F.itertuples():
        n_sched = int(np.ceil(r.sched_sec / INTERVAL))
        n_sched = max(1, min(n_sched, MAX_INTERVALS))
        n_alive = int(np.ceil(min(r.total_sec, r.sched_sec) / INTERVAL))
        n_alive = max(1, min(n_alive, n_sched))
        if full:
            n_alive = n_sched
        finished = r.method in ("KO/TKO", "SUB") and r.winner in ("r", "b")
        base = {c: getattr(r, c) for c in feats}
        for t in range(1, n_alive + 1):
            terminal = (not full) and finished and t == n_alive
            if terminal:
                who = "a" if r.winner == "r" else "b"
                cls = CLASSES.index(f"{who}_{'ko' if r.method == 'KO/TKO' else 'sub'}")
            else:
                cls = 0
            for orient in (0, 1):
                d = dict(base)
                if orient == 1:                       # mirror
                    for c in diff:
                        d[c] = -d[c]
                    c2 = cls
                    if cls in (1, 2):
                        c2 = 3 - cls                  # a_ko <-> b_ko
                    elif cls in (3, 4):
                        c2 = 7 - cls                  # a_sub <-> b_sub
                else:
                    c2 = cls
                d.update(fight_id=r.Index, date=r.date, t=t,
                         rnd=min(5, (t - 1) // 5 + 1), y=c2, orient=orient,
                         n_sched=n_sched)
                rows.append(d)
    return pd.DataFrame(rows), feats


def fit(panel, feats, seed=7):
    cols = feats + ["t", "rnd"]
    m = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
        min_samples_leaf=60, l2_regularization=1.0,
        random_state=seed).fit(panel[cols], panel.y)
    return m, cols


def fit_decision(fights, fighters, train_ids, min_prior=2):
    """P(A wins | it goes to decision), on pre-fight features only."""
    X, _ = build(fights, fighters)
    d = X[(X.min_prior >= min_prior) & X.fight_id.isin(train_ids)]
    meta = fights.set_index("fight_id").method
    d = d[d.fight_id.map(meta) == "DEC"]
    sc = StandardScaler().fit(d[WIN_FEATURES])
    m = LogisticRegression(max_iter=4000, C=0.5, fit_intercept=False).fit(
        sc.transform(d[WIN_FEATURES]), d.y)
    return m, sc


def calibrate_hazards(model, cols, val_panel, actual):
    """Two multipliers, one for KO hazards and one for submission hazards.

    A gradient booster trained where 95.7% of intervals are "survive" shrinks
    the rare classes badly: raw output gave P(decision) 0.607 against an actual
    0.525. Scaling the two finish hazards and renormalising fixes the level
    without touching the shape or the coherence — the distribution still sums
    to one by construction. Fitted on a validation slice by matching observed
    method rates, not on the test block.
    """
    from scipy.optimize import minimize_scalar
    p0 = val_panel[val_panel.orient == 0].sort_values(["fight_id", "t"])
    H = model.predict_proba(p0[cols])
    fid = p0.fight_id.values
    a = actual.reindex(pd.Index(p0.fight_id.unique()))
    want_ko = float((a.method == "KO/TKO").mean())
    want_sub = float((a.method == "SUB").mean())

    def rates(c_ko, c_sub):
        Hc = H.copy()
        Hc[:, 1:3] *= c_ko
        Hc[:, 3:5] *= c_sub
        Hc[:, 0] = np.maximum(Hc[:, 0], 1e-9)
        Hc /= Hc.sum(axis=1, keepdims=True)
        surv, cur = 1.0, None
        ko = sub = 0.0
        n = 0
        for i in range(len(Hc)):
            if fid[i] != cur:
                cur, surv = fid[i], 1.0
                n += 1
            ko += surv * (Hc[i, 1] + Hc[i, 2])
            sub += surv * (Hc[i, 3] + Hc[i, 4])
            surv *= Hc[i, 0]
        return ko / n, sub / n

    c_ko = minimize_scalar(lambda c: (rates(c, 1.0)[0] - want_ko) ** 2,
                           bounds=(0.5, 6.0), method="bounded").x
    c_sub = minimize_scalar(lambda c: (rates(c_ko, c)[1] - want_sub) ** 2,
                            bounds=(0.5, 6.0), method="bounded").x
    return float(c_ko), float(c_sub)


def _scale(H, cal):
    if cal is None:
        return H
    H = H.copy()
    H[:, 1:3] *= cal[0]
    H[:, 3:5] *= cal[1]
    H[:, 0] = np.maximum(H[:, 0], 1e-9)
    return H / H.sum(axis=1, keepdims=True)


def distribution(model, cols, rows_for_one_fight):
    """Roll the per-interval hazards into a joint outcome distribution.

    P(event at t) = P(survived to t-1) * h_event(t), which is the whole point:
    a knockout in round 3 requires surviving rounds 1 and 2, and independent
    classifiers never enforce that.
    """
    H = model.predict_proba(rows_for_one_fight[cols])
    surv = 1.0
    out = {k: 0.0 for k in CLASSES[1:]}
    by_round = {}
    for i in range(len(H)):
        h = H[i]
        rnd = int(rows_for_one_fight.rnd.iloc[i])
        stop = 0.0
        for j, k in enumerate(CLASSES[1:], start=1):
            p = surv * h[j]
            out[k] += p
            stop += p
        by_round[rnd] = by_round.get(rnd, 0.0) + stop
        surv *= h[0]
    out["decision"] = surv
    return out, by_round


def predict_all(model, cols, panel, cal=None):
    """Vectorised roll-up for every fight in `panel` (orientation 0 only)."""
    p0 = panel[panel.orient == 0].sort_values(["fight_id", "t"])
    H = _scale(model.predict_proba(p0[cols]), cal)
    fid = p0.fight_id.values
    res = {}
    surv = 1.0
    cur = None
    acc = None
    rnd_acc = None
    for i in range(len(p0)):
        if fid[i] != cur:
            if cur is not None:
                acc["decision"] = surv
                res[cur] = (acc, rnd_acc)
            cur, surv = fid[i], 1.0
            acc = {k: 0.0 for k in CLASSES[1:]}
            rnd_acc = {}
        h = H[i]
        r = int(p0.rnd.values[i])
        stop = 0.0
        for j, k in enumerate(CLASSES[1:], start=1):
            v = surv * h[j]
            acc[k] += v
            stop += v
        rnd_acc[r] = rnd_acc.get(r, 0.0) + stop
        surv *= h[0]
    if cur is not None:
        acc["decision"] = surv
        res[cur] = (acc, rnd_acc)

    recs = []
    for f, (a, rr) in res.items():
        rec = dict(fight_id=f, **a)
        rec["p_finish"] = 1.0 - a["decision"]
        rec["p_ko"] = a["a_ko"] + a["b_ko"]
        rec["p_sub"] = a["a_sub"] + a["b_sub"]
        for r in range(1, 6):
            rec[f"p_end_r{r}"] = rr.get(r, 0.0)
        recs.append(rec)
    return pd.DataFrame(recs).set_index("fight_id")
