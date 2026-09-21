"""Automated data refresh: results backfill and upcoming cards.

## Why not fetch_ufcstats_upcoming()

It is kept below, but it is the fallback, not the answer, and the reason is
specifically about automation. The only scheduler you can run without a
terminal is GitHub Actions, and Actions runs on datacenter IPs — which is
exactly what triggered UFCStats' "Checking your browser" interstitial when it
was tested. So a cron job calling UFCStats would fail in precisely the
environment it needs to run in. Use it only from a residential connection.

## What to use instead

Two sources, chosen because neither blocks datacenter traffic:

  RESULTS      raw.githubusercontent.com — the Greco1899 CSVs are plain files
               in a public repo. Re-downloading them is the entire backfill.
               No scraping, no parsing, nothing to break.

  UPCOMING     the MediaWiki API on en.wikipedia.org. It is a documented JSON
               interface that explicitly welcomes automated clients, has no
               bot-check, and is where UFC card data is reliably maintained.

## Failure policy

Every fetch here validates before it writes, and raises instead of returning
something plausible-but-wrong. A stale card shown confidently is worse than a
visible error: the whole point of automating this is that nobody is watching.
`refresh_upcoming()` will not overwrite a good file with a worse one.
"""
import json
import re
import unicodedata
from pathlib import Path

RAW_BASE = ("https://raw.githubusercontent.com/Greco1899/scrape_ufc_stats/"
            "main/")
CORPUS_FILES = ["ufc_event_details.csv", "ufc_fight_results.csv",
                "ufc_fight_stats.csv", "ufc_fighter_tott.csv",
                "ufc_fighter_details.csv"]
WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = "Cornerman/1.0 (UFC analysis project; contact via repo issues)"

# A card below this many bouts is almost certainly a parse failure, not a small
# event — the UFC has not run a card under 8 bouts in years.
MIN_PLAUSIBLE_BOUTS = 6


# ----------------------------------------------------------------- results
def refresh_corpus(data_dir="data", files=CORPUS_FILES, verbose=True):
    """Re-download the UFCStats CSV dump. This is the whole backfill: no new
    'bridge' file is needed, because the gap is staleness, not a missing
    format. Returns the list of files written."""
    import requests
    out = []
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    for name in files:
        r = requests.get(RAW_BASE + name, timeout=120,
                         headers={"User-Agent": UA})
        r.raise_for_status()
        body = r.content
        # a truncated or HTML error page would silently poison the corpus
        if len(body) < 1000 or body.lstrip()[:1] == b"<":
            raise RuntimeError(f"{name}: response does not look like a CSV "
                               f"({len(body)} bytes)")
        dest = Path(data_dir) / name
        dest.write_bytes(body)
        out.append(str(dest))
        if verbose:
            print(f"  {name}: {len(body)//1024} KB")
    return out


# ---------------------------------------------------------------- upcoming
def _wiki(params):
    import requests
    p = {"format": "json", "formatversion": "2", **params}
    r = requests.get(WIKI_API, params=p, timeout=60,
                     headers={"User-Agent": UA})
    r.raise_for_status()
    return r.json()


def _clean_name(s):
    s = re.sub(r"\[\d+\]", "", s)            # footnote markers
    s = re.sub(r"\((c|ic)\)", "", s, flags=re.I)   # champion / interim marks
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip(" .\u00a0")


def parse_card_html(html):
    """Pull (fighter_a, fighter_b) out of a Wikipedia UFC event page.

    Wikipedia lays each bout out as a table row where one cell is exactly
    'vs.' — that single anchor is far more stable across page revisions than
    column positions or CSS classes, which change constantly.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    bouts, seen = [], set()
    # Wikipedia puts the segment in a header row spanning the table
    # ("Main card (Paramount+)", "Preliminary card", "Early preliminary card").
    # Tracking it as we walk gives each bout its place on the card, which is
    # the single most useful piece of context a reader wants and which the
    # bout list alone cannot convey.
    segment = "Main card"

    def _seg(text):
        t = text.lower()
        if "early prelim" in t:
            return "Early prelims"
        if "prelim" in t:
            return "Prelims"
        if "main card" in t:
            return "Main card"
        return None

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(cells) <= 2:                       # a spanning header row
            hit = _seg(" ".join(texts))
            if hit:
                segment = hit
                continue
        for i, t in enumerate(texts):
            if t.strip().lower().rstrip(".") == "vs" and 0 < i < len(texts) - 1:
                a, b = _clean_name(texts[i - 1]), _clean_name(texts[i + 1])
                if a and b and len(a) > 2 and len(b) > 2:
                    key = frozenset((a.lower(), b.lower()))
                    if key not in seen:
                        seen.add(key)
                        bouts.append((a, b, segment))
                break
    return bouts


def parse_card_prose(text):
    """Fallback: the article body states every booked bout in sentences like
    'A bantamweight bout between X and Y is scheduled'.

    Known weakness, and the reason this is a fallback rather than the primary:
    the prose also discusses bouts that were CANCELLED or POSTPONED, so it can
    inject fights that will not happen. The table only lists what is actually
    booked. refresh_upcoming() validates the result either way.
    """
    # Two traps here, both found by testing against real article sentences:
    #   - an open-ended prefix run swallows the fighter's first name, turning
    #     "...Championship challenger Marlon Vera" into "Vera". So the prefix
    #     must be anchored on champion/challenger/medalist.
    #   - a global IGNORECASE flag makes [A-Z] match lowercase, so the name
    #     group runs on into "... is scheduled". Case-insensitivity is scoped
    #     to the prefix words only.
    NAME = r"[A-Z][\w.'\u00c0-\u024f-]+(?: [A-Z][\w.'\u00c0-\u024f-]+){0,3}"
    PRE = (r"(?:(?i:former|current|reigning|interim|two-time|three-time)\s+)*"
           r"(?:(?:UFC|Bellator|PFL|LFA|ONE|WEC|Strikeforce)\s+[\w' ]+?\s+"
           r"(?i:champion|challenger|titleholder|medalist)\s+)?")
    VERB = r"(?:bout|rematch|fight|contest|matchup)\s+between"
    pat = re.compile(rf"{VERB}\s+{PRE}({NAME})\s+and\s+{PRE}({NAME})")
    out, seen = [], set()
    for a, b in pat.findall(text):
        a, b = _clean_name(a), _clean_name(b)
        key = frozenset((a.lower(), b.lower()))
        if key not in seen:
            seen.add(key)
            out.append((a, b, "Main card"))   # prose gives no segment
    return out


DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d")


def _parse_date(text):
    """Wikipedia is not consistent about date format across tables.

    The first version accepted only "October 10, 2026" and silently skipped
    everything else, which took down card discovery entirely: every row in the
    Scheduled events table failed to parse and next_event() concluded there
    were no upcoming fights. Abbreviated months and ISO dates are just as
    common.
    """
    from datetime import datetime
    pats = [r"(\d{4}-\d{2}-\d{2})",
            r"([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})",
            r"(\d{1,2} [A-Z][a-z]{2,8}\.? \d{4})"]
    for pat in pats:
        for m in re.finditer(pat, text):
            raw = m.group(1).replace(".", "")
            for fmt in DATE_FORMATS:
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
    return None


def next_event(verbose=False):
    """Soonest scheduled UFC event, from Wikipedia's "List of UFC events".

    Returns (page_title, iso_date). Scans the Scheduled events table first and
    falls back to every wikitable on the page, because a layout change should
    degrade rather than blank the whole pipeline.
    """
    from bs4 import BeautifulSoup
    from datetime import date
    data = _wiki({"action": "parse", "page": "List of UFC events", "prop": "text"})
    soup = BeautifulSoup(data["parse"]["text"], "html.parser")

    tables, seen_rows = [], 0
    for el in soup.find_all(id=True):
        if "scheduled" in str(el.get("id", "")).lower():
            t = el.find_next("table")
            if t is not None:
                tables.append(t)
    if not tables:
        tables = soup.find_all("table", class_="wikitable")

    today = date.today()
    best = None
    for tbl in tables:
        for row in tbl.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            seen_rows += 1
            dt = _parse_date(row.get_text(" ", strip=True))
            if dt is None or dt < today:
                continue
            link = cells[0].find("a")
            title = (link.get("title") or link.get_text(" ", strip=True)) if link \
                else cells[0].get_text(" ", strip=True)
            title = _clean_name(title)
            if not title or len(title) < 3:
                continue
            if best is None or dt.isoformat() < best[1]:
                best = (title, dt.isoformat())
    if verbose:
        print(f"scanned {len(tables)} table(s), {seen_rows} rows")
    if best is None:
        raise RuntimeError(
            f"no upcoming UFC event found on 'List of UFC events' "
            f"(scanned {len(tables)} tables, {seen_rows} rows, none with a "
            f"parseable future date)")
    return best


def event_meta(title):
    """Date and venue from the page's infobox."""
    data = _wiki({"action": "parse", "page": title, "prop": "text"})
    html = data["parse"]["text"]
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    meta = {"event": title}
    for row in soup.select("table.infobox tr"):
        th, td = row.find("th"), row.find("td")
        if not th or not td:
            continue
        k = th.get_text(" ", strip=True).lower()
        v = _clean_name(td.get_text(" ", strip=True))
        if k.startswith("date"):
            d2 = _parse_date(v)
            if d2:
                meta["date"] = d2.isoformat()
        elif k.startswith("venue"):
            meta["venue"] = v
        elif k.startswith("city"):
            meta["city"] = v
    return meta


def fetch_upcoming(title=None):
    """(meta, bouts) for the next event, table first and prose as a backstop."""
    if title is None:
        title, _ = next_event()
    meta = event_meta(title)
    data = _wiki({"action": "parse", "page": title, "prop": "text"})
    html = data["parse"]["text"]
    bouts = parse_card_html(html)
    if len(bouts) < MIN_PLAUSIBLE_BOUTS:
        from bs4 import BeautifulSoup
        prose = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        alt = parse_card_prose(prose)
        if len(alt) > len(bouts):
            bouts = alt
    if len(bouts) < MIN_PLAUSIBLE_BOUTS:
        raise RuntimeError(
            f"only parsed {len(bouts)} bouts from '{title}' — refusing to "
            f"write a card this short; the page format has probably changed")
    return meta, bouts


def render_upcoming(meta, bouts):
    """Segment markers are written as `## Main card` lines so the file stays
    hand-editable and the parser stays trivial."""
    head = (f"# {meta.get('event','UFC')} | {meta.get('date','')} | "
            f"{meta.get('venue','')}{', ' + meta['city'] if meta.get('city') else ''}")
    lines, cur = [head], None
    order = {"Main card": 0, "Prelims": 1, "Early prelims": 2}
    for a, b, seg in sorted(bouts, key=lambda x: order.get(x[2], 9)):
        if seg != cur:
            lines.append(f"## {seg}")
            cur = seg
        # five rounds for the main event, which is the first bout of the card
        lines.append(f"{a} vs. {b}" + (" | 5" if len(lines) == 2 else ""))
    return "\n".join(lines) + "\n"


def refresh_upcoming(path="data/upcoming.txt", title=None, verbose=True,
                     strict=False):
    """Write data/upcoming.txt. On any failure the existing file is kept and a
    warning is printed rather than raising, unless strict=True: a card that
    cannot be parsed should not stop predictions from regenerating off
    yesterday's card. The caller decides how loud to be."""
    try:
        return _refresh_upcoming(path, title, verbose)
    except Exception as e:
        if strict:
            raise
        print(f"WARNING: could not refresh the card ({e}). "
              f"Keeping the existing {path}.")
        return None, None


def _refresh_upcoming(path="data/upcoming.txt", title=None, verbose=True):
    """Write data/upcoming.txt, but never replace a longer card with a shorter
    one — that pattern is what a half-broken parser looks like."""
    meta, bouts = fetch_upcoming(title)
    new = render_upcoming(meta, bouts)
    p = Path(path)
    if p.exists():
        old = p.read_text(encoding="utf-8")
        old_head = old.splitlines()[0] if old.splitlines() else ""
        same_event = meta.get("event", "") in old_head
        old_n = sum(1 for ln in old.splitlines() if " vs. " in ln)
        if same_event and old_n > len(bouts):
            raise RuntimeError(
                f"refusing to shrink the card for {meta.get('event')}: "
                f"have {old_n} bouts, parsed {len(bouts)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    if verbose:
        print(f"{meta.get('event')} ({meta.get('date')}): {len(bouts)} bouts -> {path}")
    return meta, bouts


def fetch_ufcstats_upcoming(url="http://ufcstats.com/statistics/events/upcoming"):
    """FALLBACK ONLY, and unverified. UFCStats served a JavaScript bot-check
    when tested from a datacenter IP. It may work from a home connection; it
    will probably not work from CI. Prefer fetch_upcoming()."""
    import requests
    from bs4 import BeautifulSoup
    html = requests.get(url, timeout=30, headers={"User-Agent": UA}).text
    if "Checking your browser" in html or "requires JavaScript" in html:
        raise RuntimeError("UFCStats served a bot-check page — use fetch_upcoming()")
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("a.b-link")
    if link is None:
        raise RuntimeError("no upcoming event link found")
    page = requests.get(link["href"], timeout=30,
                        headers={"User-Agent": UA}).text
    return parse_card_html(page)


if __name__ == "__main__":
    import sys
    if "--corpus" in sys.argv:
        print("refreshing corpus from GitHub:")
        refresh_corpus()
    if "--no-upcoming" not in sys.argv:
        refresh_upcoming()
