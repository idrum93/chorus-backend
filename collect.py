#!/usr/bin/env python3
"""
collect.py — Chorus collector, v3.

VERSION MARKER: this is v3. Confirm by searching for DOC_FREQ_CEILING, which
does not exist in earlier versions, or by reading the banner the script prints
as its first line of output.

v3 changes: HTML entities decoded; two- and three-word phrases only (single
words lost to generic vocabulary); a document-frequency ceiling so ubiquitous
phrases are excluded; browser user-agent retry on 403.

Three interchangeable sources behind one interface:

    rss         free, no key, live only          (works today)
    newsapi_ai  paid, archive back to 2014       (backfill + live)
    gdelt       free, no key, ~3 month window    (if reachable)

Nothing depends on any single one. Enable what you have; the rest sit idle.

    python3 collect.py --probe                 test every configured source
    python3 collect.py --backfill --months 12  pull history, once
    python3 collect.py                         normal run, every few hours
    python3 collect.py --report                print what's spreading

Standard library only. Set NEWSAPI_AI_KEY in the environment to enable the
paid source; without it the collector runs on RSS alone and says so.

IMPORTANT — look-ahead honesty. Backfilled rows record the article's publication
date AND a flag saying they were imported later. Any future test of "what did we
know on date X" must exclude backfilled rows, or it will quietly cheat by using
knowledge that wasn't available at the time. The flag exists so that mistake is
impossible to make by accident.
"""

import argparse, hashlib, html, json, os, re, sqlite3, sys, time
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE   = os.path.dirname(os.path.abspath(__file__))
DB     = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))
CONFIG = os.path.join(HERE, "config.json")
OUT    = os.path.join(HERE, "data.json")
VERSION = "v3"
# Bump whenever ngrams(), the stopword sets, or singularise() change. Stored in
# the database; a mismatch rebuilds every derived count. Detecting one specific
# old format was too narrow - new rules were silently not applied to old rows.
EXTRACT_VERSION = 5
UA     = "chorus-monitor/3.0 (news language monitoring)"
KEY    = os.environ.get("NEWSAPI_AI_KEY", "").strip()

WINDOW_DAYS, RECENT_DAYS = 90, 14
MIN_ARTICLES, MIN_SECTORS, MAX_TERMS = 3, 2, 80
DEDUPE_DAYS = 4          # a wire story syndicates within days, not months
DOC_FREQ_CEILING = 0.12   # a phrase in >12% of articles is vocabulary, not news

STOP = set("""
a about above after again against all also am an and any are aren as at be because been before being
below between both but by can cannot could did didn do does doesn doing don down during each few for
from further had has have having he her here hers herself him himself his how i if in into is isn it
its itself just me more most my myself no nor not now of off on once only or other ought our ours out
over own same she should so some such than that the their theirs them themselves then there these they
this those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself says said say new news first last week year years day days
report reports according company companies market markets business inc corp ltd percent million billion
quarter update updates amid could would may might
""".split())

# Words that carry no narrative on their own. A phrase built only from these
# is boilerplate, however many sectors it appears in.
GENERIC = set("""
cost costs price prices sales revenue revenues earnings profit profits growth growing
demand supply plan plans plant plants project projects program programs facility facilities
production produce market markets share shares stock stocks quarter quarterly annual
first second third fourth year years month months week weeks percent million billion
increase increases decrease decreases higher lower rise rises fall falls up down
chief executive officer president director board management team employees workers
statement statements release announced announces announcement said says report reports
new latest recent current major large small big top best worst good bad
industry industries sector sectors business businesses firm firms company companies
government federal state national international global world
cash call calls flow flows offering offerings investment investments fiscal income margin
margins dividend dividends buyback guidance outlook forecast forecasts estimate estimates
per share shares stake stakes deal deals agreement agreements contract contracts
administration administrations official officials spokesperson comment comments
initial public private total net gross average overall
""".split())

# Verbs and adverbs make grammatical debris when they land at a phrase edge:
# "held the main", "dissents the central". Nouns carry topics; these don't.
EDGE_STOP = set("""
held hold holds holding said says say saying warns warn warned expects expect expected
sets set setting cuts cut cutting raises raise raised keeps keep kept leaves leave left
makes make made takes take took gives give gave sees see saw goes go went comes come came
gets get got puts put adds add added shows show showed tells tell told asks ask asked
begins begin began ends end ended starts start started stops stop stopped
administration administrations official officials spokesperson spokesman spokeswoman
main steady likely unlikely possible able about across among during through toward
also just even still yet only very much many more less most least
""".split())

# Interior joining words. "cost of capital" is a real term, so "of" survives;
# the rest turn phrases into sentence fragments.
INTERIOR_BAD = set("""
the a an and or but nor so yet if then than that which who whom whose this these those
is are was were be been being has have had do does did will would shall should may
might must can could to for with from into onto upon at by on in as it its their his her
""".split())

# Places, durations and cardinal numbers are not topics. "United States" in
# nine sectors tells you nothing; "rare earth" in nine sectors tells you a lot.
PLACES = set("""
united states america american europe european china chinese asia asian africa india
japan japanese germany german france french britain british england uk usa canada
canadian mexico brazil russia russian ukraine texas california york washington london
brussels beijing global national federal state county city region regional country
countries world worldwide north south east west northern southern eastern western
""".split())

NUMBERS = set("""
one two three four five six seven eight nine ten eleven twelve twenty thirty forty fifty
hundred thousand million billion trillion first second third fourth fifth half quarter
day days week weeks month months year years decade quarter hour hours minute minutes
monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december
""".split())

BOILER = re.compile(r"(read more|click here|subscribe|newsletter|all rights reserved|continue reading)", re.I)
TAG    = re.compile(r"<[^>]+>")
WORD   = re.compile(r"[a-z][a-z'\-]{1,}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, url TEXT, title TEXT, summary TEXT, publisher TEXT,
  sector TEXT, source TEXT, published TEXT, observed TEXT, backfilled INTEGER DEFAULT 0,
  fp TEXT
);
CREATE TABLE IF NOT EXISTS grams(
  day TEXT, sector TEXT, gram TEXT, n INTEGER, articles INTEGER,
  PRIMARY KEY(day, sector, gram));
CREATE TABLE IF NOT EXISTS totals(
  day TEXT, sector TEXT, articles INTEGER, PRIMARY KEY(day, sector));
CREATE TABLE IF NOT EXISTS cooc(
  day TEXT, gram TEXT, other TEXT, n INTEGER, PRIMARY KEY(day, gram, other));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runlog(
  ts TEXT, source TEXT, sector TEXT, ok INTEGER, items INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS idx_grams_gram ON grams(gram);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published);
"""


# ----------------------------------------------------------------- text ---
def clean(t):
    """Decode entities BEFORE stripping tags, twice — feeds routinely
    double-encode, which is how &rsquo; became a top-ranked 'phrase'."""
    t = html.unescape(html.unescape(t or ""))
    t = TAG.sub(" ", t)
    t = t.replace("\u2019", "'").replace("\u2014", " ").replace("\u2013", " ")
    return re.sub(r"\s+", " ", t).strip()

def ngrams(text, nmax=3):
    """Two- and three-word phrases only.

    Single words lose to generic business vocabulary every time — costs,
    supply, demand, sales. A narrative needs a phrase. A phrase made entirely
    of generic words is rejected too, so 'higher costs' doesn't replace 'costs'
    as the same non-signal wearing a hat.
    """
    words = WORD.findall(text.lower())
    out = []
    for n in (2, 3):
        for i in range(len(words)-n+1):
            g = words[i:i+n]
            if g[0] in STOP or g[-1] in STOP: continue
            if g[0] in EDGE_STOP or g[-1] in EDGE_STOP: continue
            if all(w in GENERIC or w in STOP for w in g): continue
            if any(len(w) < 3 for w in g): continue
            if any(w in INTERIOR_BAD for w in g[1:-1]): continue
            if all(w in PLACES for w in g): continue
            if any(w in NUMBERS for w in g): continue
            out.append(singularise(" ".join(g)))
    return out

def fingerprint(title):
    """Identity of a story, independent of where it was republished.

    A wire story carried by four feeds is one story. Counting it four times
    would invent cross-sector spread out of syndication, which is precisely the
    thing the product claims to detect.
    """
    words = sorted(set(w for w in WORD.findall((title or "").lower())
                       if w not in STOP and len(w) > 2))
    return hashlib.sha1(" ".join(words[:12]).encode()).hexdigest()[:16] if words else None


def singularise(phrase):
    """Fold trailing plurals so 'data center' and 'data centers' are one entry.

    Deliberately crude: only the last word, only a trailing 's', and only when
    the result is still a plausible word. Real stemming would need a dependency
    and would mangle more than it fixes at this scale.
    """
    words = phrase.split()
    last = words[-1]
    if (len(last) > 4 and last.endswith("s") and not last.endswith(("ss", "us", "is", "as"))):
        words[-1] = last[:-1]
    return " ".join(words)


def host(url, fallback=""):
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).replace("www.", "") if m else fallback

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

def http(url, data=None, timeout=45, headers=None):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    if body: h["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=body, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code != 403 or data is not None:
            raise
        h["User-Agent"] = BROWSER_UA          # some feeds reject unfamiliar clients
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()


# ================================================================ sources ==
# Every adapter returns: [{url, title, summary, publisher, published(ISO date)}]

def src_rss(spec, since=None, until=None):
    """Free. Live only — RSS has no archive, so `since` is ignored."""
    raw = http(spec["url"], timeout=30)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"): continue
        f = {}
        for ch in node:
            f.setdefault(ch.tag.split("}")[-1], ch)
        def t(k):
            el = f.get(k)
            return clean(el.text or el.get("href") or "") if el is not None else ""
        link = t("link") or t("id")
        if not link:
            for ch in node:
                if ch.tag.split("}")[-1] == "link" and ch.get("href"):
                    link = ch.get("href"); break
        src = f.get("source")
        out.append({
            "url": link, "title": t("title"),
            "summary": t("description") or t("summary"),
            "publisher": (clean(src.text) if src is not None and src.text else "")
                         or spec.get("publisher") or host(link),
            "published": (t("pubDate") or t("published"))[:64],
        })
    return out


def src_newsapi_ai(spec, since=None, until=None):
    """Paid. Archive to 2014. One call returns up to 100 articles per page.

    Token cost rises with how many years back you search, so backfill month by
    month rather than asking for everything at once.
    """
    if not KEY:
        raise RuntimeError("NEWSAPI_AI_KEY not set")
    query = {
        "action": "getArticles",
        "keyword": spec["query"],
        "keywordOper": "or",
        "lang": "eng",
        "articlesPage": spec.get("page", 1),
        "articlesCount": 100,
        "articlesSortBy": "date",
        "resultType": "articles",
        "apiKey": KEY,
    }
    if since: query["dateStart"] = since
    if until: query["dateEnd"] = until
    raw = http("https://eventregistry.org/api/v1/article/getArticles", data=query)
    doc = json.loads(raw)
    if "error" in doc:
        raise RuntimeError(str(doc["error"])[:180])
    results = (doc.get("articles") or {}).get("results", [])
    return [{
        "url": a.get("url", ""),
        "title": clean(a.get("title", "")),
        "summary": clean(a.get("body", ""))[:600],
        "publisher": (a.get("source") or {}).get("title") or host(a.get("url", "")),
        "published": a.get("date", "")[:10],
    } for a in results]


def src_gdelt(spec, since=None, until=None):
    """Free, no key. Rolling window only. Unreachable from some networks —
    the probe will say so rather than failing silently."""
    params = {"query": spec["query"] + " sourcelang:english",
              "mode": "artlist", "maxrecords": 250, "format": "json"}
    if since: params["startdatetime"] = since.replace("-", "") + "000000"
    if until: params["enddatetime"]   = until.replace("-", "") + "000000"
    raw = http("https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(params), timeout=40)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON reply: {raw[:120]!r}")
    return [{
        "url": a.get("url", ""), "title": clean(a.get("title", "")), "summary": "",
        "publisher": a.get("domain", ""), "published": (a.get("seendate", "")[:8] or ""),
    } for a in doc.get("articles", [])]


ADAPTERS = {"rss": src_rss, "newsapi_ai": src_newsapi_ai, "gdelt": src_gdelt}


def normalise_date(s):
    """Whatever a source gives us, get an ISO date or today."""
    s = (s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s): return s[:10]
    if re.match(r"^\d{8}$", s): return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try: return datetime.strptime(s, fmt).date().isoformat()
        except ValueError: pass
    return datetime.now(timezone.utc).date().isoformat()


# ============================================================== ingestion ==
def store(conn, articles, sector, source, backfilled=False):
    now = datetime.now(timezone.utc).isoformat()
    by_day_grams   = defaultdict(Counter)
    by_day_arts    = defaultdict(Counter)
    by_day_cooc    = defaultdict(Counter)
    by_day_totals  = Counter()
    added = 0

    for a in articles:
        if not a["url"] or not a["title"] or BOILER.search(a["title"]): continue
        aid = hashlib.sha1(a["url"].encode()).hexdigest()[:16]
        if conn.execute("SELECT 1 FROM articles WHERE id=?", (aid,)).fetchone(): continue
        day = normalise_date(a["published"])
        fp = fingerprint(a["title"])
        if fp:
            # Syndication happens within hours. The same headline months later is
            # a different story - "Fed holds rate steady" recurs all year.
            window = (datetime.fromisoformat(day) - timedelta(days=DEDUPE_DAYS)).date().isoformat()
            if conn.execute("SELECT 1 FROM articles WHERE fp=? AND published>=? AND published<=?",
                            (fp, window, day)).fetchone():
                continue
        conn.execute("INSERT INTO articles VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            aid, a["url"], a["title"][:400], (a["summary"] or "")[:600],
            a["publisher"] or host(a["url"]), sector, source, day, now,
            int(backfilled), fp))
        added += 1
        by_day_totals[day] += 1

        grams = ngrams(a["title"] + ". " + (a["summary"] or ""))
        uniq = set(grams)
        by_day_grams[day].update(grams)
        by_day_arts[day].update(uniq)
        phrases = sorted(g for g in uniq if " " in g)[:40]
        for i, x in enumerate(phrases):
            for y in phrases[i+1:]:
                by_day_cooc[day][(x, y)] += 1
                by_day_cooc[day][(y, x)] += 1

    for day, counter in by_day_grams.items():
        for g, n in counter.items():
            conn.execute("INSERT INTO grams VALUES(?,?,?,?,?) ON CONFLICT(day,sector,gram) "
                         "DO UPDATE SET n=n+?, articles=articles+?",
                         (day, sector, g, n, by_day_arts[day][g], n, by_day_arts[day][g]))
    for day, counter in by_day_cooc.items():
        for (x, y), n in counter.items():
            conn.execute("INSERT INTO cooc VALUES(?,?,?,?) ON CONFLICT(day,gram,other) "
                         "DO UPDATE SET n=n+?", (day, x, y, n, n))
    for day, n in by_day_totals.items():
        conn.execute("INSERT INTO totals VALUES(?,?,?) ON CONFLICT(day,sector) "
                     "DO UPDATE SET articles=articles+?", (day, sector, n, n))
    conn.commit()
    return added


def rebuild(conn, verbose=True):
    """Recompute every derived count from the stored articles.

    Needed whenever the extraction rules change: old rows were produced by the
    old rules and would otherwise outrank anything new. Articles are untouched,
    so this costs nothing and fetches nothing.
    """
    rows, seen_fp = [], {}
    for title, summary, sector, published, url in conn.execute(
            "SELECT title, summary, sector, published, url FROM articles "
            "ORDER BY published"):
        fp = fingerprint(title)
        day = (published or "")[:10]
        if fp:
            prev = seen_fp.get(fp)
            if prev and day and prev and (
                    datetime.fromisoformat(day) - datetime.fromisoformat(prev)).days <= DEDUPE_DAYS:
                continue
            seen_fp[fp] = day or seen_fp.get(fp)
        rows.append((title, summary, sector, published, url))
    if verbose:
        print(f"  rebuilding counts from {len(rows)} stored articles")
    conn.execute("DELETE FROM grams"); conn.execute("DELETE FROM cooc")
    conn.execute("DELETE FROM totals")

    g_n  = defaultdict(Counter)   # (day, sector) -> gram counts
    g_a  = defaultdict(Counter)
    c_n  = defaultdict(Counter)   # day -> pair counts
    tot  = Counter()

    for title, summary, sector, day, url in rows:
        day = (day or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        tot[(day, sector)] += 1
        grams = ngrams(clean(title) + ". " + clean(summary or ""))
        uniq = set(grams)
        g_n[(day, sector)].update(grams)
        g_a[(day, sector)].update(uniq)
        phrases = sorted(uniq)[:40]
        for i, x in enumerate(phrases):
            for y in phrases[i+1:]:
                c_n[day][(x, y)] += 1
                c_n[day][(y, x)] += 1

    for (day, sector), counter in g_n.items():
        for g, n in counter.items():
            conn.execute("INSERT OR REPLACE INTO grams VALUES(?,?,?,?,?)",
                         (day, sector, g, n, g_a[(day, sector)][g]))
    for day, counter in c_n.items():
        for (x, y), n in counter.items():
            conn.execute("INSERT OR REPLACE INTO cooc VALUES(?,?,?,?)", (day, x, y, n))
    for (day, sector), n in tot.items():
        conn.execute("INSERT OR REPLACE INTO totals VALUES(?,?,?)", (day, sector, n))
    conn.commit()
    if verbose:
        left = conn.execute("SELECT COUNT(*) FROM grams").fetchone()[0]
        print(f"  {left} phrase-days rebuilt\n")
    return len(rows)


def extract_version(conn):
    r = conn.execute("SELECT value FROM meta WHERE key='extract_version'").fetchone()
    return int(r[0]) if r else 0


def set_extract_version(conn):
    conn.execute("INSERT INTO meta VALUES('extract_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=?",
                 (str(EXTRACT_VERSION), str(EXTRACT_VERSION)))
    conn.commit()


def stale_counts(conn):
    """Single-word rows can only have come from a version before phrases-only."""
    return conn.execute(
        "SELECT COUNT(*) FROM grams WHERE gram NOT LIKE '% %'").fetchone()[0]


def run_once(conn, cfg, since=None, until=None, backfilled=False, verbose=True):
    total, ok, tried = 0, 0, 0
    for sector in cfg["sectors"]:
        for spec in sector["sources"]:
            kind = spec["type"]
            if kind not in ADAPTERS: continue
            if kind == "newsapi_ai" and not KEY: continue
            tried += 1
            note, items = "", []
            try:
                items = ADAPTERS[kind](spec, since, until)
                ok += 1
            except Exception as e:
                note = str(e)[:160]
            n = store(conn, items, sector["name"], kind, backfilled) if items else 0
            total += n
            conn.execute("INSERT INTO runlog VALUES(?,?,?,?,?,?)", (
                datetime.now(timezone.utc).isoformat(), kind, sector["name"],
                int(not note), len(items), note))
            if verbose:
                print(f"  {'ok  ' if not note else 'FAIL'} {sector['name'][:14]:<15} "
                      f"{kind:<11} {len(items):>4} items  {n:>4} new  {note}")
            time.sleep(0.4)
    conn.commit()
    return total, ok, tried


def backfill_gdelt(conn, cfg, weeks):
    """Free backfill. GDELT holds a rolling window, so we walk back week by week
    and take whatever it still has. One request per sector per week, spaced out —
    GDELT blocks anything faster than a request every five seconds."""
    grand, today = 0, datetime.now(timezone.utc).date()
    specs = [(s["name"], src) for s in cfg["sectors"] for src in s["sources"]
             if src["type"] == "gdelt"]
    if not specs:
        print("  No gdelt sources in config.json."); return 0

    print(f"  {len(specs)} sector queries × {weeks} weeks = {len(specs)*weeks} requests")
    print(f"  Spaced 5s apart, so roughly {len(specs)*weeks*5//60} minutes. Let it run.\n")

    empty_weeks = 0
    for w in range(weeks):
        end   = today - timedelta(days=7*w)
        start = end - timedelta(days=7)
        week_total = 0
        for sector, spec in specs:
            try:
                items = src_gdelt(spec, start.isoformat(), end.isoformat())
                week_total += store(conn, items, sector, "gdelt", backfilled=True)
            except Exception as e:
                print(f"    {sector:<14} {str(e)[:90]}")
            time.sleep(5)
        grand += week_total
        print(f"  {start} → {end}   {week_total:>5} new articles")
        empty_weeks = empty_weeks + 1 if week_total == 0 else 0
        if empty_weeks >= 2:
            print("\n  Two empty weeks — past the edge of GDELT's window. Stopping.")
            break
    return grand


def backfill(conn, cfg, months, weeks):
    """Paid archive if a key exists, otherwise GDELT's free rolling window."""
    if not KEY:
        print("\n  No NEWSAPI_AI_KEY — using GDELT's free window instead.")
        print("  Shorter reach than a paid archive, but it costs nothing.\n")
        return backfill_gdelt(conn, cfg, weeks)

    grand, today = 0, datetime.now(timezone.utc).date()
    for m in range(months):
        end   = today - timedelta(days=30*m)
        start = end - timedelta(days=31)
        print(f"\n{start.isoformat()} → {end.isoformat()}")
        n, ok, tried = run_once(conn, cfg, start.isoformat(), end.isoformat(), backfilled=True)
        grand += n
        print(f"  month total: {n} articles ({ok}/{tried} calls succeeded)")
        if ok == 0:
            print("  Nothing succeeded — stopping rather than spending further.")
            break
    return grand


# ============================================================== reporting ==
def migrate(conn):
    """Add columns that newer versions expect.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so a schema change never reaches a database created by an earlier
    version. Every added column needs an explicit migration.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    added = []
    for col, decl in (("fp", "TEXT"), ("backfilled", "INTEGER DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {decl}")
            added.append(col)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_fp ON articles(fp)")
    if added:
        print(f"  schema updated: added {', '.join(added)}")
        # backfill the fingerprint for rows stored before the column existed
        if "fp" in added:
            rows = list(conn.execute("SELECT id, title FROM articles"))
            for aid, title in rows:
                conn.execute("UPDATE articles SET fp=? WHERE id=?", (fingerprint(title), aid))
            print(f"  fingerprinted {len(rows)} existing articles")
    conn.commit()
    return added


def days_back(n):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n-1, -1, -1)]


def build(conn):
    days = days_back(WINDOW_DAYS)
    window_start, recent_start = days[0], days[-RECENT_DAYS]
    corpus = conn.execute("SELECT SUM(articles) FROM totals WHERE day>=?",
                          (window_start,)).fetchone()[0] or 1
    ceiling = max(3, int(corpus * DOC_FREQ_CEILING))
    candidates = [r[0] for r in conn.execute(
        "SELECT gram, SUM(articles) a FROM grams WHERE day>=? GROUP BY gram "
        "HAVING a>=? AND a<=? ORDER BY a DESC LIMIT 4000",
        (recent_start, MIN_ARTICLES, ceiling))]

    out = []
    for gram in candidates:
        rows = list(conn.execute(
            "SELECT g.day, g.sector, g.articles FROM grams g WHERE g.gram=? AND g.day>=?",
            (gram, window_start)))
        if not rows: continue
        secs, daily = defaultdict(lambda: {"first": None, "recent": 0, "articles": 0}), Counter()
        for day, sector, hits in rows:
            s = secs[sector]
            if s["first"] is None or day < s["first"]: s["first"] = day
            s["articles"] += hits
            if day >= recent_start: s["recent"] += hits
            daily[day] += hits

        sectors = [{"name": k, "firstSeen": v["first"], "recent": v["recent"],
                    "articles": v["articles"]} for k, v in secs.items() if v["recent"] > 0]
        if len(sectors) < MIN_SECTORS: continue
        sectors.sort(key=lambda s: s["firstSeen"])
        tot = sum(s["recent"] for s in sectors) or 1
        d0 = datetime.fromisoformat(window_start).date()
        for s in sectors:
            s["share"] = round(s["recent"]/tot, 3)
            s["adopt"] = (datetime.fromisoformat(s["firstSeen"]).date() - d0).days

        recent = sum(daily[d] for d in days[-RECENT_DAYS:])
        prior  = sum(daily[d] for d in days[-2*RECENT_DAYS:-RECENT_DAYS])
        direction = ("new" if prior == 0 else "rising" if recent > prior*1.35
                     else "falling" if recent < prior*0.7 else "steady")
        newest = max(sectors, key=lambda s: s["firstSeen"])

        partners = [(o, n) for o, n in conn.execute(
            "SELECT other, SUM(n) s FROM cooc WHERE gram=? AND day>=? GROUP BY other "
            "ORDER BY s DESC LIMIT 6", (gram, window_start))]
        mx = max([n for _, n in partners], default=1)

        examples = [{"publisher": p, "title": t, "url": u, "sector": s}
                    for p, t, u, s in conn.execute(
            "SELECT publisher, title, url, sector FROM articles WHERE "
            "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?) AND published>=? "
            "ORDER BY published DESC LIMIT 4", (f"%{gram}%", f"%{gram}%", recent_start))]
        pubs = conn.execute("SELECT COUNT(DISTINCT publisher) FROM articles WHERE "
            "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?) AND published>=?",
            (f"%{gram}%", f"%{gram}%", recent_start)).fetchone()[0]

        out.append({
            "id": re.sub(r"[^a-z0-9]+", "-", gram), "term": gram, "direction": direction,
            "sectorCount": len(sectors), "newestSector": newest["name"],
            "daysSinceNewestSector": (datetime.now(timezone.utc).date()
                                      - datetime.fromisoformat(newest["firstSeen"]).date()).days,
            "articles14d": recent, "articlesPrior14d": prior, "publishers": pubs,
            "sectors": sectors, "cooc": [[o, round(n/mx, 2)] for o, n in partners],
            "examples": examples,
            "daily": [{"day": d, "n": daily.get(d, 0)} for d in days],
        })
    # "holds benchmark", "benchmark rate" and "holds benchmark rate" are one
    # phrase at three lengths. Keep the longest whenever the shorter form adds
    # essentially no extra coverage.
    out.sort(key=lambda t: -len(t["term"]))
    kept, drop = [], set()
    for t in out:
        redundant = False
        for k in kept:
            if t["term"] in k["term"] and len(t["term"]) < len(k["term"]):
                if t["articles14d"] <= k["articles14d"] * 1.3:
                    redundant = True
                    break
        if redundant:
            drop.add(t["term"])
        else:
            kept.append(t)
    out = [t for t in out if t["term"] not in drop]

    # Breadth matters, but so does being unusual. A phrase in nine sectors that
    # appears in a fifth of all articles is vocabulary, not news.
    for t in out:
        freq = t["articles14d"] / corpus
        t["specificity"] = round(max(0.0, 1 - freq / DOC_FREQ_CEILING), 3)
        t["rank"] = round(t["sectorCount"] * (0.35 + 0.65 * t["specificity"]), 2)
    out.sort(key=lambda t: (-t["rank"], t["daysSinceNewestSector"]))
    return out[:MAX_TERMS]


# ==================================================================== cli ==
def probe(cfg):
    """Test every source individually. Dead feeds get named so you can delete them."""
    print("\nTesting every configured source, one call each.\n")
    ok_by_type, fail_by_type, dead = Counter(), Counter(), []
    gdelt_seen = False

    for sector in cfg["sectors"]:
        for spec in sector["sources"]:
            kind = spec["type"]
            label = spec.get("publisher") or spec.get("url") or spec.get("query", "")
            if kind == "newsapi_ai" and not KEY:
                continue
            if kind == "gdelt":
                if gdelt_seen: continue      # one probe is enough to answer the question
                gdelt_seen = True
                label = "GDELT"
            try:
                items = ADAPTERS[kind](spec)
                if not items:
                    raise RuntimeError("connected but returned nothing")
                ok_by_type[kind] += 1
                print(f"  ok    {sector['name'][:13]:<14} {label[:38]:<40} {len(items):>4} items")
            except Exception as e:
                fail_by_type[kind] += 1
                dead.append((sector["name"], label, str(e)[:80]))
                print(f"  FAIL  {sector['name'][:13]:<14} {label[:38]:<40} {str(e)[:70]}")
            time.sleep(1.5 if kind != "gdelt" else 0)

    print()
    for kind in ("rss", "gdelt", "newsapi_ai"):
        if ok_by_type[kind] or fail_by_type[kind]:
            print(f"  {kind:<11} {ok_by_type[kind]} working, {fail_by_type[kind]} failed")

    if ok_by_type["gdelt"]:
        print("\n  GDELT ANSWERED from this machine. Free backfill is available:")
        print("      python3 collect.py --backfill --weeks 12")
    elif fail_by_type["gdelt"]:
        print("\n  GDELT unreachable from here too. RSS only — history accumulates forward.")

    if dead:
        print(f"\n  {len(dead)} source(s) to remove from config.json:")
        for s, l, e in dead:
            print(f"      {s} · {l}")

    total_ok = sum(ok_by_type.values())
    print("\n" + (f"  {total_ok} sources working. Ready to collect.\n" if total_ok
                  else "  Nothing reachable. Check the network.\n"))
    return 0 if total_ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--months", type=int, default=12, help="paid archive depth")
    ap.add_argument("--weeks", type=int, default=12, help="free GDELT backfill depth")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="recompute all counts from stored articles")
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))

    # Fingerprint both files at the top of every run. If this line doesn't match
    # what you expect, the workflow is running an older commit and nothing below
    # it means anything.
    raw_cfg = open(CONFIG, "rb").read()
    raw_code = open(os.path.abspath(__file__), "rb").read()
    kinds = Counter(s["type"] for sec in cfg["sectors"] for s in sec["sources"])
    print(f"collect.py {VERSION} · code {hashlib.sha1(raw_code).hexdigest()[:8]}"
          f" · config {hashlib.sha1(raw_cfg).hexdigest()[:8]}")
    print(f"  {len(cfg['sectors'])} sectors · "
          + " · ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + "\n")

    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)      # creates anything missing entirely
    conn.commit()
    migrated = migrate(conn)        # then adds columns to tables that predate them

    if args.probe: return probe(cfg)

    have_v = extract_version(conn)
    if have_v != EXTRACT_VERSION or args.rebuild or migrated:
        if have_v != EXTRACT_VERSION:
            print(f"  extraction rules changed (v{have_v} → v{EXTRACT_VERSION})"
                  " — recomputing every count from stored articles")
        rebuild(conn)
        set_extract_version(conn)

    if args.backfill:
        depth = f"{args.months} months (paid archive)" if KEY else f"{args.weeks} weeks (free)"
        print(f"Backfilling {depth}. Run this once.")
        print(f"\n  {backfill(conn, cfg, args.months, args.weeks)} historical articles stored")
    elif not args.no_fetch:
        print(f"Collecting · key {'set' if KEY else 'NOT set (RSS only)'}")
        n, ok, tried = run_once(conn, cfg)
        print(f"\n  {n} new articles · {ok}/{tried} sources responded")

    # Name feeds that failed on every recent attempt. A large feed list is only
    # worth having if the dead entries are easy to spot and remove.
    bad = list(conn.execute(
        "SELECT sector, note, COUNT(*) n, SUM(ok) good FROM runlog "
        "WHERE ts >= datetime('now','-3 days') AND source='rss' "
        "GROUP BY sector, note HAVING good=0 AND note!='' ORDER BY n DESC LIMIT 12"))
    if bad:
        print("  feeds failing consistently — worth removing from config.json:")
        for sector, note, n, _ in bad:
            print(f"      {sector:<14} {note[:60]}  ({n} attempts)")
        print()

    days_held = conn.execute("SELECT COUNT(DISTINCT day) FROM totals").fetchone()[0]
    stored    = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    live      = conn.execute("SELECT COUNT(*) FROM articles WHERE backfilled=0").fetchone()[0]
    terms = build(conn)
    print(f"\n  {stored} articles ({live} collected live, {stored-live} backfilled)"
          f" across {days_held} days")
    print(f"  {len(terms)} phrases in {MIN_SECTORS}+ sectors\n")

    if args.report:
        for t in terms[:25]:
            print(f"  {t['term'][:34]:<36} {t['sectorCount']} sectors · {t['direction']:<8}"
                  f" · newest {t['newestSector']} {t['daysSinceNewestSector']}d")
        return 0

    json.dump({"generated": datetime.now(timezone.utc).isoformat(),
               "daysCollected": days_held, "articlesStored": stored,
               "articlesLive": live, "windowDays": WINDOW_DAYS,
               "terms": terms}, open(OUT, "w"), indent=1)
    print(f"  Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
