"""Adapter: Greco1899/scrape_ufc_stats CSV dump -> the two frames build() wants.

Everything messy about the real corpus is handled here so features.py never has
to know about it. Decisions made, and why:

IDENTITY. fight_id comes from the fight-details URL hash, which fight_results
already carries. fighter_id comes from the fighter-details URL hash in
ufc_fighter_tott. Names are used ONLY to join stats rows to that hash, never as
an identity — 7 names are shared by two different fighters (two Bruno Silvas,
two Jean Silvas, etc.) and keying on the string would silently merge two careers
into one and corrupt every career average. Those bouts are dropped instead.

CORNER ORDER. BOUT is "A vs. B" and splits cleanly for all 8,912 fights. OUTCOME
is W/L or L/W relative to the FIRST name, so the first name is the red corner.
Every stats row's FIGHTER value appears in its own BOUT string (verified 100%),
so corner assignment comes from position in that string.

JOIN KEYS. EVENT has inconsistent trailing whitespace across files, so all join
keys are whitespace-collapsed and casefolded. Original strings are kept for
display.
"""
import re

import numpy as np
import pandas as pd

# Methods that carry a skill signal. Anything else (DQ, Overturned, Could Not
# Continue) still advances a fighter's stat totals but is not trained on.
NO_CONTEST = {"DQ", "Overturned", "Could Not Continue", "Other"}


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def _of(series):
    """'34 of 78' -> (34, 78). Missing/malformed -> (0, 0)."""
    sp = series.fillna("").astype(str).str.extract(r"(\d+)\s*of\s*(\d+)")
    return (pd.to_numeric(sp[0], errors="coerce").fillna(0.0),
            pd.to_numeric(sp[1], errors="coerce").fillna(0.0))


def _clock(series):
    """'4:31' -> 271 seconds. '--' or blank -> 0."""
    sp = series.fillna("").astype(str).str.extract(r"(\d+):(\d+)")
    return (pd.to_numeric(sp[0], errors="coerce").fillna(0) * 60
            + pd.to_numeric(sp[1], errors="coerce").fillna(0))


def _round_lengths(fmt):
    """'3 Rnd (5-5-5)' -> [300,300,300]. '1 Rnd + OT (12-3)' -> [720,180]."""
    m = re.search(r"\(([\d\-\s]+)\)", str(fmt))
    if not m:
        return None
    try:
        return [int(x) * 60 for x in m.group(1).split("-") if x.strip()]
    except ValueError:
        return None


def _total_seconds(row):
    """Elapsed fight time: every completed round, plus time into the final one.
    Round lengths are read from TIME FORMAT rather than assumed to be 5:00 —
    early UFC had 12-minute rounds and 20-minute single rounds."""
    rnd = int(row["ROUND"]) if pd.notna(row["ROUND"]) else 1
    into = row["_time_sec"]
    lens = _round_lengths(row["TIME FORMAT"])
    if lens is None:
        prior = 300.0 * (rnd - 1)          # No Time Limit etc.
    else:
        prior = float(sum(lens[: rnd - 1])) if rnd > 1 else 0.0
    return prior + into


def _height_in(series):
    sp = series.fillna("").astype(str).str.extract(r"(\d+)'\s*(\d+)")
    return (pd.to_numeric(sp[0], errors="coerce") * 12
            + pd.to_numeric(sp[1], errors="coerce"))


def _method(m):
    m = str(m).strip()
    if m in NO_CONTEST:
        return None
    if m.startswith("Decision"):
        return "DEC"
    if "KO/TKO" in m or m.startswith("TKO"):
        return "KO/TKO"
    if m.startswith("Submission"):
        return "SUB"
    return None


DEFAULT_DIRS = ["data", "./data", "/mnt/user-data/uploads"]


def _resolve(d):
    """Find the CSVs. Defaults to ./data so the package works from a clone."""
    import os
    cands = [d] if d else DEFAULT_DIRS
    for c in cands:
        if c and os.path.exists(os.path.join(c, "ufc_fight_stats.csv")):
            return c
    raise FileNotFoundError(
        "Could not find ufc_fight_stats.csv. Put the four UFCStats CSVs in "
        "./data/ (see data/README.md) or pass upload_dir explicitly.")


def load(upload_dir=None, verbose=True):
    P = _resolve(upload_dir).rstrip("/") + "/"
    ev = pd.read_csv(P + "ufc_event_details.csv")
    fr = pd.read_csv(P + "ufc_fight_results.csv")
    fs = pd.read_csv(P + "ufc_fight_stats.csv")
    tt = pd.read_csv(P + "ufc_fighter_tott.csv")
    try:
        fdet = pd.read_csv(P + "ufc_fighter_details.csv")
    except FileNotFoundError:
        fdet = None
    log = []

    def note(msg):
        log.append(msg)
        if verbose:
            print(msg)

    # ---------------------------------------------------------- fighters
    tt["name"] = tt.FIGHTER.astype(str).str.strip()
    tt["fighter_id"] = tt.URL.str.rsplit("/", n=1).str[-1]
    tt["key"] = tt.name.map(_norm)

    ambiguous = set(tt.key[tt.key.duplicated(keep=False)])
    note(f"ambiguous names (same string, different fighter): {len(ambiguous)}")

    tt["height_in"] = _height_in(tt.HEIGHT)
    tt["reach_in"] = pd.to_numeric(
        tt.REACH.astype(str).str.extract(r"(\d+)")[0], errors="coerce")
    tt["stance"] = tt.STANCE.replace("", np.nan).fillna("Orthodox")
    tt["dob"] = pd.to_datetime(tt.DOB, format="mixed", errors="coerce")

    # Reach is missing for ~26% of fighters, and dropping them would cost a
    # quarter of the corpus. Impute from height (they correlate strongly) and
    # flag it, so the effect of the imputation can be measured later.
    fit = tt.dropna(subset=["height_in", "reach_in"])
    b, a = np.polyfit(fit.height_in, fit.reach_in, 1)
    tt["reach_imputed"] = tt.reach_in.isna()
    tt["reach_in"] = tt.reach_in.fillna(a + b * tt.height_in)
    note(f"reach imputed from height for {tt.reach_imputed.mean():.1%} "
         f"of fighters (reach ~ {a:.1f} + {b:.2f} x height)")

    med_h = tt.height_in.median()
    tt["height_in"] = tt.height_in.fillna(med_h)
    tt["reach_in"] = tt.reach_in.fillna(a + b * med_h)

    # Nicknames, joined on the fighter-details URL. Only used for resolving
    # upcoming cards: broadcasts and news sites list fighters by nickname
    # ("Patricio Pitbull") while UFCStats stores the legal name ("Patricio
    # Freire"), and without this the two never match.
    tt["nickname"] = ""
    if fdet is not None and "NICKNAME" in fdet.columns:
        fdet["fighter_id"] = fdet.URL.str.rsplit("/", n=1).str[-1]
        nick = dict(zip(fdet.fighter_id, fdet.NICKNAME.fillna("")))
        tt["nickname"] = tt.fighter_id.map(nick).fillna("")

    fighters = tt[["fighter_id", "name", "nickname", "height_in", "reach_in",
                   "stance", "dob", "reach_imputed"]].copy()
    name2id = dict(zip(tt.key[~tt.key.isin(ambiguous)],
                       tt.fighter_id[~tt.key.isin(ambiguous)]))

    # ---------------------------------------------------------- events
    ev["ekey"] = ev.EVENT.map(_norm)
    ev["date"] = pd.to_datetime(ev.DATE, format="mixed", errors="coerce")
    ekey2date = dict(zip(ev.ekey, ev.date))

    # ---------------------------------------------------------- fights
    fr["ekey"] = fr.EVENT.map(_norm)
    fr["bkey"] = fr.BOUT.map(_norm)
    fr["fight_id"] = fr.URL.str.rsplit("/", n=1).str[-1]
    fr["date"] = fr.ekey.map(ekey2date)
    n0 = len(fr)
    miss_date = fr.date.isna().sum()
    fr = fr.dropna(subset=["date"])
    note(f"dropped {miss_date} fights whose event is absent from "
         f"ufc_event_details (no date available)")

    parts = fr.BOUT.astype(str).str.split(" vs. ", n=1, expand=True)
    fr["r_name"], fr["b_name"] = parts[0].str.strip(), parts[1].str.strip()
    fr["r_key"], fr["b_key"] = fr.r_name.map(_norm), fr.b_name.map(_norm)

    amb = fr.r_key.isin(ambiguous) | fr.b_key.isin(ambiguous)
    note(f"dropped {int(amb.sum())} fights involving an ambiguous name "
         f"(cannot assign to the right career)")
    fr = fr[~amb]

    fr["r_id"] = fr.r_key.map(name2id)
    fr["b_id"] = fr.b_key.map(name2id)
    noid = fr.r_id.isna() | fr.b_id.isna()
    note(f"dropped {int(noid.sum())} fights with a fighter missing from "
         f"ufc_fighter_tott (no physicals)")
    fr = fr[~noid]

    out = fr.OUTCOME.astype(str).str.strip()
    fr["winner"] = np.select(
        [out == "W/L", out == "L/W", out == "D/D"],
        ["r", "b", "draw"], default="nc")

    fr["method"] = fr.METHOD.map(_method)
    unscored = fr.method.isna()
    note(f"{int(unscored.sum())} fights have a non-skill outcome "
         f"(DQ / Overturned / Could Not Continue) -> kept as no-contest")
    fr.loc[unscored, "winner"] = "nc"
    fr["method"] = fr.method.fillna("DEC")

    # Scheduled length, needed by the hazard model: a fight that went 10:00 of
    # a five-round bout is censored at 25 minutes, not 15, and treating those
    # the same corrupts every survival estimate.
    def _sched(fmt):
        lens = _round_lengths(fmt)
        return float(sum(lens)) if lens else 900.0
    fr["sched_sec"] = fr["TIME FORMAT"].map(_sched)

    fr["_time_sec"] = _clock(fr.TIME)
    fr["total_sec"] = fr.apply(_total_seconds, axis=1)
    bad_t = (fr.total_sec <= 0) | fr.total_sec.isna()
    note(f"dropped {int(bad_t.sum())} fights with unparseable duration")
    fr = fr[~bad_t]

    # ---------------------------------------------------------- per-fight stats
    fs = fs.dropna(subset=["FIGHTER"]).copy()
    fs["ekey"] = fs.EVENT.map(_norm)
    fs["bkey"] = fs.BOUT.map(_norm)
    fs["fkey"] = fs.FIGHTER.map(_norm)
    fs = fs.drop_duplicates(subset=["ekey", "bkey", "ROUND", "fkey"])

    fs["ss_landed"], fs["ss_att"] = _of(fs["SIG.STR."])
    fs["td_landed"], fs["td_att"] = _of(fs["TD"])
    fs["sub_att"] = pd.to_numeric(fs["SUB.ATT"], errors="coerce").fillna(0.0)
    fs["ctrl_sec"] = _clock(fs["CTRL"])
    # Target and position breakdowns. UFCStats populates these for 99.9% of
    # rounds and they are the only window it offers onto HOW a fighter strikes
    # rather than how much: where the strikes go (head/body/leg) and from what
    # range (distance/clinch/ground). No sequence data exists at any
    # granularity, so combinations are not recoverable — these shares are the
    # closest available proxy for style.
    for src, dst in (("HEAD", "head"), ("BODY", "body"), ("LEG", "leg"),
                     ("DISTANCE", "dist"), ("CLINCH", "clinch"),
                     ("GROUND", "ground")):
        fs[f"{dst}_landed"], fs[f"{dst}_att"] = _of(fs[src])
    fs["kd"] = pd.to_numeric(fs["KD"], errors="coerce").fillna(0.0)
    fs["rev"] = pd.to_numeric(fs["REV."], errors="coerce").fillna(0.0)

    # --- round-phase splits, for the fortitude cluster -------------------
    # Round 1 versus rounds 3+ is the only view of a fight's trajectory this
    # data supports. Round 2 is deliberately excluded from both sides: it is
    # transitional, and including it blurs exactly the contrast being measured.
    fs["_rd"] = pd.to_numeric(
        fs.ROUND.astype(str).str.extract(r"(\d+)")[0], errors="coerce")
    ph = []
    for tag, mask in (("r1", fs._rd == 1), ("late", fs._rd >= 3)):
        g = (fs[mask].groupby(["ekey", "bkey", "fkey"], as_index=False)
             [["ss_landed", "ss_att", "ctrl_sec", "kd"]].sum()
             .rename(columns={c: f"{tag}_{c}" for c in
                              ("ss_landed", "ss_att", "ctrl_sec", "kd")}))
        ph.append(g)
    rounds = (fs.groupby(["ekey", "bkey", "fkey"], as_index=False)
              ._rd.max().rename(columns={"_rd": "n_rounds"}))
    rounds["deep_rounds"] = (rounds.n_rounds - 2).clip(lower=0)

    agg = (fs.groupby(["ekey", "bkey", "fkey"], as_index=False)
             [["ss_landed", "ss_att", "td_landed", "td_att", "sub_att", "ctrl_sec",
               "kd", "rev"]
              + [f"{d}_{k}" for d in ("head", "body", "leg", "dist", "clinch",
                                      "ground") for k in ("landed", "att")]]
             .sum())

    for extra in ph + [rounds]:
        agg = agg.merge(extra, on=["ekey", "bkey", "fkey"], how="left")
    PHASE = [f"{t}_{c}" for t in ("r1", "late")
             for c in ("ss_landed", "ss_att", "ctrl_sec", "kd")]
    agg[PHASE] = agg[PHASE].fillna(0.0)
    agg[["n_rounds", "deep_rounds"]] = agg[["n_rounds", "deep_rounds"]].fillna(1.0)

    COLS = (["ss_landed", "ss_att", "td_landed", "td_att", "sub_att", "ctrl_sec",
             "kd", "rev", "n_rounds", "deep_rounds"]
            + PHASE
            + [f"{d}_{k}" for d in ("head", "body", "leg", "dist", "clinch",
                                    "ground") for k in ("landed", "att")])
    for corner in ("r", "b"):
        a = agg.rename(columns={c: f"{corner}_{c}" for c in COLS})
        fr = fr.merge(a, left_on=["ekey", "bkey", f"{corner}_key"],
                      right_on=["ekey", "bkey", "fkey"], how="left").drop(
                          columns=["fkey"])

    no_stats = fr.r_ss_att.isna() | fr.b_ss_att.isna()
    note(f"dropped {int(no_stats.sum())} fights with no round-by-round stats "
         f"(mostly pre-2001 events, which UFCStats never tracked)")
    fr = fr[~no_stats]

    fights = fr[["fight_id", "date", "r_id", "b_id", "winner", "method",
                 "total_sec", "sched_sec", "WEIGHTCLASS", "EVENT", "BOUT"]
                + [f"{c}_{s}" for c in ("r", "b") for s in COLS]].copy()
    fights = fights.rename(columns={"WEIGHTCLASS": "weight_class",
                                    "EVENT": "event", "BOUT": "bout"})
    fights = fights.sort_values(["date", "fight_id"]).reset_index(drop=True)

    fighters = fighters[fighters.fighter_id.isin(
        set(fights.r_id) | set(fights.b_id))].reset_index(drop=True)

    note(f"\nusable corpus: {len(fights)} fights of {n0} rows "
         f"({len(fights)/n0:.1%}), {len(fighters)} fighters, "
         f"{fights.date.min().date()} -> {fights.date.max().date()}")
    return fights, fighters, log


if __name__ == "__main__":
    f, p, _ = load()
    print()
    print(f[["date", "bout", "winner", "method", "total_sec",
             "r_ss_landed", "b_ss_landed", "r_ctrl_sec"]].tail(5).to_string(index=False))
