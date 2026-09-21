"""Single chronological pass over the fight corpus.

THE INVARIANT: inside the loop, `snapshot -> emit -> update` runs in that order
and nothing else touches state. Every feature row for fight N is therefore a
function of fights 1..N-1 only. tests.py asserts this independently.

Each fight is emitted TWICE, once in each orientation, sharing a fight_id so
folds can group them. Every feature is antisymmetric, so combined with
fit_intercept=False the model satisfies P(A beats B) = 1 - P(B beats A) exactly.
"""
import math

import numpy as np
import pandas as pd

from .config import ELO_START, ELO_K, ELO_METHOD_MULT, ELO_REGRESS_PER_YEAR
from .state import FighterState

# Plain differentials: feature = A's value minus B's value.
DIFF_KEYS = [
    "elo", "win_pct", "log_exp", "opp_elo",
    "adj_slpm", "sapm", "str_acc", "str_def",
    "adj_td15", "td_acc", "td_def", "sub15", "ctrl_share",
    "finish_rate", "ko_loss_rate",
    "reach", "height", "age", "log_layoff", "southpaw",
    "kd15", "kd_against15", "body_share", "leg_share",
    "clinch_share", "ground_share",
]

# The fortitude cluster. Held separate from DIFF_KEYS so it can be tested as a
# pre-registered family (PREREGISTRATION.md, addendum 4) rather than quietly
# folded into the win model.
FORTITUDE_KEYS = ["late_acc_delta", "late_volume_ret", "ctrl_retention",
                  "induced_decay", "deep_experience", "kd_recovery"]
FORTITUDE_FEATURES = [f"d_{k}" for k in FORTITUDE_KEYS]

FEATURE_NAMES = ([f"d_{k}" for k in DIFF_KEYS]
                 + ["grapple_edge", "ko_edge", "sub_edge",
                    "power_edge", "position_edge"])

# Pruned set. Each removal below is a feature whose signal is already carried by
# an interaction term or a better-adjusted twin (r > 0.85 with something kept).
# On synthetic data this matches the full set's accuracy while cutting the design
# matrix condition number from ~42 to ~10, which is what stops coefficient signs
# from flipping. Prefer this one for anything you intend to interpret.
DROPPED = {
    "d_adj_td15": "carried by grapple_edge",
    "d_sub15": "carried by sub_edge",
    "d_win_pct": "carried by d_elo, which is opponent-adjusted",
    "d_td_acc": "carried by grapple_edge",
    "d_ctrl_share": "carried by grapple_edge",
    "d_height": "carried by d_reach",
    "d_southpaw": "no measurable effect",
    "d_td_def": "carried by grapple_edge",
    "d_finish_rate": "carried by ko_edge",
}
DROPPED.update({k: "fortitude cluster, tested separately" for k in FORTITUDE_FEATURES})
CORE_FEATURES = [f for f in FEATURE_NAMES if f not in DROPPED]

# The win model deliberately EXCLUDES the style features. Adding the eight
# target/position/knockdown differentials changed held-out log loss by
# -0.0009, 95% CI [-0.0041, +0.0022], P(better) = 0.293 — no detectable effect
# on predicting who wins. They stay in the codebase because they demonstrably
# do help predict OUTPUT (see projections.py: strike-volume ranking Spearman
# 0.441 vs 0.283 for a career average; takedown probability AUC 0.742), and
# because the explanation panel displays them. Keeping them out of the win
# model is the disciplined call, not an oversight: see PREREGISTRATION.md for
# the targeted second attempt via interaction terms.
STYLE_FEATURES = [f for f in CORE_FEATURES
                  if any(k in f for k in ("kd", "_share", "power_edge",
                                          "position_edge"))]
WIN_FEATURES = [f for f in CORE_FEATURES if f not in STYLE_FEATURES]


def elo_eff(st, d):
    """Rating decayed toward the mean by time since last appearance."""
    yrs = st.layoff_days(d) / 365.25
    return ELO_START + (st.elo - ELO_START) * math.exp(-ELO_REGRESS_PER_YEAR * yrs)


def make_features(sa, sb):
    f = {f"d_{k}": sa[k] - sb[k] for k in DIFF_KEYS + FORTITUDE_KEYS}
    # Interactions: offense discounted by the specific defense in front of it.
    f["grapple_edge"] = (sa["adj_td15"] * (1 - sb["td_def"])
                         - sb["adj_td15"] * (1 - sa["td_def"]))
    f["ko_edge"] = (sa["adj_slpm"] * sb["ko_loss_rate"]
                    - sb["adj_slpm"] * sa["ko_loss_rate"])
    f["sub_edge"] = (sa["sub15"] * (1 - sb["td_def"])
                     - sb["sub15"] * (1 - sa["td_def"]))
    # Power vs chin, measured in knockdowns rather than career method mix.
    f["power_edge"] = (sa["kd15"] * sb["kd_against15"]
                       - sb["kd15"] * sa["kd_against15"])
    # A clinch- or ground-heavy striker needs to get there first; weight their
    # positional preference by whether the opponent can be taken down.
    f["position_edge"] = ((sa["clinch_share"] + sa["ground_share"]) * (1 - sb["td_def"])
                          - (sb["clinch_share"] + sb["ground_share"]) * (1 - sa["td_def"]))
    return f


def _init_states(fighters):
    st = {}
    for r in fighters.itertuples():
        st[r.fighter_id] = FighterState(
            fid=r.fighter_id,
            dob=r.dob,
            height_in=float(r.height_in),
            reach_in=float(r.reach_in),
            stance=r.stance,
        )
    return st


def walk(fights: pd.DataFrame, fighters: pd.DataFrame):
    """Generator over the corpus in chronological order.

    Yields (fight_row, snapshot_A, snapshot_B, states) BEFORE that fight has
    been applied to either fighter, then advances state when the consumer asks
    for the next item. Every consumer therefore inherits the same
    snapshot-before-update guarantee, so there is exactly one place where
    temporal leakage could be introduced instead of one per model.
    """
    states = _init_states(fighters)
    for f in fights.sort_values(["date", "fight_id"], kind="mergesort").itertuples():
        A, B = states[f.r_id], states[f.b_id]
        d = f.date
        sa, sb = A.snapshot(d), B.snapshot(d)
        sa["elo"], sb["elo"] = elo_eff(A, d), elo_eff(B, d)
        n_prior = (A.n_fights, B.n_fights)

        yield f, sa, sb, n_prior

        _apply(f, A, B, sa, sb)


def _apply(f, A, B, sa, sb):
    """Advance both fighters by one fight. Called only after the snapshot has
    been handed out."""
    minutes = f.total_sec / 60.0
    if f.winner == "r":
        res_a, res_b, score = "W", "L", 1.0
    elif f.winner == "b":
        res_a, res_b, score = "L", "W", 0.0
    else:
        res_a, res_b, score = "D", "D", 0.5

    exp_a = 1.0 / (1.0 + 10 ** ((sb["elo"] - sa["elo"]) / 400.0))
    k = ELO_K * ELO_METHOD_MULT.get(f.method, 1.0)
    delta = k * (score - exp_a)
    A.elo += delta
    B.elo -= delta

    BRK = ("head_landed", "body_landed", "leg_landed",
           "dist_landed", "clinch_landed", "ground_landed",
           "r1_ss_landed", "r1_ss_att", "late_ss_landed", "late_ss_att",
           "r1_ctrl_sec", "late_ctrl_sec", "deep_rounds")
    REN = {"r1_ss_landed": "r1_landed", "r1_ss_att": "r1_att",
           "late_ss_landed": "late_landed", "late_ss_att": "late_att",
           "r1_ctrl_sec": "r1_ctrl", "late_ctrl_sec": "late_ctrl"}
    xa = {REN.get(k2, k2): getattr(f, "r_" + k2, 0.0) for k2 in BRK}
    xa["kd"], xa["kd_against"] = getattr(f, "r_kd", 0.0), getattr(f, "b_kd", 0.0)
    # the OPPONENT's round-1 and late output is what measures induced decay
    xa["opp_r1_att"] = getattr(f, "b_r1_ss_att", 0.0)
    xa["opp_late_att"] = getattr(f, "b_late_ss_att", 0.0)
    xb = {REN.get(k2, k2): getattr(f, "b_" + k2, 0.0) for k2 in BRK}
    xb["kd"], xb["kd_against"] = getattr(f, "b_kd", 0.0), getattr(f, "r_kd", 0.0)
    xb["opp_r1_att"] = getattr(f, "r_r1_ss_att", 0.0)
    xb["opp_late_att"] = getattr(f, "r_late_ss_att", 0.0)

    common = dict(date=f.date, minutes=minutes, method=f.method)
    A.update(**common, **xa, result=res_a, opp_snapshot=sb,
             ss_landed=f.r_ss_landed, ss_att=f.r_ss_att,
             ss_absorbed=f.b_ss_landed, ss_faced=f.b_ss_att,
             td_landed=f.r_td_landed, td_att=f.r_td_att,
             td_conceded=f.b_td_landed, td_faced=f.b_td_att,
             sub_att=f.r_sub_att, ctrl_sec=f.r_ctrl_sec)
    B.update(**common, **xb, result=res_b, opp_snapshot=sa,
             ss_landed=f.b_ss_landed, ss_att=f.b_ss_att,
             ss_absorbed=f.r_ss_landed, ss_faced=f.r_ss_att,
             td_landed=f.b_td_landed, td_att=f.b_td_att,
             td_conceded=f.r_td_landed, td_faced=f.r_td_att,
             sub_att=f.b_sub_att, ctrl_sec=f.b_ctrl_sec)


PANEL_OWN = ["adj_slpm", "sapm", "str_acc", "str_def", "adj_td15", "td_acc",
             "td_def", "sub15", "ctrl_share", "elo", "opp_elo", "age",
             "log_exp", "log_layoff", "reach", "height", "southpaw",
             "kd15", "kd_against15", "body_share", "leg_share",
             "clinch_share", "ground_share"]


def build_panel(fights: pd.DataFrame, fighters: pd.DataFrame) -> pd.DataFrame:
    """One row per fighter per fight, carrying absolute pre-fight LEVELS for
    both sides plus that fighter's realised output. Differentials predict who
    wins; levels are what you need to predict how much someone does."""
    rows = []
    for f, sa, sb, n_prior in walk(fights, fighters):
        if min(n_prior) < 2 or f.total_sec <= 0:
            continue
        mins = f.total_sec / 60.0
        for me, op, pre in ((sa, sb, "r"), (sb, sa, "b")):
            r = {"fight_id": f.fight_id, "date": f.date, "minutes": mins,
                 "y_slpm": getattr(f, pre + "_ss_landed") / mins,
                 "y_td15": getattr(f, pre + "_td_landed") / mins * 15.0,
                 "y_ctrl_pm": getattr(f, pre + "_ctrl_sec") / mins,
                 "y_kd15": getattr(f, pre + "_kd") / mins * 15.0}
            r.update({f"own_{k}": me[k] for k in PANEL_OWN})
            r.update({f"opp_{k}": op[k] for k in PANEL_OWN})
            rows.append(r)
    return pd.DataFrame(rows)


def build(fights: pd.DataFrame, fighters: pd.DataFrame):
    """Mirrored feature rows for the win model.

    Expressed through walk() so there is exactly ONE update path in the
    codebase. It previously had its own inline copy, which silently went stale
    when the fortitude accumulators were added — build() kept passing the old
    field list while build_panel() passed the new one, and the resulting
    features were constant. Two copies of this loop is the bug, not a style
    preference.
    """
    states = _init_states(fighters)
    rows = []
    for f, sa, sb, n_prior, states in _walk_with_states(fights, fighters, states):
        if f.winner not in ("r", "b"):
            continue
        base = dict(fight_id=f.fight_id, date=f.date, method=f.method,
                    min_prior=min(n_prior))
        fwd, rev = make_features(sa, sb), make_features(sb, sa)
        y = 1 if f.winner == "r" else 0
        rows.append({**base, "orient": 0, "a_id": f.r_id, "b_id": f.b_id,
                     "y": y, **fwd})
        rows.append({**base, "orient": 1, "a_id": f.b_id, "b_id": f.r_id,
                     "y": 1 - y, **rev})
    return pd.DataFrame(rows), states


def _walk_with_states(fights, fighters, states):
    """walk(), but yielding the state dict too so build() can return it."""
    for f in fights.sort_values(["date", "fight_id"], kind="mergesort").itertuples():
        A, B = states[f.r_id], states[f.b_id]
        d = f.date
        sa, sb = A.snapshot(d), B.snapshot(d)
        sa["elo"], sb["elo"] = elo_eff(A, d), elo_eff(B, d)
        n_prior = (A.n_fights, B.n_fights)
        yield f, sa, sb, n_prior, states
        _apply(f, A, B, sa, sb)


def current_snapshot(states, fid, on_date):
    """Feature vector for an upcoming (unfought) bout."""
    st = states[fid]
    s = st.snapshot(on_date)
    s["elo"] = elo_eff(st, on_date)
    return s
