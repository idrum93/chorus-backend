"""Synthetic fight corpus in UFCStats shape.

Why bother: with real data you never know the ceiling, so you can't tell a
mediocre model from a hard problem. Here the true generating process is known,
so train.py can report ORACLE accuracy (what a model with perfect knowledge of
latent skill would score) alongside the fitted model's. The gap is what's left
on the table; the oracle itself is the noise floor.

Deliberately included realism:
  - matchmaking pairs fighters of similar strength, which compresses the
    observable skill gap and is the main reason real MMA tops out near 70%
  - roster churn, aging curves, style archetypes on a striker/grappler axis
  - stats are emitted from the same latent skills that decide the fight, so
    opponent adjustment has something real to recover
"""
import numpy as np
import pandas as pd

RNG_DIMS = ["so", "sd", "go", "gd", "dur", "pace"]


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def make_corpus(n_fighters=520, n_events=340, bouts_per_event=11,
                start="2016-01-09", seed=7):
    rng = np.random.default_rng(seed)

    # ---------------------------------------------------------- fighters
    skill = rng.normal(0, 1, n_fighters)
    dims = {d: 0.62 * skill + 0.78 * rng.normal(0, 1, n_fighters) for d in RNG_DIMS}
    # style axis: positive = striker, negative = grappler
    style = rng.normal(0, 1, n_fighters)
    dims["go"] += -0.85 * style
    dims["gd"] += -0.35 * style
    dims["so"] += 0.75 * style

    height = rng.normal(70.5, 3.0, n_fighters)
    reach = height + rng.normal(1.5, 2.0, n_fighters)
    stance = np.where(rng.random(n_fighters) < 0.19, "Southpaw", "Orthodox")

    start_ts = pd.Timestamp(start)
    # staggered debuts across the timeline so the roster churns
    debut_off = rng.integers(0, int(n_events * 0.82), n_fighters)
    age_at_debut = rng.normal(28.5, 3.2, n_fighters).clip(21, 38)

    fighters = pd.DataFrame({
        "fighter_id": [f"F{i:04d}" for i in range(n_fighters)],
        "name": [f"Fighter {i:04d}" for i in range(n_fighters)],
        "height_in": height.round(1),
        "reach_in": reach.round(1),
        "stance": stance,
    })
    fighters["dob"] = [
        start_ts + pd.Timedelta(days=int(debut_off[i] * 7) - int(age_at_debut[i] * 365.25))
        for i in range(n_fighters)
    ]

    truth = pd.DataFrame({d: dims[d] for d in RNG_DIMS})
    truth["skill"] = skill
    truth["style"] = style
    truth["fighter_id"] = fighters["fighter_id"]

    # ---------------------------------------------------------- careers
    active = np.zeros(n_fighters, dtype=bool)
    losses_recent = np.zeros(n_fighters, dtype=int)
    n_bouts = np.zeros(n_fighters, dtype=int)
    retired = np.zeros(n_fighters, dtype=bool)
    last_ev = np.full(n_fighters, -99)

    rows = []
    fid_ctr = 0

    for ev in range(n_events):
        date = start_ts + pd.Timedelta(days=7 * ev)
        active |= (debut_off <= ev) & (~retired)
        pool = np.where(active & (last_ev < ev - 8))[0]  # ~2 month turnaround
        if len(pool) < bouts_per_event * 2:
            continue

        # matchmaking: sort by perceived strength, pair neighbours with jitter
        perceived = skill[pool] + rng.normal(0, 0.35, len(pool))
        pool = pool[np.argsort(perceived)]
        pairs = []
        idx = list(range(len(pool)))
        rng.shuffle(idx)
        # take adjacent pairs from the strength-sorted list
        used = set()
        for i in range(len(pool) - 1):
            if len(pairs) >= bouts_per_event:
                break
            if i in used or (i + 1) in used:
                continue
            pairs.append((pool[i], pool[i + 1]))
            used.add(i)
            used.add(i + 1)

        for a, b in pairs:
            if rng.random() < 0.5:
                a, b = b, a
            age_a = (date - fighters.dob.iloc[a]).days / 365.25
            age_b = (date - fighters.dob.iloc[b]).days / 365.25

            def edge(x, age):
                """Age decline folded into effective attributes."""
                return -0.055 * max(0.0, age - 32.0) ** 1.35

            ea, eb = edge(a, age_a), edge(b, age_b)
            reach_edge = 0.045 * (reach[a] - reach[b])

            # ---- simulate the full 15 minutes of exchanges first ----------
            FULL = 15.0

            def exchange(x, y, x_edge, y_edge, r_adv):
                att_pm = 9.5 * np.exp(0.30 * dims["pace"][x]) * rng.uniform(0.85, 1.15)
                acc = _sig(-0.25 + 0.55 * (dims["so"][x] + x_edge) - 0.55 * dims["sd"][y]
                           + 0.35 * r_adv)
                ssa = max(1, int(rng.poisson(att_pm * FULL)))
                ssl = int(rng.binomial(ssa, np.clip(acc, 0.05, 0.90)))

                td_pm15 = 4.6 * _sig(1.25 * dims["go"][x] - 1.05)
                tda = int(rng.poisson(max(0.0, td_pm15)))
                tacc = _sig(0.15 + 0.85 * (dims["go"][x] + x_edge) - 0.85 * dims["gd"][y])
                tdl = int(rng.binomial(tda, np.clip(tacc, 0.03, 0.92))) if tda else 0
                sub = int(rng.poisson(2.1 * _sig(1.05 * dims["go"][x] - 1.25)))
                ctrl = float(min(FULL * 60 * 0.6, rng.gamma(2.0, 42.0) * tdl))
                return dict(ssl=ssl, ssa=ssa, tdl=tdl, tda=tda, sub=sub, ctrl=ctrl)

            A = exchange(a, b, ea, eb, reach_edge)
            B = exchange(b, a, eb, ea, -reach_edge)
            if A["ctrl"] + B["ctrl"] > FULL * 60 * 0.85:
                sc = FULL * 60 * 0.85 / (A["ctrl"] + B["ctrl"])
                A["ctrl"] *= sc
                B["ctrl"] *= sc

            # ---- finishes are hazards driven by what actually landed ------
            def ko_hazard(att, dfn):
                return 0.0027 * (att["ssl"] / FULL) * np.exp(-0.85 * dims["dur"][dfn])

            def sub_hazard(att, dfn):
                return 0.2400 * (att["sub"] / FULL) * np.exp(-0.80 * dims["gd"][dfn])

            events = [
                ("KO/TKO", "r", ko_hazard(A, b)), ("KO/TKO", "b", ko_hazard(B, a)),
                ("SUB", "r", sub_hazard(A, b)), ("SUB", "b", sub_hazard(B, a)),
            ]
            t_end, m, win_c = FULL, "DEC", None
            for meth, corner, rate in events:
                if rate <= 0:
                    continue
                t = rng.exponential(1.0 / rate)
                if t < t_end:
                    t_end, m, win_c = t, meth, corner

            # ---- decisions are scored from the stats, like real judging ---
            if m == "DEC":
                def score(s):
                    return 0.55 * s["ssl"] + 2.4 * s["tdl"] + 0.016 * s["ctrl"] + 0.9 * s["sub"]
                margin = score(A) - score(B) + rng.normal(0, 4.5)  # judge noise
                win_c = "r" if margin > 0 else "b"

            # ---- truncate the stat line to when the fight actually ended --
            frac = t_end / FULL
            total_sec = int(round(t_end * 60))
            for s in (A, B):
                s["ssa"] = max(1, int(round(s["ssa"] * frac)))
                s["ssl"] = min(s["ssa"], int(round(s["ssl"] * frac)))
                s["tda"] = int(round(s["tda"] * frac))
                s["tdl"] = min(s["tda"], int(round(s["tdl"] * frac)))
                s["sub"] = int(round(s["sub"] * frac))
                s["ctrl"] = int(round(min(s["ctrl"] * frac, total_sec * 0.8)))

            a_wins = win_c == "r"
            rnd = min(3, total_sec // 300 + 1)
            t_sec = total_sec - (rnd - 1) * 300

            rows.append(dict(
                fight_id=f"B{fid_ctr:05d}", date=date,
                r_id=fighters.fighter_id.iloc[a], b_id=fighters.fighter_id.iloc[b],
                winner="r" if a_wins else "b", method=m, round=int(rnd),
                time_sec=int(t_sec), total_sec=int(total_sec), title=False,
                r_ss_landed=A["ssl"], r_ss_att=A["ssa"], b_ss_landed=B["ssl"], b_ss_att=B["ssa"],
                r_td_landed=A["tdl"], r_td_att=A["tda"], b_td_landed=B["tdl"], b_td_att=B["tda"],
                r_sub_att=A["sub"], b_sub_att=B["sub"],
                r_ctrl_sec=A["ctrl"], b_ctrl_sec=B["ctrl"],
            ))
            fid_ctr += 1

            n_bouts[a] += 1
            n_bouts[b] += 1
            last_ev[a] = last_ev[b] = ev
            w, l = (a, b) if a_wins else (b, a)
            losses_recent[l] += 1
            losses_recent[w] = max(0, losses_recent[w] - 1)
            for x in (a, b):
                if losses_recent[x] >= 3 or n_bouts[x] > 22 or \
                   (date - fighters.dob.iloc[x]).days / 365.25 > 40:
                    retired[x] = True
                    active[x] = False

    fights = pd.DataFrame(rows)
    return fights, fighters, truth


if __name__ == "__main__":
    f, p, t = make_corpus()
    print(f"{len(f)} fights, {f.date.min().date()}..{f.date.max().date()}, "
          f"{f.r_id.nunique()} distinct red corners")
    print(f.method.value_counts().to_dict())
