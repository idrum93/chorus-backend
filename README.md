# Cornerman

UFC fight analysis: a win-probability model, calibrated projections of fight
output, and a frontend that shows its reasoning.

**It does not beat the betting market, and it is not a betting tool.** See
[Model vs market](#model-vs-market) — that was tested, not assumed.

## What usually happens: the conditional base-rate panel

`baserates.py` splits the corpus into quartiles on ten registered conditions
and reports the outcome rate in each, with n and a 95% interval. Counts, not
model output.

| condition | outcome | Q1 | Q4 | spread |
|---|---|---|---|---|
| own takedown rate | lands a takedown | .238 | .630 | **.392** |
| own clinch+ground share | lands a takedown | .331 | .541 | .210 |
| own knockdown rate | scores a knockdown | .114 | .294 | .180 |
| combined knockdown rate | ends inside distance | .410 | .588 | .178 |
| combined submission rate | goes the distance | .553 | .433 | .120 |
| opponent's takedown defence | lands a takedown | .495 | .386 | .109 |
| reach advantage | lands a takedown | .472 | .387 | .085 |
| combined strike volume | ends inside distance | .505 | .471 | .041 |
| age gap | ends inside distance | .495 | .510 | .041 |
| combined control share | ends inside distance | .480 | .486 | **.018** |

**The flat rows are rendered too, and that is the point.** A conditional-rate
display that only ever showed strong relationships would be indistinguishable
from one that picked them after looking, and a reader could not tell the
difference. Three of the ten are flat. The condition list is frozen in
[PREREGISTRATION addendum 10](mmastat/PREREGISTRATION.md) with a stated
mechanism for each, written before the five new ones were measured.

In a bout's drawer the same panel marks which quartile that matchup falls into
on every condition, so the general pattern and the specific fight are read off
one grid.

## Track record instead of AUC

The site does not print AUC anywhere. It reports what actually happened on
fights held out of training, recomputed on every refresh:

| it said | how often it was right | n |
|---|---|---|
| 50-60% | 60% | 366 |
| 60-70% | 68% | 299 |
| 70-80% | 83% | 138 |
| 80-100% | 81% | 26 |

| projection | model said (avg) | actually happened | n |
|---|---|---|---|
| takedown likely | 72% | **72%** | 394 |
| takedown unlikely | 24% | **25%** | 820 |
| knockdown unlikely | 16% | **17%** | 1,598 |

The takedown model is almost exactly calibrated — when it says 72%, it happens
72% of the time. That is a more useful and more checkable statement than
"AUC .742", and it is the same fact.

Deliberately NOT done: inventing a composite "confidence score". A made-up
index that makes a model look comprehensible is how sites overstate what they
know. These are counts, and a reader can verify them.

`mmastat/reliability.py` computes it; the numbers ride along in
`predictions.json` so the site always quotes its current record rather than a
figure hard-coded months ago.

## The site

The site shows each pick against the closing line where prices are available:
the probability bar carries a **gold tick at the market's number**, so agreement
and disagreement are visible at a glance, and every bout lists the features
driving its number. Market prices come from **The Odds API** (perpetual free tier, 500
requests/month; the daily job uses about 30). Get a key, then add it under
**Settings -> Secrets and variables -> Actions** as `ODDS_API_KEY`. Without it
the market panel is omitted rather than showing a stale price. A local
`data/ufc_betting_odds_daily.csv` is used as a fallback when present.

**Props have no market feed at all.** The Odds API covers MMA fight winner odds
only, and no free source quotes method, round or totals for MMA. So the site
prices 17 markets per bout from its own models and puts a market marker on the
moneyline alone. Everything else is labelled a projection, because a model
number next to a market number is a comparison and a model number by itself
is not.

`site/index.html` is the whole front end — one file, no framework, no build
step. It is a **single-screen dashboard**, not a scrolling list: the whole card
fits at once as a grid of tiles grouped by Main card / Prelims / Early prelims,
with detail in a slide-over drawer so you never lose your place.

Reading order is deliberate. The **model-vs-market strip** sits at the very top,
because disagreement is the only thing here a price cannot tell you. Each tile
carries a model bar, a gold market bar directly beneath it, and an amber
connector spanning the gap — so agreement and divergence read without a legend.
Prop chips on each tile surface the loudest projections (inside-the-distance,
takedown, round total, knockdown), with the strongest ones highlighted.

The timestamp is Eastern, to the minute, so you can tell which scheduled run
produced what is on screen. `Intl` handles the EDT/EST switch.

**Refresh runtime.** `reliability.build_record` refits six gradient-boosting
models and takes ~67 seconds — measured, it was the overwhelming majority of a
3m22s run, with every other step in single-digit seconds. What it measures
moves by one card a week, so it is now cached in `site/track_record.json` and
rebuilt only when it is 7+ days old or the corpus has grown by 40+ fights.
Combined with pip caching that takes a typical run to roughly a minute. It fetches `site/predictions.json` and renders the card, then a breakdown
per bout: win probability, a method distribution that sums to 100%, round-by-round
finish probabilities, and 80% ranges for strikes and control.

Publish it free on GitHub Pages: **Settings -> Pages -> Source -> "GitHub
Actions"**, once.

**The deploy lives inside `refresh.yml`**, as its last three steps. There is
no separate pages workflow, on purpose.

A standalone `pages.yml` listening on `push` never fires, because GitHub
deliberately does not trigger workflows for pushes made with the default
`GITHUB_TOKEN` — so the refresh job would commit a new `predictions.json`,
report success, and the site would keep serving a stale card. The obvious fix,
a `workflow_run` trigger, adds cross-workflow timing and a checkout-ref
subtlety of its own. Deploying in the same job that produced the files removes
the whole class of problem: the artifact uploaded is, by construction, the one
just generated.

`capture.yml` does not deploy, because the ledger it writes is not read by the
site.

To preview locally, serve the folder (opening the file directly will not work —
`fetch` is blocked on `file://`):

    python -m http.server -d site 8000     # then open http://localhost:8000

## Setup

1. Install dependencies: `pip install -r requirements.txt`
2. Put the four UFCStats CSVs in `data/` — see [data/README.md](data/README.md)
3. `python -m mmastat.tests` — **run this first, and after every change**
4. `python -m mmastat.train` — fits, validates, writes `mmastat/model.json`
5. `python -m mmastat.projections` — fits the output-range models
6. `python -m mmastat.props` — fits the prop models
7. `python -m mmastat.upcoming` — predicts the next card from `data/upcoming.txt`

To refresh everything by hand: `python -m mmastat.sources --corpus` (re-download
the corpus and the next card), then `python -m mmastat.upcoming --json`.

The market-residual work in `residual.py` additionally needs `ufc-master.csv`
in `data/` for the closing lines.

`python -m mmastat.train --synth` runs the whole thing on a simulated corpus
instead, which is the only mode with a known ceiling to compare against.

## Results

8,764 fights, 1994–2026, 98.3% of the source retained. Trained on 2012–2024,
tested on 829 fights it never saw (April 2024 – September 2026).

| model | accuracy | AUC | log loss |
|---|---|---|---|
| pick higher Elo | 54.5% | .5727 | .6827 |
| pick better record | 62.2% | .6486 | .6589 |
| **logistic, 14 features** | **67.1%** | **.7143** | **.6218** |
| gradient boosting | 63.7% | .6999 | .6290 |

Gradient boosting loses to plain logistic at this sample size. That's expected
with ~3,300 training fights and shouldn't be revisited until the corpus is much
larger.

The largest single factor is **age** (standardised weight −0.39), ahead of
striking output and Elo. Aging is more punishing in MMA than the modelling
literature suggests.

### Projections

Output is reported as calibrated ranges, never point estimates. The median model
explains ~11% of single-fight variance, so "expect 4.6 strikes/min" would be
false precision. Ranking is where the model earns its place.

| stat | R² | career-avg R² | Spearman | career-avg Spearman | coverage (claims 80%) |
|---|---|---|---|---|---|
| significant strikes /min | .104 | .040 | **.441** | .283 | 79.3% |
| control time (upper bound) | .116 | .156 | **.430** | .383 | 79.4% |

| event | base rate | AUC | career-avg AUC |
|---|---|---|---|
| lands ≥1 takedown | 42.4% | **.742** | .718 |
| lands ≥1 knockdown | 18.6% | **.668** | .638 |

Takedowns and knockdowns are zero-inflated — most fighters record none in most
fights, so quantile regression predicted a constant zero and scored R² = −0.34.
They're probabilities instead. Control time has a point mass at zero (17% record
none), which makes a two-sided interval dishonest: the lower bound excludes
nothing and the interval over-covers at 86.6%. It gets a one-sided bound.

Intervals are calibrated by conformalized quantile regression, which carries a
coverage guarantee that doesn't depend on the quantile models being good.

## Model vs market

582 fights with closing lines, same test window, model trained only on earlier
data.

| | accuracy | AUC | log loss |
|---|---|---|---|
| **closing line (devigged)** | **70.3%** | **.7643** | **.5794** |
| our model | 68.9% | .7337 | .6114 |
| market + model blend | 70.3% | .7716 | .5702 |

The market wins on every metric: log-loss gap −0.0317, 95% CI [−0.053, −0.010].
The two agree on 78.7% of fights; on the 124 where they disagree, the market is
right 53.2% and the model 46.8%. **The model's disagreements with the line are
worse than a coin flip.** Flat-staking them loses 3–6% per bet at every sane
threshold.

The blend does beat the market on log loss (+0.0092, CI [+0.0018, +0.0166]), but
accuracy is identical — it sharpens confidence, not picks. Nowhere near enough
to clear the 3.7% vig.

**Only 51% of the closing line is reproducible from this dataset** (R² of market
logit on our features). The other half is injuries, camp reports, weight cuts,
short-notice replacements and betting flow — none of which appear in a stat line.
That's a hard ceiling on this approach, not a tuning problem.

## Upcoming cards

There is no reliable free machine-readable feed of upcoming UFC fights.
UFCStats' upcoming page sits behind a JavaScript interstitial, and the Kaggle
`upcoming.csv` turned out to be a frozen snapshot five months stale. So
`data/upcoming.txt` is a hand-typed list — thirteen lines off any fight card,
no terminal, and nothing that can break silently:

    # UFC 331 | 2026-09-19 | Crypto.com Arena, Los Angeles
    ## Main card
    Joshua Van vs. Alexandre Pantoja | 5
    Arman Tsarukyan vs. Mauricio Ruffy
    ## Prelims
    Charles Jourdain vs. Marlon Vera
    ## Early prelims
    Giga Chikadze vs. Joanderson Brito

`## Main card` / `## Prelims` / `## Early prelims` group the card, and the site
renders each segment as its own block. `| 5` marks a five-round bout. The
Wikipedia parser reads these segments off the table's header rows, so the
grouping is automatic. A file with no markers is treated as all Main card.

`upcoming.py` resolves those names against the corpus, then emits win
probability, strike and control ranges, and takedown/knockdown probabilities per
fighter. It resolves nicknames ("Patricio Pitbull" to UFCStats' "Patricio
Freire") and spacing variants ("Joo Sang Yoo" to "JooSang Yoo"), and refuses
rather than guesses when it cannot.

**Refusing matters.** On UFC 331 it declined four of thirteen bouts: two
newcomers with too little history, and two names absent from the corpus. The
instructive one is Patricio Pitbull — his brother **Patricky** Freire is the one
carrying the nickname "Pitbull" in UFCStats' details file, so a fuzzy match
resolves to the wrong man. `data/aliases.txt` lets you state the mapping
yourself. A confident 50/50 on a debutant is worse than "no read".

## Market-residual model

`residual.py` puts `logit(p_market)` in as a fixed offset with its coefficient
pinned at 1, so every fitted coefficient answers one question: *given the
price, what does this feature still tell you?* This is the right architecture
for the question — the previous approach fit a standalone model and blended
afterwards, which wasted capacity re-deriving the 51% of the line our features
already reproduce.

Walk-forward over six disjoint test blocks, 2,034 fights:

| | accuracy | AUC | log loss |
|---|---|---|---|
| closing line | 66.32% | .7201 | .6114 |
| + residual, 22 features | 67.40% | .7235 | .6086 |
| + residual, 5 features | 67.21% | .7243 | **.6080** |

It beat the line in **6 of 6 folds** (sign test p ≈ 0.03). Pooled gain
**+0.0035, 95% CI [-0.0010, +0.0079], P(better) = 0.934** — consistent
direction, interval still crossing zero.

The coefficients are coherent, which is why this is worth pursuing rather than
filing with the null results. The line appears to **overreact to narrative
qualities and underreact to dull ones**: it under-penalises age (−0.158) and
getting hit (−0.147), while over-valuing knockdown power (−0.123) and
over-penalising veteran status (+0.098) and ring rust (+0.068).

Flat-staking with real payouts was positive at every threshold; the best single
cut was favourites with >2% edge, n=484, **+7.20%, t = +2.13**. That is one of
~12 cuts examined, so it does not survive multiple-comparison correction, and
the reserved holdout has **no odds** (the Kaggle file stops 2026-03-28) so it
cannot be tested there. The frozen rule and the forward-test protocol are in
[PREREGISTRATION.md](mmastat/PREREGISTRATION.md). **Do not bet this yet.**

## The largest available edge is not a model

Across 749 bouts where every book quoted within a 6-hour window (median 8
books), taking the **best** available price instead of a median book:

| | median book | shopping best of both sides |
|---|---|---|
| two-way overround | 5.68% | **1.70%** |
| payout uplift per bet | — | **+3.59% median** |

That is roughly four percentage points of cost removed per bet, mechanically,
with no model. **Every model edge found in this project is smaller than that.**
Line shopping across books dominates anything the features achieved.

Two caveats. 16.8% of those bouts show a negative overround, which would be
outright arbitrage — implausibly high for a real market, so some of the spread
is stale quotes rather than genuine disagreement even inside the 6-hour window.
And beating the closing line by shopping is not the same as beating the closing
line by knowing something. The overround reduction is the robust part; treat
the arbitrage figure as an upper bound.

## The opening line — the most promising lead, and exploratory

The market beats the model at the **close**. It may not at the **open**.

On 315 bouts with repeated snapshots the closing line is far sharper than the
opening one (log loss 0.5974 to 0.5670), so the market genuinely learns during
fight week. And on 209 held-out bouts the model predicts which way it learns:
where the model disagreed with the opening price, the line then moved toward
the model's number (Spearman +0.186, p = 0.007). Betting the model's side at
the open on every 2-point disagreement gave **mean CLV +1.21 points, 55.7%
positive**.

If it holds, the model is right but early — it reads the same public
statistics the market eventually prices, and the market takes a week to get
there. That is the first result in this project that points at a mechanism
rather than a coefficient.

**It is exploratory.** Thresholds were chosen on the same data, it is one
analysis among many, and 209 bouts is small. The rule is frozen in
PREREGISTRATION addendum 13 and the forward ledger is already recording the
snapshots needed to test it honestly. Do not bet it.

## Favourite-longshot bias: there is less chaos than the market prices

12,976 fighter-prices, 2010-2026, real payouts from raw American odds:

| implied (incl. vig) | n | actual | ROI | t |
|---|---|---|---|---|
| <=20% | 555 | 11.2% | **-32.6%** | -3.98 |
| 20-30% | 1,421 | 22.0% | **-15.6%** | -3.66 |
| 40-50% | 2,038 | 44.8% | -0.4% | -0.14 |
| 70-80% | 1,741 | 73.9% | -1.0% | -0.68 |
| >=80% | 889 | 86.7% | +2.0% | 1.48 |

Underdogs at 30% or worse return **-20.4%, t = -5.31**. Bettors systematically
overpay for upsets. So hunting for a "chaos signal" to find live underdogs is
working into a 20% headwind on top of the vig.

The other side is not free either: heavy favourites (>=70%) return **+0.03%,
t = 0.03** — exactly break-even — and the effect has decayed by era (+1.87%,
+0.26%, -1.17%, -0.93%).

## Props

`props.py` models method, distance and round lines on **symmetric** features
(sum and absolute gap, never signed differentials — whether a fight goes to
decision does not depend on which corner you call red).

| prop | base rate | AUC | Brier gain |
|---|---|---|---|
| KO/TKO | 30.4% | **.665** | +.0153 |
| submission | 16.8% | .651 | +.0012 |
| decision | 52.8% | .626 | +.0083 |
| over 2.5 rounds | 57.8% | .623 | +.0078 |
| over 1.5 rounds | 70.3% | .609 | -.0001 |

KO/TKO is the most predictable. There are **no prop odds in any of the source
datasets**, so this establishes that the props are predictable, not that they
beat a prop market. Those are different claims and the gap between them is the
whole question.

## Competing-risks hazard model

`survival.py` treats a fight ending as a race between four hazards — each
fighter by KO or submission — against the clock, in one-minute intervals,
censored at each bout's own scheduled length. One fit yields method, round and
winner from a single survival curve.

| | hazard model | independent classifiers | market |
|---|---|---|---|
| KO/TKO AUC | .6617 | **.6686** | — |
| submission AUC | **.6625** | .6477 | — |
| decision AUC | .6077 | **.6234** | — |
| KO/TKO Brier (market subset) | **.1873** | — | .1922 |
| submission Brier | .1319 | — | **.1216** |
| decision Brier | .2533 | — | **.2363** |

Accuracy is a wash. **Coherence is not.** The independent classifiers in
`props.py` sum to a mean of 0.9619 across KO/submission/decision, ranging from
0.584 to 1.313 — some fights get a method distribution totalling 131%. The
hazard model sums to **1.000000** by construction and throws off P(ends in
round N) and expected duration for free. For a site that publishes these
numbers, that is worth more than a thousandth of Brier.

Calibrated on a validation slice: P(decision) .487 vs .525 actual, P(KO) .307
vs .303, P(sub) .205 vs .172.

On who wins, the direct logistic still leads (66.01% / .7153 AUC) over the
hazard roll-up (65.02% / .7073), with the average of the two marginally best
(66.26% / .7164). Use the hazard model for method and round, the logistic for
the winner.

### The bug that made it look inverted

The first version rolled the survival curve over each fight's *observed*
duration instead of its *scheduled* duration. A first-round knockout then had
one interval to decay over and came out as the likeliest decision on the card —
P(decision) scored **AUC 0.19**, reliably backwards. That is the survival
analogue of temporal leakage: using the outcome to decide how far to integrate.
`test_no_leakage` does not catch it, because it lives in the prediction step
rather than the feature build.

## Props lose to the prop market too

The daily-odds feed turned out to carry **4,867 fights with a complete six-leg
method-of-victory market** (each fighter by KO / submission / decision),
joinable to the corpus by the UFCStats fight hash. That answered the question
`props.py` could not: do our prop models beat a prop market?

No. Out of sample on 912 fights, 2021-2024:

| prop | our AUC | market AUC | our Brier | market Brier |
|---|---|---|---|---|
| KO/TKO | .598 | **.667** | .2269 | **.2004** |
| submission | .625 | **.680** | .1564 | **.1412** |
| decision | .583 | **.663** | .2611 | **.2335** |

KO/TKO Brier, bootstrapped: market better by **-0.0267, 95% CI [-0.0371,
-0.0162], P(model better) = 0.000**. The margin is *wider* than on the
moneyline, which kills the "props are less efficient so look there" reasoning.

And the cost is brutal: the median six-way overround is **22.3%**, against 3.7%
on the moneyline. Method markets are roughly six times more expensive to bet,
so even a real edge would struggle to clear the margin.

(The stacked blend in that run is not reported because it was fit on in-sample
base-model predictions and overfit catastrophically — a meta-model needs
cross-validated inputs. Mentioned so nobody revives the number.)

## Things that were tested and did not work

Recorded because a negative result you can't find gets re-tried forever.

| idea | result |
|---|---|
| target/position style features (head/body/leg, distance/clinch/ground, knockdowns) in the win model | log loss **−0.0009**, CI [−0.0041, +0.0022], P(better) 0.293 — no effect |
| margin-aware rating (continuous dominance score instead of binary W/L) | no effect |
| pace decay / cardio (round 3 strikes ÷ round 1) | no effect |
| both new ratings together | **−0.0001**, CI [−0.0013, +0.0011] |
| clustering fights into types, then predicting the type | log loss 1.1296 vs 1.1157 for a constant — worse than constant |
| gradient boosting on the win model | loses to logistic by 3.4 points |
| isotonic calibration | overfits a few-hundred-row slice, cost ~2 points of accuracy; use Platt |
| prop models vs the prop market | market wins on all three, wider margin than the moneyline; P(model better) = 0.000 |
| the fortitude cluster: late-round accuracy change, volume/control retention, induced opponent decay, deep-round experience, knockdown recovery | pre-registered, **0 of 6 survive BH correction**, 5 of 6 carry the wrong sign; whole cluster makes the model *worse* (-0.0016, CI [-0.0052, +0.0019]) |
| Bradley-Terry maximum-likelihood ratings (Elo's exact form) | catastrophic: 47-50% alone, and **-0.17 to -0.42** log loss when added. ~2,700 fighters and ~3,200 training fights means more free parameters than observations |
| Pythagorean expectation on strikes landed/absorbed, combined via Bill James log5 | null at every exponent tested (k = 1.0 to 4.0), gain -0.0017 to -0.0023 |
| pruning the 3 features whose CIs cross zero | looked like a win on test (67.55% vs 67.31%) — **that was test-set overfitting**. Selected honestly on validation it reversed: -0.0044, CI [-0.0090, +0.0002] |
| recency weighting (exponential decay on career accumulators) | null at the validation-chosen 3-year half-life: +0.0000, CI [-0.0052, +0.0046]. Shorter half-lives were negative |
| nine pre-registered style interactions (leg-kicks x stance, reach x range, power x chin, ...) | **0 of 9**; smallest p .060 against a .011 threshold. Reach x range and reach x clinch both came back with the WRONG sign — reach is a flat effect |
| venue (Apex small cage), altitude, referee stoppage tendency, weight-class moves, career mileage | **0 of 5**. Referee career KO rates spread 0.160-0.437 but predict the next fight at AUC .523 |
| damage-weighted striking (head 1.0 / body 0.6 / leg 0.4, from boxing and Muay Thai scoring) | null: +0.0000 added, -0.0017 when swapped in. **Swapping raised accuracy to 68.17%, the best this model has shown, while log loss got worse** — a live demonstration of why the pre-registered metric is log loss |
| active vs stalling control (ground strikes per second of control, from IBJJF positional scoring) | null: -0.0004, CI [-0.0009, +0.0001] |
| ten cross-field formulas (log-ratios, ape index, Sharpe consistency, volatility, method entropy, Gompertz age, sqrt experience, kinetic power, inverse-square reach) | **0 of 10**. Kinetic power had the smallest p-value AND a significantly negative effect — a "keep what's significant" search would have promoted a transform that hurts |
| line drift beyond the closing price ("steam") | nothing beyond the close: +0.0006, CI [-0.013, +0.013] |
| historical-analog dispersion as a chaos signal | correlates with market error at +.172, but the trivial "is the line close to even" baseline correlates at +.468; AUC 0.548 alone vs 0.642 for the price, and adds nothing on top |

The style features are kept in the codebase because they demonstrably help
**projections** (strike ranking Spearman .441 vs .283; takedown AUC .742) and
because the frontend displays them. They are deliberately excluded from
`WIN_FEATURES`. Interesting aside from the cardio test: median round-3 output is
**1.00×** round-1 output. Fighters do not measurably slow down, which contradicts
most commentary.

### A leakage trap in a popular public dataset

The `full_data_silver_plus` parquet on Kaggle carries `f_1_fighter_SlpM`,
`f_1_fighter_Str_Def`, `f_1_fighter_TD_Def` and similar. Those are
career-aggregate values scraped from each fighter's **current** UFCStats
profile, so on a 2015 fight row they summarise a career running through 2026.
Used as features they are textbook temporal leakage, and nothing about them
looks wrong. Several public UFC models are built on that shape of data, which
is one reason published accuracy figures above ~75% should be treated as bugs
until proven otherwise.

This project does not use those files. The round-level data they contain is
already in `ufc_fight_stats.csv`, and the ~12,000 rolling-window features in
`ufc_features.parquet` duplicate what `features.walk()` computes under a tested
guarantee. Importing unverified equivalents would trade away the one property
that makes any of these numbers meaningful.

Next ideas are pre-registered in [mmastat/PREREGISTRATION.md](mmastat/PREREGISTRATION.md)
with predicted signs and a reserved holdout, so they can't be reported
selectively after the fact.

## How it's kept honest

`python -m mmastat.tests` runs five checks:

1. **No temporal leakage.** Random fights are rebuilt from a corpus truncated at
   that fight and the features must be bit-identical. Currently 0.00e+00 drift.
   This is the failure mode that produces beautiful backtests and worthless
   models, and it fails silently.
2. **Antisymmetry.** Every feature satisfies f(A,B) = −f(B,A), and the model is
   fit without an intercept, so P(A beats B) = 1 − P(B beats A) exactly. This
   matters: UFCStats lists the winner first, so the red corner "wins" 62.7% of
   the time in the raw file. A model trained naively learns that.
3. **Null model.** Shuffled labels must score ~0.500 AUC, averaged over seeds
   because one 300-row test block has a standard deviation of ±0.035.
4. **Settle respects dates.** A rematch must not be settled with the earlier
   fight's result. This fired for real on the first live capture and is the
   reason the check exists.
5. **Leak detector has power.** Injecting a post-fight statistic must move the
   metric (+0.12 AUC). A leakage test never shown to fire proves nothing.

`build()` and `build_panel()` share one update path, so there is exactly one
place in the codebase where leakage could be introduced. This was not true for
a while: `walk()` was added but `build()` kept its own inline copy, and when the
fortitude accumulators arrived the two went out of sync — `build()` passed the
old field list and silently produced constant features. The tests all still
passed, because constant features are not leaky, just useless. Duplicated
update logic is the bug; the near-miss is why it is now a single path.

## Data notes

- **Fighter identity** keys on the UFCStats URL hash, never on names. Seven
  names are shared by two different fighters (two Bruno Silvas, two Jean
  Silvas); keying on strings merges two careers and corrupts every average.
  Those 51 bouts are dropped.
- **Reach** is imputed from height for 25.7% of fighters (reach ≈ −1.9 + 1.05 ×
  height), flagged in `reach_imputed`. Dropping them would cost a quarter of the
  corpus.
- **Round lengths** are read from the TIME FORMAT column, not assumed to be
  5:00 — early UFC had 12-minute rounds and 20-minute single rounds.
- **Default cutoff is 2012.** Older fights still build each fighter's history,
  they just aren't trained on. Strike volume tripled between 1995 and 2015;
  including the 1990s costs ~1.7 points.

## Layout

    mmastat/
      config.py        priors, shrinkage constants, Elo settings — the tuning surface
      state.py         per-fighter accumulator; the only thing that advances history
      features.py      walk() + build() (win model) + build_panel() (projections)
      loaders.py       UFCStats CSV dump -> the two frames everything else expects
      odds.py          closing-line join and devigging (mdabbert source)
      odds_daily.py    the jerzyszocik feed: prop markets, multi-book, line movement
      projections.py   calibrated output ranges and event probabilities
      residual.py      market-offset model: what the line does not price
      props.py         method / distance / rounds on symmetric features
      train.py         fit, walk-forward validate, calibrate, export model.json
      tests.py         the four checks
      synth.py         simulated corpus with known latents (has an ORACLE ceiling)
      sources.py       automated refresh: corpus from GitHub, cards from Wikipedia
      upcoming.py      resolve a card to fighter_ids and predict it
    site/
      index.html       the site: reads predictions.json, no build step
      predictions.json committed output of the refresh job
    data/              you put the CSVs here; not committed

## Not affiliated with the UFC

Statistics compiled from public records. This is an analysis tool. It is not
betting advice, and the market comparison above is the reason why.
