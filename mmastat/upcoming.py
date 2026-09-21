"""Turn an upcoming fight card into predictions.

On retrieval. There is no reliable free machine-readable feed of upcoming UFC
cards. UFCStats' own upcoming page sits behind a JavaScript interstitial (a
plain request returns "Checking your browser..."), the Kaggle `upcoming.csv`
turned out to be a frozen snapshot months out of date, and every other listing
is a news site whose HTML changes without notice. A scraper built on any of
those is a thing that breaks silently and leaves you showing stale fights.

So the input here is a plain text file you edit by hand, `data/upcoming.txt`:

    # UFC 331 | 2026-09-19 | Crypto.com Arena, Los Angeles
    Joshua Van vs. Alexandre Pantoja
    Arman Tsarukyan vs. Mauricio Ruffy
    ...

Thirteen lines copied off any fight card takes a minute, needs no terminal, and
cannot break. `fetch_ufcstats_upcoming()` is provided for when you want to
automate it, but it is unverified from this environment and the manual path is
the one that will still work next year.

Name resolution is the real problem, not retrieval. Newcomers have no history
(Patricio Pitbull's UFC debut has no UFCStats record however famous he is), and
spellings drift between sources. Unresolved and insufficient-history fighters
are reported explicitly rather than silently defaulted to league averages,
because a confident-looking 50/50 on a debut is worse than saying "no read".
"""
import difflib
import re
import unicodedata

import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

from .features import (PANEL_OWN, WIN_FEATURES, _init_states, build,
                       elo_eff, make_features)
from .projections import FEATS as PROJ_FEATS, RANGE_TARGETS, EVENT_TARGETS

MIN_PRIOR = 2


def _key(s):
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", re.sub(r"\s+", " ", s).strip().lower())


SEGMENTS = ["Main card", "Prelims", "Early prelims"]


def parse_card(path):
    """Returns (meta, [(name_a, name_b, rounds, segment)]).

    `## Main card` / `## Prelims` / `## Early prelims` lines set the segment
    for everything under them. A file with no markers is all Main card, which
    keeps older hand-written files working."""
    meta, bouts = {}, []
    segment = "Main card"
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            seg = line.lstrip("#").strip()
            segment = next((s2 for s2 in SEGMENTS if s2.lower() == seg.lower()), seg)
            continue
        if line.startswith("#"):
            parts = [p.strip() for p in line.lstrip("#").split("|")]
            for k, v in zip(("event", "date", "venue"), parts):
                meta[k] = v
            continue
        rounds = 3
        if "|" in line:                      # "A vs. B | 5" marks a 5-rounder
            line, tail = line.rsplit("|", 1)
            if tail.strip().startswith("5"):
                rounds = 5
        m = re.split(r"\s+vs\.?\s+", line, maxsplit=1, flags=re.I)
        if len(m) == 2:
            bouts.append((m[0].strip(), m[1].strip(), rounds, segment))
    return meta, bouts


def _alias_table(fighters):
    """Every string a source might plausibly use for a fighter.

    Three real failure modes this fixes, all observed on a live card:
      - nickname for surname: "Patricio Pitbull" is UFCStats' "Patricio Freire"
      - spacing: "Joo Sang Yoo" is stored as "JooSang Yoo"
      - accents: handled upstream in _key via NFKD stripping
    """
    table = {}

    def add(s, fid):
        k = _key(s)
        if not k or len(k) < 4:
            return
        table.setdefault(k, fid)
        table.setdefault(k.replace(" ", ""), fid)   # spacing-insensitive

    nicks = fighters.nickname if "nickname" in fighters.columns else [""] * len(fighters)
    for name, nick, fid in zip(fighters.name, nicks, fighters.fighter_id):
        add(name, fid)
        parts = str(name).split()
        nick = str(nick or "").strip()
        if nick and parts:
            add(f"{parts[0]} {nick}", fid)          # first name + nickname
            add(f"{nick} {parts[-1]}", fid)         # nickname + surname
    return table


def load_aliases(path="data/aliases.txt"):
    """Manual name overrides, one per line: `Card name = UFCStats name`.

    Needed because some cases are genuinely unresolvable automatically and
    dangerous to guess. Live example: news sources list "Patricio Pitbull",
    UFCStats stores "Patricio Freire", and his BROTHER "Patricky Freire" is the
    one actually carrying the nickname "Pitbull" in the details file. Fuzzy
    matching resolves that to the wrong man. Refusing and letting you state the
    mapping is the safe behaviour.
    """
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            a, b = line.split("=", 1)
            out[_key(a)] = _key(b)
    except FileNotFoundError:
        pass
    return out


def resolve(names, fighters, cutoff=0.88, alias_path="data/aliases.txt"):
    """Map display names onto fighter_ids. Exact normalised match, then the
    alias forms, then a close-match; anything below `cutoff` is returned as
    unresolved rather than guessed at."""
    table = _alias_table(fighters)
    manual = load_aliases(alias_path)
    keys = list(table)
    out, unresolved = {}, []
    for n in names:
        k = manual.get(_key(n), _key(n))
        if k in table:
            out[n] = table[k]
            continue
        if k.replace(" ", "") in table:
            out[n] = table[k.replace(" ", "")]
            continue
        near = difflib.get_close_matches(k, keys, n=1, cutoff=cutoff)
        if near:
            out[n] = table[near[0]]
        else:
            unresolved.append(n)
    return out, unresolved


def _states_after(fights, fighters):
    """Replay the whole corpus and return each fighter's state as it stands
    after their last recorded bout. Uses the same _apply() the training walk
    uses, so an upcoming-fight snapshot is built exactly like a historical
    one."""
    from .features import _apply
    states = _init_states(fighters)
    for f in fights.sort_values(["date", "fight_id"], kind="mergesort").itertuples():
        A, B = states[f.r_id], states[f.b_id]
        sa, sb = A.snapshot(f.date), B.snapshot(f.date)
        sa["elo"], sb["elo"] = elo_eff(A, f.date), elo_eff(B, f.date)
        _apply(f, A, B, sa, sb)
    return states


def _market_for_card(card_path, odds_path="data/ufc_betting_odds_daily.csv"):
    """Devigged consensus price per bout, if the odds feed happens to be present.

    Optional by design: that file is a Kaggle download with no public raw URL,
    so the daily refresh job cannot fetch it and the site simply omits the
    market comparison when it is absent. Better a missing panel than a stale
    price presented as current.
    """
    import os
    # The ledger FIRST. The capture job has already paid for these prices and
    # written them down; refresh making its own call was buying the same
    # numbers twice, at 3 credits a day for nothing. Latest capture per bout.
    try:
        from .ledger import read as _lread
        L = _lread()
        if not L.empty and "venue" in L.columns:
            L = L[L.venue == "sportsbook"].sort_values("captured_utc")
            last = L.groupby(["fighter", "opponent"]).tail(1)
            out = {}
            for r in last.itertuples():
                k = frozenset((_key(r.fighter), _key(r.opponent)))
                if k not in out:
                    out[k] = (_key(r.fighter), float(r.p_market_devig), r.books)
            # Use the ledger only if it actually covers THIS card. The first
            # version returned early whenever the ledger held any prices at
            # all — so right after a card advanced, the ledger held only the
            # finished card's bouts, matched none of the new ones, and the
            # site showed no market prices while never falling back to the API.
            try:
                _, card = parse_card(card_path)
                want = {frozenset((_key(a), _key(b))) for a, b, *_ in card}
                have = sum(1 for k in want if k in out)
                cover = have / len(want) if want else 0.0
            except Exception:
                cover = 0.0
            if out and cover >= 0.5:
                return out, f"ledger (latest capture, {cover:.0%} of card)"
            if out:
                print(f"note: ledger covers only {cover:.0%} of this card - "
                      f"fetching live instead")
    except Exception as e:
        print(f"note: ledger prices unavailable ({e})")
    # Only if the ledger has nothing — e.g. a card listed before its first
    # capture — spend a call on it.
    try:
        from .odds_live import fetch_moneylines
        live = fetch_moneylines()
        if live:
            return live, "the-odds-api"
    except Exception as e:
        print(f"note: live odds unavailable ({e})")
    if not os.path.exists(odds_path):
        return {}, None
    try:
        from .odds_daily import load_raw, _nm, BACKFILL_SOURCE
        d = load_raw(odds_path)
        B = d[(d.source != BACKFILL_SOURCE) & d.odds_1.notna() & d.odds_2.notna()]
        if B.empty:
            return {}, None
        B = B.sort_values("snap")
        out = {}
        for (k1, k2), g in B.groupby([B.fighter_1.map(_nm), B.fighter_2.map(_nm)]):
            last = g.groupby("source").last()
            o1, o2 = last.odds_1.median(), last.odds_2.median()
            p1 = (1 / o1) / (1 / o1 + 1 / o2)
            out[frozenset((k1, k2))] = (k1, float(p1), int(last.shape[0]))
        return out, "local csv"
    except Exception:
        return {}, None


def predict_card(path, fights, fighters, verbose=True):
    from .projections import fit_range, predict_range
    from .props import symmetric_features, prop_targets, BINARY
    from sklearn.ensemble import HistGradientBoostingClassifier

    market, market_src = _market_for_card(path)
    MARKET_SRC["src"] = market_src
    meta, bouts = parse_card(path)
    as_of = pd.Timestamp(meta.get("date") or fights.date.max())
    states = _states_after(fights, fighters)

    names = sorted({n for b in bouts for n in b[:2]})
    ids, unresolved = resolve(names, fighters)

    # --- human-readable names for the win-model features, used to explain
    # each pick. A price with no reasoning is what the market already sells;
    # the reasoning is the only thing this site has that they do not.
    LABELS = {
        "d_elo": "career quality", "d_opp_elo": "strength of schedule",
        "d_log_exp": "experience", "d_adj_slpm": "striking output",
        "d_sapm": "strikes absorbed", "d_str_acc": "striking accuracy",
        "d_str_def": "striking defence", "d_reach": "reach",
        "d_age": "age", "d_log_layoff": "layoff",
        "d_ko_loss_rate": "durability", "grapple_edge": "takedown threat",
        "ko_edge": "knockout threat", "sub_edge": "submission threat",
    }

    # --- win model, trained on everything available
    X, _ = build(fights, fighters)
    d = X[(X.min_prior >= MIN_PRIOR) & (X.date >= pd.Timestamp("2012-01-01"))]
    sc = StandardScaler().fit(d[WIN_FEATURES])
    win = LogisticRegression(max_iter=4000, C=0.5, fit_intercept=False).fit(
        sc.transform(d[WIN_FEATURES]), d.y)

    # --- projection + prop models
    from .features import build_panel
    P = build_panel(fights, fighters)
    P = P[P.date >= pd.Timestamp("2012-01-01")].sort_values("date")
    vcut = P.date.quantile(0.85)
    ptr, pval = P[P.date <= vcut], P[P.date > vcut]
    rng_models = {t: fit_range(ptr, pval, t, two_sided=cfg[4])
                  for t, cfg in RANGE_TARGETS.items()}
    evt_models = {}
    for t in EVENT_TARGETS:
        evt_models[t] = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
            min_samples_leaf=40, l2_regularization=1.0,
            random_state=7).fit(P[PROJ_FEATS], (P[t] > 0).astype(int))

    # --- competing-risks model for method and round, fitted once
    from .survival import (expand as sv_expand, fit as sv_fit, CLASSES,
                           calibrate_hazards, distribution, INTERVAL)
    from .features import WIN_FEATURES as WF
    sv_tr, sv_feats = sv_expand(fights, fighters, full=False)
    sv_pr, _ = sv_expand(fights, fighters, full=True)
    _vc = sv_tr.date.quantile(0.85)
    sv_model, sv_cols = sv_fit(sv_tr[sv_tr.date <= _vc], sv_feats)
    sv_cal = calibrate_hazards(sv_model, sv_cols, sv_pr[sv_pr.date > _vc],
                               fights.set_index("fight_id"))
    sum_keys = [c for c in sv_feats if c.startswith("sum_")]

    def method_round(sa, sb, n_rounds):
        row = {f"d_{k.replace('d_', '')}": 0.0 for k in []}
        fv = make_features(sa, sb)
        base = {c: fv[c] for c in WF}
        base.update({f"sum_{k}": sa[k] + sb[k] for k in PANEL_OWN
                     if f"sum_{k}" in sum_keys})
        n = int(n_rounds * 5)
        grid = pd.DataFrame([{**base, "t": t, "rnd": min(5, (t - 1) // 5 + 1)}
                             for t in range(1, n + 1)])
        H = sv_model.predict_proba(grid[sv_cols])
        H = H.copy()
        H[:, 1:3] *= sv_cal[0]
        H[:, 3:5] *= sv_cal[1]
        H[:, 0] = np.maximum(H[:, 0], 1e-9)
        H /= H.sum(axis=1, keepdims=True)
        surv = 1.0
        out = {k: 0.0 for k in CLASSES[1:]}
        by_r = {}
        curve = []
        for i in range(len(H)):
            r = int(grid.rnd.iloc[i])
            stop = 0.0
            for j, k in enumerate(CLASSES[1:], start=1):
                v = surv * H[i, j]
                out[k] += v
                stop += v
            by_r[r] = by_r.get(r, 0.0) + stop
            surv *= H[i, 0]
            curve.append(surv)          # P(still going after minute i+1)
        out["decision"] = surv
        return out, by_r, curve

    rows, skipped, _snap = [], [], {}
    for na, nb, n_rounds, segment in bouts:
        if na not in ids or nb not in ids:
            skipped.append((na, nb, "name not in UFCStats corpus", segment))
            continue
        A, B = states[ids[na]], states[ids[nb]]
        if min(A.n_fights, B.n_fights) < MIN_PRIOR:
            skipped.append((na, nb,
                            f"insufficient history ({A.n_fights} / {B.n_fights} prior bouts)",
                            segment))
            continue
        sa, sb = A.snapshot(as_of), B.snapshot(as_of)
        sa["elo"], sb["elo"] = elo_eff(A, as_of), elo_eff(B, as_of)
        _snap[na], _snap[nb] = sa, sb

        fv = make_features(sa, sb)
        x = pd.DataFrame([fv])[WIN_FEATURES]
        p_a = float(win.predict_proba(sc.transform(x))[0, 1])

        # per-feature push on the log-odds: coefficient x standardised value
        z = sc.transform(x)[0]
        contrib = sorted(
            [(WIN_FEATURES[i], float(win.coef_[0][i] * z[i])) for i in range(len(WIN_FEATURES))],
            key=lambda kv: -abs(kv[1]))

        r = dict(bout=f"{na} vs. {nb}", a=na, b=nb, rounds=n_rounds,
                 segment=segment,
                 p_a=round(p_a, 4), p_b=round(1 - p_a, 4))
        # Raw pre-fight numbers. Without these the drawer can say "age pushes
        # +0.90" and never that one man is 36 and the other 24 — the model
        # shows its reasoning in standard deviations, which is the wrong unit
        # for a reader.
        def tot(S, W, L, N):
            return {"record": f"{W}-{L}", "n_fights": N,
                    "age": round(S["age"], 1), "height": round(S["height"], 1),
                    "reach": round(S["reach"], 1), "stance": S.get("stance_name", ""),
                    "elo": round(S["elo"]), "opp_elo": round(S["opp_elo"]),
                    "slpm": round(S["adj_slpm"], 2), "sapm": round(S["sapm"], 2),
                    "str_acc": round(S["str_acc"], 3), "str_def": round(S["str_def"], 3),
                    "td15": round(S["adj_td15"], 2), "td_def": round(S["td_def"], 3),
                    "sub15": round(S["sub15"], 2), "ctrl": round(S["ctrl_share"], 3),
                    "kd15": round(S["kd15"], 2), "ko_loss": round(S["ko_loss_rate"], 3)}
        sa["stance_name"] = A.stance
        sb["stance_name"] = B.stance
        r["stats"] = {"a": tot(sa, A.wins, A.losses, A.n_fights),
                      "b": tot(sb, B.wins, B.losses, B.n_fights)}
        # How much evidence stands behind this number at all.
        r["evidence"] = {"a": A.n_fights, "b": B.n_fights,
                         "min": min(A.n_fights, B.n_fights)}

        r["drivers"] = [{"label": LABELS.get(k, k), "value": round(v, 4),
                         "favours": "a" if v > 0 else "b", "key": k}
                        for k, v in contrib[:6] if abs(v) > 0.01]
        hit = market.get(frozenset((_key(na), _key(nb))))
        if hit:
            src, pm, nbooks = hit
            p_mkt = pm if src == _key(na) else 1 - pm
            r["p_market"] = round(p_mkt, 4)
            r["books"] = nbooks
            r["edge"] = round(p_a - p_mkt, 4)

        try:
            dist, by_r, curve = method_round(sa, sb, n_rounds)
            r["m_a_ko"] = round(dist["a_ko"], 4)
            r["m_b_ko"] = round(dist["b_ko"], 4)
            r["m_a_sub"] = round(dist["a_sub"], 4)
            r["m_b_sub"] = round(dist["b_sub"], 4)
            r["m_decision"] = round(dist["decision"], 4)
            r["p_finish"] = round(1 - dist["decision"], 4)
            for rr in range(1, n_rounds + 1):
                r[f"p_end_r{rr}"] = round(by_r.get(rr, 0.0), 4)
            # Round totals, read straight off the survival curve. These are
            # real prop markets and the hazard model prices them coherently:
            # "over 1.5 rounds" is simply P(the fight is still going at 7:30).
            # No free feed quotes MMA props, so there is nothing to compare
            # them against — they are projections, labelled as such.
            def surv_at(minutes):
                i = int(minutes) - 1
                if i < 0:
                    return 1.0
                if i >= len(curve):
                    return curve[-1]
                lo = curve[i]
                if minutes == int(minutes):
                    return lo
                hi = curve[i + 1] if i + 1 < len(curve) else curve[-1]
                return lo + (hi - lo) * (minutes - int(minutes))
            r["totals"] = {}
            for line in (1.5, 2.5, 3.5, 4.5):
                if line > n_rounds:
                    continue
                r["totals"][f"over_{str(line).replace('.', '_')}"] = \
                    round(float(surv_at(line * 5.0)), 4)
        except Exception as e:
            r["method_error"] = str(e)[:80]
        for who, me, op in (("a", sa, sb), ("b", sb, sa)):
            row = {f"own_{k}": me[k] for k in PANEL_OWN}
            row.update({f"opp_{k}": op[k] for k in PANEL_OWN})
            xx = pd.DataFrame([row])[PROJ_FEATS]
            for t, (models, k) in rng_models.items():
                lo, mid, hi = predict_range(models, k, xx, RANGE_TARGETS[t][4])
                nm = "ctrl" if t == "y_ctrl_pm" else "slpm"
                r[f"{who}_{nm}_lo"] = round(float(lo[0]), 2)
                r[f"{who}_{nm}_mid"] = round(float(mid[0]), 2)
                r[f"{who}_{nm}_hi"] = round(float(hi[0]), 2)
            for t, (lab, _) in EVENT_TARGETS.items():
                r[f"{who}_p_{lab}"] = round(
                    float(evt_models[t].predict_proba(xx)[0, 1]), 3)
        rows.append(r)

    # Results, once the corpus has them. Not live: the UFCStats CSVs refresh
    # roughly a day after an event, so a card grades the morning after rather
    # than round by round. Showing a half-filled live card would mean inventing
    # a results feed we do not have.
    from .upcoming import _key as _k2
    done = {}
    _parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    for r_, b_, w, mth, dt, ts in zip(_parts[0], _parts[1], fights.winner,
                                      fights.method, fights.date, fights.total_sec):
        done[frozenset((_k2(r_), _k2(b_)))] = (_k2(r_), w, mth, dt, ts)
    for r in rows:
        hit = done.get(frozenset((_key(r["a"]), _key(r["b"]))))
        if not hit:
            continue
        red, w, mth, dt, ts = hit
        if abs((pd.Timestamp(dt) - as_of).days) > 4 or w not in ("r", "b"):
            continue
        a_won = (w == "r") == (_key(r["a"]) == red)
        r["result"] = {"winner": r["a"] if a_won else r["b"], "method": mth,
                       "seconds": int(ts),
                       "model_right": bool((r["p_a"] > 0.5) == a_won)}

    # Conditional base rates, plus where THIS bout sits on each condition.
    # Locating a fight on a fixed list is not selection — the list is frozen
    # in PREREGISTRATION addendum 10 and every entry renders every time.
    try:
        from .baserates import write_json as _br_write, band_for, CONDITIONS
        _tbl = _br_write(fights, fighters, verbose=False)
        _by = {t["id"]: t for t in _tbl}
        for r in rows:
            sa2 = _snap.get(r["a"])
            sb2 = _snap.get(r["b"])
            if not sa2 or not sb2:
                continue
            # Fighter-level conditions get a band for EACH corner. Showing
            # only one silently answered "whose takedown rate?" with "the red
            # corner's", which is not something a reader can infer.
            per_fighter = {
                "C1": (sa2["adj_td15"], sb2["adj_td15"]),
                "C2": (sb2["td_def"], sa2["td_def"]),      # the one in front of you
                "C3": (sa2["kd15"], sb2["kd15"]),
                "C6": (sa2["reach"] - sb2["reach"], sb2["reach"] - sa2["reach"]),
                "C10": (sa2["clinch_share"] + sa2["ground_share"],
                        sb2["clinch_share"] + sb2["ground_share"]),
            }
            per_fight = {
                "C4": sa2["kd15"] + sb2["kd15"],
                "C5": sa2["ctrl_share"] + sb2["ctrl_share"],
                "C7": sa2["adj_slpm"] + sb2["adj_slpm"],
                "C8": abs(sa2["age"] - sb2["age"]),
                "C9": sa2["sub15"] + sb2["sub15"],
            }
            bands = {}
            for k, (va, vb) in per_fighter.items():
                if k in _by:
                    bands[k] = {"a": band_for(va, _by[k]), "b": band_for(vb, _by[k])}
            for k, v in per_fight.items():
                if k in _by:
                    bands[k] = {"fight": band_for(v, _by[k])}
            r["bands"] = bands
    except Exception as e:
        print(f"note: base rates unavailable ({e})")

    # Opening price: the FIRST ledger capture for each bout. The market is far
    # sharper at the close than the open (log loss 0.597 -> 0.567), and the
    # exploratory finding in PREREGISTRATION addendum 13 is that the model
    # anticipates which way it moves. Showing where the line started is what
    # lets a reader see that, bout by bout.
    try:
        from .ledger import read as _lread
        L = _lread()
        if not L.empty and "venue" in L.columns:
            _ed = pd.to_datetime(meta.get("date"), errors="coerce")
            _d = pd.to_datetime(L.event_date, errors="coerce")
            L = L[(L.venue == "sportsbook") & ((_d - _ed).abs() <= pd.Timedelta(days=1))]
            L = L.sort_values("captured_utc")
            first = L.groupby(["fighter", "opponent"]).p_market_devig.first()
            for r in rows:
                v = first.get((r["a"], r["b"]))
                if v is None:
                    v2 = first.get((r["b"], r["a"]))
                    v = (1 - v2) if v2 is not None else None
                if v is not None:
                    r["p_open"] = round(float(v), 4)
    except Exception as e:
        print(f"note: opening prices unavailable ({e})")

    # Measured reliability, recomputed each run so the site quotes its own
    # current track record rather than a figure hard-coded months ago.
    try:
        from .reliability import load_or_build
        TRACK["rec"] = load_or_build(fights, fighters)
    except Exception as e:
        print(f"note: track record unavailable ({e})")

    out = pd.DataFrame(rows)
    if verbose:
        print(f"{meta.get('event','(card)')} | {meta.get('date','')} | "
              f"{meta.get('venue','')}")
        n_mkt = int(out.p_market.notna().sum()) if "p_market" in out.columns else 0
        print(f"{len(bouts)} bouts parsed, {len(out)} predicted, {len(skipped)} skipped")
        print(f"market prices: {n_mkt} bouts from "
              f"{MARKET_SRC.get('src') or 'no source (set ODDS_API_KEY)'}\n")
        if len(out):
            cc = [c for c in ["bout", "p_a", "p_b", "p_finish", "m_decision",
                              "a_p_takedown", "b_p_takedown"]
                  if c in out.columns]
            show = out[cc].copy()
            for c in ("p_a", "p_b", "p_finish", "m_decision"):
                if c in show:
                    show[c] = (show[c] * 100).round(1)
            print(show.to_string(index=False))
        if skipped:
            print("\nNO READ (reported, not guessed):")
            for na, nb, why, _seg in skipped:
                print(f"  {na} vs. {nb}  --  {why}")
        if unresolved:
            print("\nunmatched names:", ", ".join(unresolved))
    return out, skipped, unresolved


def fetch_ufcstats_upcoming(url="http://ufcstats.com/statistics/events/upcoming"):
    """UNVERIFIED. UFCStats served a JavaScript interstitial when tested from a
    datacenter IP; it may work from a residential connection. Returns a list of
    (name_a, name_b) or raises. Prefer the manual text file."""
    import requests
    from bs4 import BeautifulSoup
    html = requests.get(url, timeout=20, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/127.0 Safari/537.36")}).text
    if "Checking your browser" in html or "requires JavaScript" in html:
        raise RuntimeError("UFCStats served a bot-check page; use data/upcoming.txt")
    soup = BeautifulSoup(html, "html.parser")
    ev = soup.select_one("a.b-link")
    if ev is None:
        raise RuntimeError("could not find an upcoming event link")
    page = requests.get(ev["href"], timeout=20).text
    s2 = BeautifulSoup(page, "html.parser")
    bouts = []
    for row in s2.select("tr.b-fight-details__table-row"):
        ps = [a.get_text(strip=True) for a in row.select("a.b-link")]
        if len(ps) >= 2:
            bouts.append((ps[0], ps[1]))
    return bouts


MARKET_SRC = {"src": None}
TRACK = {"rec": None}


def archive_previous(new_event, path="site/predictions.json",
                     archive_dir="site/archive", verbose=True):
    """Keep the outgoing card before it is overwritten.

    `refresh_upcoming` advances data/upcoming.txt to the next event the moment
    Wikipedia lists one, so predictions.json is rebuilt for the new card and
    the finished one vanishes — usually before its results have even reached
    the corpus. Grading only ever worked in the overlap between "card is over"
    and "card is still the current file", which is frequently empty. Archiving
    on event change removes the race entirely.
    """
    import json
    import re
    from pathlib import Path
    src = Path(path)
    if not src.exists():
        return None
    try:
        old = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return None
    ev = old.get("event")
    if not ev or ev == new_event:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(ev).lower()).strip("-")
    dest = Path(archive_dir) / f"{slug}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(old, indent=1), encoding="utf-8")
    if verbose:
        print(f"archived previous card '{ev}' -> {dest}")
    return str(dest)


def grade_archive(fights, archive_dir="site/archive", out_path="site/history.json",
                  keep=8, verbose=True):
    """Grade archived cards against the corpus and write the history file.

    Re-graded from scratch every run: a card archived before its results
    existed simply grades on a later pass, with no state to get stuck.
    """
    import json
    from pathlib import Path
    d = Path(archive_dir)
    if not d.exists():
        return None
    done = {}
    parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    for r_, b_, w, mth, dt, ts in zip(parts[0], parts[1], fights.winner,
                                      fights.method, fights.date, fights.total_sec):
        done[frozenset((_key(r_), _key(b_)))] = (_key(r_), w, mth, dt, int(ts))

    cards = []
    for fp in sorted(d.glob("*.json")):
        try:
            card = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        ev_date = pd.to_datetime(card.get("date"), errors="coerce")
        graded, right = [], 0
        for b in card.get("bouts", []):
            hit = done.get(frozenset((_key(b["a"]), _key(b["b"]))))
            if not hit:
                continue
            red, w, mth, dt, ts = hit
            if w not in ("r", "b"):
                continue
            if pd.notna(ev_date) and abs((pd.Timestamp(dt) - ev_date).days) > 4:
                continue
            a_won = (w == "r") == (_key(b["a"]) == red)
            ok = (b["p_a"] > 0.5) == a_won
            right += ok
            graded.append({"bout": b["bout"], "a": b["a"], "b": b["b"],
                           "p_a": b["p_a"], "segment": b.get("segment"),
                           "winner": b["a"] if a_won else b["b"],
                           "method": mth, "seconds": ts, "model_right": bool(ok)})
        if graded:
            cards.append({"event": card.get("event"), "date": card.get("date"),
                          "venue": card.get("venue"), "n": len(graded),
                          "right": int(right), "bouts": graded})
    cards.sort(key=lambda c: str(c.get("date")), reverse=True)
    cards = cards[:keep]
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(
        {"cards": cards,
         "total_graded": sum(c["n"] for c in cards),
         "total_right": sum(c["right"] for c in cards)}, indent=1), encoding="utf-8")
    if verbose and cards:
        tot = sum(c["n"] for c in cards)
        rt = sum(c["right"] for c in cards)
        print(f"graded {len(cards)} past card(s), {rt}/{tot} winners correct "
              f"-> {out_path}")
    return out_path


def write_json(out, skipped, unresolved, path="site/predictions.json",
               card_path="data/upcoming.txt"):
    """Emit what the frontend reads. Skipped bouts are included with their
    reason so the site can render "no read" rather than omitting them
    silently — a card with three fights quietly missing looks broken."""
    import json
    import math
    from pathlib import Path

    def clean(o):
        """NaN and Infinity are valid Python floats and INVALID JSON. pandas
        fills missing columns with NaN, so a card where only some bouts have a
        market price produced `"p_market": NaN` and the browser refused the
        whole file. Null them out, then dump with allow_nan=False so this can
        never be written silently again — a hard failure here is far better
        than a site that cannot load."""
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, float):
            return None if (math.isnan(o) or math.isinf(o)) else o
        if hasattr(o, "item"):          # numpy scalars
            return clean(o.item())
        return o

    meta, _ = parse_card(card_path)
    archive_previous(meta.get("event"), path=path)
    payload = {
        "event": meta.get("event"), "date": meta.get("date"),
        "venue": meta.get("venue"),
        "generated_utc": pd.Timestamp.now('UTC').isoformat(),
        "bouts": out.to_dict(orient="records"),
        "no_read": [{"a": a, "b": b, "reason": why, "segment": seg}
                    for a, b, why, seg in skipped],
        "unresolved_names": unresolved,
        "market_source": MARKET_SRC.get("src"),
        "track_record": TRACK.get("rec"),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(clean(payload), indent=1, allow_nan=False),
                          encoding="utf-8")
    return path


if __name__ == "__main__":
    import json as _json
    import sys

    from .loaders import load

    f, p, _ = load(verbose=False)
    out, skipped, unresolved = predict_card("data/upcoming.txt", f, p)
    if "--json" in sys.argv:
        path = write_json(out, skipped, unresolved)
        print("\nwrote", path)

        # Grade any archived card whose results have since reached the corpus.
        # This call was missing entirely: archive_previous() ran inside
        # write_json, so cards were being saved, but nothing ever graded them
        # and site/history.json was never written.
        try:
            from .odds_live import read_usage
            u = read_usage()
            if u:
                Path("site/usage.json").write_text(_json.dumps(u, indent=1), encoding="utf-8")
        except Exception:
            pass

        try:
            grade_archive(f)
        except Exception as e:
            print(f"note: grading unavailable ({e})")

        # Record every market this card claims, before it happens, so each one
        # accumulates its own hit rate instead of borrowing trust from the
        # winner model.
        try:
            from .scorecard import log as sc_log, settle as sc_settle, scorecard as sc_build
            sc_log(_json.loads(Path(path).read_text(encoding="utf-8")))
            sc_settle(f)
            sc_build()
        except Exception as e:
            print(f"note: scorecard unavailable ({e})")
