# Forward odds ledger

Rows are written to a **per-month shard**, `data/ledger/2026-09.jsonl`.

Not for size — a year of capture is under 2 MB. For churn. `settle` rewrites
the ledger each run, so with one growing file every one of the ~1,460 commits
a year stores a fresh blob of the whole thing. With monthly shards a file
stops changing the day its month ends and costs nothing after that, and
`settle` only writes a shard whose contents actually changed. The legacy
`odds_log.jsonl` is still read, so nothing already captured is orphaned.

At this rate the repo takes on roughly 2 MB a year of ledger plus 0.7 MB of
archived cards. Object storage (R2, S3, release assets) is not needed at any
point on that curve; if it ever is, the shards are already the natural unit to
move.

`odds_log.jsonl` was written here by `python -m mmastat.ledger capture`, four
times a day via `.github/workflows/capture.yml`. It **is** committed — unlike
the rest of `data/` — because it is the experiment, not an input to it.

## The sweep: opening prices at no extra cost

Every call to The Odds API returns every listed MMA bout, often weeks of cards
ahead. The first capture code kept only the bouts in `data/upcoming.txt`,
which advances once a day and can lag the feed by a week — so a card was first
recorded days after its line opened, once the softest price had already been
bid away. That systematically understated the very effect addendum 13 is
trying to measure.

`sweep()` logs every bout the model can price from the same response. The
first time a bout appears is flagged `opening: true`; that row is the closest
available thing to its true open. It costs **nothing extra**, which matters:

| | credits / month |
|---|---|
| capture, 4 a day x 3 regions | 360 |
| refresh, 1 a day x 3 regions | 90 |
| **total** | **450 of 500** |

The API bills markets x regions per call, so each call is 3 credits, not 1.
(An earlier note here said four daily captures cost about 120 a month. That
was wrong by the same factor of three.) Adding calls to find opens sooner
would exhaust the free tier, so the sweep extracts more from calls already
being made.

Each bout is guarded by its **own** start time, so a bout that has begun is
never priced — closing off the resolved-market failure at its source rather
than filtering it afterwards. Event dates are converted to US Eastern, because
a Saturday card commences around 02:00 UTC Sunday and the raw UTC date would
never match the corpus.

Bouts involving fighters with no UFC history — PFL, Bellator and the rest of
the feed — are skipped naturally, since the model has nothing to price them
with.

## Two venues, never pooled

| venue | source | auth | cost of trading |
|---|---|---|---|
| `sportsbook` | The Odds API consensus | `ODDS_API_KEY` | ~3.7% two-way overround |
| `polymarket` | Gamma + CLOB, public | **none** | the bid-ask spread, logged per row |

The pre-registered rule was frozen on sportsbook prices, so Polymarket rows
accumulate as a **separate test with their own count toward 300**. Pooling a
second venue into a running experiment widens the population mid-flight, which
is the same error as re-specifying a hypothesis after seeing data.

Polymarket matters for one reason: **a prediction market has almost no vig.**
The residual model's measured advantage is +0.0035 log loss — hopeless against
a bookmaker's margin, but the right order of magnitude against a venue whose
cost is a one- or two-cent spread. Whether that survives contact with reality
is exactly what the ledger is for.

**Three guards against capturing a decided fight.** Polymarket leaves markets
listed until they resolve and then quotes them at 1.0 / 0.0. A run after the
bell logged eight such rows as if they were forecasts the model got exactly
right — the one failure mode that manufactures evidence instead of destroying
it. Now: `capture` refuses any card dated in the past, `moneylines` rejects
prices within 2 cents of certainty, and `settle` prunes any that slipped
through. `python -m mmastat.ledger prune` runs the cleanup by hand.

Prices from thin books are dropped rather than recorded: wider than 6 cents or
under $250 of depth and the row never appears. A price you cannot trade is not
a price, and letting one in would quietly flatter the comparison.

One row per fighter-price per capture:

    captured_utc, venue, event, event_date, fighter, opponent,
    p_market_devig, implied_with_vig, p_model, edge, bet,
    books, spread, depth_usd, settled, won

Rules, enforced in code:

- **Deduplicated on every read and write**, keyed on
  (captured_utc, event_date, fighter, opponent). A scheduled job committing an
  append-only file will eventually duplicate it — a rebase replays a local
  rewrite onto a remote that already has it and both copies survive. That
  happened on the second live capture. Duplicates would double-count in CLV
  and ROI. `.gitattributes` also sets `merge=union` on this file, so a merge
  keeps both sides and dedupe cleans up: no captured prediction is ever lost.
- **Append only.** `settle` marks outcomes; nothing else is ever rewritten.
- **Frozen coefficients.** The rule lives in `mmastat/residual_model.json` and
  is applied, never refitted. Refitting would turn the forward test back into
  a retrospective one.
- **Settlement is re-derived from the corpus every run**, not skipped once
  set, so a bad match heals itself on the next pass. Captured prediction
  fields are never touched; only outcome fields are.
- **Matching requires the event date**, within 4 days, not just the fighter
  pair. 424 corpus fights are rematches — on the very first live capture, pair-
  only matching settled a Van vs Pantoja rematch with their 2025-12-06 first
  meeting, marking tonight's fight decided hours before it happened.
  `tests.py::test_settle_respects_dates` now guards this.
- **The last row before an event is the closing price**, which is what CLV is
  measured against.

`python -m mmastat.ledger report` shows where it stands.

**CLV is measured on the backed side only.** Averaged across both corners it is
identically zero — every point the favourite gains, the underdog loses — and
the first version did exactly that, reporting -0.0 on a card where lines had
moved almost two points. `all_sides_move_pp` is kept in the output purely as a
check: it should stay at 0.0, and if it drifts the two sides of a fight are no
longer being paired correctly.

## How long this takes

The rule triggers on roughly 14% of prices, or about **180 qualifying bets a
year**. The pre-registered threshold is 300 settled bets, so a verdict on ROI
is about **20 months** away.

Closing line value converges much faster — a few dozen bets — which is why
`report` puts CLV first. If the model is systematically getting worse prices
than the close, that shows up long before ROI could, and it is sufficient to
kill the rule early.
