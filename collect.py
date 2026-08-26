#!/usr/bin/env python3
"""
collect.py — Crosstalk collector, v3.

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
know on date X" must exclude backfilled rows, or it quietly cheat by using
knowledge that wasn't available at the time. The flag exists so that mistake is
impossible to make by accident.
"""

import argparse, hashlib, html, json, math, os, re, shutil, sqlite3, sys, time
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import combinations
from datetime import datetime, timedelta, timezone

HERE   = os.path.dirname(os.path.abspath(__file__))
DB     = os.environ.get("CHORUS_DB", os.path.join(HERE, "chorus.db"))
CONFIG = os.path.join(HERE, "config.json")
OUT    = os.path.join(HERE, "data.json")
VERSION = "v3"
# Bump whenever ngrams(), the stopword sets, or singularise() change. Stored in
# the database; a mismatch rebuilds every derived count. Detecting one specific
# old format was too narrow - new rules were silently not applied to old rows.
# Bump this whenever extraction changes — including companies.json and the
# stop lists, which feed it. Without a bump, stored grams keep the old rules
# and the change appears to have done nothing.
EXTRACT_VERSION = 153
UA     = "crosstalk-monitor/3.0 (news language monitoring; crosstalkwire.com)"
KEY    = os.environ.get("NEWSAPI_AI_KEY", "").strip()

WINDOW_DAYS, RECENT_DAYS = 90, 14
MIN_ARTICLES, MIN_SECTORS, MAX_TERMS = 3, 2, 80
MIN_DAYS_SEEN = 4        # coverage bunched into fewer days than this, at more
                         # than two articles a day, is one story rather than
                         # language in use
PER_EVENT_MAX = 2.5      # articles per distinct story. Above this, several
                         # newsrooms are covering one event rather than a
                         # phrase spreading between them
ONE_FEED_HARD = 0.6     # this much from one newsroom is that newsroom's own
                         # usage however many others also carry it
ONE_FEED_SHARE = 0.55     # a phrase this concentrated in one publisher, with few
                         # others carrying it, is that newsroom writing to habit
EVERYWHERE_SHARE = 0.5   # a phrase in more than half the sectors is what they
                         # all already say, not language crossing between them
COMMON_CEILING = 0.03    # a phrase in more than this share of all coverage is
                         # already common knowledge; the site is for language
                         # that has not got there yet
GRIP_FULL = 0.5          # half a sector's outlets reaching for the phrase
                         # counts as fully gripping that sector
APART_MIN_WORDS = 12     # a phrase needs a cloud this big before its isolation
                         # means anything rather than reflecting thin coverage
APART_SHARE = 0.25       # a quarter of what a typical phrase shares with its
                         # nearest neighbour. Half put the cut inside a cluster
                         # and marked nine of forty, which is not a distinction.
VOCAB_DEPTH = 1500       # how many of a sector's most-used phrases stand for
                         # its vocabulary when comparing two sectors
CROSS_HALFLIFE = 21      # days over which a fresh crossing keeps its weight
HOME_WEIGHT = 1.5        # how hard to favour a phrase that still has a home
                         # sector over one used evenly everywhere
SHOW_TERMS = 28          # how many reach the phrase list; the rest still feed
                         # companions, sector counts and the maps
ORG_MIN_AWAY = 2         # mentions an agency needs in a sector that is not its
                         # own before that counts as attention having travelled
CO_MIN_PHRASE_SECTORS = 2 # an attached phrase must reach this many sectors AND
                          # have started outside the company's own sector: a
                          # utility beside "grid capacity" is a tautology
CO_DIR_MIN = 4            # below this the fortnight comparison is two articles
                          # against one, so no direction is claimed at all
CO_MIN_ARTICLES = 2       # a company needs at least this much coverage, and at
                          # least one phrase attached, to be worth a row
ORG_MIN_ARTICLES = 3      # an agency needs this many mentions, and must be named
                          # by more than one sector — a regulator inside a single
                          # sector is that sector's news, not a crossing
# sectors that cover one subject from different desks; they share an allowance
SECTOR_BLOCS = [frozenset({"Energy", "Utilities", "Materials", "Waste & water"})]
FAMILY_MIN = 3            # phrases sharing a word this often are one subject
MAX_PER_FAMILY = 2        # places any one subject may take in the shown list
MAX_PER_BLOC = 8          # places a whole bloc may take in the shown list
MAX_PER_SECTOR = 6        # how many phrases may lead with the same sector before
                          # the rest are pushed down the list
SECTOR_MIN_EFFECTIVE = 1.75 # and must behave like nearly two sectors in absolute
                            # terms — a lopsided pair is one sector with a mention
SECTOR_EVENNESS = 0.5     # effective sectors must be at least half the sectors
                          # claimed, or the spread is one sector plus spillover
SECTOR_MIN_ARTICLES = 3   # a sector must carry a phrase this often to count toward
                          # its spread. Two cleared most of the noise; three
                          # leaves only sectors genuinely using the phrase.
OBSERVED_LAG_DAYS = 3    # published more than this before we saw it = not live
COOC_FEED_SHARE = 0.7    # a pairing this concentrated in one newsroom, with
                         # few others carrying it, is that desk's own phrasing
COOC_MIN_LIFT = 2.0      # a companion must be distinctive to this phrase, not merely
                         # common. 1.8 emptied the panel; a specific phrase like
                         # "rare earth magnet" sits nearer 1.5 in a small corpus.
# Place names that survive into phrases. PLACES already holds the ones that
# disqualify a phrase outright; these are the ones a story can legitimately be
# named after, so two phrases sharing one are usually the same story.
PLACE_ANCHORS = set()      # filled below from PLACE_WORDS plus the place list

PLACE_WORDS = set("""
shelf continental gulf strait peninsula

iran iraq israel gaza lebanon syria yemen ukraine russia china taiwan korea japan
india pakistan brazil mexico canada australia germany france britain nigeria libya
venezuela panama suez hormuz malacca baltic arctic texas california alberta
""".split())

WATER_FAMILY = {"low water", "water level", "water levels", "river level"}
# the same thing written two ways, so they should not compete as separate finds
SYNONYM_FAMILIES = [
    {"energy storage", "storage system", "battery storage", "storage project",
     "storage capacity"},

    {"mwh battery", "mwh bess", "battery energy storage", "battery storage system"},
    {"low water", "water level", "water levels", "river level"},
    {"solar plus storage", "solar and storage", "solar storage",
     "co-located solar"},
    {"houthi attack", "houthi threat", "houthi strike", "houthi militant"},
]
SAME_STORY_SHARE = 0.6   # two phrases found together in this much of the
                         # smaller one's coverage are one story, whatever
                         # words they happen to use
PLACE_ANCHORS.update(PLACE_WORDS)
PLACE_ANCHORS.update("""
sea gulf strait canal peninsula bay ocean border coast region cape
east west north south middle asia europe africa america pacific atlantic
arab arabian persian iranian israeli saudi emirati levant
""".split())

JOIN_SHARE = 0.8         # two trigrams overlapping by two words, whose counts
                         # track this closely, are halves of one four-word phrase
FRAGMENT_SHARE = 0.75    # if a three-word phrase accounts for this much of a
                         # two-word phrase's coverage, the shorter one is a
                         # fragment of it rather than a phrase in its own right
COOC_MIN_SECTORS = 2     # and must appear in more than one sector, or it is that
                         # sector's own vocabulary rather than a companion
COOC_MIN_ARTICLES = 4    # a companion must recur; once is a sentence, not a topic
COOC_MIN_NEW = 3         # a first appearance carries no comparison, so it needs
                         # more of its own evidence — but four was too many at
                         # this corpus size and cut genuine specifics
COOC_PHRASES = 10        # pairs grow with the square: 12 gives 66 per article,
                         # 14 gives 91 (+38%), 16 would give 120 (+82%). The
                         # co-occurrence table is the largest one and the
                         # database has hit GitHub's ceiling once already.        # pairs grow as the square: 40 phrases is 1,560 pairs per
                         # article, 12 is 132. At 7,500 articles that is the
                         # difference between a database GitHub accepts and one it rejects.
COOC_DAYS    = 29        # a fortnight against the fortnight before it needs 28
                         # days present. Retaining 21 left the prior window half
                         # empty, which made phrases read as first appearances
COOC_KEEP_PER_GRAM = 4   # partners retained per phrase per day; the rest are
                         # noise and the table is what pushes the file toward
                         # GitHub's 100 MB ceiling
GRAM_KEEP    = 120       # keep enough for a 90-day view plus a baseline.
                         # Shortened from 150 when the file reached 96 MB with
                         # the window still filling; at 119 days of collection
                         # this discarded nothing that existed.
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
quarter update updates amid could would might
""".split())

# Words that carry no narrative on their own. A phrase built only from these
# is boilerplate, however many sectors it appears in.
GENERIC = set("""
cost costs price prices sale sales revenue revenues earning earnings profit profits
growth growing
demand supply plan plans plant plants project projects program programs facility facilities
production produce market markets share shares stock stocks quarter quarterly annual
first second third fourth year years month months week weeks percent million billion
increase increases decrease decreases higher lower rise rises fall falls up down
chief executive officer president director board management team employees workers
statement statements release announced announces announcement said says report reports
note notes comment comments update updates summary summaries
new latest recent current major large small big top best worst good bad
industry industries sector sectors business businesses firm firms company companies
government federal state national international global world
cash call calls flow flows offering offerings investment investments fiscal income margin
margins dividend dividends buyback guidance outlook forecast forecasts estimate estimates
per share shares stake stakes deal deals agreement agreements contract contracts
administration administrations official officials spokesperson comment comments
initial public private total net gross average overall
result results beat beats expectation expectations record target common capital
allocation pilot commercial operation operations announces announced signs
strong strongest weak long short term terms highlight highlights
high low free early late partial real scale grade world wide
cent cents percent percentage lakh crore
competitive advantage advantages financial saving savings support supports
nearly doubled tripled halved standing binding big giant boom bust non pre post
series conference senior junior vice deputy chief tech
based backed led driven focused oriented service services commerce
headline headlines summaryview transcript webinar podcast newsletter
fully wholly partly largely mainly integrated additional further
owned successfully previously currently formerly served completed broader
gdp cpi ppi pmi mou understanding adobe istock shutterstock getty com
digit digits double triple single metric ton tons tonne tonnes barrel barrels
level levels daily weekly monthly quarterly annually future
vast majority surge surges import imports adjusted source sources number numbers

customer customers leadership position positions agreement agreements
deposit deposits diluted share shares quality resource resources part parts
surcharge cubic gallon gallons barrel litre litres heavy light
human fcnr cfr volume mix jump jumps rose rise risen product products
pricing originally published republished syndicated
floor ceiling expenditure hike hikes paper total white rate rates bank banks
provider providers manufacturer manufacturers managing director directors
significant impact direction opposite far beyond highest lowest
nasdaq nyse otcmkt otcmkts amex tsx
highlight highlights address addresses addressed
appear appears appeared emerge emerges emerged
decision making season point points inflection next firm cup kingdom
performance operating economic pressure maker makers exporter importer
innovation round rounds strategy strategic approach effort efforts
part parts lifecycle funding partnership partnerships communication communications
time times track fast better best worse worst largest biggest smallest square foot feet
project projects announce buy sell hold
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
president senator governor secretary minister chairman commissioner
must should despite ongoing amid whether toward
rising falling climbing surging soaring slipping easing widening narrowing
meet meets meeting post posts posted near nears since amid versus
growing building sign signs signed next firm firms unit units boom
appear appears appeared emerge emerges emerged report reported reports
break breaks broke lead leads provide provides provided today yesterday tomorrow
headline headlines per worldwide hellenic newswire crore lakh
finalize finalizes finalized mou amend amends amended secure reduce
winnipeg naturalnews queensland otcmkt otcmkts nasdaq nyse amex tsx
drive drives driving offer offers offered help helps helped record records
reit reits maker makers court courts sale sales fssai pricing
industry industries sector sectors segment segments market markets
sharp steep sudden modest slight duty national sovereign long short heavy fresh
higher lower group line lines solution solutions
built around across toward towards beyond amid despite via
rapidly quickly slowly steadily sharply significantly increasingly
expanding growing rising falling declining
driven backed led owned run based aimed geared tied linked
commercial cross border hope mwh mw gwh kwh bess
close financial venture joint major majors
airport airports equity floating fixed rate rates
forms form part parts manager managers director directors executive executives
armed force forces combustion voltage fired development
eps ebitda revenue revenues margin margins guidance outlook
helping making taking giving getting seeking looking moving gaining
move moves gain gains push pushes step steps
strait straits
fell rose grew jumped slipped climbed dropped surged eased gained lost
create creates creating build builds boost boosts drive drives
said added noted warned reported announced launched plans expects
largest biggest smallest highest lowest fastest slowest newest oldest
leading major primary secondary
loss losses income profit profits earnings surcharge cubic feet foot season
address addresses successfully previously highlights apac emea latam
announce announces announced project projects plan plans program programs
reach reaches reached hit hits raise raises raised cut cuts
let lets letting turn turns turned survive survives surviving rewrite rewrites
keep keeps kept make makes made take takes took get gets got go goes went
come comes came look looks looked seem seems seemed want wants wanted
need needs needed give gives gave put puts show shows showed tell tells
find finds found leave leaves left bring brings brought
bancshare bancshares bancorp holdings incorporated corporation enterprises
doe epa ferc nrc iaea
agency agencies commission commissions laboratory laboratories bureau
department departments association associations institute institutes
council councils authority authorities ministry administration
main steady likely unlikely possible able about across among during through toward
also just even still yet only very much many more less most least
""".split())

# Interior joining words. "cost of capital" is a real term, so "of" survives;
# the rest turn phrases into sentence fragments.
INTERIOR_BAD = set("""
the a an and or but nor so yet if then than that which who whom whose this these those
is are was were be been being has have had do does did would shall should may
might must can could to for with from into onto upon at by on in as it its their his her
since near until while though although because per
anyone everyone someone anything everything nothing nobody somebody non
out off up down over under again once here there then now
""".split())

# Places, durations and cardinal numbers are not topics. "United States" in
# nine sectors tells you nothing; "rare earth" in nine sectors tells you a lot.
PLACES = set("""
united states america american europe european china chinese asia asian africa india
japan japanese germany german france french britain british england uk usa canada
canadian mexico brazil russia russian ukraine texas california york washington london
brussels beijing global national federal state county city region regional country
countries world worldwide north south east west northern southern eastern western
korea korean korean japanese chinese indian german italian spanish dutch swiss swedish
alabama alaska arizona arkansas colorado connecticut delaware florida georgia hawaii
idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts
michigan minnesota mississippi missouri montana nebraska nevada hampshire jersey
mexico carolina dakota ohio oklahoma oregon pennsylvania rhode tennessee utah vermont
virginia wisconsin wyoming
angeles francisco diego houston dallas boston seattle denver atlanta miami
phoenix detroit philadelphia austin chicago vegas orleans portland baltimore
los las san santa fort saint coast coastal valley bay gulf atlantic pacific midwest
vegas orleans angeles diego antonio francisco jose paulo janeiro
reuters bloomberg xinhua kyodo yonhap afp pti ani tass
hellenic newswire worldwide dispatch gazette chronicle herald tribune
wall street kingdom britain scotland wales ireland
southeast northeast southwest northwest asia europe africa oceania
america americas latin nordic baltic balkan gulf levant maghreb
arab arabia arabian persian iranian israeli saudi emirati gulf levant peninsula
tamil nadu maharashtra gujarat karnataka kerala punjab bengal odisha
mandeb hormuz bosphorus dardanelles kerch magellan
apac emea latam central australia australian queensland victoria tasmania
hong kong korea taiwan japan india china emirates aramco
abu dhabi dubai doha riyadh miami dade broward palm orange county
singapore tokyo seoul taipei beijing shanghai mumbai delhi bengaluru dubai
sydney melbourne toronto vancouver london paris berlin amsterdam dublin oslo
stockholm copenhagen madrid rome athens zurich brussels vienna warsaw
""".split())

NUMBERS = set("""
one two three four five six seven eight nine ten eleven twelve twenty thirty forty fifty
hundred thousand million billion trillion first second third fourth fifth half quarter
day days week weeks month months year years decade quarter hour hours minute minutes
monday tuesday wednesday thursday friday saturday sunday january february march april
may july august september october november december
""".split())

BOILER = re.compile(r"(read more|click here|subscribe|newsletter|all rights reserved|continue reading)", re.I)
TAG    = re.compile(r"<[^>]+>")
WORD   = re.compile(r"[a-z][a-z']{1,}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, url TEXT, title TEXT, summary TEXT, publisher TEXT,
  sector TEXT, source TEXT, published TEXT, observed TEXT, backfilled INTEGER DEFAULT 0,
  fp TEXT, story TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS grams(
  day TEXT, sector TEXT, gram TEXT, n INTEGER, articles INTEGER,
  PRIMARY KEY(day, sector, gram));
CREATE TABLE IF NOT EXISTS totals(
  day TEXT, sector TEXT, articles INTEGER, PRIMARY KEY(day, sector));
CREATE TABLE IF NOT EXISTS orgs(
  day TEXT, sector TEXT, name TEXT, acronym TEXT, n INTEGER,
  PRIMARY KEY(day, sector, name));
CREATE INDEX IF NOT EXISTS idx_orgs ON orgs(name, day);

CREATE TABLE IF NOT EXISTS gramcc(
  day TEXT, gram TEXT, country TEXT, n INTEGER,
  PRIMARY KEY(day, gram, country));
CREATE INDEX IF NOT EXISTS idx_gramcc ON gramcc(gram, day);

CREATE TABLE IF NOT EXISTS cooc(
  day TEXT, gram TEXT, other TEXT, n INTEGER, PRIMARY KEY(day, gram, other));
CREATE TABLE IF NOT EXISTS mentions(
  article_id TEXT, ticker TEXT, day TEXT, sector TEXT,
  PRIMARY KEY(article_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON mentions(ticker, day);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runlog(
  ts TEXT, source TEXT, sector TEXT, ok INTEGER, items INTEGER, note TEXT, publisher TEXT);
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
    words = [anglicise(w) for w in WORD.findall(text.lower())]
    out = []
    for n in (2, 3):
        for i in range(len(words)-n+1):
            g = words[i:i+n]
            if g[0] in STOP or g[-1] in STOP: continue
            if " ".join(g) in CONJUNCTION_WRECKS: continue
            if " ".join(g) in STOCK_PHRASES: continue
            if g[0] in EDGE_STOP or g[-1] in EDGE_STOP: continue
            if g[0] in FIRST_NAMES: continue          # "andy burnham" is a person
            if all(w in GENERIC or w in STOP or w in PLACES for w in g): continue
            if COMPANY_WORDS and any(w in COMPANY_WORDS for w in g): continue
            raw, folded = " ".join(g), singularise(" ".join(g))
            if raw in COMPANY_PHRASES or folded in COMPANY_PHRASES: continue
            if raw in seen_companies or folded in seen_companies: continue
            if any(len(w) < 3 for w in g): continue
            if any(w in INTERIOR_BAD for w in g[1:-1]): continue
            # a verb in the middle of a three-word phrase makes it a sentence
            # fragment: "notes rising data" is grammar, not a topic
            if any(w in EDGE_STOP for w in g[1:-1]): continue
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


ALL_PUBLISHERS = []

def set_publishers(cfg):
    """Every configured publisher name, longest first.

    Stripping only the name stored on a row fails for articles saved by earlier
    versions, which recorded the domain instead. Removing all known mastheads is
    both simpler and version-proof.
    """
    names = {s.get("publisher", "").strip()
             for sec in cfg.get("sectors", []) for s in sec.get("sources", [])}
    ALL_PUBLISHERS[:] = sorted((n for n in names if len(n) > 3), key=len, reverse=True)
    return len(ALL_PUBLISHERS)


COMPANIES = []          # [(ticker, name, sector, compiled pattern)]

COMPANY_WORDS = set()
# Pairs that only ever arise because a conjunction was stripped between them.
# Stock phrases journalists reach for whatever the subject, so they spread
# across sectors without saying anything about them.
STOCK_PHRASES = {
    "growth engine", "fresh capital", "game changer", "sea change",
    "perfect storm", "double down", "deep dive", "low hanging fruit",
    "silver bullet", "uphill battle", "green light", "red flag",
    "consumer price", "price index", "producer price",
    "regulatory approval", "regulatory framework", "final approval",
    "forms part", "asset management", "order book", "general manager",
    "power system", "management system", "water system", "energy technology",
    "entry level", "social media", "fuel price", "rapidly expanding",
    "energy system", "control system", "information technology",
    "managing director", "chief executive", "vice president", "board member",
    "golden state", "price cap",
    # job titles
    "general manager", "managing director", "chief executive", "vice president",
    "chief financial", "chief operating", "board member", "senior vice",
    # business functions named in every release
    "asset management", "wealth management", "human resource", "order book",
    "annual report", "press release", "business unit", "operating segment",
    # filler that survives because both words are common
    "forms part", "part company", "wider programme",
    "financial close", "joint venture", "oil major", "oil majors",
    "definitive agreement", "share purchase", "letter of intent",
}

CONJUNCTION_WRECKS = {
    "oil gas", "gas oil", "power water", "water power", "health safety",
    "oil chemical", "food beverage", "mergers acquisition", "research development",
}

COMPANY_PHRASES = set()
seen_companies = set()      # names recognised by shape as articles arrive

# ---------------------------------------------------------------------------
# Institutions
#
# Regulators, agencies and standards bodies show up as broken phrases —
# "regulatory commission nrc", "international atomic energy" — because every
# individual word is ordinary and only the capitalisation marks them as a name.
# Extraction lowercases everything, so that signal is thrown away.
#
# Titles are useless for this: RSS headlines are frequently Title Case, so every
# phrase in them looks like a proper noun. Summaries are written as prose, so
# capitalisation there means what it normally means. Only summaries are used.
# ---------------------------------------------------------------------------

INSTITUTION_WORDS = set("""
agency agencies commission commissions department departments ministry ministries
authority authorities bureau bureaus board boards council councils institute
institutes association associations organisation organization office offices
administration administrations regulator regulators inspectorate directorate
federation confederation committee committees tribunal panel secretariat
court courts parliament congress senate assembly reserve
""".split())

# A run of capitalised words, allowing the small joining words a name can carry.
# A run of capitalised words. The separator is whitespace, optionally carrying
# one of the small joining words a formal name can contain.
ENTITY = re.compile(
    r"\b([A-Z][a-zA-Z]{1,}"
    r"(?:\s+(?:of|for|and|the|on|in|to)\s+[A-Z][a-zA-Z]{1,}"
    r"|\s+[A-Z][a-zA-Z]{1,}){1,5})")
ACRONYM = re.compile(r"^\(([A-Z]{2,6})\)")

# Trade press names an agency in full once and then uses the acronym for the rest
# of the year, so full-name matching alone finds almost nothing. These are
# regulators and standards bodies only — not industry jargon like LNG or ESG,
# which are topics rather than institutions.
NAMED_BODIES = {
    "white house": "White House", "downing street": "Downing Street",
    "european commission": "European Commission", "european parliament": "European Parliament",
    "federal reserve": "Federal Reserve", "bank of england": "Bank of England",
    "european central bank": "European Central Bank",
    "world bank": "World Bank", "united nations": "United Nations",
}

AGENCY_ACRONYMS = {
    "FERC":"Federal Energy Regulatory Commission",
    "NRC":"Nuclear Regulatory Commission",
    "EPA":"Environmental Protection Agency",
    "DOE":"Department of Energy",
    "DOT":"Department of Transportation",
    "FDA":"Food and Drug Administration",
    "FTC":"Federal Trade Commission",
    "FCC":"Federal Communications Commission",
    "FAA":"Federal Aviation Administration",
    "OSHA":"Occupational Safety and Health Administration",
    "NERC":"North American Electric Reliability Corporation",
    "CFTC":"Commodity Futures Trading Commission",
    "USDA":"Department of Agriculture",
    "CDC":"Centers for Disease Control",
    "NHTSA":"National Highway Traffic Safety Administration",
    "EIA":"Energy Information Administration",
    "IAEA":"International Atomic Energy Agency",
    "IEA":"International Energy Agency",
    "OPEC":"Organization of the Petroleum Exporting Countries",
    "WTO":"World Trade Organization",
    "WHO":"World Health Organization",
    "IMO":"International Maritime Organization",
    "ICAO":"International Civil Aviation Organization",
    "OFGEM":"Ofgem",
    "OFCOM":"Ofcom",
    "ACCC":"Australian Competition and Consumer Commission",
    "SEBI":"Securities and Exchange Board of India",
    "CERC":"Central Electricity Regulatory Commission",
    "METI":"Ministry of Economy, Trade and Industry",
    "NDRC":"National Development and Reform Commission",
    "ANSI":"American National Standards Institute",
    "ISO":"International Organization for Standardization",
    "IEEE":"Institute of Electrical and Electronics Engineers",
    "PHMSA":"Pipeline and Hazardous Materials Safety Administration",
    "MSHA":"Mine Safety and Health Administration",
    "PJM":"PJM Interconnection",
    "ERCOT":"Electric Reliability Council of Texas",
    "MISO":"Midcontinent Independent System Operator",
    "CAISO":"California Independent System Operator",
    "NYISO":"New York Independent System Operator",
    "SPP":"Southwest Power Pool",
    "NCUA":"National Credit Union Administration",
    "NHS":"National Health Service",
    "FSA":"Financial Services Authority",
    "FCA":"Financial Conduct Authority",
    "FSSAI":"Food Safety and Standards Authority of India",
    "CFR":"Code of Federal Regulations",
    "CAA":"Clean Air Act",
    "SEBI":"Securities and Exchange Board of India",
    "TRAI":"Telecom Regulatory Authority of India",
    "CERT":"Computer Emergency Response Team",
    "SEC":"Securities and Exchange Commission",
}
BARE_ACRONYM = re.compile(r"\b([A-Z]{2,6})\b")
ACRONYM_OF = {v.lower(): k for k, v in AGENCY_ACRONYMS.items()}
SENT_START = re.compile(r"(?:^|[.!?]\s+)$")


LEGAL_SUFFIX = re.compile(
    r"\b([A-Z][a-zA-Z&.'-]+(?:\s+[A-Z][a-zA-Z&.'-]+){0,4})\s+"
    r"(?:Inc|Corp|Corporation|Co|Company|Ltd|Limited|LLC|PLC|LP|LLP|"
    r"AG|SA|NV|BV|AB|ASA|Oyj|SpA|GmbH|KK|Pte|Bhd|Holdings|Group)\b\.?")
TICKER_TAG = re.compile(
    r"\b([A-Z][a-zA-Z&.'-]+(?:\s+[A-Z][a-zA-Z&.'-]+){0,4})\s*"
    r"\(\s*(?:NASDAQ|NYSE|NYSEAMERICAN|AMEX|LSE|TSX|TSXV|ASX|OTC|OTCMKTS|"
    r"HKEX|SGX|KRX|TWSE|BSE|NSE|JSE|EPA|ETR|FRA)\s*:")


def companies_named(text):
    """Company names spotted by shape rather than by list.

    "Gladstone Commercial", "Dream Industrial" and every other firm we never
    listed used to reach the phrase list. A legal suffix or an exchange
    parenthetical identifies them without anyone maintaining an entry.
    """
    if not text:
        return set()
    out = set()
    for pat in (LEGAL_SUFFIX, TICKER_TAG):
        for m in pat.finditer(text):
            nm = " ".join(m.group(1).split())
            if 3 < len(nm) < 60:
                out.add(nm)
    return out


def institutions(summary):
    """Named institutions in a prose summary, with any acronym they define.

    Returns [(name, acronym or None)]. Only names containing an institution word
    qualify — "Nuclear Regulatory Commission" does, "Exxon Mobil" does not, and
    companies already have their own layer.
    """
    if not summary:
        return []
    out, seen = [], set()
    for m in ENTITY.finditer(summary):
        name = " ".join(m.group(1).split())
        if name.split()[0] in ("The", "A", "An"):
            name = name.split(" ", 1)[1]
        words = name.lower().split()
        if len(words) < 2 or len(name) > 80:
            continue
        if not any(w in INSTITUTION_WORDS for w in words):
            continue
        tail = summary[m.end():m.end() + 12].strip()
        ac = ACRONYM.match(tail)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, (ac.group(1) if ac else None) or ACRONYM_OF.get(name.lower())))

    # bodies whose names contain no institution word at all
    low = summary.lower()
    for key, proper in NAMED_BODIES.items():
        if key in low and proper.lower() not in seen:
            seen.add(proper.lower())
            out.append((proper, ACRONYM_OF.get(proper.lower())))

    # then the acronyms, which is how these bodies are usually written
    for m in BARE_ACRONYM.finditer(summary):
        a = m.group(1)
        full = AGENCY_ACRONYMS.get(a)
        if not full or full.lower() in seen:
            continue
        seen.add(full.lower())
        out.append((full, a))
    return out[:8]


def company_vocabulary(companies):
    """Distinctive words that only exist because a company is called that.

    "Westinghouse" and "Cameco" are distinctive. "Energy", "Electric" and
    "Advanced" are ordinary English that happens to appear in company names, so
    they are excluded — otherwise half the corpus would disappear. A single
    distinctive word is enough to a phrase as a company name, which is what
    catches "westinghouse electric" and "constellation energy".
    """
    ordinary = GENERIC | STOP | PLACES | set("""
        energy electric electrical power powers gas oil water solar wind nuclear
        steel metal metals material materials system systems technology technologies
        industry industries resource resources holding group international national
        advanced applied general united first second third air auto motor motors
        health healthcare medical financial capital bank banks trust insurance
        communications communication network networks data digital global public
        service services product products supply chain logistics transport
        electronics semiconductor pharmaceutical pharmaceuticals science sciences
        petroleum chemical chemicals mining minerals grid utility utilities freight
        rail marine port ports storage battery equipment machinery construction
        property properties realty estate retail food beverage automotive vehicle
        mill mills works plant plants brands brand stores shops farm farms
        industrial commercial residential dream general standard premier superior
        allied continental liberty summit apex pioneer western eastern northern
        southern central pacific atlantic gulf midwest coastal metro urban rural
        partners holdings enterprises ventures corporation incorporated limited
    """.split())
    words = set()
    for c in companies:
        forms = " ".join([c.get("name", "")] + list(c.get("aliases", [])))
        for w in WORD.findall(forms.lower()):
            # Four-letter words are too often ordinary English that happens to
            # sit in a company name — "Mills" made "paper mill" disappear.
            if len(w) > 4 and w not in ordinary:
                words.add(w)
                words.add(singularise(w))
    return words



def load_companies():
    """Compile name and alias patterns once.

    Case-sensitive with word boundaries. "Apple Inc" matches, "apple pie" does
    not; "Target Corporation" matches, "target market" does not. Ambiguous
    single words are stored with a qualifier in companies.json for exactly this
    reason — a false company mention is worse than a missed one, because it
    silently attaches a narrative to a business that was never discussed.
    """
    COMPANIES.clear()
    path = os.path.join(HERE, "companies.json")
    if not os.path.exists(path):
        return 0
    data = json.load(open(path, encoding="utf-8"))
    for c in data.get("companies", []):
        forms = [c["name"]] + list(c.get("aliases", []))
        pat = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        COMPANIES.append((c["ticker"], c["name"], c["sector"],
                          re.compile(r"(?<![A-Za-z0-9])(" + pat + r")(?![A-Za-z0-9])")))
    COMPANY_WORDS.clear()
    COMPANY_WORDS.update(company_vocabulary(data.get("companies", [])))
    COMPANY_PHRASES.clear()
    for co in data.get("companies", []):
        for form in [co.get("name", "")] + list(co.get("aliases", [])):
            COMPANY_PHRASES.update(ngrams(form))
            COMPANY_PHRASES.add(singularise(clean(form)))
    return len(COMPANIES)


def companies_in(text):
    if not text or not COMPANIES:
        return []
    return [(t, n, s) for t, n, s, pat in COMPANIES if pat.search(text)]


FIRST_NAMES = set("""
james john robert michael william david richard joseph thomas charles christopher
daniel matthew anthony donald paul steven andrew kenneth george joshua kevin
brian edward ronald timothy jason jeffrey ryan jacob gary nicholas eric stephen
jonathan larry justin scott brandon benjamin samuel gregory alexander patrick jack
dennis jerry tyler aaron jose adam henry douglas peter zachary kyle walter ethan
jeremy harold carl keith roger gerald terry sean austin arthur noah lawrence jesse
joe bryan billy jordan albert dylan bruce willie gabriel alan juan logan wayne ralph
roy eugene randy vincent russell louis philip johnny stewart stuart neil andy nigel
graham colin ian alistair angus duncan malcolm rory callum
mary patricia jennifer linda elizabeth barbara susan jessica sarah karen nancy lisa
margaret betty sandra ashley dorothy kimberly emily donna michelle carol amanda
melissa deborah stephanie rebecca laura sharon cynthia kathleen amy angela shirley
anna brenda pamela emma nicole helen samantha katherine christine debra rachel
catherine carolyn janet ruth maria heather diane virginia julie joyce victoria
olivia kelly christina joan evelyn lauren judith megan cheryl andrea hannah martha
jacqueline frances gloria ann teresa kathryn sara janice jean alice madison
""".split())

BYLINE = re.compile(
    r"^\s*(?:by|von|par|de)\s+[A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3}"
    r"\s*(?:[,|·—–-]|\||$)", re.I)


def cut(text, limit):
    """Truncate at a word boundary. Cutting mid-word invents phrases that were
    never written — a summary sliced through "electric" leaves "tric"."""
    if not text or len(text) <= limit:
        return text or ""
    edge = text[:limit].rsplit(" ", 1)[0]
    return edge if len(edge) >= limit * 0.5 else text[:limit]


def strip_byline(text):
    """Remove a leading credit line. Author names are not topics."""
    if not text:
        return text
    out = BYLINE.sub("", text, count=1)
    return out.lstrip(" ,·—–-|")


def strip_masthead(text, publisher):
    """Remove the publisher's own name from its text.

    A blocklist is the wrong tool here: "Data Center Dynamics" is a masthead,
    but "data center" is a real topic, and blocking the name would delete the
    topic. Removing the literal masthead string leaves the words free to count
    wherever they actually occur in the writing.
    """
    if not text:
        return text
    out = text
    for name in ([publisher] if publisher else []) + ALL_PUBLISHERS:
        if name and len(name) > 3 and name.lower() in out.lower():
            out = re.sub(re.escape(name), " ", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


# One spelling per concept. "data centre" and "data center" counted separately
# halves the evidence for both and can hide a spread entirely. This covers
# British forms and the common American misspellings that split the same way —
# "liquified" for "liquefied", for instance, which is frequent enough in trade
# press to matter for a phrase as central as liquefied natural gas.
BRITISH = {
    "liquified":"liquefied", "liquify":"liquefy", "liquifying":"liquefying",
    "judgement":"judgment", "acknowledgement":"acknowledgment",
    "focussed":"focused", "targetted":"targeted", "benefitted":"benefited",
    "accomodate":"accommodate", "occurence":"occurrence",
    "rizing":"rising", "recieve":"receive", "seperate":"separate",
    "enterprize":"enterprise", "analyse":"analyze", "organise":"organize",
    "definately":"definitely", "managment":"management",
    "centre":"center", "centres":"centers", "centred":"centered",
    "colour":"color", "colours":"colors", "behaviour":"behavior",
    "labour":"labor", "favour":"favor", "neighbour":"neighbor",
    "harbour":"harbor", "vapour":"vapor", "rumour":"rumor",
    "defence":"defense", "offence":"offense", "licence":"license",
    "practise":"practice", "programme":"programs", "programmes":"programs",
    "metre":"meter", "metres":"meters", "litre":"liter", "litres":"liters",
    "fibre":"fiber", "fibres":"fibers", "theatre":"theater",
    "aluminium":"aluminum", "sulphur":"sulfur", "catalogue":"catalog",
    "cheque":"check", "tyre":"tire", "tyres":"tires", "storey":"story",
    "grey":"gray", "ageing":"aging", "enrolment":"enrollment",
    "instalment":"installment", "fulfil":"fulfill", "travelled":"traveled",
    "modelling":"modeling", "labelled":"labeled", "cancelled":"canceled",
}
_ISE = re.compile(r"(is|IS)(e|es|ed|ing|ation|ations)$")


# Nouns ending -ise that the verb rule would otherwise mangle.
ISE_NOUNS = set("""
enterprise surprise compromise franchise merchandise expertise paradise
promise premise demise disguise comprise precise concise wise rise arise
exercise supervise advertise revise devise reprise chastise
""".split())


def anglicise(w):
    """One spelling per concept. Suffix rules catch the -ise/-isation family."""
    if w in ISE_NOUNS:
        return w
    if w.endswith("'s") and len(w) > 3:
        w = w[:-2]          # "world's" is "world"; possessives are not new words
    if w in BRITISH:
        return BRITISH[w]
    if len(w) > 5 and _ISE.search(w):
        head = w[:_ISE.search(w).start()]
        if len(head) >= 3:
            return head + "iz" + _ISE.search(w).group(2)
    if w.endswith("ised") or w.endswith("ising") or w.endswith("isation"):
        return w.replace("ised", "ized").replace("ising", "izing").replace("isation", "ization")
    return w


def _fold_places():
    """Place names in both spellings, since phrases are folded before matching."""
    for w in list(PLACES):
        s = singularise(w)
        if s != w:
            PLACES.add(s)


def singularise(phrase):
    """Fold trailing plurals so 'data center' and 'data centers' are one entry.

    Deliberately crude: only the last word, only a trailing 's', and only when
    the result is still a plausible word. Real stemming would need a dependency
    and would mangle more than it fixes at this scale.
    """
    words = phrase.split()
    last = words[-1]
    if len(last) > 4 and not last.endswith(("ss", "us", "is", "as")):
        if last.endswith("ies") and len(last) > 5:
            words[-1] = last[:-3] + "y"       # inventories -> inventory
        elif last.endswith(("ches", "shes", "sses", "xes")):
            words[-1] = last[:-2]             # batches -> batch
        elif last.endswith("s"):
            words[-1] = last[:-1]
    return " ".join(words)


# Runs once at import, after singularise exists: place names are stored in both
# spellings so a folded phrase still matches.
_fold_places()


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
        "summary": cut(clean(a.get("body", "")), 600),
        "publisher": (a.get("source") or {}).get("title") or host(a.get("url", "")),
        "published": a.get("date", "")[:10],
    } for a in results]


def src_gdelt(spec, since=None, until=None):
    """Free, no key. Rolling window only. Unreachable from some networks —
    the probe say so rather than failing silently."""
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
def known_phrases(conn, min_articles=3):
    """Phrases the corpus has already seen more than once."""
    try:
        return {g for (g,) in conn.execute(
            "SELECT gram FROM grams GROUP BY gram HAVING SUM(articles) >= ?",
            (min_articles,))}
    except sqlite3.OperationalError:
        return set()


def pick_companions(uniq, known):
    """Only phrases the corpus has already seen more than once.

    build() discards any partner that does not recur, so pairs involving a
    one-off phrase are written and then ignored — pure weight in the largest
    table. Until a phrase is known, a few alphabetical picks seed the set.
    """
    cands = [g for g in uniq if " " in g]
    seen = sorted(g for g in cands if g in known)
    if len(seen) >= 4 or not cands:
        return seen[:COOC_PHRASES]
    rest = sorted(g for g in cands if g not in known)
    return (seen + rest)[:COOC_PHRASES]      # cold start only


SYNDICATED = [0]


def body_key(summary):
    """A key for the syndicated summary, exact wording only.

    Deliberately strict: near-matching prose can mean opposite things
    ("opens" against "closes"), so only an identical body counts.
    """
    t = re.sub(r"\s+", " ", (summary or "").strip().lower())
    return hashlib.sha1(t.encode()).hexdigest()[:16] if len(t) >= 60 else ""


def story_key(url, title):
    """A key shared by the same story republished under sister titles.

    Industry Dive runs two dozen of the feeds read here and gives the same
    piece an identical URL slug on each. Without this, one newsroom writing
    once reads as language crossing between sectors.
    """
    for part in reversed([p for p in (url or "").split("/") if p]):
        if part.count("-") >= 3 and not part.isdigit():
            return part.lower()
    t = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
    return " ".join(t.split()[:9])


def store(conn, articles, sector, source, backfilled=False):
    now = datetime.now(timezone.utc).isoformat()
    by_day_grams   = defaultdict(Counter)
    by_day_arts    = defaultdict(Counter)
    by_day_cc      = defaultdict(int)
    by_day_org     = {}
    known          = known_phrases(conn)
    by_day_cooc    = defaultdict(Counter)
    by_day_totals  = Counter()
    added = 0
    syndicated = 0

    for a in articles:
        if not a["url"] or not a["title"] or BOILER.search(a["title"]): continue
        aid = hashlib.sha1(a["url"].encode()).hexdigest()[:16]
        if conn.execute("SELECT 1 FROM articles WHERE id=?", (aid,)).fetchone(): continue
        day = normalise_date(a["published"])

        # the same story under a sister title is not a second sighting
        skey = story_key(a["url"], a["title"])
        if skey and len(skey) > 20 and conn.execute(
                "SELECT 1 FROM articles WHERE story=?", (skey,)).fetchone():
            syndicated += 1
            continue
        bkey = body_key(a.get("summary"))
        if bkey and conn.execute(
                "SELECT 1 FROM articles WHERE body=?", (bkey,)).fetchone():
            syndicated += 1
            continue

        # An article published well before we saw it was not observed live,
        # whatever mode we are running in. GDELT routinely returns weeks-old
        # pieces during a normal run. Marking these live would let a future
        # backtest read knowledge that did not exist on the date it claims.
        late = backfilled
        try:
            lag = (datetime.now(timezone.utc).date()
                   - datetime.fromisoformat(day).date()).days
            late = backfilled or lag > OBSERVED_LAG_DAYS
        except ValueError:
            pass

        fp = fingerprint(a["title"])
        if fp:
            # Syndication happens within hours. The same headline months later is
            # a different story - "Fed holds rate steady" recurs all year.
            window = (datetime.fromisoformat(day) - timedelta(days=DEDUPE_DAYS)).date().isoformat()
            if conn.execute("SELECT 1 FROM articles WHERE fp=? AND published>=? AND published<=?",
                            (fp, window, day)).fetchone():
                continue
        conn.execute("INSERT INTO articles(id,url,title,summary,publisher,sector,"
                     "source,published,observed,backfilled,fp,country,story,body) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            aid, a["url"], cut(a["title"], 400), cut(a["summary"] or "", 600),
            a["publisher"] or host(a["url"]), sector, source, day, now,
            int(late), fp, a.get("country", "US"), skey, bkey))
        added += 1
        by_day_totals[day] += 1

        for ticker, _cname, _csector in companies_in(a["title"] + ". " + (a["summary"] or "")):
            conn.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?,?)",
                         (aid, ticker, day, sector))

        pub = a["publisher"] or host(a["url"])
        grams = ngrams(strip_masthead(a["title"], pub) + ". "
                       + strip_masthead(a["summary"] or "", pub))
        uniq = set(grams)
        by_day_grams[day].update(grams)
        by_day_arts[day].update(uniq)
        cc = a.get("country", "US")
        for g in uniq:                      # one count per article, per phrase
            by_day_cc[(day, g, cc)] += 1
        for nm, ac in institutions(a["summary"] or ""):
            by_day_org[(day, nm)] = ac or by_day_org.get((day, nm))
        for nm in companies_named((a["title"] or "") + ". " + (a["summary"] or "")):
            seen_companies.update(ngrams(nm))
        phrases = pick_companions(uniq, known)
        for i, x in enumerate(phrases):
            for y in phrases[i+1:]:
                by_day_cooc[day][(x, y) if x < y else (y, x)] += 1

    for day, counter in by_day_grams.items():
        for g, n in counter.items():
            conn.execute("INSERT INTO grams VALUES(?,?,?,?,?) ON CONFLICT(day,sector,gram) "
                         "DO UPDATE SET n=n+?, articles=articles+?",
                         (day, sector, g, n, by_day_arts[day][g], n, by_day_arts[day][g]))
    for (day, nm), ac in by_day_org.items():
        conn.execute("INSERT INTO orgs VALUES(?,?,?,?,1) "
                     "ON CONFLICT(day,sector,name) DO UPDATE SET n=n+1, "
                     "acronym=COALESCE(acronym, ?)", (day, sector, nm, ac, ac))
    for (day, g, cc), n in by_day_cc.items():
        conn.execute("INSERT INTO gramcc VALUES(?,?,?,?) "
                     "ON CONFLICT(day,gram,country) DO UPDATE SET n=n+?",
                     (day, g, cc, n, n))
    for day, counter in by_day_cooc.items():
        for (x, y), n in counter.items():
            conn.execute("INSERT INTO cooc VALUES(?,?,?,?) ON CONFLICT(day,gram,other) "
                         "DO UPDATE SET n=n+?", (day, x, y, n, n))
    for day, n in by_day_totals.items():
        conn.execute("INSERT INTO totals VALUES(?,?,?) ON CONFLICT(day,sector) "
                     "DO UPDATE SET articles=articles+?", (day, sector, n, n))
    conn.commit()
    if syndicated:
        SYNDICATED[0] += syndicated
    return added


def rebuild(conn, verbose=True):
    """Recompute every derived count from the stored articles.

    Needed whenever the extraction rules change: old rows were produced by the
    old rules and would otherwise outrank anything new. Articles are untouched,
    so this costs nothing and fetches nothing.
    """
    rows, seen_fp = [], {}
    for title, summary, sector, published, url, pub, cc in conn.execute(
            "SELECT title, summary, sector, published, url, publisher, "
            "COALESCE(country,'US') FROM articles ORDER BY published"):
        fp = fingerprint(title)
        day = (published or "")[:10]
        if fp:
            prev = seen_fp.get(fp)
            if prev and day and prev and (
                    datetime.fromisoformat(day) - datetime.fromisoformat(prev)).days <= DEDUPE_DAYS:
                continue
            seen_fp[fp] = day or seen_fp.get(fp)
        rows.append((title, summary, sector, published, url, pub, cc))
    if verbose:
        print(f"  rebuilding counts from {len(rows)} stored articles")
    conn.execute("DELETE FROM grams"); conn.execute("DELETE FROM cooc")
    conn.execute("DELETE FROM totals"); conn.execute("DELETE FROM mentions")
    conn.execute("DELETE FROM gramcc"); conn.execute("DELETE FROM orgs")

    # First pass counts how often each phrase occurs across the whole corpus, and
    # keeps the extraction so the second pass does not repeat it. On six thousand
    # articles that is half the work saved.
    freq = Counter()
    grams_of = []
    for title, summary, sector, day, url, pub, cc in rows:
        gs = ngrams(strip_masthead(clean(title), pub) + ". "
                    + strip_masthead(clean(strip_byline(summary or "")), pub))
        grams_of.append(gs)
        freq.update(set(gs))
    known = {g for g, n in freq.items() if n >= 3}

    g_n  = defaultdict(Counter)   # (day, sector) -> gram counts
    g_a  = defaultdict(Counter)
    c_n  = defaultdict(Counter)   # day -> pair counts
    cc_n = defaultdict(int)       # (day, gram, country) -> articles
    org_n = {}                    # (day, sector, name) -> (articles, acronym)
    tot  = Counter()

    for (title, summary, sector, day, url, pub, cc), grams in zip(rows, grams_of):
        day = (day or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        tot[(day, sector)] += 1
        aid = hashlib.sha1((url or "").encode()).hexdigest()[:16]
        for ticker, _n, _s in companies_in((title or "") + ". " + (summary or "")):
            conn.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?,?)",
                         (aid, ticker, day, sector))
        uniq = set(grams)
        g_n[(day, sector)].update(grams)
        g_a[(day, sector)].update(uniq)
        for g in uniq:
            cc_n[(day, g, cc)] += 1          # provenance, same as a live store
        for nm in companies_named((title or "") + ". " + (summary or "")):
            seen_companies.update(ngrams(nm))
        for nm, ac in institutions(summary or ""):
            org_n[(day, sector, nm)] = org_n.get((day, sector, nm), (0, None))
            cnt, prev = org_n[(day, sector, nm)]
            org_n[(day, sector, nm)] = (cnt + 1, prev or ac)
        phrases = pick_companions(uniq, known)
        for i, x in enumerate(phrases):
            for y in phrases[i+1:]:
                c_n[day][(x, y)] += 1
                c_n[day][(y, x)] += 1

    conn.executemany("INSERT OR REPLACE INTO grams VALUES(?,?,?,?,?)",
        [(day, sector, g, n, g_a[(day, sector)][g])
         for (day, sector), counter in g_n.items() for g, n in counter.items()])
    conn.executemany("INSERT OR REPLACE INTO gramcc VALUES(?,?,?,?)",
        [(day, g, cc, n) for (day, g, cc), n in cc_n.items()])
    conn.executemany("INSERT OR REPLACE INTO orgs VALUES(?,?,?,?,?)",
        [(day, sector, nm, ac, n) for (day, sector, nm), (n, ac) in org_n.items()])
    conn.executemany("INSERT OR REPLACE INTO cooc VALUES(?,?,?,?)",
        [(day, x, y, n) for day, counter in c_n.items()
         for (x, y), n in counter.items()])          # the largest table by far
    conn.executemany("INSERT OR REPLACE INTO totals VALUES(?,?,?)",
        [(day, sector, n) for (day, sector), n in tot.items()])
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
                for it in items:                      # provenance travels with the article
                    it.setdefault("country", spec.get("country", "US"))
                ok += 1
            except Exception as e:
                note = str(e)[:160]
            n = store(conn, items, sector["name"], kind, backfilled) if items else 0
            total += n
            conn.execute("INSERT INTO runlog VALUES(?,?,?,?,?,?,?)", (
                datetime.now(timezone.utc).isoformat(), kind, sector["name"],
                int(not note), len(items), note,
                spec.get("publisher") or spec.get("url", "")[:60]))
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    if "body" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN body TEXT")
        rows = list(conn.execute("SELECT id, summary FROM articles"))
        conn.executemany("UPDATE articles SET body=? WHERE id=?",
                         [(body_key(sm), i) for i, sm in rows])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_body ON articles(body)")
        print(f"  schema updated: added body key to {len(rows):,} articles")
    if "story" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN story TEXT")
        rows = list(conn.execute("SELECT id, url, title FROM articles"))
        conn.executemany("UPDATE articles SET story=? WHERE id=?",
                         [(story_key(u, t), i) for i, u, t in rows])
        print(f"  schema updated: added story key to {len(rows):,} articles")
    # created here, not in SCHEMA, because SCHEMA runs before this migration
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_story "
                 "ON articles(story, published)")
    conn.commit()
    """Add columns that newer versions expect.

    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so a schema change never reaches a database created by an earlier
    version. Every added column needs an explicit migration.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    added = []
    for col, decl in (("fp", "TEXT"), ("backfilled", "INTEGER DEFAULT 0"),
                      ("country", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {decl}")
            added.append(col)
    renamed = 0
    for old, new in (("Labour", "Labor"),):
        n = conn.execute("SELECT COUNT(*) FROM articles WHERE sector=?", (old,)).fetchone()[0]
        if n:
            for table in ("articles", "grams", "totals", "mentions"):
                try:
                    conn.execute(f"UPDATE OR REPLACE {table} SET sector=? WHERE sector=?", (new, old))
                except sqlite3.OperationalError:
                    pass
            renamed += n
    if renamed:
        added.append(f"renamed {renamed} rows Labour->Labor")

    rl = {r[1] for r in conn.execute("PRAGMA table_info(runlog)")}
    if rl and "publisher" not in rl:
        conn.execute("ALTER TABLE runlog ADD COLUMN publisher TEXT")
        added.append("runlog.publisher")
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


def remap_mentions(conn, verbose=True):
    """Move mentions off tickers that no longer exist in companies.json.

    When two listings of one firm are merged, rows recorded under the retired
    ticker keep the old company alive in the dashboard. Match them to the
    survivor by name, and drop the rest.
    """
    try:
        live = {c["ticker"]: c for c in
                json.load(open(os.path.join(HERE, "companies.json")))["companies"]}
    except Exception:
        return
    by_alias = {}
    for tk, c in live.items():
        for form in [c["name"]] + list(c.get("aliases", [])):
            by_alias[form.lower()] = tk

    stale = [t for (t,) in conn.execute("SELECT DISTINCT ticker FROM mentions")
             if t not in live]
    moved = dropped = 0
    for t in stale:
        target = by_alias.get(t.lower())
        if target:
            conn.execute("UPDATE OR IGNORE mentions SET ticker=? WHERE ticker=?", (target, t))
            moved += 1
        conn.execute("DELETE FROM mentions WHERE ticker=?", (t,))
        if not target:
            dropped += 1
    if verbose and (moved or dropped):
        print(f"  mentions · {moved} ticker(s) remapped, {dropped} retired")
    conn.commit()


def drop_syndicated(conn, verbose=True):
    """Remove copies of a story already stored under sister titles.

    The store-time check only guards new arrivals. Anything collected before
    that existed is still duplicated, inflating sector counts for exactly the
    phrases the site reports on.
    """
    for col in ("story", "body"):
        for key, n in conn.execute(
                f"SELECT {col}, COUNT(*) FROM articles "
                f"WHERE {col} IS NOT NULL AND {col} <> '' AND LENGTH({col}) > 15 "
                f"GROUP BY {col} HAVING COUNT(*) > 1").fetchall():
            keep = conn.execute(
                f"SELECT id FROM articles WHERE {col}=? ORDER BY published, id LIMIT 1",
                (key,)).fetchone()[0]
            conn.execute(f"DELETE FROM articles WHERE {col}=? AND id<>?", (key, keep))
    conn.commit()

    groups = list(conn.execute(
        "SELECT story, COUNT(*) FROM articles "
        "WHERE story IS NOT NULL AND story <> '' AND LENGTH(story) > 20 "
        "GROUP BY story HAVING COUNT(*) > 1"))
    removed = 0
    for skey, n in groups:
        keep = conn.execute(
            "SELECT id FROM articles WHERE story=? ORDER BY published, id LIMIT 1",
            (skey,)).fetchone()[0]
        cur = conn.execute("DELETE FROM articles WHERE story=? AND id<>?", (skey, keep))
        removed += cur.rowcount
    if verbose and (groups or removed):
        print(f"  {removed} duplicate copies removed "
              f"across {len(groups)} stories run under several titles")
    if removed:
        conn.commit()
    return removed


def prune(conn, verbose=True):
    """Bound the derived tables.

    Articles are the asset and are never deleted. Everything else is derived and
    can be recomputed from them, so old rows are dead weight — and the committed
    database has a hard size ceiling.
    """
    today = datetime.now(timezone.utc).date()
    gram_cut = (today - timedelta(days=GRAM_KEEP)).isoformat()
    cooc_cut = (today - timedelta(days=COOC_DAYS)).isoformat()
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("grams", "cooc")}
    conn.execute("DELETE FROM grams WHERE day < ?", (gram_cut,))
    conn.execute("DELETE FROM gramcc WHERE day < ?", (gram_cut,))
    conn.execute("DELETE FROM orgs WHERE day < ?", (gram_cut,))
    conn.execute("DELETE FROM cooc  WHERE day < ?", (cooc_cut,))


    # And cap how many partners any one phrase keeps per day.
    conn.execute("""
        DELETE FROM cooc WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid, ROW_NUMBER() OVER (
                    PARTITION BY day, gram ORDER BY n DESC) rn
                FROM cooc) WHERE rn > ?)""", (COOC_KEEP_PER_GRAM,))
    conn.execute("DELETE FROM runlog WHERE ts < ?",
                 ((today - timedelta(days=14)).isoformat(),))
    conn.commit()
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("grams", "cooc")}
    conn.execute("VACUUM")
    if verbose and (before != after):
        print(f"  pruned · grams {before['grams']:,} → {after['grams']:,}"
              f" · cooc {before['cooc']:,} → {after['cooc']:,}")
    return after


def write_digest(terms, path, stored, days_held):
    """A weekly summary as markdown, for sending to someone.

    A dashboard asks a reader to explore. A digest gives them something to react
    to in thirty seconds, which is what actually gets replies. Same data, framed
    as "here is what changed" rather than "here is a tool".
    """
    today = datetime.now(timezone.utc).date()
    crossed = [t for t in terms if t["daysSinceNewestSector"] <= 7 and t["sectorCount"] >= 3]
    crossed.sort(key=lambda t: (-t["sectorCount"], t["daysSinceNewestSector"]))

    moving = [t for t in terms if t["articlesPrior14d"] >= 3]
    for t in moving:
        t["_growth"] = t["articles14d"] / max(1, t["articlesPrior14d"])
    moving = [t for t in moving if t["_growth"] >= 1.4]
    moving.sort(key=lambda t: -t["_growth"])

    fmt = lambda iso: (datetime.fromisoformat(iso).strftime("%-d %B %Y")
                       if iso and len(iso) == 10 else iso)

    L = [f"# Crosstalk — week ending {today:%-d %B %Y}", "",
         f"*{stored:,} articles over {days_held} days of collection. Every figure is a "
         f"count of articles actually read; nothing is modelled, estimated or forecast.*", ""]

    L += ["## Crossed into a new sector this week", ""]
    if crossed:
        for t in crossed[:8]:
            when = "today" if t["daysSinceNewestSector"] == 0 else f"{t['daysSinceNewestSector']}d ago"
            first = min(t["sectors"], key=lambda s: s["firstSeen"])
            L.append(f"**{t['term']}** — now in {t['sectorCount']} sectors. "
                     f"**{t['newestSector']}** picked it up {when}; it was first seen in "
                     f"{first['name'].lower()} coverage on {fmt(first['firstSeen'])}.")
            for e in t["examples"][:2]:
                L.append(f"  - [{e['title']}]({e['url']}) · {e['publisher']} · {e['sector'].lower()}")
            L.append("")
    else:
        L += ["Nothing crossed into a new sector this week.", ""]

    L += ["## Being used more than a fortnight ago", ""]
    if moving:
        L += ["| phrase | sectors | articles | vs prior fortnight |",
              "|---|---|---|---|"]
        for t in moving[:10]:
            L.append(f"| {t['term']} | {t['sectorCount']} | {t['articles14d']} | "
                     f"{t['_growth']:.1f}× |")
        L.append("")
    else:
        L += ["Nothing moved sharply this fortnight.", ""]

    L += ["---", "",
          "Crosstalk tracks which phrases appear in more than one sector's trade press "
          "and when each sector started using them. It makes no forecast — testing found "
          "that neither a phrase accelerating nor spreading across sectors predicted what "
          "followed. \"Rising\" describes the last fortnight, not the next one.", "",
          "crosstalkwire.com"]

    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    return len(crossed), len(moving)


def days_back(n):
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n-1, -1, -1)]


def like_forms(phrase):
    """One coarse SQL pattern: the words in order, anything between them.

    The gram is a folded form — hyphens removed, plurals singularised — while
    the article may write "solid-state batteries" or "iron phosphate (LFP)".
    Enumerating spellings never keeps up, so the query is deliberately loose
    and gap_pattern does the real matching afterwards.
    """
    ws = [w for w in phrase.split() if w]
    if not ws:
        return ["%"]
    return ["%" + "%".join(ws) + "%"]


def gap_pattern(phrase):
    """The phrase as an article might write it.

    Between words: a hyphen, a slash, a bracket, whitespace. After each word:
    an optional plural, because the gram was singularised and the article was
    not. This is what decides whether a candidate really carries the phrase.
    """
    def one(w):
        if len(w) > 3 and w.endswith("y"):        # battery / batteries
            return re.escape(w[:-1]) + r"(?:y|ies)"
        if w.endswith(("s", "x", "z", "ch", "sh")):
            return re.escape(w) + r"(?:es)?"
        return re.escape(w) + r"s?"

    parts = [one(w) for w in phrase.split()]
    return re.compile(r"\b" + r"[-\u2010-\u2015/()\[\],.\s]{0,3}".join(parts),
                      re.I)


def mark_in(text, phrase):
    """The text with the phrase wrapped in markers, or unchanged if absent."""
    if not text:
        return text or ""
    m = gap_pattern(phrase).search(text)
    if not m:
        return text
    i, end = m.start(), m.end()
    while end < len(text) and text[end].isalpha():
        end += 1                       # "general motor" marks "General Motors"
    return text[:i] + "\u2f00" + text[i:end] + "\u2f01" + text[end:]


def snippet(title, summary, phrase, width=110):
    """The stretch of text the phrase was found in, or nothing if it is only
    in the headline. Shown so a reader can see the evidence without trusting a
    link that may lead to a rewritten page."""
    text = (summary or "").strip()
    if not text:
        return ""
    m = gap_pattern(phrase).search(text)
    if not m:
        return ""
    low, p, i = text.lower(), phrase.lower(), m.start()
    # the headline is marked separately, so a match there is not a reason to
    # suppress the sentence the phrase actually sits in
    stop = i + len(p)
    while stop < len(text) and text[stop].isalpha():
        stop += 1                      # do not cut a word in half
    start = max(0, i - width // 3)
    end = min(len(text), stop + width)
    out = text[start:end]
    # mark the phrase where it sits, keeping the publisher's own capitalisation
    rel, rstop = i - start, stop - start
    out = out[:rel] + "\u2f00" + out[rel:rstop] + "\u2f01" + out[rstop:]
    out = out.strip()
    return ("\u2026" if start else "") + out + ("\u2026" if end < len(text) else "")


def org_fragments(conn, window_start):
    """Phrases that are really pieces of an institution's name.

    "nuclear regulatory" and "international atomic energy" are shards of the
    Nuclear Regulatory Commission and the IAEA. Now that the names are known,
    their own n-grams can leave the phrase list — the institution belongs in its
    own layer, not as two or three broken phrases.
    """
    frags = set()
    try:
        for (nm,) in conn.execute(
                "SELECT DISTINCT name FROM orgs WHERE day>=?", (window_start,)):
            frags.update(ngrams(nm))
    except sqlite3.OperationalError:
        pass
    return frags


def add_days(day, n):
    return (datetime.fromisoformat(day).date() + timedelta(days=n)).isoformat()


def mark_apart(conn, out, recent_start, shown_terms):
    """Phrases whose company is not the company the others keep.

    Every phrase on the list sits in a cloud of words that appear in the same
    articles. Most of those clouds overlap: red sea and houthi attack share
    theirs almost entirely. A phrase whose cloud touches nobody else's is one
    the list has no context for, which is the thing a reader notices.
    """
    if len(shown_terms) < 6:
        return 0
    near = {}
    q = ("SELECT gram, other FROM cooc WHERE day>=? AND gram IN ("
         + ",".join("?" * len(shown_terms)) + ")")
    for gram, other in conn.execute(q, [recent_start] + shown_terms):
        near.setdefault(gram, set()).add(other)
    if len(near) < 6:
        return 0

    scores = {}
    for term, mine in near.items():
        if len(mine) < APART_MIN_WORDS:
            continue          # too small a cloud to say anything about
        best = 0.0
        for other, theirs in near.items():
            if other == term or not theirs:
                continue
            # share of this phrase's words that the other phrase also has
            best = max(best, len(mine & theirs) / len(mine))
        scores[term] = best
    if len(scores) < 6:
        return 0

    vals = sorted(scores.values())
    n = len(vals)
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    cut = mid * APART_SHARE
    marked = 0
    for t in out:
        s = scores.get(t["term"])
        if s is None:
            continue
        t["nearShare"] = round(s, 3)
        if s <= cut and t.get("display", True):
            t["apart"] = True
            marked += 1
    rows = sorted((t for t in out if t.get("nearShare") is not None
                   and t.get("display", True)), key=lambda t: t["nearShare"])
    print(f"  shared surroundings · typical {mid:.0%}, apart below {cut:.0%} · "
          + ", ".join(f"{t['term']} {t['nearShare']:.0%}"
                      + ("*" if t.get("apart") else "") for t in rows[:10]))
    return marked


def sector_overlap(conn, window_start):
    """For each pair of sectors, the share of vocabulary they hold in common.

    Jaccard over the distinct grams each sector used: shared divided by the
    total between them. Utilities and Energy come out high because they cover
    one subject from two desks; sectors that rarely meet come out low.
    """
    ranked = defaultdict(list)
    for sector, gram, n in conn.execute(
            "SELECT sector, gram, SUM(articles) a FROM grams WHERE day>=? "
            "GROUP BY sector, gram HAVING a >= 3 ORDER BY sector, a DESC",
            (window_start,)):
        ranked[sector].append(gram)
    # the same number from each sector, so a prolific desk does not appear to
    # share less simply by printing more
    vocab = {s: set(g[:VOCAB_DEPTH]) for s, g in ranked.items()
             if len(g) >= VOCAB_DEPTH // 4}
    missing = sorted(set(ranked) - set(vocab))
    if missing:
        print(f"  sectors too small to compare · {', '.join(missing)}")
    out = {}
    for a, b in combinations(sorted(vocab), 2):
        va, vb = vocab[a], vocab[b]
        union = len(va | vb)
        out[(a, b)] = (len(va & vb) / union) if union else 0.0
    return out


def normalise_directions(out):
    """Judge each phrase against what the typical phrase did this fortnight.

    A week of heavy intake lifts the denominator every share is measured
    against, so all of them fall together. Dividing by the median ratio removes
    that common movement and leaves what is specific to the phrase.
    """
    ratios = sorted(t["ratio"] for t in out if t.get("ratio"))
    if len(ratios) < 6:
        mid = 1.0                      # too few to establish a normal
    else:
        n = len(ratios)
        mid = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
    mid = mid or 1.0
    for t in out:
        if t["direction"] == "new" or not t.get("ratio"):
            continue
        rel = t["ratio"] / mid
        t["direction"] = ("rising" if rel > 1.3 else
                          "falling" if rel < 0.72 else "steady")
        t["relRatio"] = round(rel, 2)
    return mid


def sector_horizons(conn):
    """The first day each sector produced anything.

    Feeds were added over months, so a sector that began three weeks ago will
    show every phrase arriving within those weeks. That is the microphone, not
    a crossing, and it has to be distinguishable.
    """
    return {s: d for s, d in conn.execute(
        "SELECT sector, MIN(day) FROM totals GROUP BY sector")}


def visible_horizon(conn):
    """The earliest day we could possibly know about, plus a two-day margin.

    Either collection had not started, or pruning has removed the record. A
    phrase first seen at this edge was not necessarily new then.
    """
    first_row = conn.execute("SELECT MIN(day) FROM grams").fetchone()[0]
    kept_from = (datetime.now(timezone.utc).date()
                 - timedelta(days=GRAM_KEEP - 2)).isoformat()
    return max(first_row or "0000-00-00", kept_from)


def build(conn):
    days = days_back(WINDOW_DAYS)
    horizon = visible_horizon(conn)
    sec_start = sector_horizons(conn)
    window_start, recent_start = days[0], days[-RECENT_DAYS]
    corpus = conn.execute("SELECT SUM(articles) FROM totals WHERE day>=?",
                          (window_start,)).fetchone()[0] or 1
    ceiling = max(3, int(corpus * DOC_FREQ_CEILING))
    candidates = [r[0] for r in conn.execute(
        "SELECT gram, SUM(articles) a FROM grams WHERE day>=? GROUP BY gram "
        "HAVING a>=? AND a<=? ORDER BY a DESC LIMIT 4000",
        (recent_start, MIN_ARTICLES, ceiling))]

    sector_size = dict(conn.execute(
        "SELECT sector, SUM(articles) FROM totals WHERE day>=? GROUP BY sector",
        (window_start,)))

    # bigram -> the best trigram that contains it, and how much of the bigram's
    # coverage that trigram accounts for
    sector_pubs = {s: n for s, n in conn.execute(
        "SELECT sector, COUNT(DISTINCT publisher) FROM articles "
        "WHERE published>=? GROUP BY sector", (recent_start,))}

    tri_counts = {}
    for g, n in conn.execute(
            "SELECT gram, SUM(articles) FROM grams WHERE day>=? GROUP BY gram",
            (window_start,)):
        if g.count(" ") == 2:
            tri_counts[g] = n
    bi_counts = {g: n for g, n in conn.execute(
        "SELECT gram, SUM(articles) FROM grams WHERE day>=? GROUP BY gram",
        (window_start,)) if g.count(" ") == 1}
    # The largest single containing trigram, not the sum. An article counts
    # once toward the bigram and once toward each trigram it contains, so
    # summing double-counts and eventually swallows every common phrase —
    # "data center" was being deleted as a fragment of itself.
    inside = defaultdict(int)
    for tri, tn in tri_counts.items():
        w = tri.split()
        for bi in (" ".join(w[:2]), " ".join(w[1:])):
            inside[bi] = max(inside[bi], tn)
    swallowed = {bi for bi, n in bi_counts.items()
                 if n and inside.get(bi, 0) / n >= FRAGMENT_SHARE}

    # rejoin overlapping trigrams into the four-word phrase they came from
    joined, absorbed = {}, set()
    tri_list = sorted(tri_counts.items(), key=lambda kv: -kv[1])
    for a, an in tri_list:
        aw = a.split()
        for b, bn in tri_list:
            if a == b or b in absorbed or a in absorbed:
                continue
            bw = b.split()
            if aw[1:] != bw[:2]:
                continue                       # halves must overlap by two words
            if min(an, bn) / max(an, bn) < JOIN_SHARE:
                continue                       # and move together
            full = " ".join(aw + bw[2:])
            joined[a] = joined[b] = full
            absorbed.update((a, b))
            break

    frags = org_fragments(conn, window_start)
    dropped_frags = 0
    dropped_swallow = 0

    # Phrases that recur across the corpus. A companion seen once came from one
    # article's sentence structure — "executives discussed capacity", "estimates
    # suggest jet" — and says nothing about the topic.
    # Lift compares a fortnight's co-occurrence against a fortnight's base
    # rates. Counting the numerator over 14 days and the denominators over 90
    # understated every lift by roughly six times.
    gram_articles = dict(conn.execute(
        "SELECT gram, SUM(articles) FROM grams WHERE day>=? GROUP BY gram",
        (recent_start,)))
    corpus_articles = conn.execute(
        "SELECT SUM(articles) FROM totals WHERE day>=?", (recent_start,)).fetchone()[0] or 1

    recurring = {g for (g,) in conn.execute(
        "SELECT gram FROM grams WHERE day>=? GROUP BY gram "
        "HAVING SUM(articles) >= ? AND COUNT(DISTINCT sector) >= ?",
        (window_start, COOC_MIN_ARTICLES, COOC_MIN_SECTORS))}
    recurring -= frags        # institution names broken into pieces
    recurring -= swallowed    # two-word remnants of a three-word phrase

    out = []
    for gram in candidates:
        if gram in joined:
            gram = joined[gram]          # show the whole phrase, not a half
        if gram in frags:
            dropped_frags += 1
            continue
        if gram in swallowed:
            dropped_swallow += 1
            continue
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

        # A sector counted on a single article, which is how a phrase reached
        # three sectors on three articles and looked like a spread. Two is a
        # low bar but it is the difference between a pattern and a coincidence.
        sectors = [{"name": k, "firstSeen": v["first"], "recent": v["recent"],
                    "articles": v["articles"]} for k, v in secs.items()
                   if v["recent"] > 0 and v["articles"] >= SECTOR_MIN_ARTICLES]
        if len(sectors) < MIN_SECTORS: continue
        # Breadth, measured rather than counted. A phrase can touch five sectors
        # while four-fifths of its articles sit in one — that is concentration
        # with spillover, not spreading. A sector counts toward breadth only if
        # the phrase is over-represented there against its corpus-wide rate.
        tot_arts = sum(s["articles"] for s in sectors) or 1
        for s in sectors:
            size = sector_size.get(s["name"], 0)
            s["share"] = round(s["articles"] / size, 4) if size else 0

        # Effective sectors: how many it behaves like, not how many it touches.
        # A phrase in five sectors with four-fifths of its articles in one
        # behaves like two, and comparing that against the five it claims is
        # what separates spreading from concentration with spillover.
        eff = math.exp(-sum((s["articles"] / tot_arts) * math.log(s["articles"] / tot_arts)
                            for s in sectors if s["articles"]))
        display = (eff / len(sectors) >= SECTOR_EVENNESS
                   and eff >= SECTOR_MIN_EFFECTIVE)

        # a sector that had only just started listening cannot have been
        # "reached" — mark those so the interface can say so
        for s in sectors:
            began = sec_start.get(s["name"])
            s["atSectorStart"] = bool(
                began and s["firstSeen"] and s["firstSeen"] <= add_days(began, 2))

        firsts = sorted(s["firstSeen"] for s in sectors if s["firstSeen"])
        adoption_span = None
        if len(firsts) > 1:
            adoption_span = (datetime.fromisoformat(firsts[-1]).date()
                             - datetime.fromisoformat(firsts[0]).date()).days

        sectors.sort(key=lambda s: s["firstSeen"])
        tot = sum(s["recent"] for s in sectors) or 1
        d0 = datetime.fromisoformat(window_start).date()
        for s in sectors:
            s["share"] = round(s["recent"]/tot, 3)
            # how many of that sector's outlets reached for the phrase, not
            # how many articles carried it: one prolific title cannot move this
            base = sector_pubs.get(s["name"], 0)
            used = conn.execute(
                "SELECT COUNT(DISTINCT publisher) FROM articles WHERE sector=? "
                "AND published>=? AND LOWER(title || '. ' || COALESCE(summary,'')) LIKE ?",
                (s["name"], recent_start, like_forms(gram)[0])).fetchone()[0] or 0
            s["grip"] = round(used / base, 3) if base else 0.0
            s["outlets"] = used
            s["adopt"] = (datetime.fromisoformat(s["firstSeen"]).date() - d0).days

        recent = sum(daily[d] for d in days[-RECENT_DAYS:])
        prior  = sum(daily[d] for d in days[-2*RECENT_DAYS:-RECENT_DAYS])
        # share of that fortnight's coverage, not the raw count
        prior_corpus = conn.execute(
            "SELECT SUM(articles) FROM totals WHERE day>=? AND day<?",
            (days[-2 * RECENT_DAYS], days[-RECENT_DAYS])).fetchone()[0] or 1
        r_share = recent / (corpus or 1)
        p_share = prior / prior_corpus
        # settled once every phrase has a ratio; see normalise_directions
        t_ratio = (r_share / p_share) if (prior and p_share) else None
        direction = "new" if prior == 0 else "steady"
        # Weekly composition: how the mix of industries carrying this phrase has
        # shifted. Weekly rather than daily because a streamgraph of daily counts
        # is noise, and the question is the shape of the change, not the detail.
        week_of = {d: i // 7 for i, d in enumerate(days)}
        n_weeks = (len(days) + 6) // 7
        bands = {}
        for day, sector, hits in rows:
            w = week_of.get(day)
            if w is None:
                continue
            bands.setdefault(sector, [0] * n_weeks)[w] += hits
        # only industries that actually carry it, biggest first
        bands = {k: v for k, v in bands.items() if sum(v) > 0}
        order = sorted(bands, key=lambda k: -sum(bands[k]))
        flow = {"weeks": [days[min(i * 7, len(days) - 1)] for i in range(n_weeks)],
                "bands": [{"sector": k, "n": bands[k]} for k in order]}

        # If a phrase was already present at the edge of what we can see, its
        # "first seen" date says when we started looking — or when retention
        # cut the record — not when the industry started writing. Both edges
        # count: collection start, and the gram retention horizon, whichever
        # is nearer. Once collection outlives retention the second one binds.
        earliest = min(s["firstSeen"] for s in sectors)
        at_corpus_start = earliest <= horizon

        origins = {}
        try:
            for cc, cnt in conn.execute(
                    "SELECT country, SUM(n) FROM gramcc WHERE gram=? AND day>=? "
                    "GROUP BY country", (gram, window_start)):
                origins[cc] = cnt
        except sqlite3.OperationalError:
            pass

        newest = max(sectors, key=lambda s: s["firstSeen"])

        # Split the co-occurrence window so the panel can say what is newly
        # being discussed alongside a phrase, not just what always was. A broad
        # phrase like "data center" means something different when its
        # companions shift from "campus" to "interconnection queue".
        half = days[-(RECENT_DAYS)]
        cooc_prior = days[-(2 * RECENT_DAYS)]      # the fortnight before that
        recent_p, prior_p = {}, {}
        for o, n in conn.execute(
                "SELECT o, SUM(n) FROM ("
                "  SELECT other o, n FROM cooc WHERE gram=? AND day>=? "
                "  UNION ALL "
                "  SELECT gram o, n FROM cooc WHERE other=? AND day>=?"
                ") GROUP BY o", (gram, half, gram, half)):
            if o not in recurring:
                continue
            recent_p[o] = n
        for o, n in conn.execute(
                "SELECT o, SUM(n) FROM ("
                "  SELECT other o, n FROM cooc WHERE gram=? AND day<? AND day>=? "
                "  UNION ALL "
                "  SELECT gram o, n FROM cooc WHERE other=? AND day<? AND day>=?"
                ") GROUP BY o",
                (gram, half, cooc_prior, gram, half, cooc_prior)):
            prior_p[o] = n

        # A companion sharing words with the phrase is a rearrangement of it —
        # "united arab" under "united arab emirate", "large language" under
        # "large language model". Restating the topic is not a companion.
        own = set(gram.split())
        own_articles = gram_articles.get(gram, 0)
        recent_p = {o: n for o, n in recent_p.items() if not (own & set(o.split()))}
        prior_p = {o: n for o, n in prior_p.items() if o in recent_p or True}

        # Collapse overlapping partners. Sorting by length first means the
        # longest form is always considered before its own fragments, so
        # "estate investment" cannot be kept ahead of "estate investment trust".
        recent_p = {o: n for o, n in recent_p.items() if o not in swallowed}
        cands = sorted(recent_p, key=lambda o: (-len(o), -recent_p[o]))
        chosen = []
        for o in cands:
            if any(o in k for k in chosen):
                continue
            chosen.append(o)
        chosen.sort(key=lambda o: -recent_p[o])
        chosen = chosen[:24]          # lift decides which six survive
        partners = [(o, recent_p[o]) for o in chosen]
        mx = max([n for _, n in partners], default=1)
        # Lift, as the regional view uses: how much more often this companion
        # appears with this phrase than with the corpus at large. Ranking by raw
        # co-occurrence surfaced whatever is simply common, which is why the
        # panel read as the phrase's own vocabulary restated.
        lifted = []
        for o, n in partners:
            base = gram_articles.get(o, 0)
            if not base or not own_articles:
                continue
            expected = own_articles * (base / max(1, corpus_articles))
            lift = n / expected if expected else 0
            if lift >= COOC_MIN_LIFT:
                lifted.append((o, n, lift))
        lifted = [(o, n, lf) for o, n, lf in lifted
                  if prior_p.get(o, 0) > 0 or n >= COOC_MIN_NEW]
        lifted.sort(key=lambda t: (-t[2], -len(t[0])))
        seen_shape, deduped = set(), []
        for o, n, lf in lifted:
            shape = (n, round(lf, 1))
            if shape in seen_shape:
                continue          # same articles, different words
            ow = set(o.split())
            if any(len(ow & set(k.split())) / min(len(ow), len(k.split())) >= 0.5
                   and min(n, kn) / max(n, kn) >= 0.75
                   for k, kn, _ in deduped):
                continue          # same words, same volume: one thing twice
            seen_shape.add(shape)
            deduped.append((o, n, lf))
        lifted = deduped
        partners = [(o, n) for o, n, _ in lifted[:6]]
        lift_of = {o: lf for o, _, lf in lifted}

        together, drop_pairs = {}, set()
        for o, _n in partners:
            def pair_rows(patterns, limit):
                clause = " AND ".join(
                    ["LOWER(title || '. ' || COALESCE(summary,'')) LIKE ?"] * len(patterns))
                return list(conn.execute(
                    "SELECT title, url, publisher, sector, summary FROM articles "
                    f"WHERE published>=? AND {clause} ORDER BY published DESC LIMIT ?",
                    tuple([recent_start] + patterns + [limit])))

            rows2 = []
            for gf in like_forms(gram):
                for of in like_forms(o):
                    rows2 = pair_rows([gf, of], 6)
                    if rows2:
                        break
                if rows2:
                    break
            if not rows2:
                loose = pair_rows([f"%{w[:-1] if len(w) > 5 else w}%"
                                   for w in gram.split() + o.split()], 30)
                pg, po = gap_pattern(gram), gap_pattern(o)
                rows2 = [r for r in loose
                         if pg.search(f"{r[0]}. {r[4] or ''}")
                         and po.search(f"{r[0]}. {r[4] or ''}")][:6]
            pubs2 = [r[2] for r in rows2]
            if not pubs2:
                drop_pairs.add(o)     # nothing to show for it
                continue
            lead = max(Counter(pubs2).values()) / len(pubs2)
            if lead > COOC_FEED_SHARE and len(set(pubs2)) < 4:
                drop_pairs.add(o)     # one newsroom pairing them, not a pairing
                continue
            together[o] = [{"title": mark_in(ti, o), "url": u, "publisher": p,
                            "sector": s,
                            "quote": snippet(ti, sm, o) or snippet(ti, sm, gram)}
                           for ti, u, p, s, sm in rows2]

        def seen_together_before(other):
            """Did these two share an article in the prior fortnight?

            The pair table is pruned hard, so its silence is not evidence.
            The articles are not pruned, so they can settle it.
            """
            return conn.execute(
                "SELECT 1 FROM articles WHERE published>=? AND published<? "
                "AND LOWER(title || '. ' || COALESCE(summary,'')) LIKE ? "
                "AND LOWER(title || '. ' || COALESCE(summary,'')) LIKE ? LIMIT 1",
                (cooc_prior, half, f"%{gram}%", f"%{other}%")).fetchone() is not None

        pdir = {}
        for o, n in partners:
            p = prior_p.get(o, 0)
            if p == 0 and seen_together_before(o):
                pdir[o] = "steady"       # present before, merely pruned away
            else:
                pdir[o] = ("new" if p == 0 else "rising" if n > p * 1.35
                           else "falling" if n < p * 0.7 else "steady")

        def articles_for(patterns, limit=12):
            clause = " AND ".join(
                ["LOWER(title || '. ' || COALESCE(summary,'')) LIKE ?"] * len(patterns))
            return list(conn.execute(
                "SELECT publisher, title, url, sector, COALESCE(country,'US'), summary, published "
                "FROM articles "
                f"WHERE published>=? AND {clause} ORDER BY published DESC LIMIT ?",
                tuple([recent_start] + patterns + [limit])))

        # the phrase as written, which is what a reader will look for
        rows = []
        pat = gap_pattern(gram)
        for gf in like_forms(gram):
            # the query is coarse by design; this is where it is made precise
            rows = [r for r in articles_for([gf], limit=40)
                    if pat.search(f"{r[1]}. {r[5] or ''}")][:12]
            if rows:
                break
        if not rows:
            # only then loosen, for phrases folded from a plural — but keep
            # only what the phrase can actually be found in, hyphens and all
            loose = articles_for(
                [f"%{w[:-1] if len(w) > 5 else w}%" for w in gram.split()], limit=40)
            rows = [r for r in loose
                    if pat.search(f"{r[1]}. {r[5] or ''}")][:12]
        examples = [{"publisher": p, "title": mark_in(t, gram), "url": u,
                     "date": d,
                     "sector": s, "country": cc, "quote": snippet(t, sm, gram)}
                    for p, t, u, s, cc, sm, d in rows]
        events = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(story,''), id)) FROM articles "
            "WHERE (LOWER(title) LIKE ? OR LOWER(summary) LIKE ?) AND published>=?",
            (f"%{gram}%", f"%{gram}%", recent_start)).fetchone()[0] or 0

        pub_rows = conn.execute(
            "SELECT publisher, COUNT(*) FROM articles WHERE "
            "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?) AND published>=? "
            "GROUP BY publisher ORDER BY 2 DESC",
            (f"%{gram}%", f"%{gram}%", recent_start)).fetchall()
        pubs = len(pub_rows)
        pub_total = sum(n for _, n in pub_rows) or 1
        top_pub_share = (pub_rows[0][1] / pub_total) if pub_rows else None

        out.append({
            "id": re.sub(r"[^a-z0-9]+", "-", gram), "term": gram, "direction": direction,
            "ratio": t_ratio,
            "display": display,
            "adoptionSpan": adoption_span,
            "pace": ("simultaneous" if adoption_span is not None and adoption_span <= 7
                     else "gradual" if adoption_span is not None and adoption_span >= 28
                     else "mixed"),
            "kind": ("place" if set(gram.split()) & PLACE_ANCHORS else "concept"),
            "sectorCount": len(sectors), "effectiveSectors": round(eff, 2),
            "newestSector": newest["name"],
            "daysSinceNewestSector": (datetime.now(timezone.utc).date()
                                      - datetime.fromisoformat(newest["firstSeen"]).date()).days,
            "articles14d": recent, "articlesPrior14d": prior, "publishers": pubs,
            "events": events,
            "topPubShare": (round(top_pub_share, 3)
                            if top_pub_share is not None else None),
            "sectors": sectors,
            "sinceStart": at_corpus_start,
            "origins": sorted(([k, v] for k, v in origins.items()),
                              key=lambda kv: -kv[1])[:6],
            "flow": flow,
            "cooc": [[o, round(n/mx, 2), pdir.get(o, "steady"), n, prior_p.get(o, 0),
                      round(lift_of.get(o, 0), 1), together.get(o, [])]
                     for o, n in partners if o not in drop_pairs],
            "examples": examples,
            "daily": [{"day": d, "n": daily.get(d, 0)} for d in days],
            "_daily": daily,
        })
    # "holds benchmark", "benchmark rate" and "holds benchmark rate" are one
    # phrase at three lengths. Keep the longest whenever the shorter form adds
    # essentially no extra coverage.
    # One phrase inside another is one topic named two ways. Keep whichever is
    # used more and carry the other as a variant, rather than picking a winner
    # and discarding the name a reader may have been looking for.
    out.sort(key=lambda t: -t["articles14d"])
    kept, drop = [], set()
    for t in out:
        redundant = False
        for k in kept:
            # Compare against the host and everything already folded into it.
            # "storage system" is inside "energy storage system", which is
            # itself a variant of "energy storage" — checking only hosts let
            # the third form through.
            family = [k["term"]] + k.get("variants", [])
            a = t["term"]
            if any((a in b or b in a) and a != b for b in family):
                k.setdefault("variants", []).append(a)
                redundant = True
                break
        if redundant:
            drop.add(t["term"])
        else:
            kept.append(t)
    out = [t for t in out if t["term"] not in drop]

    # Words cannot tell "energy storage" from "storage system", or "electric
    # utility" from "utility scale" — they share a word without either
    # containing the other. Shared articles can: if two phrases turn up in most
    # of the same coverage, they are one story under two names. This is
    # evidence rather than spelling, so it does not merge "crude oil" with
    # "palm oil", which share a word but almost no articles.
    by_term = {t["term"]: t for t in out}
    pair_n = {}
    for a, b, n in conn.execute(
            "SELECT gram, other, SUM(n) FROM cooc WHERE day>=? GROUP BY gram, other",
            (recent_start,)):
        if a in by_term and b in by_term:
            pair_n[(a, b)] = pair_n[(b, a)] = n

    kept2, drop2 = [], set()
    for t in sorted(out, key=lambda t: -t["articles14d"]):
        host = None
        for k in kept2:
            small = min(t["articles14d"], k["articles14d"]) or 1
            together = pair_n.get((t["term"], k["term"]), 0)
            if together / small >= SAME_STORY_SHARE:
                host = k
                break
        if host is not None:
            host.setdefault("variants", []).append(t["term"])
            drop2.add(t["term"])
        else:
            kept2.append(t)
    out = [t for t in out if t["term"] not in drop2]

    # Breadth matters, but so does being unusual. A phrase in nine sectors that
    # appears in a fifth of all articles is vocabulary, not news.
    corpus_move = normalise_directions(out)
    if abs(corpus_move - 1) > 0.08:
        print(f"  corpus itself moved {corpus_move:.2f}\u00d7 this fortnight; "
              f"directions are measured against that")
    homeless = bunched = everywhere = oneshop = oneevent = unsourced = 0
    places = 0
    all_sectors = {s["name"] for t in out for s in t["sectors"]}
    for t in out:
        freq = t["articles14d"] / corpus
        t["docFreq"] = round(freq, 4)
        # specificity is now measured against the common ceiling, so it
        # discriminates across the range phrases actually occupy
        t["specificity"] = round(max(0.0, 1 - freq / COMMON_CEILING), 3)
        # effective sectors rather than the raw count: a phrase claiming five
        # sectors while behaving like two should not outrank one that behaves
        # like the three it claims.
        secs = t["sectors"]
        recent_tot = sum(s.get("recent", 0) for s in secs) or 1
        home_share = max(s.get("recent", 0) for s in secs) / recent_tot
        t["homeShare"] = round(home_share, 3)
        recent_days = [d for d in days[-RECENT_DAYS:] if t["_daily"].get(d, 0)]
        t["daysSeen"] = len(recent_days)
        if t["docFreq"] >= COMMON_CEILING:
            t["display"] = False        # already everyone's word, not a crossing
            homeless += 1
        elif t["sectorCount"] > max(4, int(len(all_sectors) * EVERYWHERE_SHARE)):
            t["display"] = False        # in most of the trade press at once
            everywhere += 1
        elif t.get("kind") == "place":
            t["display"] = False        # a place is a subject, not language
            places += 1
        elif not t.get("examples"):
            t["display"] = False        # nothing to show the reader
            unsourced += 1
        elif (t.get("events") and t["articles14d"] / t["events"] >= PER_EVENT_MAX
              and t["events"] < 5):
            t["display"] = False        # one story retold, not language moving
            oneevent += 1
        elif (t.get("topPubShare") is not None
              and (t["topPubShare"] > ONE_FEED_HARD
                   or (t["topPubShare"] > ONE_FEED_SHARE and 0 < t["publishers"] < 4))):
            t["display"] = False        # one newsroom's habit, not a crossing
            oneshop += 1
        elif t["daysSeen"] < MIN_DAYS_SEEN and t["articles14d"] >= t["daysSeen"] * 2:
            t["display"] = False        # one burst, not sustained use
            bunched += 1
        # how recently it reached its newest sector: a crossing keeps most of
        # its weight for three weeks, then settles rather than vanishing
        fresh = 0.55 + 0.45 * math.exp(-(t.get("daysSinceNewestSector") or 0)
                                       / CROSS_HALFLIFE)
        # sectors that rarely share anything saying the same thing is the
        # finding; two neighbours doing it is ordinary
        # led by the sector where the phrase took the largest bite of that
        # sector's own output, so a quiet trade press is not drowned out
        grip = max((s.get("grip") or 0) for s in t["sectors"]) if t["sectors"] else 0
        odd = 0.6 + 0.4 * min(1.0, grip / GRIP_FULL)
        t["rank"] = round(t["effectiveSectors"] * (0.35 + 0.65 * t["specificity"])
                          * (home_share ** HOME_WEIGHT) * fresh * odd, 3)
    out.sort(key=lambda t: (-t["rank"], t["daysSinceNewestSector"]))

    # "iran war" and "iran conflict" are one story; "energy storage" and
    # "energy security" are not. What separates them is that the shared word is
    # a place. Sharing an ordinary word — energy, oil, data, grid — says
    # nothing, because those head dozens of unrelated phrases.
    #
    # Grouped, never deleted: the smaller form is shown beneath the larger
    # rather than removed, because the rule still cannot tell "texas grid" from
    # "texas heat" and a wrong grouping should stay visible.
    grouped = []
    for t in sorted(out, key=lambda t: -t["articles14d"]):
        tw = set(t["term"].split())
        fam = next((f for f in SYNONYM_FAMILIES if t["term"] in f), None)
        host = next((k for k in grouped if fam and k["term"] in fam), None)
        if host is None:
            host = next((k for k in grouped
                         if (tw & set(k["term"].split()) & PLACE_WORDS)
                         and min(t["articles14d"], k["articles14d"])
                           / max(1, k["articles14d"]) >= 0.2), None)
        if host is not None:
            host.setdefault("variants", []).append(t["term"])
            continue
        grouped.append(t)
    # restore the ranked order: the sort above was only to choose hosts
    grouped.sort(key=lambda t: (-t["rank"], t["daysSinceNewestSector"]))
    out = grouped

    overlap = sector_overlap(conn, window_start)
    if overlap:
        vals = sorted(overlap.values())
        m = len(vals)
        mid = vals[m // 2] if m % 2 else (vals[m // 2 - 1] + vals[m // 2]) / 2
        lo = min(overlap.items(), key=lambda kv: kv[1])
        hi = max(overlap.items(), key=lambda kv: kv[1])
        print(f"  sector vocabulary overlap · typical {mid:.1%}, "
              f"closest {hi[0][0]}/{hi[0][1]} {hi[1]:.0%}, "
              f"most distant {lo[0][0]}/{lo[0][1]} {lo[1]:.1%}")

    shown_terms = [t["term"] for t in out if t.get("display", True)]
    apart = mark_apart(conn, out, recent_start, shown_terms)
    if apart:
        print(f"  {apart} standing apart "
              f"(few of the words around it are around the others)")

    # a word heading several candidate phrases marks a subject, so that one
    # subject cannot take the whole allowance
    cand = [t for t in out if t.get("display", True)]
    word_n = Counter(w for t in cand for w in set(t["term"].split()))
    def family_of(t):
        hits = [w for w in t["term"].split() if word_n[w] >= FAMILY_MIN]
        return max(hits, key=lambda w: (word_n[w], w)) if hits else None

    fam_count = Counter()
    lead_count = Counter()
    spread, held, pool = [], [], []
    for t in out:
        if not t["display"]:
            pool.append(t)          # companions only; never in the phrase list
            continue
        lead = max(t["sectors"], key=lambda s: s["articles"])["name"]
        # neighbouring sectors that cover one subject count against a shared
        # allowance, or four of them fill the list between them
        key = next((b for b in SECTOR_BLOCS if lead in b), None) or lead
        fam = family_of(t)
        if (lead_count[key] < (MAX_PER_BLOC if key in SECTOR_BLOCS
                               else MAX_PER_SECTOR)
                and (fam is None or fam_count[fam] < MAX_PER_FAMILY)):
            lead_count[key] += 1
            if fam:
                fam_count[fam] += 1
            spread.append(t)
        else:
            held.append(t)          # kept, but behind everything better spread

    # Past about forty a reader stops reading and starts scrolling. Ranking
    # already puts the strongest first, so the tail is simply not displayed —
    # it still feeds companions, sector counts and the maps.
    refill = []
    for t in held:
        lead = max(t["sectors"], key=lambda s: s["articles"])["name"]
        if next((b for b in SECTOR_BLOCS if lead in b), None) is not None:
            continue                  # bloc phrases never refill
        fam = family_of(t)
        if fam and fam_count[fam] >= MAX_PER_FAMILY:
            continue                  # nor does a subject already at its limit
        if fam:
            fam_count[fam] += 1
        refill.append(t)
    rest = [t for t in held if t not in refill]
    for t in rest:
        t["heldForSpread"] = True     # ranked in, displaced by the bloc quota
    shown = (spread + refill)[:SHOW_TERMS]
    for t in (spread + refill)[SHOW_TERMS:] + rest:
        t["display"] = False
        pool.append(t)
    out = shown + pool

    n_cooc = sum(len(t.get("cooc", [])) for t in out)
    n_uniq = len({r[0] for t in out for r in t.get("cooc", [])})
    print(f"  phrases · {len(shown)} shown, {len(pool)} pooled for companions")
    if places:
        print(f"  {places} set aside as place names "
              f"(the site reports phrases, not where events happened)")
    if unsourced:
        print(f"  {unsourced} set aside with no article to show for them")
    if oneevent:
        print(f"  {oneevent} set aside as one story retold "
              f"(over {PER_EVENT_MAX:g} articles per distinct event)")
    if oneshop:
        print(f"  {oneshop} set aside as one newsroom's own usage "
              f"(over {ONE_FEED_SHARE:.0%} of articles from a single publisher)")
    if everywhere:
        print(f"  {everywhere} set aside as shared vocabulary "
              f"(in more than half the sectors at once)")
    if homeless:
        print(f"  {homeless} set aside as already common "
              f"(in over {COMMON_CEILING:.0%} of recent coverage)")
    if bunched:
        print(f"  {bunched} set aside as a single burst "
              f"(seen on fewer than {MIN_DAYS_SEEN} days)")
    for t in out:
        t.pop("_daily", None)
    spread = Counter(t["sectorCount"] for t in out if t.get("display", True))
    if spread:
        print("  sector spread · " + ", ".join(
            f"{k}:{spread[k]}" for k in sorted(spread)))
    lead_mix = Counter(max(t["sectors"], key=lambda s: s["articles"])["name"]
                  for t in out if t.get("display", True))
    fam_shown = Counter(family_of(t) for t in out if t.get("display", True))
    fam_shown.pop(None, None)
    if fam_shown:
        print("  subjects in the shown list · " + ", ".join(
            f"{f} {n}" for f, n in fam_shown.most_common(6)))
    if lead_mix:
        print("  sectors leading the shown list · " + ", ".join(
            f"{s} {n}" for s, n in lead_mix.most_common(6)))
    print(f"  {len(shown)} of {SHOW_TERMS} places filled "
          f"({len(rest)} bloc phrases held back)")
    lead = [t for t in out if t.get("display", True)][:6]
    if lead:
        print("  leading the list · " + ", ".join(
            f"{t['term']} ({t.get('rank', 0):.2f})" for t in lead))
    edge = [t for t in out if t.get("display", True)][SHOW_TERMS:SHOW_TERMS + 4]
    if edge:
        print("  just below the cut · " + ", ".join(t["term"] for t in edge))

    gripped = sorted((t for t in out if t.get("display", True)),
                     key=lambda t: -max((s.get("grip") or 0) for s in t["sectors"]))[:5]
    if gripped:
        print("  most outlets in a sector reaching for it · " + ", ".join(
            f"{t['term']} {max((s.get('grip') or 0) for s in t['sectors']):.0%}"
            for t in gripped))
    dense = sorted((t for t in out
                    if t.get("display", True) and t.get("events")),
                   key=lambda t: -(t["articles14d"] / t["events"]))[:5]
    if dense:
        print("  most event-bound · " + ", ".join(
            f"{t['term']} {t['articles14d']/t['events']:.1f}/story" for t in dense))
    conc = sorted((t for t in out
                   if t.get("display", True) and t.get("topPubShare") is not None),
                  key=lambda t: -t["topPubShare"])[:6]
    if conc:
        print("  most concentrated · " + ", ".join(
            f"{t['term']} {t['topPubShare']:.0%}/{t['publishers']}pub"
            for t in conc))
    busiest = sorted(out, key=lambda t: -t.get("docFreq", 0))[:8]
    if busiest:
        print("  busiest phrases · " + ", ".join(
            f"{t['term']} {t.get('docFreq', 0):.1%}" for t in busiest))
    print(f"  companions · {n_cooc} attachments, {n_uniq} distinct")
    if SYNDICATED[0]:
        print(f"  {SYNDICATED[0]} syndicated copies skipped "
              f"(same story under a sister title)")
    if dropped_frags or dropped_swallow:
        print(f"  dropped · {dropped_frags} institution fragments, "
              f"{dropped_swallow} swallowed by a longer phrase")
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



SITE = "https://crosstalkwire.com"


def esc(s):
    return html.escape(str(s or ""), quote=True)


def plain(s):
    """Marked text as safe HTML, with the phrase markers become bold."""
    t = html.escape(str(s or ""), quote=True)
    return t.replace("\u4e00", "<b>").replace("\u4e01", "</b>")


def human_list(names):
    names = [n.lower() for n in names]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def write_phrase_pages(terms, root):
    """One real page per shown phrase, so search engines have something to read.

    The dashboard draws everything from data.json after load, which means a
    crawler sees an empty shell and "#t=rare-earth" is a fragment rather than a
    page. These are plain HTML with the facts already in them: the phrase, the
    sectors carrying it, when each picked it up, and the articles behind the
    count. Each links back to the interactive view.
    """
    out = os.path.join(root, "phrases")
    shutil.rmtree(out, ignore_errors=True)      # phrases leave the list; pages
    os.makedirs(out, exist_ok=True)             # must not outlive them
    shown = [t for t in terms if t.get("display", True)]
    written = []

    for t in shown:
        secs = sorted(t["sectors"], key=lambda s: s.get("firstSeen") or "")
        home = max(t["sectors"], key=lambda s: s.get("articles", 0))["name"]
        others = [s["name"] for s in secs if s["name"] != home]
        lead = (f"\u201c{t['term']}\u201d started in {home.lower()} coverage and is "
                f"now also being used in {human_list(others)}."
                if others else
                f"\u201c{t['term']}\u201d appears in {home.lower()} coverage.")
        desc = (f"{lead} {t['articles14d']} articles in the last 14 days across "
                f"{t['sectorCount']} sectors of the trade press.")

        rows = "".join(
            f"<tr><td>{esc(s['name'])}</td>"
            f"<td>{esc(str(s.get('firstSeen') or ''))}</td>"
            f"<td>{s.get('articles', 0)}</td></tr>"
            for s in secs)

        arts = "".join(
            f"<li><a href=\"{esc(a['url'])}\" rel=\"nofollow\">{plain(a['title'])}</a>"
            f"<br><span class=\"src\">{esc(a.get('publisher',''))} \u00b7 "
            f"{esc(a.get('sector',''))}</span>"
            + (f"<p class=\"q\">{plain(a['quote'])}</p>" if a.get("quote") else "")
            + "</li>"
            for a in (t.get("examples") or [])[:6])

        html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(t['term'])} \u2014 which industries are using it | Crosstalk</title>
<meta name="description" content="{esc(desc)[:300]}">
<link rel="canonical" href="{SITE}/phrases/{t['id']}.html">
<meta property="og:type" content="article">
<meta property="og:title" content="{esc(t['term'])} \u2014 which industries are using it">
<meta property="og:description" content="{esc(desc)[:300]}">
<meta property="og:url" content="{SITE}/phrases/{t['id']}.html">
<style>
 body{{max-width:44rem;margin:0 auto;padding:2rem 1.25rem;
   font:16px/1.55 Georgia,serif;color:#1B1A17;background:#FBFAF7}}
 a{{color:#1B3BC7}} h1{{font-size:1.6rem;line-height:1.2;margin:0 0 .3rem}}
 .kicker{{font:600 .7rem/1 ui-monospace,monospace;letter-spacing:.12em;
   text-transform:uppercase;color:#6B6A64;text-decoration:none}}
 table{{border-collapse:collapse;width:100%;margin:1.2rem 0;font-size:.9rem}}
 th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #E5E3DC}}
 ul{{list-style:none;padding:0}} li{{margin:0 0 1rem}}
 .src{{font:.72rem/1.4 ui-monospace,monospace;color:#6B6A64;
   letter-spacing:.05em;text-transform:uppercase}}
 .q{{margin:.35rem 0 0;padding-left:.7rem;border-left:2px solid #E5E3DC;
   color:#4A4842;font-size:.92rem}}
 footer{{margin-top:2.5rem;padding-top:1rem;border-top:1px solid #E5E3DC;
   font-size:.85rem;color:#6B6A64}}
</style></head><body>
<a class="kicker" href="{SITE}/">Crosstalk</a>
<h1>{esc(t['term'])}</h1>
<p>{esc(lead)}</p>
<p>Over the last 14 days it appeared in <b>{t['articles14d']} articles</b> from
{t.get('publishers', 0)} trade publications, across {t['sectorCount']} sectors.</p>
<h2 style="font-size:1.05rem">When each sector started using it</h2>
<table><thead><tr><th>Sector</th><th>First seen</th><th>Articles</th></tr></thead>
<tbody>{rows}</tbody></table>
<h2 style="font-size:1.05rem">Recent coverage</h2>
<ul>{arts or '<li>No articles captured yet.</li>'}</ul>
<footer>
<p><a href="{SITE}/#t={t['id']}">Open this phrase in the dashboard</a> to see
what it is discussed alongside, which companies are named with it, and how
usage moved week by week.</p>
<p>Crosstalk reads trade publications and reports which phrases are crossing
between industries. Every figure is a count of articles; nothing is forecast.
Headlines and excerpts belong to the publishers who wrote them.</p>
</footer></body></html>"""
        with open(os.path.join(out, f"{t['id']}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        written.append(t["id"])

    stamp = datetime.now(timezone.utc).date().isoformat()
    urls = "".join(
        f"<url><loc>{SITE}/phrases/{i}.html</loc><lastmod>{stamp}</lastmod>"
        f"<changefreq>daily</changefreq></url>" for i in written)
    with open(os.path.join(root, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f'<url><loc>{SITE}/</loc><lastmod>{stamp}</lastmod>'
                f'<changefreq>daily</changefreq><priority>1.0</priority></url>'
                f'{urls}</urlset>')
    with open(os.path.join(root, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n")
    return len(written)


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
    set_publishers(cfg)
    n_co = load_companies()
    raw_cfg = open(CONFIG, "rb").read()
    raw_code = open(os.path.abspath(__file__), "rb").read()
    kinds = Counter(s["type"] for sec in cfg["sectors"] for s in sec["sources"])
    print(f"crosstalk {VERSION} · code {hashlib.sha1(raw_code).hexdigest()[:8]}"
          f" · config {hashlib.sha1(raw_cfg).hexdigest()[:8]}")
    print(f"  {len(cfg['sectors'])} sectors · {len(ALL_PUBLISHERS)} mastheads · "
          f"{n_co} companies · "
          + " · ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + "\n")

    if args.probe:
        return probe(cfg)           # touches nothing on disk

    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)      # creates anything missing entirely
    conn.commit()
    migrated = migrate(conn)        # then adds columns to tables that predate them

    # Duplicates must go before anything is recomputed from them.
    drop_syndicated(conn)

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
        "SELECT sector, COALESCE(publisher,'?') p, note, COUNT(*) n, SUM(ok) good "
        "FROM runlog WHERE ts >= datetime('now','-3 days') AND source='rss' "
        "GROUP BY sector, p, note HAVING good=0 AND note!='' AND n>=3 "
        "ORDER BY n DESC LIMIT 12"))
    if bad:
        print("  feeds failing consistently — worth removing from config.json:")
        for sector, pub, note, n, _ in bad:
            print(f"      {sector:<14} {pub[:24]:<26} {note[:44]}  ({n} attempts)")
        print()

    # again after fetching: the pass above cleaned history, this cleans arrivals
    if drop_syndicated(conn):
        rebuild(conn, verbose=False)

    days_held = conn.execute("SELECT COUNT(DISTINCT day) FROM totals").fetchone()[0]
    stored    = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    live      = conn.execute("SELECT COUNT(*) FROM articles WHERE backfilled=0").fetchone()[0]
    terms = build(conn)
    print(f"\n  {stored} articles ({live} observed live, {stored-live} backdated)"
          f" across {days_held} days")
    if stored and (stored - live) / stored > 0.25:
        print("  Most of this corpus was imported after publication. Fine for the"
              " dashboard;\n  the backtest excludes backdated rows, so its usable"
              " history is shorter.")
    print(f"  {len(terms)} phrases in {MIN_SECTORS}+ sectors\n")

    if args.report:
        for t in terms[:25]:
            print(f"  {t['term'][:34]:<36} {t['sectorCount']} sectors · {t['direction']:<8}"
                  f" · newest {t['newestSector']} {t['daysSinceNewestSector']}d")
        return 0

    # Company view: the same data indexed by ticker instead of phrase. Which
    # narratives are attaching to a business, and when each one arrived.
    companies = []
    try:
        recent = days_back(RECENT_DAYS)[0]
        rows = list(conn.execute(
            "SELECT ticker, COUNT(DISTINCT article_id) n FROM mentions "
            "WHERE day>=? GROUP BY ticker HAVING n>=2 ORDER BY n DESC LIMIT 60",
            (days_back(WINDOW_DAYS)[0],)))
        meta = {c["ticker"]: c for c in
                json.load(open(os.path.join(HERE, "companies.json"),
                               encoding="utf-8")).get("companies", [])} if COMPANIES else {}
        term_ids = {t["term"]: t for t in terms}
        for ticker, n in rows:
            arts = [r[0] for r in conn.execute(
                "SELECT article_id FROM mentions WHERE ticker=? AND day>=?",
                (ticker, days_back(WINDOW_DAYS)[0]))]
            if not arts:
                continue
            qs = ",".join("?" * len(arts))
            titles = list(conn.execute(
                f"SELECT title, summary, sector, published, url, publisher FROM articles "
                f"WHERE id IN ({qs}) ORDER BY published DESC", arts))
            # which tracked phrases appear in this company's coverage
            attached = []
            for t in terms:
                hits = [x for x in titles
                        if t["term"] in (x[0] + " " + (x[1] or "")).lower()]
                if hits:
                    secs = t.get("sectors") or []
                    home = (max(secs, key=lambda s: s.get("articles", 0))["name"]
                            if secs else "")
                    if t.get("kind") == "place":
                        continue      # a place is not borrowed vocabulary
                    attached.append({"term": t["term"], "id": t["id"],
                                     "articles": len(hits),
                                     "home": home, "shown": t.get("display", True),
                                     "sectors": t["sectorCount"],
                                     "direction": t["direction"],
                                     "variants": list(t.get("variants", [])),
                                     "examples": [e for e in t.get("examples", [])[:3]]})
            attached.sort(key=lambda a: -a["articles"])
            recent_n = sum(1 for x in titles if (x[3] or "") >= recent)
            prior_start = days_back(2 * RECENT_DAYS)[0]
            prior_n = sum(1 for x in titles
                          if prior_start <= (x[3] or "") < recent)
            # Collapse variants: "energy storage" beside "energy storage system"
            # is one thing said twice. Longest first, then anything overlapping
            # most of it is already covered.
            attached.sort(key=lambda a: (-len(a["term"]), -a["articles"]))
            merged = []
            for a in attached:
                aw = set(a["term"].split())
                host = next((k for k in merged
                             if len(aw & set(k["term"].split()))
                                / min(len(aw), len(k["term"].split())) >= 0.6), None)
                if host is not None:
                    # say what was folded in, the same as the phrase list does
                    host.setdefault("variants", []).append(a["term"])
                    continue
                merged.append(a)
            merged.sort(key=lambda a: -a["articles"])
            attached = merged

            if not attached or recent_n < CO_MIN_ARTICLES:
                continue          # one article is a name match, not coverage
            own = meta.get(ticker, {}).get("sector", "")
            borrowed = [a for a in attached
                        if a["sectors"] >= CO_MIN_PHRASE_SECTORS
                        and a.get("shown")
                        and a["home"] and own and a["home"] != own]
            if not borrowed:
                continue          # only ever discussed in its own sector's words
            # lead with the language it borrowed, not the language it owns
            attached.sort(key=lambda a: (a not in borrowed, -a["articles"]))
            companies.append({
                "prior14d": prior_n,
                "direction": (None if max(recent_n, prior_n) < CO_DIR_MIN else
                              "new" if prior_n == 0 else
                              "rising" if recent_n > prior_n * 1.35 else
                              "falling" if recent_n < prior_n * 0.7 else "steady"),
                "ticker": ticker,
                "name": meta.get(ticker, {}).get("name", ticker),
                "sector": meta.get(ticker, {}).get("sector", ""),
                "borrowed": [a["term"] for a in borrowed],
                "articles": len(titles), "articles14d": recent_n,
                "terms": attached[:8],
                "examples": [{"publisher": p,
                              "title": mark_in(mark_in(ti, meta.get(ticker, {}).get("name", ticker)),
                                              borrowed[0]["term"]),
                              "url": u, "sector": s,
                              "quote": (snippet(ti, sm, borrowed[0]["term"])
                                        or snippet(ti, sm,
                                                   meta.get(ticker, {})
                                                   .get("name", ticker)))}
                             for ti, sm, s, _d, u, p in titles[:4]],
            })
        companies.sort(key=lambda c: (-len(c["terms"]), -c["articles14d"]))
    except sqlite3.OperationalError:
        companies = []

    pub_count = len({s.get("publisher") for sec in cfg.get("sectors", [])
                     for s in sec.get("sources", []) if s.get("publisher")})

    # How much of the corpus each country supplies. A phrase being 80% Indian
    # means nothing until you know India is 8% of everything.
    win_days = days_back(WINDOW_DAYS)
    win_start = win_days[0]
    coverage = []
    try:
        coverage = [[cc, n] for cc, n in conn.execute(
            "SELECT COALESCE(a.country,'US') c, COUNT(*) FROM articles a "
            "WHERE a.published>=? GROUP BY c ORDER BY 2 DESC", (win_start,))]
    except sqlite3.OperationalError:
        pass

    # Institutions: regulators, agencies and standards bodies. Cross-sector by
    # nature, which fits the premise better than companies do.
    orgs_list = []
    try:
        rows = list(conn.execute(
            "SELECT name, MAX(acronym), COUNT(DISTINCT sector) s, SUM(n) n, MIN(day) "
            "FROM orgs WHERE day>=? GROUP BY name "
            "HAVING SUM(n) >= ? AND COUNT(DISTINCT sector) > 1 "
            "ORDER BY s DESC, n DESC LIMIT 30",
            (win_start, ORG_MIN_ARTICLES)))
        for nm, ac, nsec, n, first in rows:
            sec_rows = list(conn.execute(
                "SELECT sector, SUM(n) FROM orgs WHERE name=? AND day>=? "
                "GROUP BY sector ORDER BY 2 DESC", (nm, win_start)))
            secs = [r[0] for r in sec_rows]
            total_mentions = sum(r[1] for r in sec_rows) or 1
            home_sector = secs[0] if secs else ""
            away = [(s, v) for s, v in sec_rows[1:] if v >= ORG_MIN_AWAY]
            away_share = sum(v for _, v in away) / total_mentions
            recent = conn.execute(
                "SELECT SUM(n) FROM orgs WHERE name=? AND day>=?",
                (nm, win_days[-RECENT_DAYS])).fetchone()[0] or 0
            prior = conn.execute(
                "SELECT SUM(n) FROM orgs WHERE name=? AND day<? AND day>=?",
                (nm, win_days[-RECENT_DAYS], win_start)).fetchone()[0] or 0
            # candidates by substring, then confirmed on a word boundary:
            # SQLite LIKE cannot express one, and Python can
            word_re = re.compile(
                r"\b" + re.escape(ac) + r"\b") if ac else None
            name_re = re.compile(r"\b" + re.escape(nm.lower()) + r"\b")
            body_sql = "(title || '. ' || COALESCE(summary,''))"
            clauses, params = [f"LOWER({body_sql}) LIKE ?"], [f"%{nm.lower()}%"]
            if ac:
                clauses += [f"{body_sql} GLOB ?", f"{body_sql} GLOB ?"]
                params += [f"*[^A-Za-z]{ac}[^A-Za-z]*", f"{ac}[^A-Za-z]*"]
            arts = []
            for ti, u, p, s, sm in conn.execute(
                    "SELECT title, url, publisher, sector, summary FROM articles "
                    f"WHERE published>=? AND ({' OR '.join(clauses)}) "
                    "ORDER BY published DESC LIMIT 60",
                    tuple([win_days[-RECENT_DAYS]] + params)):
                body = f"{ti}. {sm or ''}"
                if not (name_re.search(body.lower())
                        or (word_re and word_re.search(body))):
                    continue
                arts.append({"title": mark_in(ti, nm), "url": u, "publisher": p,
                             "sector": s,
                             "quote": snippet(ti, sm, nm) or snippet(ti, sm, ac or nm)})
                if len(arts) == 3:
                    break
            orgs_list.append({
                "articles_list": arts,
                "name": nm, "acronym": ac, "sectors": secs, "sectorCount": nsec,
                "home": home_sector,
                "away": [s for s, _ in away],
                "awayShare": round(away_share, 3),
                "articles": n, "recent": recent, "prior": prior, "firstSeen": first,
                "direction": ("new" if prior == 0 else "rising" if recent > prior * 1.35
                              else "falling" if recent < prior * 0.7 else "steady")})
    except sqlite3.OperationalError:
        pass

    # Filing history, if edgar.py has run. Ten years of the same question the
    # news view asks over ten weeks — which industries use this language — with
    # the sector label assigned by the SEC rather than by which paper ran it.
    filings = {}
    try:
        base = {}
        for q, sec, n in conn.execute(
                "SELECT quarter, sector, SUM(n) FROM filings "
                "WHERE phrase='__all_filings__' GROUP BY quarter, sector"):
            base[(q, sec)] = n
        for term in terms:
            rows = []
            for q, sec, n in conn.execute(
                    "SELECT quarter, sector, SUM(n) FROM filings WHERE phrase=? "
                    "GROUP BY quarter, sector ORDER BY quarter", (term["term"],)):
                b = base.get((q, sec))
                if b:
                    rows.append({"q": q, "sector": sec,
                                 "share": round(n / b, 5)})
            if rows:
                filings[term["term"]] = rows
    except sqlite3.OperationalError:
        pass

    # Sector prices, if prices.py has run. Concurrent context only — the
    # dashboard must present these as "what happened alongside", never as an
    # outcome the language predicted.
    prices = {}
    try:
        window_start = days_back(WINDOW_DAYS)[0]
        for sector, day, close in conn.execute(
                "SELECT sector, day, close FROM prices WHERE day>=? ORDER BY day",
                (window_start,)):
            prices.setdefault(sector, []).append({"day": day, "close": round(close, 2)})
    except sqlite3.OperationalError:
        pass                                  # prices.py not run yet; harmless

    remap_mentions(conn)
    prune(conn)
    size_mb = os.path.getsize(DB) / 1e6 if os.path.exists(DB) else 0
    try:
        rows = list(conn.execute(
            "SELECT COALESCE(country,'US') c, COUNT(*) n FROM articles "
            "GROUP BY c ORDER BY n DESC"))
        if len(rows) > 1:
            print("  coverage · " + " · ".join(f"{c} {n:,}" for c, n in rows[:8]))
    except sqlite3.OperationalError:
        pass

    try:
        n_org = conn.execute("SELECT COUNT(DISTINCT name) FROM orgs").fetchone()[0]
        n_cross = conn.execute(
            "SELECT COUNT(*) FROM (SELECT name FROM orgs GROUP BY name "
            "HAVING COUNT(DISTINCT sector) > 1)").fetchone()[0]
        n_sum = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE summary IS NOT NULL "
            "AND LENGTH(summary) > 40").fetchone()[0]
        n_art = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print(f"  institutions · {n_org} named, {n_cross} in 2+ sectors "
              f"· from {n_sum:,} of {n_art:,} articles with a usable summary")
    except sqlite3.OperationalError:
        pass

    print(f"  database {size_mb:.1f} MB"
          + ("   note: large. A GitHub release asset allows 2 GB, so this is "
             "not near a limit" if size_mb > 500 else ""))

    json.dump({"generated": datetime.now(timezone.utc).isoformat(),
               "placeWords": sorted(PLACE_ANCHORS),
               "prices": prices,
               "coverage": coverage,
               "publishers": pub_count,
               "countryCount": len(coverage),
               "institutions": orgs_list,
               "companies": companies,
               "filings": filings,
               "daysCollected": days_held, "articlesStored": stored,
               "articlesLive": live, "windowDays": WINDOW_DAYS,
               "terms": terms}, open(OUT, "w"), indent=1)
    print(f"  Wrote {OUT}")
    try:
        n_pages = write_phrase_pages(terms, os.path.dirname(OUT) or ".")
        print(f"  Wrote {n_pages} phrase pages, sitemap.xml and robots.txt")
    except Exception as e:
        print(f"  phrase pages skipped: {e}")

    crossed, moving = write_digest(terms, os.path.join(HERE, "DIGEST.md"),
                                   stored, days_held)
    print(f"  Wrote DIGEST.md — {crossed} crossed a new sector, {moving} moving")
    return 0


if __name__ == "__main__":
    sys.exit(main())
