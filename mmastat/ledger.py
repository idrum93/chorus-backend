"""Forward odds capture: the only remaining way to test the residual rule.

The problem this solves. Every result in this project is retrospective, and the
one finding that ever pointed the right way — the market-residual model,
better in 6 of 6 walk-forward folds — cannot be confirmed retrospectively. The
252-fight holdout has no prices. Re-scoring old fights against old odds is
re-reading data the rule was built on. The only honest test is to write the
prediction down before the fight and check it afterwards.

So this is an append-only log. Three commands:

    capture   fetch live lines, compute the frozen rule's number, append a row
    settle    match logged rows to finished fights, record the outcome
    report    bets, ROI, CLV, and progress toward the 300-bet threshold

Three rules that keep it honest, all enforced in code rather than by intention:

  FROZEN COEFFICIENTS. The rule's weights live in residual_model.json and are
  applied, never refitted. Refitting on each capture would quietly turn the
  forward test back into a retrospective one.

  APPEND ONLY. Rows are never rewritten, only settled. A ledger you can edit
  is a ledger you will edit.

  CLOSING LINE = LAST SNAPSHOT. Capture runs repeatedly; the row nearest the
  event is the closing price. Closing line value converges far faster than ROI
  (~300 bets for ROI, a few dozen for CLV), so CLV is the metric that will
  tell you something first.
"""
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

LEDGER_DIR = "data/ledger"
LEGACY = "data/ledger/odds_log.jsonl"
LEDGER = LEGACY          # kept for call sites that pass an explicit path


def month_file(when=None, directory=LEDGER_DIR):
    """Ledger rows go in a per-month file, e.g. data/ledger/2026-09.jsonl.

    Not for size — a year of capture is under 2 MB. For CHURN. settle()
    rewrites the whole ledger each run, so with one big file every one of the
    ~1,460 commits a year stores a fresh blob of an ever-growing file. With
    monthly files, a month's file stops changing the day the month ends and
    costs nothing thereafter. Cheap now, and it removes the only part of this
    that would eventually need object storage.
    """
    ts = pd.Timestamp(when) if when is not None else pd.Timestamp.now(tz="UTC")
    return os.path.join(directory, f"{ts.year:04d}-{ts.month:02d}.jsonl")


def ledger_files(directory=LEDGER_DIR):
    """Every ledger shard, oldest first. The legacy single file is included so
    existing rows are never orphaned by the switch."""
    import glob
    files = sorted(glob.glob(os.path.join(directory, "[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl")))
    if os.path.exists(LEGACY):
        files = [LEGACY] + files
    return files
MODEL = os.path.join(os.path.dirname(__file__), "residual_model.json")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_rule(path=MODEL):
    with open(path) as fh:
        return json.load(fh)


def rule_probability(feat_row, p_market, rule):
    """P(win) = sigmoid(logit(market) + x.beta), with x standardised by the
    frozen scaler. feat_row is a dict of the five feature values."""
    x = np.array([float(feat_row[k]) for k in rule["features"]])
    z = (x - np.array(rule["mean"])) / np.array(rule["scale"])
    pm = min(max(float(p_market), 1e-6), 1 - 1e-6)
    logit = math.log(pm / (1 - pm)) + float(z @ np.array(rule["coef"]))
    return 1.0 / (1.0 + math.exp(-logit))


def fires(p_model, implied_with_vig, rule):
    """Does the pre-registered bet trigger? Favourites only, edge over the
    price you would actually pay (vig included), not the devigged estimate."""
    r = rule["rule"]
    if r.get("favourites_only") and implied_with_vig < r["min_implied"]:
        return False
    return (p_model - implied_with_vig) > r["min_edge"]


def append(rows, path=None):
    """Append each row to the shard for its own capture month."""
    if not rows:
        return 0
    by_file = {}
    for r in rows:
        f = path or month_file(r.get("captured_utc"))
        by_file.setdefault(f, []).append(r)
    n = 0
    for f, rs in by_file.items():
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            for r in rs:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        n += len(rs)
    return n


KEY = ["captured_utc", "venue", "event_date", "fighter", "opponent"]


def dedupe(rows):
    """One row per (capture time, bout, fighter), last write wins.

    An append-only log committed by a scheduled job will eventually get
    duplicated: a rebase replays a local rewrite on top of a remote that
    already has it, and both copies survive. That happened on the second live
    capture — the 13:56 Van/Pantoja rows appeared twice, once with the old bad
    settlement and once corrected. Duplicates would double-count in both CLV
    and ROI, so the file is deduplicated on every write. The capture fields
    are identical across copies by construction, so keeping the last is safe.
    """
    seen = {}
    for r in rows:
        k = tuple(str(r.get(k2)) for k2 in KEY)
        if k in seen and seen[k].get("opening") and not r.get("opening"):
            # Last-write-wins must never erase an opening marker: that flag
            # records the first time a bout was seen, and a merge or a
            # same-second recapture could otherwise silently turn it off.
            r = dict(r, opening=True)
        seen[k] = r
    return list(seen.values())


# Fields added after the ledger was already running, with the value that rows
# written before them are known to have had. An append-only log keeps every
# historical schema forever, so every consumer must tolerate the oldest one:
# adding `venue` to the dedupe key broke capture outright with
# KeyError: ['venue'] not in index, because the first 48 rows predate it.
BACKFILL = {"venue": "sportsbook", "spread": None, "depth_usd": None,
            "books": None, "settled": False}


def migrate(rows):
    """Give every row today's schema, so old and new rows compare equal."""
    out = []
    for r in rows:
        d = dict(r)
        for k, v in BACKFILL.items():
            d.setdefault(k, v)
            if d.get(k) is None and k == "venue":
                d[k] = "sportsbook"
        out.append(d)
    return out


def _raw(path=None):
    files = [path] if path else ledger_files()
    rows = []
    for f in files:
        if not f or not os.path.exists(f):
            continue
        rows += [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
    return rows


def read(path=None):
    rows = _raw(path)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(dedupe(migrate(rows)))
    for k, v in BACKFILL.items():          # guarantee the column exists
        if k not in df.columns:
            df[k] = v
    return df


def prune_resolved(path=None, eps=0.02, verbose=True):
    """Remove rows whose market price is at certainty.

    Append-only is a discipline for PREDICTIONS. A row quoting 1.0 after the
    fight is not a prediction that turned out well, it is corrupt input, and
    leaving it in would hand the model a fabricated perfect record. Removals
    are counted and reported rather than done silently.
    """
    drop = 0
    for f in ([path] if path else ledger_files()):
        if not f or not os.path.exists(f):
            continue
        rows = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
        keep = [r for r in rows
                if r.get("p_market_devig") is None
                or eps < float(r["p_market_devig"]) < 1 - eps]
        if len(keep) != len(rows):
            drop += len(rows) - len(keep)
            Path(f).write_text(
                "\n".join(json.dumps(r, sort_keys=True) for r in keep) + "\n",
                encoding="utf-8")
    if drop and verbose:
        print(f"pruned {drop} rows priced at certainty (resolved markets)")
    return drop


def _rewrite(path=None):
    """Deduplicate each shard in place. Only shards that actually change are
    written, so a closed month never produces a new git blob."""
    removed = 0
    for f in ([path] if path else ledger_files()):
        if not f or not os.path.exists(f):
            continue
        raw = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
        clean = dedupe(migrate(raw))
        if len(clean) != len(raw):
            Path(f).write_text(
                "\n".join(json.dumps(r, sort_keys=True) for r in clean) + "\n",
                encoding="utf-8")
            removed += len(raw) - len(clean)
    return removed


def capture(fights, fighters, card_path="data/upcoming.txt", path=None,
            verbose=True, venues_only=None):
    """Log every priced bout on the upcoming card, at this moment."""
    from .odds_live import fetch_moneylines
    from .upcoming import parse_card, resolve, _states_after, _key
    from .features import elo_eff, make_features

    rule = load_rule()

    # Two venues, logged separately and never pooled.
    #
    #   sportsbook   The Odds API consensus. Cost is the bookmaker margin, so
    #                the payable price is the devigged probability inflated by
    #                half the two-way overround.
    #   polymarket   peer-to-peer, near-zero fee. There is no vig to add back:
    #                the ASK is literally what you pay. Its cost is the spread,
    #                which is recorded per row.
    #
    # They stay separate because the pre-registered rule was frozen on
    # sportsbook prices. Pooling a second venue into it would widen the
    # population mid-experiment, which is the same error as re-specifying a
    # hypothesis after seeing data. Polymarket rows accumulate as their own
    # test, with their own count toward 300.
    venues = {}
    try:
        sb = fetch_moneylines()
        if sb:
            venues["sportsbook"] = sb
    except Exception as e:
        print(f"note: sportsbook odds unavailable ({e})")
    try:
        from .polymarket import fetch_all
        pm, _props, _rows = fetch_all(with_book=True)
        if pm:
            venues["polymarket"] = pm
    except Exception as e:
        print(f"note: polymarket unavailable ({e})")
    if venues_only:
        venues = {k: v for k, v in venues.items() if k in venues_only}
    if not venues:
        if verbose:
            print("capture: no card-keyed venues to record")
        return 0
    book = venues.get("sportsbook") or {}

    meta, bouts = parse_card(card_path)

    # Hard stop: a capture is a PREDICTION, so it is only meaningful before
    # the event. Polymarket keeps resolved fights listed and quotes them at
    # 1.0/0.0; a run after the bell logged eight of those as if they were
    # forecasts the model nailed. No venue-specific fix is sufficient here —
    # the date is the invariant.
    ev_date = pd.to_datetime(meta.get("date"), errors="coerce")
    if pd.notna(ev_date) and pd.Timestamp.now(tz="UTC").normalize() > \
            ev_date.tz_localize("UTC") + pd.Timedelta(days=1):
        if verbose:
            print(f"card dated {meta.get('date')} is in the past - "
                  f"refusing to capture prices for a finished event")
        return 0

    states = _states_after(fights, fighters)
    names = sorted({n for b in bouts for n in b[:2]})
    ids, _ = resolve(names, fighters)
    as_of = pd.Timestamp(meta.get("date") or fights.date.max())

    rows, ts = [], _now()
    for venue, vbook in venues.items():
        for na, nb, n_rounds, _segment in bouts:
            hit = vbook.get(frozenset((_key(na), _key(nb))))
            if hit is None or na not in ids or nb not in ids:
                continue
            A, B = states[ids[na]], states[ids[nb]]
            if min(A.n_fights, B.n_fights) < 2:
                continue
            sa, sb = A.snapshot(as_of), B.snapshot(as_of)
            sa["elo"], sb["elo"] = elo_eff(A, as_of), elo_eff(B, as_of)
            fv = make_features(sa, sb)

            src, p_raw, extra = hit
            p_mkt = p_raw if src == _key(na) else 1 - p_raw
            meta_x = extra if isinstance(extra, dict) else {"books": extra}
            for side, name, opp, pm in (("a", na, nb, p_mkt), ("b", nb, na, 1 - p_mkt)):
                f = fv if side == "a" else {k: -v for k, v in fv.items()}
                p_model = rule_probability(f, pm, rule)
                if venue == "polymarket":
                    # the ask is the price; no margin to add back
                    sp = meta_x.get("spread")
                    implied = pm + (sp / 2 if sp else 0.0)
                else:
                    implied = pm * (1 + 0.037 / 2)
                rows.append(dict(
                    captured_utc=ts, venue=venue,
                    event=meta.get("event"), event_date=meta.get("date"),
                    fighter=name, opponent=opp, side=side,
                    p_market_devig=round(pm, 5), implied_with_vig=round(implied, 5),
                    p_model=round(p_model, 5), edge=round(p_model - implied, 5),
                    books=meta_x.get("books"), spread=meta_x.get("spread"),
                    depth_usd=meta_x.get("depth_usd"),
                    bet=bool(fires(p_model, implied, rule)),
                    rule_frozen_on=rule.get("frozen_on"), settled=False))
    # Read the raw rows, not a DataFrame view: indexing by column list breaks
    # the moment a key field postdates some of the file.
    prior = []
    if os.path.exists(path):
        prior = migrate([json.loads(l) for l in open(path, encoding="utf-8") if l.strip()])
    have = {tuple(str(r.get(k)) for k in KEY) for r in prior}
    rows = [r for r in rows if tuple(str(r.get(k)) for k in KEY) not in have]
    n = append(rows, path)
    _rewrite(path)                      # collapse any duplicates a merge left
    if verbose:
        from collections import Counter
        per = Counter(r["venue"] for r in rows)
        b = Counter(r["venue"] for r in rows if r["bet"])
        print(f"captured {n} fighter-prices for {meta.get('event')}")
        for v in per:
            print(f"  {v:<12} {per[v]:>3} prices, {b.get(v,0)} trigger the rule")
    return n


def event_date_et(commence):
    """UFCStats dates a card by its local evening; The Odds API gives UTC.
    A Saturday-night card commences around 02:00 UTC Sunday, so the raw UTC
    date is a day late and would never line up with the corpus. Eastern time
    matches the corpus for nearly every card."""
    ts = pd.to_datetime(commence, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.tz_convert("America/New_York").date().isoformat()


def sweep(fights, fighters, path=None, verbose=True):
    """Log EVERY listed bout the model can price, at no extra API cost.

    The point is the opening price. A bout's first appearance in the feed is
    the closest thing to its true open, and the old capture only logged the
    card in data/upcoming.txt — which advances once a day and often lags the
    feed by a week or more. So a card was first captured days after its line
    opened, after the softest price had already been bid away, systematically
    understating the effect addendum 13 is trying to measure.

    Each bout is guarded by its OWN start time, not the card's: a bout that
    has already begun is never priced, which also closes off the resolved-
    market failure at the root rather than filtering it afterwards.
    """
    from .odds_live import fetch_events
    from .upcoming import resolve, _states_after
    from .features import elo_eff, make_features

    rule = load_rule()
    try:
        events = fetch_events()
    except Exception as e:
        if verbose:
            print(f"sweep: odds unavailable ({e})")
        return 0
    if not events:
        if verbose:
            print("sweep: no events (set ODDS_API_KEY)")
        return 0

    now = pd.Timestamp.now(tz="UTC")
    upcoming = [e for e in events
                if pd.to_datetime(e["commence"], utc=True, errors="coerce") > now]
    names = sorted({n for e in upcoming for n in (e["name1"], e["name2"])})
    ids, _ = resolve(names, fighters)
    states = _states_after(fights, fighters)

    prior = {tuple(str(r.get(k)) for k in ("venue", "event_date", "fighter", "opponent"))
             for r in migrate(_raw(path))}
    rows, ts, new_bouts, skipped = [], _now(), 0, 0
    for e in upcoming:
        na, nb = e["name1"], e["name2"]
        if na not in ids or nb not in ids:
            skipped += 1                 # not a UFC fighter we have history for
            continue
        A, B = states[ids[na]], states[ids[nb]]
        if min(A.n_fights, B.n_fights) < 2:
            skipped += 1
            continue
        ed = event_date_et(e["commence"])
        as_of = pd.Timestamp(ed)
        sa, sb = A.snapshot(as_of), B.snapshot(as_of)
        sa["elo"], sb["elo"] = elo_eff(A, as_of), elo_eff(B, as_of)
        fv = make_features(sa, sb)
        p_a = e["p1"] if _nm_key(na) == e["k1"] else 1 - e["p1"]
        first_seen = ("sportsbook", ed, na, nb) not in prior
        new_bouts += first_seen
        for side, name, opp, pm in (("a", na, nb, p_a), ("b", nb, na, 1 - p_a)):
            f = fv if side == "a" else {k: -v for k, v in fv.items()}
            p_model = rule_probability(f, pm, rule)
            implied = pm * (1 + 0.037 / 2)
            rows.append(dict(
                captured_utc=ts, venue="sportsbook", event=None, event_date=ed,
                fighter=name, opponent=opp, side=side,
                p_market_devig=round(pm, 5), implied_with_vig=round(implied, 5),
                p_model=round(p_model, 5), edge=round(p_model - implied, 5),
                books=e["books"], spread=None, depth_usd=None,
                bet=bool(fires(p_model, implied, rule)),
                opening=bool(first_seen),
                rule_frozen_on=rule.get("frozen_on"), settled=False))
    n = append(rows, path)
    if verbose:
        print(f"sweep: {len(upcoming)} upcoming bouts in feed, {n // 2} priced, "
              f"{new_bouts} seen for the first time (opening prices), "
              f"{skipped} skipped (no UFC history)")
    return n


def _nm_key(s):
    from .odds_live import _nm
    return _nm(s)


def settle(fights, path=None, date_tol_days=4, verbose=True):
    """Attach outcomes to finished bouts.

    Matching is on the name pair AND the event date, within a few days. Name
    pair alone is wrong: 424 fights in the corpus are rematches, and on the
    first live capture this settled a Van vs Pantoja rematch using their
    2025-12-06 first meeting — marking tonight's fight decided, hours before
    it happened. Silently resolving a prediction with a different fight's
    result is the worst failure this ledger could have.

    Settlement is RE-DERIVED from the corpus on every run rather than skipped
    once set, so a bad settle heals itself on the next pass. The captured
    prediction fields are never touched; only the outcome fields are.
    """
    from .upcoming import _key
    files = [path] if path else ledger_files()
    if not any(f and os.path.exists(f) for f in files):
        if verbose:
            print("ledger is empty")
        return 0

    parts = fights.bout.str.split(" vs. ", n=1, expand=True)
    by_pair = {}
    for r_, b_, w, dt in zip(parts[0], parts[1], fights.winner, fights.date):
        by_pair.setdefault(frozenset((_key(r_), _key(b_))), []).append((dt, _key(r_), w))

    n, fixed = 0, 0
    for f in files:
        if not f or not os.path.exists(f):
            continue
        out = []
        for d in migrate([json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]):
            was = bool(d.get("settled"))
            d.pop("won", None)
            d.pop("void", None)
            d["settled"] = False
            cands = by_pair.get(frozenset((_key(d["fighter"]), _key(d["opponent"]))), [])
            ev = pd.to_datetime(d.get("event_date"), errors="coerce")
            best = None
            for dt, red, w in cands:
                if pd.isna(ev):
                    continue
                gap = abs((pd.Timestamp(dt) - ev).days)
                if gap <= date_tol_days and (best is None or gap < best[0]):
                    best = (gap, red, w)
            if best is not None:
                _, red, winner = best
                if winner not in ("r", "b"):
                    d["settled"], d["void"] = True, True
                else:
                    d["settled"] = True
                    d["won"] = bool((winner == "r") == (_key(d["fighter"]) == red))
                n += 1
            if was and not d["settled"]:
                fixed += 1
            out.append(d)

        out = dedupe(out)
        new = "\n".join(json.dumps(r, sort_keys=True) for r in out) + "\n"
        if new != Path(f).read_text(encoding="utf-8"):
            Path(f).write_text(new, encoding="utf-8")   # untouched shards stay untouched
    if verbose:
        print(f"settled {n} rows" + (f"; un-settled {fixed} wrongly matched" if fixed else ""))
    return n


def report(path=LEDGER, vig=0.037, venue=None):
    """Where the forward test stands. CLV first — it converges long before ROI."""
    L = read(path)
    if L.empty:
        return {"status": "ledger empty"}
    # one row per fighter-price: the LAST capture before the fight is the close
    if venue:
        L = L[L.venue == venue]
        if L.empty:
            return {"status": f"no rows for venue {venue}"}
    L = L.sort_values("captured_utc")
    gk = ["venue", "event_date", "fighter", "opponent"]
    close = L.groupby(gk, as_index=False).last()
    first = L.groupby(gk, as_index=False).first()
    move = close.p_market_devig.values - first.p_market_devig.values

    # CLV is only defined for the side you BACKED. Averaged over both corners
    # it is identically zero: every point the favourite gains, the underdog
    # loses. The first version did exactly that and reported -0.0 on a card
    # where the lines had genuinely moved almost two points.
    betmask = close.bet.fillna(False).values.astype(bool)
    clv = move[betmask]

    bets = close[(close.bet == True) & (close.get("settled") == True)]  # noqa: E712
    out = {
        "venue": venue or "all",
        "captured_prices": int(len(close)),
        "bets_triggered": int((close.bet == True).sum()),  # noqa: E712
        "bets_settled": int(len(bets)),
        "bets_needed": max(0, 300 - int(len(bets))),
        # movement on the backed side only; positive means the market came to us
        "clv_bets_n": int(betmask.sum()),
        "clv_mean_pp": round(float(np.nanmean(clv) * 100), 3) if len(clv) else None,
        "clv_positive_share": (round(float(np.mean(clv > 0)), 3) if len(clv) else None),
        # sanity: across both corners this must be ~0, and if it is not the
        # two sides of a fight are not being paired correctly
        "all_sides_move_pp": round(float(np.nanmean(move) * 100), 4),
    }
    if len(bets):
        won = bets.get("won")
        dec = 1.0 / bets.implied_with_vig
        pnl = np.where(won.fillna(False), dec - 1.0, -1.0)
        out["win_rate"] = round(float(won.fillna(False).mean()), 4)
        out["roi_pct"] = round(float(pnl.mean() * 100), 3)
        out["roi_se_pct"] = round(float(pnl.std(ddof=1) / np.sqrt(len(pnl)) * 100), 3)
        out["verdict"] = ("too early — need 300 settled bets"
                          if len(bets) < 300 else
                          ("rule SUPPORTED" if out["roi_pct"] > 2.0 else "rule REJECTED"))
    return out


if __name__ == "__main__":
    import sys
    from .loaders import load
    f, p, _ = load(verbose=False)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "capture":
        # The sweep logs every listed bout, which includes the current card, so
        # it replaces the card-only capture for the sportsbook venue. The
        # card-only path still runs for Polymarket, which is keyed to the card.
        sweep(f, p)
        capture(f, p, venues_only=("polymarket",))
    elif cmd == "settle":
        prune_resolved()
        settle(f)
    elif cmd == "prune":
        prune_resolved()
    for v in (None, "sportsbook", "polymarket"):
        r = report(venue=v)
        if r.get("status"):
            continue
        print(json.dumps(r, indent=1))
