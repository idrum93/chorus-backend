"""Per-fighter running accumulator.

The contract that keeps the pipeline honest: a FighterState object holds ONLY
what has happened strictly before the fight currently being processed. Reading
a rate off this object is therefore always leakage-safe, and `update()` is the
only thing that advances it. features.py must call snapshot() before update().
"""
from dataclasses import dataclass, field
import math

from .config import PRIOR, SHRINK, ADJ_CLIP, ELO_START


@dataclass
class FighterState:
    fid: str
    dob: object = None
    height_in: float = 70.0
    reach_in: float = 72.0
    stance: str = "Orthodox"

    elo: float = ELO_START
    n_fights: int = 0
    wins: int = 0
    losses: int = 0

    # striking
    ss_landed: float = 0.0
    ss_att: float = 0.0
    ss_absorbed: float = 0.0
    ss_faced: float = 0.0

    # grappling
    td_landed: float = 0.0
    td_att: float = 0.0
    td_conceded: float = 0.0
    td_faced: float = 0.0
    sub_att: float = 0.0
    ctrl_sec: float = 0.0

    # target / position breakdown of significant strikes landed
    head_landed: float = 0.0
    body_landed: float = 0.0
    leg_landed: float = 0.0
    dist_landed: float = 0.0
    clinch_landed: float = 0.0
    ground_landed: float = 0.0
    kd: float = 0.0
    kd_against: float = 0.0

    # --- fortitude cluster: how a fighter holds up as a fight goes long.
    # Round 1 vs rounds 3+, accumulated over a career. Round 2 is excluded
    # from both sides as transitional.
    r1_landed: float = 0.0
    r1_att: float = 0.0
    late_landed: float = 0.0
    late_att: float = 0.0
    r1_ctrl: float = 0.0
    late_ctrl: float = 0.0
    deep_rounds: float = 0.0        # career rounds logged past round 2
    kd_absorbed: float = 0.0
    kd_survived: float = 0.0        # knocked down, did not lose by strikes
    opp_r1_att: float = 0.0         # opponents' output IN ROUND 1 against this
    opp_late_att: float = 0.0       # ... and late. The ratio is induced decay.
    opp_deep_rounds: float = 0.0

    # time
    minutes: float = 0.0

    # method mix
    w_ko: int = 0
    w_sub: int = 0
    w_dec: int = 0
    l_ko: int = 0

    # strength of schedule: sums of opponent quality AT TIME OF FIGHT
    opp_str_def_sum: float = 0.0
    opp_td_def_sum: float = 0.0
    opp_elo_sum: float = 0.0
    n_qual: int = 0

    last_date: object = None

    # ------------------------------------------------------------ raw rates
    def _rate(self, num, den, prior_key, k_key):
        k = SHRINK[k_key]
        return (num + k * PRIOR[prior_key]) / (den + k)

    @property
    def slpm(self):
        return self._rate(self.ss_landed, self.minutes, "slpm", "minutes")

    @property
    def sapm(self):
        return self._rate(self.ss_absorbed, self.minutes, "sapm", "minutes")

    @property
    def str_acc(self):
        return self._rate(self.ss_landed, self.ss_att, "str_acc", "attempts")

    @property
    def str_def(self):
        k = SHRINK["attempts"]
        allowed = (self.ss_absorbed + k * (1 - PRIOR["str_def"])) / (self.ss_faced + k)
        return 1.0 - allowed

    @property
    def td15(self):
        return self._rate(self.td_landed, self.minutes / 15.0, "td15", "blocks15")

    @property
    def td_acc(self):
        return self._rate(self.td_landed, self.td_att, "td_acc", "attempts")

    @property
    def td_def(self):
        k = SHRINK["attempts"]
        allowed = (self.td_conceded + k * (1 - PRIOR["td_def"])) / (self.td_faced + k)
        return 1.0 - allowed

    @property
    def sub15(self):
        return self._rate(self.sub_att, self.minutes / 15.0, "sub15", "blocks15")

    @property
    def ctrl_share(self):
        k = SHRINK["ctrl_sec"]
        return (self.ctrl_sec + k * PRIOR["ctrl_share"]) / (self.minutes * 60.0 + k)

    # ---- style shares: WHERE the offense goes, not how much of it there is.
    # Shrunk toward the league share on strike count, so a fighter with 20
    # landed strikes is not credited with a distinctive style.
    def _share(self, num, prior_key, k=60.0):
        return (num + k * PRIOR[prior_key]) / (self.ss_landed + k)

    @property
    def body_share(self):
        return self._share(self.body_landed, "body_share")

    @property
    def leg_share(self):
        return self._share(self.leg_landed, "leg_share")

    @property
    def clinch_share(self):
        return self._share(self.clinch_landed, "clinch_share")

    @property
    def ground_share(self):
        return self._share(self.ground_landed, "ground_share")

    @property
    def kd15(self):
        return self._rate(self.kd, self.minutes / 15.0, "kd15", "blocks15")

    @property
    def kd_against15(self):
        """Knockdowns conceded per 15 min — a more direct durability read than
        the share of career losses that ended by strikes."""
        return self._rate(self.kd_against, self.minutes / 15.0,
                          "kd_against15", "blocks15")

    # ---- fortitude -----------------------------------------------------
    # Everything here shrinks toward the NULL (no change, ratio 1.0) rather
    # than toward a league mean, because the quantity being measured is a
    # deviation. A fighter with two deep rounds on record should look average.
    _ACC_K = 45.0      # strike attempts of prior
    _RATE_K = 3.0      # rounds of prior
    _VOL = 22.0        # league significant-strike attempts per round
    _CTRL_R = 40.0     # league control seconds per round

    @property
    def late_acc_delta(self):
        """Striking accuracy in rounds 3+ minus accuracy in round 1.

        Chosen over volume retention because volume is confounded: an opponent
        retreating into a shell looks identical to a fighter tiring. Accuracy
        is a cleaner read on whether the hands still work late."""
        base = PRIOR["str_acc"]
        a1 = (self.r1_landed + self._ACC_K * base) / (self.r1_att + self._ACC_K)
        a3 = (self.late_landed + self._ACC_K * base) / (self.late_att + self._ACC_K)
        return a3 - a1

    def _per_round(self, tot, rounds, prior):
        return (tot + self._RATE_K * prior) / (rounds + self._RATE_K)

    @property
    def late_volume_ret(self):
        r1 = self._per_round(self.r1_att, self.n_fights, self._VOL)
        lt = self._per_round(self.late_att, self.deep_rounds, self._VOL)
        return lt / max(r1, 1e-6)

    @property
    def ctrl_retention(self):
        r1 = self._per_round(self.r1_ctrl, self.n_fights, self._CTRL_R)
        lt = self._per_round(self.late_ctrl, self.deep_rounds, self._CTRL_R)
        return lt / max(r1, 1e-6)

    @property
    def induced_decay(self):
        """How much this fighter's OPPONENTS slow down late. Below 1.0 means
        people fade against him — a pressure measure with no equivalent
        elsewhere in the feature set, and distinct from pacing himself."""
        r1 = self._per_round(self.opp_r1_att, self.n_fights, self._VOL)
        lt = self._per_round(self.opp_late_att, self.opp_deep_rounds, self._VOL)
        return lt / max(r1, 1e-6)

    @property
    def deep_experience(self):
        return math.log1p(self.deep_rounds)

    @property
    def kd_recovery(self):
        """Share of knockdowns absorbed that did not end the fight. Low sample
        for most fighters, hence heavy shrinkage toward the league rate."""
        k = 2.0
        return (self.kd_survived + k * 0.62) / (self.kd_absorbed + k)

    @property
    def finish_rate(self):
        k = SHRINK["fights"]
        fin = self.w_ko + self.w_sub
        return (fin + k * PRIOR["finish_rate"]) / (self.n_fights + k)

    @property
    def ko_loss_rate(self):
        """Durability proxy: share of career bouts ending in being finished by strikes."""
        k = SHRINK["fights"]
        return (self.l_ko + k * PRIOR["ko_loss_rate"]) / (self.n_fights + k)

    # ------------------------------------------------- strength of schedule
    @property
    def opp_str_def(self):
        return self.opp_str_def_sum / self.n_qual if self.n_qual else PRIOR["str_def"]

    @property
    def opp_td_def(self):
        return self.opp_td_def_sum / self.n_qual if self.n_qual else PRIOR["td_def"]

    @property
    def opp_elo(self):
        return self.opp_elo_sum / self.n_qual if self.n_qual else ELO_START

    # ------------------------------------------------- opponent-adjusted rates
    def _sos_mult(self, opp_def, league_def):
        """Fought stingy opposition -> raw output understates you -> scale up."""
        raw = (1.0 - league_def) / max(1e-6, (1.0 - opp_def))
        return min(ADJ_CLIP[1], max(ADJ_CLIP[0], raw))

    @property
    def adj_slpm(self):
        return self.slpm * self._sos_mult(self.opp_str_def, PRIOR["str_def"])

    @property
    def adj_sapm(self):
        """Absorbed rate adjusted for how hard opponents actually hit."""
        # Faced high-volume opponents -> absorbing more is expected -> scale down.
        return self.sapm

    @property
    def adj_td15(self):
        return self.td15 * self._sos_mult(self.opp_td_def, PRIOR["td_def"])

    # ------------------------------------------------------------ misc
    def age_on(self, d):
        # 5.3% of UFCStats fighters have no listed DOB, and a missing date
        # arrives as pandas NaT, which is not None — hence the != self check.
        # Falls back to the roster median age rather than propagating NaN.
        if self.dob is None or self.dob != self.dob or d is None or d != d:
            return 30.0
        return (d - self.dob).days / 365.25

    def layoff_days(self, d):
        if self.last_date is None or self.last_date != self.last_date \
                or d is None or d != d:
            return 365.0
        return max(0.0, (d - self.last_date).days)

    def snapshot(self, d):
        """Everything the model may look at, computed as of date `d`."""
        return {
            "elo": self.elo,
            "n_fights": self.n_fights,
            "win_pct": (self.wins + 2.0) / (self.n_fights + 4.0),
            "adj_slpm": self.adj_slpm,
            "sapm": self.sapm,
            "net_strike": self.adj_slpm - self.sapm,
            "str_acc": self.str_acc,
            "str_def": self.str_def,
            "adj_td15": self.adj_td15,
            "td_acc": self.td_acc,
            "td_def": self.td_def,
            "sub15": self.sub15,
            "ctrl_share": self.ctrl_share,
            "finish_rate": self.finish_rate,
            "ko_loss_rate": self.ko_loss_rate,
            "opp_elo": self.opp_elo,
            "reach": self.reach_in,
            "height": self.height_in,
            "age": self.age_on(d),
            "log_layoff": math.log1p(self.layoff_days(d)),
            "log_exp": math.log1p(self.n_fights),
            "southpaw": 1.0 if self.stance == "Southpaw" else 0.0,
            "body_share": self.body_share,
            "leg_share": self.leg_share,
            "clinch_share": self.clinch_share,
            "ground_share": self.ground_share,
            "kd15": self.kd15,
            "kd_against15": self.kd_against15,
            "late_acc_delta": self.late_acc_delta,
            "late_volume_ret": self.late_volume_ret,
            "ctrl_retention": self.ctrl_retention,
            "induced_decay": self.induced_decay,
            "deep_experience": self.deep_experience,
            "kd_recovery": self.kd_recovery,
        }

    # ------------------------------------------------------------ advance
    def update(self, *, date, minutes, ss_landed, ss_att, ss_absorbed, ss_faced,
               td_landed, td_att, td_conceded, td_faced, sub_att, ctrl_sec,
               result, method, opp_snapshot,
               head_landed=0.0, body_landed=0.0, leg_landed=0.0,
               dist_landed=0.0, clinch_landed=0.0, ground_landed=0.0,
               kd=0.0, kd_against=0.0,
               r1_landed=0.0, r1_att=0.0, late_landed=0.0, late_att=0.0,
               r1_ctrl=0.0, late_ctrl=0.0, deep_rounds=0.0,
               opp_r1_att=0.0, opp_late_att=0.0, **_ignored):
        self.minutes += minutes
        self.ss_landed += ss_landed
        self.ss_att += ss_att
        self.ss_absorbed += ss_absorbed
        self.ss_faced += ss_faced
        self.td_landed += td_landed
        self.td_att += td_att
        self.td_conceded += td_conceded
        self.td_faced += td_faced
        self.sub_att += sub_att
        self.ctrl_sec += ctrl_sec
        self.head_landed += head_landed
        self.body_landed += body_landed
        self.leg_landed += leg_landed
        self.dist_landed += dist_landed
        self.clinch_landed += clinch_landed
        self.ground_landed += ground_landed
        self.kd += kd
        self.kd_against += kd_against
        self.r1_landed += r1_landed
        self.r1_att += r1_att
        self.late_landed += late_landed
        self.late_att += late_att
        self.r1_ctrl += r1_ctrl
        self.late_ctrl += late_ctrl
        self.deep_rounds += deep_rounds
        self.opp_r1_att += opp_r1_att
        self.opp_late_att += opp_late_att
        self.opp_deep_rounds += deep_rounds
        if kd_against > 0:
            self.kd_absorbed += kd_against
            if not (result == "L" and method == "KO/TKO"):
                self.kd_survived += kd_against

        self.n_fights += 1
        if result == "W":
            self.wins += 1
            if method == "KO/TKO":
                self.w_ko += 1
            elif method == "SUB":
                self.w_sub += 1
            else:
                self.w_dec += 1
        elif result == "L":
            self.losses += 1
            if method == "KO/TKO":
                self.l_ko += 1

        # record who they faced, at the quality that opponent had that night
        self.opp_str_def_sum += opp_snapshot["str_def"]
        self.opp_td_def_sum += opp_snapshot["td_def"]
        self.opp_elo_sum += opp_snapshot["elo"]
        self.n_qual += 1

        self.last_date = date
