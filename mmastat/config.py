"""Tuning surface for the whole pipeline. Everything hand-set lives here."""

# ---------------------------------------------------------------- Elo
ELO_START = 1500.0
ELO_K = 24.0
# A finish is stronger evidence than a split decision. Multiplies K.
ELO_METHOD_MULT = {"KO/TKO": 1.30, "SUB": 1.30, "DEC": 1.00, "DRAW": 1.00, "NC": 0.0}
# Elo points per year of layoff decay toward the mean (ring rust / stale rating).
ELO_REGRESS_PER_YEAR = 0.06

# ---------------------------------------------------------------- League priors
# Divisional means a fighter is shrunk toward when they have little tape.
# Replace with values computed from your own scraped corpus.
PRIOR = {
    "slpm": 4.00,        # significant strikes landed per minute
    "sapm": 4.00,        # significant strikes absorbed per minute
    "str_acc": 0.45,     # landed / attempted
    "str_def": 0.55,     # 1 - (absorbed / faced)
    "td15": 1.50,        # takedowns landed per 15 min
    "td_acc": 0.35,
    "td_def": 0.65,
    "sub15": 0.60,       # submission attempts per 15 min
    "ctrl_share": 0.18,  # control seconds / fight seconds
    "finish_rate": 0.55,
    "ko_loss_rate": 0.30,
    # Target and position shares, measured from the 2012+ corpus rather than
    # guessed. head+body+leg = 1 and dist+clinch+ground = 1 by construction, so
    # only two of each trio are ever used as features (the third is implied and
    # would make the design matrix singular).
    "body_share": 0.208,
    "leg_share": 0.162,
    "clinch_share": 0.121,
    "ground_share": 0.114,
    "kd15": 0.297,          # knockdowns landed per 15 min, per fighter
    "kd_against15": 0.297,
}

# Shrinkage strength = how much fictitious prior data every fighter starts with.
# Higher = more conservative for low-sample fighters.
SHRINK = {
    "minutes": 15.0,     # for per-minute rates
    "blocks15": 2.0,     # for per-15-minute rates
    "attempts": 25.0,    # for accuracy / defense percentages
    "ctrl_sec": 900.0,   # for control share
    "fights": 4.0,       # for method-mix rates
}

# ---------------------------------------------------------------- Opponent adjustment
# Clip on the strength-of-schedule multiplier so one weird opponent can't
# blow up a fighter's adjusted rate.
ADJ_CLIP = (0.72, 1.38)

# ---------------------------------------------------------------- Sample gates
# Fights where either corner has fewer than this many prior bouts on record are
# emitted but flagged; train.py drops them by default. Debuts have no features.
MIN_PRIOR_FIGHTS = 2

# ---------------------------------------------------------------- Validation
# Fraction of the timeline used for the final held-out test block.
TEST_FRACTION = 0.20
# Number of expanding-window folds inside the training block.
N_WALK_FOLDS = 5
