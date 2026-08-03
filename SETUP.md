# Crosstalk — setup and operation

**crosstalkwire.com** · a monitoring tool for cross-sector language in the trade press.

Everything runs on GitHub. No terminal, nothing installed locally, no servers.

---

## What it does

Reads 43 sector-specific trade publications every six hours, counts which
phrases each industry is using, and reports which ones have crossed from one
industry's coverage into another's — with the articles linked.

It makes no forecast. Testing found that neither a phrase accelerating nor
spreading across sectors predicted what followed, so scores and historical
analogs were removed rather than shown with caveats.

---

## The files

| file | role |
|---|---|
| `collect.py` | Reads feeds, extracts phrases, writes `data.json`. The core |
| `config.json` | 43 feeds tagged by sector. **The thing most worth editing** |
| `companies.json` | 177 companies matched against article text |
| `dashboard.html` | The interface. Reads `data.json`, falls back to samples |
| `prices.py` | Daily sector ETF closes. Stored, deliberately not displayed |
| `backtest.py` | The prediction test. Refuses to run until the data supports it |
| `edgar.py` | SEC filings probe. Exploratory |
| `chorus.db` | Accumulated history. **Never delete or rename this** |

The database keeps its original filename on purpose — renaming it would orphan
every article collected so far.

---

## Running it

The schedule fires every six hours by itself. Nothing needs doing.

To run by hand: **Actions → collect → Run workflow**. Leave `backfill_weeks` at
`0` for a normal run.

**Actions → probe** tests all 43 feeds plus the price providers and names
anything broken. Run it after editing `config.json`.

---

## Reading a run

The first line fingerprints what actually executed:

```
crosstalk v3 · code 5ebb19ac · config e5e346a1
  14 sectors · 43 mastheads · 177 companies · 43 rss
```

If those numbers don't match what you expect, the workflow is running older
files and nothing below the banner means anything.

Then: which feeds responded, how many articles are new, the total corpus, and
how many phrases reached two or more sectors. Feeds that fail three times in
three days are named for removal.

---

## Secrets

| name | needed for |
|---|---|
| `TWELVEDATA_KEY` | Sector prices. Free, email signup |
| `EDGAR_UA` | SEC probe. Format: `crosstalk-research you@crosstalkwire.com` |

Settings → Secrets and variables → Actions.

---

## The part worth your attention

`config.json` is the product. Everything else is plumbing.

Sector labels come from the publication — a Utility Dive article is utilities
coverage by definition. That mapping is where every cross-sector claim
originates, so the quality of the feed list determines the quality of the
output. One feed belongs to exactly one sector; listing it twice would
manufacture spread that isn't there.

To add a publication:

```json
{ "type": "rss", "url": "https://example.com/feed/", "publisher": "Example Weekly" }
```

Then run the probe. It names anything that fails.

---

## Deliberate omissions

**No article text is stored.** Headlines, summaries, links and counts only —
cheaper, and clear of redistribution questions.

**No language models.** Counting phrases is arithmetic. Running cost is zero.

**No sentiment.** It is what everyone else sells, and it is not what this
measures.

**Prices are collected but not shown.** A price chart under a spread map implies
causation that testing did not find. The data is there so the question can be
asked properly later, not so it can be hinted at now.
