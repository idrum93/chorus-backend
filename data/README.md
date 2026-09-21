# Put the data files here

Nothing in this folder is committed (see `.gitignore`) — these are other
people's datasets, they're large, and they can be re-downloaded.

## Required — the raw UFCStats dump

From **github.com/Greco1899/scrape_ufc_stats** (Code → Download ZIP):

    ufc_event_details.csv
    ufc_fight_results.csv
    ufc_fight_stats.csv
    ufc_fighter_tott.csv

Also download **`ufc_fighter_details.csv`** from the same repo. It carries the
NICKNAME column, which is what lets `upcoming.py` match a card that says
"Patricio Pitbull" to UFCStats' "Patricio Freire". Without it the pipeline still
runs; you just resolve more names by hand.

`ufc_fight_details.csv` isn't used.

## Optional — betting odds, for the market benchmark

From Kaggle, `mdabbert/ultimate-ufc-dataset`: `ufc-master.csv`

Only needed to reproduce the model-vs-market comparison.

## Optional — the daily-snapshot odds feed

Kaggle, `jerzyszocik/ufc-betting-odds-daily-dataset`, saved as
`ufc_betting_odds_daily.csv`. Richer than the above and read by
`mmastat/odds_daily.py`. Three things only this source has:

- **method-of-victory prices** for 4,867 fights (2012-2024), joinable by the
  UFCStats fight hash
- **multiple bookmakers** (median 21 per bout since 2025), so best-available
  price is computable
- **repeated snapshots** — 1,314 bouts with 2+, median 4-day tracking span, so
  line movement is measurable

It is **not** a closing-line archive. The last snapshot lands a median of 5
hours before the event on recent bouts but 27 hours out across the full recent
history, and the 2010-2024 portion is a single bulk-imported price of unknown
timing. A pre-close price is a less efficient market, so scoring a model
against it flatters the model. Fine for description and line-movement work;
it does not substitute for the prospective capture the pre-registration
requires.

Note it also contains non-UFC promotions (KSW and others). Joining to the
corpus filters them out. Note that as of
September 2026 this dataset's coverage stopped at 2026-03-28, so it may be
abandoned. The `upcoming.csv` in it is a stale snapshot, not a live card —
don't build anything on it.

## Committed, not downloaded

`upcoming.txt` and `aliases.txt` ARE tracked in git — they're yours to edit, not
someone else's data.

`upcoming.txt` is the next card, typed by hand:

    # UFC 331 | 2026-09-19 | Crypto.com Arena, Los Angeles
    Joshua Van vs. Alexandre Pantoja
    Arman Tsarukyan vs. Mauricio Ruffy

`aliases.txt` fixes names automatic matching can't or shouldn't guess:

    Patricio Pitbull = Patricio Freire
