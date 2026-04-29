"""
HOWL STREET — post drafter (editorial overhaul, replaces queue.html /
feed.xml / cards.py pipeline).

Editorial rules — every draft must obey these:
  1. Valuable on its own. The tweet stands alone, no click required.
  2. Lead with consequence / implication, not the headline.
  3. Real numbers. Compress what happened in the first 5 words.
  4. Voice: terse, confident, no hype, no emojis, no hashtags, no
     exclamation points. Bloomberg-via-FinTwit-with-edge.
  5. No screenshots of article headlines. Charts and primary-source
     clippings only (handled by signals.py / insider_trades.py).
  6. End with link to howlstreet.github.io OR no link at all. Source
     cited in plain text ("via Reuters", "per Fed release") — never
     linked, since the goal is to make Howl Street the destination.

Six post formats:
  A) MARKET MOVE      — fed by signals.py big-move detections
  B) POLICY READ      — Fed/Treasury/ECB/BoJ releases
  C) CORRUPTION WATCH — insider trades (Form 4) + RSS corruption items
  D) GLOBAL DESK      — non-US story FinTwit ignores
  E) DATA DROP        — economic releases (CPI, NFP, GDP, etc.)
  F) THE TAKE         — manual only (the_take.md), v1

No auto-posting. drafter.py emits:
  - drafts.json : pending tweet drafts for human review
  - review.html : local-only UI to approve / reject / edit / mark posted
  - posted.json : reviewed by user; pasted back to repo to dedupe
                  future runs

Each format function is a PURE function: input dict in, draft dict out.
No global state mutation. All thresholds + caps are constants at the top
of this file.
"""

import hashlib
import html as html_lib
import json
import re
import sys
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent
DRAFTS_PATH = REPO_ROOT / "drafts.json"
POSTED_PATH = REPO_ROOT / "posted.json"
REVIEW_PATH = REPO_ROOT / "review.html"
THE_TAKE_PATH = REPO_ROOT / "the_take.md"
NY = ZoneInfo("America/New_York")

# ────────────────────────────────────────────────────────────────────
# CONSTANTS — tune here. No magic numbers buried in logic below.
# ────────────────────────────────────────────────────────────────────

# Wire scoring threshold. RSS items below this don't get drafted at all.
MIN_SCORE_FOR_DRAFT = 5.0

# Cap drafts emitted per format per cron run. Prevents flooding the
# review queue when a busy news day hits.
MAX_DRAFTS_PER_FORMAT_PER_RUN = 5

# Move size that qualifies for a MARKET MOVE draft (signals.py already
# enforces 5% as its big-move threshold; keep this in sync if tuning).
MOVE_PCT_THRESHOLD_FOR_MARKET_MOVE = 5.0

# Insider trade dollar threshold. Below this we skip — ten-thousand-dollar
# director purchases aren't newsworthy.
INSIDER_DOLLAR_THRESHOLD = 250_000

# How long pending drafts stay in drafts.json before being expired
# (independent of the user's review action).
DRAFT_TTL_HOURS = 48

# How long the posted dedupe record sticks around. Beyond this the same
# story could resurface — usually fine, news has a half-life.
POSTED_TTL_DAYS = 30

# Sources whose RSS items qualify for POLICY READ drafts.
POLICY_SOURCES = {
    "FED", "TREASURY", "BIS", "IMF",
}
POLICY_KEYWORDS = re.compile(
    r"\b(?:Fed|FOMC|Federal\s+Reserve|ECB|BoJ|Bank\s+of\s+Japan|"
    r"Bank\s+of\s+England|BoE|PBOC|People'?s\s+Bank\s+of\s+China|"
    r"Treasury|Powell|Yellen|Lagarde|Ueda|Bailey)\b"
    r".*\b(?:rate|policy|decision|cut|hike|hold|pause|"
    r"statement|minutes|action|tightening|easing)\b",
    re.IGNORECASE,
)

# Sources / patterns that qualify for GLOBAL DESK. Non-US stories with
# market relevance.
GLOBAL_DESK_SOURCE_PATTERNS = re.compile(
    r"\b(?:NIKKEI|SCMP|CAIXIN|REUTERS|BLOOMBERG|FT|GUARDIAN|"
    r"TELEGRAPH|EURONEWS|AL\s+JAZEERA)\b",
    re.IGNORECASE,
)
GLOBAL_DESK_REGION_HINTS = re.compile(
    r"\b(?:China|Beijing|Shanghai|Tencent|Alibaba|Hong\s+Kong|"
    r"Japan|Tokyo|Yen|Nikkei|"
    r"Europe|Eurozone|ECB|Brussels|Frankfurt|"
    r"India|Mumbai|Sensex|Nifty|"
    r"Brazil|Mexico|Argentina|"
    r"Saudi|UAE|Iran|OPEC|Hormuz|"
    r"Russia|Ruble|Moscow)\b",
    re.IGNORECASE,
)

# Economic-release detection for DATA DROP.
DATA_DROP_KEYWORDS = re.compile(
    r"\b(?:CPI|PPI|PCE|GDP|nonfarm\s+payrolls?|NFP|unemployment\s+rate|"
    r"jobless\s+claims|retail\s+sales|industrial\s+production|"
    r"durable\s+goods|housing\s+starts|consumer\s+sentiment|"
    r"ISM\s+(?:manufacturing|services)|trade\s+balance|"
    r"factory\s+orders|building\s+permits|home\s+sales)\b",
    re.IGNORECASE,
)

# ────────────────────────────────────────────────────────────────────
# BANNED PHRASES — stripped from any draft output. The voice rule:
# "periods are weapons. no flab." Hedge phrases, vague cliché, and
# pundit-speak get cut.
# ────────────────────────────────────────────────────────────────────

_BANNED_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r",?\s*it\s+remains\s+to\s+be\s+seen[^.]*\.?",
        r",?\s*investors\s+are\s+watching\s+closely[^.]*\.?",
        r",?\s*this\s+could\s+potentially\s+impact[^.]*\.?",
        r",?\s*amid\s+(?:concerns|worries|fears)\s+over[^.]*\.?",
        r",?\s*amid\s+mounting[^.]*\.?",
        r",?\s*in\s+the\s+wake\s+of[^.]*\.?",
        r",?\s*experts\s+say[^.]*\.?",
        r",?\s*analysts\s+say[^.]*\.?",
        r",?\s*sources\s+familiar\s+with\s+the\s+matter[^.]*\.?",
        r",?\s*according\s+to\s+sources[^.]*\.?",
        r",?\s*it'?s\s+worth\s+noting[^.]*\.?",
        r",?\s*as\s+(?:the\s+)?market\s+(?:weighs|digests|grapples)[^.]*\.?",
        r",?\s*time\s+will\s+tell[^.]*\.?",
        r",?\s*at\s+the\s+end\s+of\s+the\s+day[^.]*\.?",
        r",?\s*only\s+time\s+will\s+tell[^.]*\.?",
    ]
]


def _strip_banned_phrases(text):
    """Remove banned hedge / pundit phrases. Idempotent. Preserves
    paragraph breaks (\\n\\n) — only collapses horizontal whitespace."""
    if not text:
        return text
    out = text
    for pat in _BANNED_PATTERNS:
        out = pat.sub("", out)
    # Collapse only horizontal whitespace — keep newlines intact.
    out = re.sub(r"[ \t]{2,}", " ", out)
    # Strip space before punctuation
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    out = re.sub(r",[ \t]*([.;])", r"\1", out)
    # Trim leading orphan punctuation on each line
    out = re.sub(r"^[ \t]*[,.;:][ \t]*", "", out, flags=re.MULTILINE)
    return out.strip()


# ────────────────────────────────────────────────────────────────────
# TEXT HELPERS (minimal, self-contained)
# ────────────────────────────────────────────────────────────────────

def _trim_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _first_sentence(text, max_chars=240):
    """Return the first complete sentence, capped at max_chars."""
    if not text:
        return ""
    text = _trim_ws(text)
    m = re.match(r"^(.{20,}?[.!?])(?:\s|$)", text)
    if m:
        s = m.group(1)
        return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + "."
    return text[:max_chars].rsplit(" ", 1)[0] + "." if len(text) > max_chars else text


_BODY_BOILERPLATE = (
    "follow us", "sign up for", "subscribe to", "all rights reserved",
    "cookie policy", "privacy policy", "newsletter", "click here",
    "read more at", "this article was", "advertisement", "share this",
    "originally appeared", "view comments", "the post ", "appeared first",
    "©", "browser to view", "javascript", "enable javascript",
    # .gov / official-website chrome that appears on every federal page
    "official websites use .gov", "a .gov website belongs",
    ".gov website belongs to", "an official government organization",
    "secure .gov websites use https", "secure.gov websites use https",
    "lock locked padlock", "lock ( locked padlock",
    "share sensitive information only on official",
    "an official website of the united states",
    # Generic CMS / paywall chrome
    "you have read", "to continue reading", "subscribe now",
    "create a free account", "log in to read", "sign in to continue",
    # Fed / Treasury press-release marketing boilerplate
    "the central bank of the united states, provides",
    "provides the nation with a safe, flexible, and stable",
    "for release at", "for immediate release",
)
_BODY_FILLER = (
    "is addressing", "press conference", "press briefing",
    "told reporters", "spokesperson", "sources said",
    "sources familiar", "according to people familiar",
)
# Sentences with these signals get prioritized — concrete facts beat
# scene-setting filler.
_BODY_FACT_SIGNAL = re.compile(
    r"\$\s?\d|€\s?\d|£\s?\d|¥\s?\d"
    r"|\b\d+(?:[\.,]\d+)?\s*(?:percent|%|bps|basis\s+points|pct)\b"
    r"|\b\d+(?:[\.,]\d+)?\s*(?:billion|trillion|million|bn|tn|mn)\b"
    r"|\b(?:rose|fell|jumped|dropped|gained|lost|surged|plunged|climbed|"
    r"declined|slipped|advanced|tumbled|rallied|cut|raised|hiked|held)\b"
    r"|\b(?:Fed|FOMC|ECB|BoJ|PBOC|BOE|Treasury|SEC|DOJ|FTC|CFTC)\b"
    r"|\b(?:CPI|PPI|GDP|EPS|revenue|guidance|earnings|yield|"
    r"unemployment|jobless\s+claims|payrolls)\b",
    re.IGNORECASE,
)


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)


def _fetch_article_body(url, timeout=15):
    """Pull the article HTML and return a list of substantive paragraph
    strings — boilerplate/filler stripped, prioritized by fact-signal
    density. Returns [] on any failure (cookie walls, paywalls, 403).

    Used by every format function that has a source_url, to fill the
    draft body with real article content. Retries once on failure since
    a single transient timeout was leaving good articles with no body."""
    if not url or not url.startswith("http"):
        return []
    page = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                page = resp.read(800_000).decode(charset, errors="replace")
            break
        except Exception:
            if attempt == 2:
                return []
            continue
    if not page:
        return []
    raw_paras = re.findall(r"<p\b[^>]*>(.*?)</p>", page, re.DOTALL | re.IGNORECASE)
    out = []
    for raw in raw_paras:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_lib.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 80:
            continue
        low = text.lower()
        if any(b in low for b in _BODY_BOILERPLATE):
            continue
        if any(f in low for f in _BODY_FILLER):
            continue
        out.append(text)
        if len(out) >= 8:
            break
    return out


def _split_sentences(paragraph):
    """Split a paragraph into sentences. Handles common abbreviations
    (Mr., Mrs., Inc., Co., U.S., etc.) so we don't cut at every period."""
    # Protect common abbreviations
    p = re.sub(r"\b(Mr|Mrs|Ms|Dr|Inc|Co|Corp|Ltd|Jr|Sr|St|U\.S|U\.K|E\.U)\.\s",
               r"\1<DOT> ", paragraph)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])", p)
    return [s.replace("<DOT>", ".").strip() for s in parts if s.strip()]


def _pick_body_sentences(body_paras, title="", max_sentences=6, min_chars=400):
    """From article body paragraphs, pick up to max_sentences that are:
      - complete (end in . ! or ?)
      - not a verbatim restatement of the title
      - prioritized: ones with fact signals first
    Returns a list of complete sentence strings."""
    sentences = []
    for p in body_paras:
        sentences.extend(_split_sentences(p))
    # Drop sentences that don't end in punctuation (incomplete)
    sentences = [s for s in sentences if s.endswith((".", "!", "?"))]
    # Drop sentences that just restate the title
    title_norm = re.sub(r"\W+", "", title.lower())[:40]
    if title_norm:
        sentences = [s for s in sentences
                     if not re.sub(r"\W+", "", s.lower())[:40].startswith(title_norm[:30])]
    # Sort: fact-signal sentences first, preserving original order within group
    fact_idx = []
    other_idx = []
    for i, s in enumerate(sentences):
        if _BODY_FACT_SIGNAL.search(s):
            fact_idx.append(i)
        else:
            other_idx.append(i)
    ordered = [sentences[i] for i in fact_idx] + [sentences[i] for i in other_idx]
    # Take up to max_sentences, respecting a min total char count target.
    picked = []
    total = 0
    for s in ordered:
        picked.append(s)
        total += len(s)
        if len(picked) >= max_sentences and total >= min_chars:
            break
    return picked


_OG_IMAGE_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']', re.IGNORECASE),
]


def fetch_og_image(url, timeout=8):
    """Fetch the article URL and return the og:image / twitter:image URL.
    This is the photo Reuters / Bloomberg / Fortune / etc. picked for
    social cards — usually a journalism-quality photo, not a headline
    screenshot. Returns the absolute image URL or None on failure."""
    if not url or not url.startswith("http"):
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0; +https://howlstreet.github.io)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read(400_000).decode(charset, errors="replace")
    except Exception as e:
        # Quiet fail — many sources block bots / require cookies. We accept
        # missing og:image as a normal case.
        return None
    for pat in _OG_IMAGE_PATTERNS:
        m = pat.search(page)
        if m:
            img_url = html_lib.unescape(m.group(1)).strip()
            # Resolve protocol-relative URLs
            if img_url.startswith("//"):
                img_url = "https:" + img_url
            if img_url.startswith("http"):
                return img_url
    return None


def _content_hash(*parts):
    """Stable hash from arbitrary string parts. Used to dedupe drafts
    against posted.json across sources (a story republished by a second
    outlet shares the same normalized content hash)."""
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            continue
        norm = re.sub(r"\W+", "", str(p).lower())[:200]
        h.update(norm.encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:16]


# ────────────────────────────────────────────────────────────────────
# STATE I/O
# ────────────────────────────────────────────────────────────────────

def _load_posted():
    """Load posted.json. Each entry: {content_hash, source_url, posted_at}.
    Filtered to within POSTED_TTL_DAYS so the file doesn't grow forever."""
    if not POSTED_PATH.exists():
        return []
    try:
        data = json.loads(POSTED_PATH.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    cutoff = datetime.utcnow() - timedelta(days=POSTED_TTL_DAYS)
    out = []
    for entry in data:
        try:
            ts = datetime.fromisoformat(entry.get("posted_at", ""))
            if ts > cutoff:
                out.append(entry)
        except (TypeError, ValueError):
            continue
    return out


def _is_already_posted(content_hash, source_url, posted):
    """Match either the normalized content_hash OR the literal source URL.
    The hash catches the same story republished by a different outlet."""
    for entry in posted:
        if entry.get("content_hash") == content_hash:
            return True
        if source_url and entry.get("source_url") == source_url:
            return True
    return False


def _save_drafts(drafts):
    try:
        DRAFTS_PATH.write_text(json.dumps(drafts, indent=2))
    except Exception as e:
        print(f"  ! drafts save failed: {e}", file=sys.stderr)


# ────────────────────────────────────────────────────────────────────
# FORMAT FUNCTIONS — each takes one input, returns a draft dict or None.
# Pure functions: no global state mutation, no I/O.
# ────────────────────────────────────────────────────────────────────

_RSS_OPENERS = [
    # Label-style — leans into the bit
    "FOR THE WOLF PACK NERDS:",
    "PACK BRIEFING:",
    "FIELD REPORT:",
    "PACK MEMO:",
    "HOT OFF THE WIRE:",
    "FOR THE READERS:",
    "FILED UNDER 'PAY ATTENTION':",
    "TRADING DESK READ:",
    "WOLVES ONLY:",
    "TONIGHT'S PACK BRIEFING:",
    # Conversational with colon
    "Hey wolves, read this:",
    "Worth your two minutes:",
    "The boring stuff that actually matters:",
    "Quietly, this matters:",
    "Listen up, pack:",
    "Real talk for the pack:",
    "From the trenches:",
    "The pack should know this:",
    "Skip CNBC, read this:",
    "Two minutes that beat your scroll:",
    "Smart take incoming:",
    "Pour a coffee and read this:",
    "Long read worth your time:",
    "Save this one:",
    "Bookmark this:",
    "Heads-up wolves:",
    "If you read one thing tonight:",
    "Sharp take ahead:",
    "A couple things worth knowing:",
    "For the curious wolves:",
    "Wolves who read win:",
    "Cliff notes for the pack:",
    "Brief on the desk:",
    "The kind of read that pays:",
]


def _pick_rss_opener(seed):
    """Deterministic opener for RSS-fed drafts. Same article always gets
    the same opener on refresh. Generic pool — used as fallback when no
    topic-specific opener applies."""
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return _RSS_OPENERS[h % len(_RSS_OPENERS)]


# ────────────────────────────────────────────────────────────────────
# TOPIC-AWARE OPENERS — different pools per topic so the lead actually
# fits what the article is about. Selection is deterministic per item.
# ────────────────────────────────────────────────────────────────────

# POLICY READ — split by central-bank actor.
_POLICY_OPENERS_BY_ACTOR = {
    "FED": [
        "FED WATCH:",
        "FOMC update.",
        "Powell on the wire.",
        "From the Eccles Building:",
        "The Fed just spoke. Here's what matters:",
        "RATE-PATH READ:",
        "Live from the Federal Reserve:",
        "If you trade rates, read this:",
        "FOMC desk:",
        "POWELL POSTCARD:",
    ],
    "ECB": [
        "ECB DESK:",
        "Lagarde on the wire.",
        "From Frankfurt:",
        "The ECB just moved.",
        "EURO RATES READ:",
        "ECB POSTCARD:",
        "Eurozone policy update:",
    ],
    "BOJ": [
        "BOJ DESK:",
        "Ueda on the wire.",
        "From Tokyo:",
        "BoJ just printed.",
        "Yen-watchers, this is for you:",
        "JAPAN POLICY READ:",
    ],
    "BOE": [
        "BOE DESK:",
        "Bailey on the wire.",
        "From Threadneedle Street:",
        "UK rates read:",
        "BoE update:",
    ],
    "PBOC": [
        "PBOC DESK:",
        "From Beijing's central bank:",
        "China policy update:",
        "PBOC postcard:",
    ],
    "TREASURY": [
        "TREASURY WIRE:",
        "From the Treasury building:",
        "Yellen on the wire.",
        "Fiscal policy update:",
        "TREASURY DESK:",
    ],
    "DEFAULT": [
        "POLICY READ:",
        "Central bank corner:",
        "Rate-path desk:",
        "If you watch rates, read this:",
        "Policy update for the pack:",
    ],
}

# DATA DROP — split by economic release type.
_DATA_OPENERS_BY_TOPIC = {
    "CPI": [
        "INFLATION READ:",
        "CPI just printed.",
        "The CPI scoreboard:",
        "Inflation update:",
        "CPI POSTCARD:",
        "From the inflation desk:",
        "How sticky is inflation? Let's see:",
    ],
    "PPI": [
        "PRODUCER-PRICE READ:",
        "PPI just dropped.",
        "Wholesale inflation update:",
        "PPI desk:",
    ],
    "JOBS": [
        "JOBS DAY.",
        "NFP just dropped.",
        "LABOR MARKET READ:",
        "Payrolls hit the wire.",
        "From the BLS desk:",
        "Hiring scoreboard:",
        "EMPLOYMENT POSTCARD:",
    ],
    "JOBLESS": [
        "JOBLESS-CLAIMS READ:",
        "Weekly claims just printed.",
        "Labor-market thermometer:",
        "From the unemployment desk:",
    ],
    "GDP": [
        "GDP DAY.",
        "Growth read just printed.",
        "GROWTH POSTCARD:",
        "How fast is the economy moving? Now we know:",
        "From the BEA desk:",
    ],
    "RETAIL": [
        "RETAIL-SALES READ:",
        "Consumer scoreboard:",
        "From the consumption desk:",
        "How is the consumer holding up? Let's see:",
    ],
    "HOUSING": [
        "HOUSING READ:",
        "From the housing desk:",
        "Affordability scoreboard:",
        "Mortgage-rate corner:",
    ],
    "DEFAULT": [
        "DATA DROP:",
        "Numbers just hit:",
        "From the data desk:",
        "Fresh print:",
        "Scoreboard update:",
    ],
}

# GLOBAL DESK — split by region.
_GLOBAL_OPENERS_BY_REGION = {
    "ASIA": [
        "ASIA DESK:",
        "From Tokyo to Hong Kong:",
        "APAC wire:",
        "Asia overnight:",
        "PACK BRIEFING — ASIA:",
    ],
    "EUROPE": [
        "EUROPE DESK:",
        "From Brussels and beyond:",
        "EU/UK wire:",
        "Continental update:",
        "PACK BRIEFING — EUROPE:",
    ],
    "MIDDLE_EAST": [
        "MIDDLE EAST DESK:",
        "Gulf wire:",
        "From the Gulf:",
        "PACK BRIEFING — MIDDLE EAST:",
        "Levant update:",
    ],
    "AFRICA": [
        "AFRICA DESK:",
        "Continental wire:",
        "From the African desk:",
        "PACK BRIEFING — AFRICA:",
    ],
    "AMERICAS": [
        "LATAM DESK:",
        "From the Americas:",
        "Canada/LatAm wire:",
        "PACK BRIEFING — AMERICAS:",
    ],
    "CHINA": [
        "CHINA DESK:",
        "From Beijing:",
        "Mainland wire:",
        "PACK BRIEFING — CHINA:",
    ],
    "DEFAULT": [
        "GLOBAL DESK:",
        "From the international wire:",
        "Cross-border read:",
        "World wire:",
    ],
}

# CORRUPTION WATCH — split by sentiment (justice served vs ongoing threat).
_CORRUPTION_OPENERS_BY_SENTIMENT = {
    "JUSTICE": [
        "JUSTICE SERVED:",
        "Score one for the good guys.",
        "Caught.",
        "Pack — got one.",
        "About damn time.",
        "FRAUDSTER DOWN:",
        "The wolves howled, the regulators listened.",
        "FROM THE COURTHOUSE:",
        "Indictment of the day:",
        "One off the streets:",
    ],
    "THREAT": [
        "PACK ALERT — SCAM:",
        "RETAIL BEWARE:",
        "Heads up, the wolves at the door.",
        "Watch your wallets:",
        "If you're invested, you'll want to read this:",
        "ACTIVE THREAT:",
        "New scam vector:",
        "PROTECTION BRIEFING:",
        "Pack — defensive read:",
    ],
    "DEFAULT": [
        "CORRUPTION DESK:",
        "From the corruption desk:",
        "FRAUD WATCH:",
        "On the trail:",
        "Pack — eyes up:",
    ],
}

# LOUD HOWL — flagship daily pick. Just generic, since the topic varies.
_LOUD_HOWL_OPENERS = [
    "TODAY'S LOUDEST HOWL:",
    "FRONT-PAGE FOR THE PACK:",
    "THE BIG ONE:",
    "If you read one thing today:",
    "The story the pack is talking about:",
    "FLAGSHIP READ:",
    "PACK FRONT-PAGE:",
]

# Topic detection regexes
_FED_NAMES_RE = re.compile(r"\b(?:fed|fomc|federal\s+reserve|powell|jerome\s+powell)\b", re.IGNORECASE)
_ECB_NAMES_RE = re.compile(r"\b(?:ecb|european\s+central\s+bank|lagarde|christine\s+lagarde)\b", re.IGNORECASE)
_BOJ_NAMES_RE = re.compile(r"\b(?:boj|bank\s+of\s+japan|ueda|kazuo\s+ueda)\b", re.IGNORECASE)
_BOE_NAMES_RE = re.compile(r"\b(?:boe|bank\s+of\s+england|bailey|andrew\s+bailey)\b", re.IGNORECASE)
_PBOC_NAMES_RE = re.compile(r"\b(?:pboc|people'?s\s+bank\s+of\s+china)\b", re.IGNORECASE)
_TREASURY_NAMES_RE = re.compile(r"\b(?:treasury|yellen|janet\s+yellen|bessent)\b", re.IGNORECASE)

_CPI_RE = re.compile(r"\b(?:cpi|inflation|consumer\s+price)\b", re.IGNORECASE)
_PPI_RE = re.compile(r"\b(?:ppi|producer\s+price)\b", re.IGNORECASE)
_JOBS_RE = re.compile(r"\b(?:nfp|nonfarm|payrolls?|jobs\s+report|employment\s+report)\b", re.IGNORECASE)
_JOBLESS_RE = re.compile(r"\b(?:jobless\s+claims|initial\s+claims|unemployment\s+claims)\b", re.IGNORECASE)
_GDP_RE = re.compile(r"\bgdp\b", re.IGNORECASE)
_RETAIL_SALES_RE = re.compile(r"\bretail\s+sales\b", re.IGNORECASE)
_HOUSING_RE = re.compile(r"\b(?:housing|mortgage|home\s+sales|new\s+home|existing\s+home)\b", re.IGNORECASE)

_REGION_ASIA_RE = re.compile(
    r"\b(?:japan|tokyo|south\s+korea|seoul|taiwan|taipei|singapore|hong\s+kong|"
    r"vietnam|thailand|bangkok|philippines|indonesia|jakarta|malaysia|"
    r"asia|asian|apac)\b", re.IGNORECASE)
_REGION_CHINA_RE = re.compile(r"\b(?:china|chinese|beijing|shanghai|shenzhen)\b", re.IGNORECASE)
_REGION_EUROPE_RE = re.compile(
    r"\b(?:germany|berlin|france|paris|italy|rome|spain|madrid|uk|britain|london|"
    r"eurozone|europe|european\s+union|brussels|netherlands|amsterdam|switzerland|"
    r"poland|warsaw|sweden|stockholm)\b", re.IGNORECASE)
_REGION_ME_RE = re.compile(
    r"\b(?:saudi|uae|qatar|israel|iran|tehran|jordan|kuwait|bahrain|"
    r"middle\s+east|gulf|opec|levant|lebanon|syria)\b", re.IGNORECASE)
_REGION_AFRICA_RE = re.compile(
    r"\b(?:africa|african|nigeria|south\s+africa|kenya|egypt|morocco|ethiopia)\b",
    re.IGNORECASE)
_REGION_AMERICAS_RE = re.compile(
    r"\b(?:canada|canadian|toronto|mexico|brazil|argentina|chile|colombia|"
    r"latin\s+america|latam)\b", re.IGNORECASE)


def _pick_from(pool, seed):
    """Deterministic pick from a pool list."""
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return pool[h % len(pool)]


def _pick_authentic_opener(fmt, item, seed=None):
    """Topic-aware opener selection. Each format reads the item's
    title+summary, detects topic/actor/region, and picks from the
    matching sub-pool. Falls back to format-default → generic if no
    topic match. Result is deterministic per item."""
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    blob = f"{title} {summary}"
    seed = seed or item.get("link") or title

    if fmt == "POLICY_READ":
        # Title-first match (more specific) — actor names in title beat
        # mentions in summary.
        for re_, key in [
            (_FED_NAMES_RE, "FED"),
            (_ECB_NAMES_RE, "ECB"),
            (_BOJ_NAMES_RE, "BOJ"),
            (_BOE_NAMES_RE, "BOE"),
            (_PBOC_NAMES_RE, "PBOC"),
            (_TREASURY_NAMES_RE, "TREASURY"),
        ]:
            if re_.search(title) or re_.search(summary):
                return _pick_from(_POLICY_OPENERS_BY_ACTOR[key], seed)
        return _pick_from(_POLICY_OPENERS_BY_ACTOR["DEFAULT"], seed)

    if fmt == "DATA_DROP":
        for re_, key in [
            (_CPI_RE, "CPI"),
            (_PPI_RE, "PPI"),
            (_JOBLESS_RE, "JOBLESS"),
            (_JOBS_RE, "JOBS"),
            (_GDP_RE, "GDP"),
            (_RETAIL_SALES_RE, "RETAIL"),
            (_HOUSING_RE, "HOUSING"),
        ]:
            if re_.search(blob):
                return _pick_from(_DATA_OPENERS_BY_TOPIC[key], seed)
        return _pick_from(_DATA_OPENERS_BY_TOPIC["DEFAULT"], seed)

    if fmt == "GLOBAL_DESK":
        for re_, key in [
            (_REGION_CHINA_RE, "CHINA"),
            (_REGION_ME_RE, "MIDDLE_EAST"),
            (_REGION_AFRICA_RE, "AFRICA"),
            (_REGION_AMERICAS_RE, "AMERICAS"),
            (_REGION_ASIA_RE, "ASIA"),
            (_REGION_EUROPE_RE, "EUROPE"),
        ]:
            if re_.search(blob):
                return _pick_from(_GLOBAL_OPENERS_BY_REGION[key], seed)
        return _pick_from(_GLOBAL_OPENERS_BY_REGION["DEFAULT"], seed)

    if fmt == "CORRUPTION_WATCH":
        is_justice = bool(_JUSTICE_RE.search(blob))
        is_threat = bool(_THREAT_RE.search(blob))
        if is_justice:
            return _pick_from(_CORRUPTION_OPENERS_BY_SENTIMENT["JUSTICE"], seed)
        if is_threat:
            return _pick_from(_CORRUPTION_OPENERS_BY_SENTIMENT["THREAT"], seed)
        return _pick_from(_CORRUPTION_OPENERS_BY_SENTIMENT["DEFAULT"], seed)

    if fmt == "LOUD_HOWL":
        return _pick_from(_LOUD_HOWL_OPENERS, seed)

    # Unknown format — generic pool
    return _pick_rss_opener(seed)


_ENGAGEMENT_QUESTIONS = [
    # Universal — fit any framing (positive, negative, neutral)
    "What do you think?",
    "What's your read?",
    "How do you see this playing out?",
    "Where does this go from here?",
    "Worth watching?",
    "Following this one?",
    "Thoughts?",
    "Tell me what I'm missing.",
    "Anyone else watching this?",
    "Pack — sound off.",
    "Why is no one talking about this?",
    "Wolves see this. Do you?",
    "Bigger deal than it looks?",
    "Sleeper story, or noise?",
    "Reading between the lines — what's actually going on?",
    "Did you see this coming?",
    "Is this the start of something?",
    "Worth the attention?",
    "Smart move, or smoke and mirrors?",
    "How big does this get?",
    "Are we paying attention to this enough?",
    "What does the timeline look like from here?",
    "Reply with your take.",
    "Quiet story, loud implications?",
    "Two months from now — what's the headline?",
    "Long overdue, or too little too late?",
    "Where do we go next?",
    "Does this matter, or no?",
    "How does this land for you?",
    "Should this be bigger news?",
]


def _pick_engagement_question(seed):
    """Deterministic question selection — same draft always closes with
    the same prompt on refresh. Seed off the source URL or title."""
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return _ENGAGEMENT_QUESTIONS[h % len(_ENGAGEMENT_QUESTIONS)]


# Sentiment-tuned questions for CORRUPTION WATCH. Two cases:
#   1. Justice served — indictment, conviction, ban, raid, shutdown.
#      Celebratory / "about time" framing.
#   2. Ongoing threat — active scam, exploit, hack, victims rising.
#      Protective / "how did this happen" framing.
_JUSTICE_SERVED_QUESTIONS = [
    "Justice served, or just the start?",
    "Does this set a precedent?",
    "About time?",
    "Long overdue?",
    "More to come, or one-and-done?",
    "Who else should be next?",
    "Pack — celebrating this one?",
    "Real deterrent, or symbolic?",
    "Does this slow the next one down?",
    "Will the punishment fit the crime?",
    "Should this be bigger news?",
    "Why isn't this on every front page?",
    "Other regulators watching?",
    "Should other countries follow?",
    "Score one for the good guys?",
    "Who's the next domino?",
]

_ONGOING_THREAT_QUESTIONS = [
    "How did this get this far?",
    "Who's next on the list?",
    "How do regular people protect themselves?",
    "Where were the regulators?",
    "Who's really to blame here?",
    "How big is this actually?",
    "How do we stop this from spreading?",
    "Pack — anyone else seeing this in the wild?",
    "Tell me what I'm missing.",
    "What gets exposed next?",
    "Why isn't this front-page news?",
    "Who's accountable here?",
    "When does action get taken?",
    "How many more victims before something happens?",
    "What's the pack's defense play?",
    "Is anyone watching out for retail?",
]


_JUSTICE_RE = re.compile(
    r"\b(?:"
    r"sentenced|sentencing|jailed|imprisoned|convicted|conviction|"
    r"indicted|indictment|charged\s+with|pleads?\s+guilty|guilty\s+(?:plea|verdict)|"
    r"arrested|arrest|raided|raid|seized|frozen|froze|"
    r"shut\s+down|shuts\s+down|shutdown|"
    r"bans?\b|banned|moves?\s+to\s+ban|"
    r"halts?\b|halted|suspends?\b|suspended|"
    r"settles?\b|settled|settlement|"
    r"fines?\b|fined|ordered\s+to\s+pay|"
    r"cracks?\s+down|crackdown|"
    r"verdict|disgorge|barred\s+from|disbarred|"
    r"recovered|claws?\s+back|clawback"
    r")\b",
    re.IGNORECASE,
)

_THREAT_RE = re.compile(
    r"\b(?:"
    r"victims|scammed|scammers?|drained|stolen|losses?\s+(?:to|from)|"
    r"exploit(?:s|ed|ing)?|hack(?:s|ed|ing)?|breach(?:es|ed)?|"
    r"warns?\s+of|warning|alert|"
    r"spreads?|spreading|surge\s+in|wave\s+of|spree|"
    r"rising|rises|soar(?:s|ed|ing)?|soaring|"
    r"active\s+scam|new\s+scam|growing\s+(?:fraud|scam)"
    r")\b",
    re.IGNORECASE,
)


def _pick_corruption_question(title, summary, seed):
    """Pick a sentiment-aware question for CORRUPTION WATCH. If the
    article reads as justice-being-served, pull from the celebratory
    pool. If it reads as an ongoing-threat exposé, pull from the
    protective pool. Mixed signal (action against a threat) — treat
    as justice, since the action is the news."""
    blob = f"{title} {summary}"
    is_justice = bool(_JUSTICE_RE.search(blob))
    is_threat = bool(_THREAT_RE.search(blob))
    if is_justice:
        pool = _JUSTICE_SERVED_QUESTIONS
    elif is_threat:
        pool = _ONGOING_THREAT_QUESTIONS
    else:
        pool = _ENGAGEMENT_QUESTIONS
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return pool[h % len(pool)]


_HOWLSTREET_CTA = (
    "24/7 global market terminal, corruption watch, and insider news "
    "here: http://howlstreet.github.io"
)


def _decorate_rss_body(sentences, fmt, item):
    """Wrap body sentences with a topic-aware opener (prepended to first
    sentence). _make_draft handles the trailer (article URL + CTA) so
    the article URL lands before the CTA URL — X renders the FIRST URL
    as the link card, so this ordering ensures the article's og:image
    shows up, not howlstreet.github.io's."""
    if not sentences:
        return sentences
    seed = item.get("link") or item.get("title", "")
    opener = _pick_authentic_opener(fmt, item, seed)
    sentences = list(sentences)
    sentences[0] = f"{opener} {sentences[0]}"
    return sentences


def _compose_body_from_article(title, summary, body_paras, want_sentences=6):
    """Build the multi-sentence draft body from title + RSS summary +
    fetched article body. Drops [FILL] placeholders — we either have
    real article content or we use the summary verbatim. With X Premium
    (4000 chars) we can afford 3-4 substantive sentences per draft."""
    out = []
    seen_norm = set()

    def _take(text):
        if not text or not text.strip():
            return False
        n = re.sub(r"\W+", "", text.lower())[:60]
        if n in seen_norm or not n:
            return False
        seen_norm.add(n)
        out.append(text.strip())
        return True

    # Skip the headline — X's link card already shows the title from the
    # source URL. Repeating it in the tweet body wastes a sentence and
    # reads as redundant. We lead straight with the substance.

    # 1. Pull substantive body sentences — numbers, central-bank moves,
    # dollar amounts, etc.
    if body_paras:
        body_sents = _pick_body_sentences(
            body_paras, title=title, max_sentences=want_sentences,
            min_chars=200,
        )
        for s in body_sents:
            if not _take(s):
                continue
            if len(out) >= want_sentences:
                break

    # 2. Fall back to the RSS summary when we couldn't fetch the body —
    # but only if the summary actually differs from the title. Many
    # press releases (Fed approvals, etc.) ship a summary that's just
    # the title repeated; that adds nothing the link card doesn't show.
    if not out and summary:
        title_norm = re.sub(r"\W+", "", title.lower())[:80]
        summary_norm = re.sub(r"\W+", "", summary.lower())[:80]
        if title_norm and summary_norm and not summary_norm.startswith(title_norm):
            _take(_first_sentence(summary, max_chars=300))

    # No title fallback — X's link card already shows the headline.
    # If we couldn't get a body or summary, skip the draft entirely
    # rather than ship a tweet that just repeats what the card shows.

    return out


def _make_draft(*, fmt, body, primary_source, source_url,
                source_title, source_summary, image_path=None, data=None):
    """Common draft-dict builder. Strips banned phrases, computes hash.

    image_path is currently always set to None — user feedback was that
    the matplotlib-generated charts/cards are redundant with the tweet
    text (same info twice). Article og:image fetching is a separate
    decision (see review.html behavior)."""
    body = _strip_banned_phrases(body)
    # Trailer is just the article URL. No CTA, no site link — X uses the
    # article's og:image as the card and the body carries the editorial
    # voice. The user wanted the queue posts to be punchy without the
    # 'visit our site' boilerplate at the bottom.
    if body:
        body = body.rstrip()
        # Strip any leftover CTA fragments from older runs that might
        # still be cached in drafts via content_hash dedup.
        for marker in ("24/7 global market", "Check out our 24/7",
                       "howlstreet.github.io"):
            if marker in body:
                body = body.split(marker)[0].rstrip()
        if source_url and not body.endswith(source_url):
            body = f"{body}\n\n{source_url}"
    return {
        "id": str(uuid.uuid4())[:8],
        "format": fmt,
        "status": "pending",
        "draft_text": body,
        "image_path": None,  # forced — no matplotlib output in drafts
        "og_image_url": None,  # populated by parallel fetch in collect_drafts
        "primary_source": primary_source or "",
        "source_url": source_url or "",
        "source_title": source_title or "",
        "source_summary": (source_summary or "")[:400],
        "drafted_at": datetime.utcnow().isoformat(),
        "data": data or {},
        "content_hash": _content_hash(source_title, source_url),
    }


_MARKET_OPENERS = [
    # Pack voice
    "The pack is watching.",
    "Heads up, pack.",
    "Caught a move.",
    "On the prowl.",
    "Eyes up.",
    "Tracks in the snow.",
    "Sniff this.",
    "Don't sleep on this.",
    "Worth a look.",
    "Howl on this.",
    "Wolves track moves. Here's one:",
    "Pack alert.",
    # Sharp-salesman voice
    "Pay attention to this one.",
    "You feeling that?",
    "Look at this number. Then look again.",
    "This is the take-notes part.",
    "Whatever you're doing, stop and read this.",
    "I'll keep it short:",
    "Two seconds, trust me.",
    "If you're not watching this, you're behind.",
    "The chart told me. Now I'm telling you.",
    "Nobody on TV said this today.",
    "Quick one for you.",
    "You don't need to be a quant to see this.",
    "Numbers walked into the room.",
    "While everyone's debating, this happened:",
    "Pop quiz — when's the last time you saw this?",
    "Skip the noise.",
    "Print this and put it on the fridge.",
    "I love a good move. This is a great one.",
    "Boring? Maybe. Actionable? Definitely.",
    "Some moves whisper. This one's screaming.",
    "Here's what the talking heads missed:",
    "This is the kind of move that hits twice.",
    "Wake up — here's the tape:",
    "You blink, you miss this:",
    "I don't say this often, but pay attention:",
    "Numbers don't lie. Look:",
    "Real-time update for the pack:",
    "Hot tape:",
    "The market did a thing:",
    "Read this twice.",
    "If your alerts didn't go off, mine did.",
]

_MARKET_KICKERS = {
    "high": "Multi-year high. The pack remembers what comes after these.",
    "low": "Multi-year low. Pressure has somewhere to go.",
    "move_up": "Sharpest move on this tape in a while. Stay sharp.",
    "move_down": "Sharpest drop on this tape in a while. Stay nimble.",
}

# Direction-aware engagement question per signal kind. Pop + wonder.
_MARKET_QUESTIONS = {
    "high": "Last time we saw this level, what came next?",
    "low": "Where does the floor sit from here?",
    "move_up": "Sustained run, or short squeeze unwinding?",
    "move_down": "Buying opportunity, or just the start?",
}


def _pick_market_opener(seed):
    """Deterministic opener selection so the same signal always gets the
    same line on refresh."""
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return _MARKET_OPENERS[h % len(_MARKET_OPENERS)]


def draft_market_move(signal_post):
    """A) MARKET MOVE — fed by signals.collect_signal_posts() entries.

    Voice: pack opener, the move, the matters sentence, a kicker that
    reads off the signal kind (high/low/move). Same wolf voice as the
    insider drafts so the queue reads consistently."""
    if not signal_post:
        return None
    kind = signal_post.get("kind", "")
    if kind not in ("move_up", "move_down", "high", "low"):
        return None
    headline = signal_post.get("headline", "")
    matters = signal_post.get("matters", "")
    if not headline:
        return None

    opener = _pick_market_opener(signal_post.get("signal_id", "") or headline)
    # Glue the opener to the headline as one lead line — feels less
    # robotic than a standalone interjection on its own paragraph.
    lead = f"{opener} {headline.rstrip('.').rstrip()}."

    parts = [lead]
    if matters:
        # Pull every complete sentence from the matters template — these
        # are hand-curated 'why this matters' lines, so all of them
        # belong in the post. Order them concrete-first so the fact-laden
        # sentence leads the explanation.
        sents = _split_sentences(matters)
        sents = [s for s in sents if s.endswith((".", "!", "?"))]
        if sents:
            scored = [(i, s, len(_BODY_FACT_SIGNAL.findall(s)))
                      for i, s in enumerate(sents)]
            # Concrete first, but keep relative order within tie groups.
            scored.sort(key=lambda x: (-x[2], x[0]))
            for _, s, _ in scored[:3]:
                parts.append(s)
    kicker = _MARKET_KICKERS.get(kind)
    if kicker:
        parts.append(kicker)
    body = "\n\n".join(p for p in parts if p)
    return _make_draft(
        fmt="MARKET_MOVE",
        body=body,
        primary_source=signal_post.get("source", ""),
        source_url=signal_post.get("data_url", ""),
        source_title=signal_post.get("label", ""),
        source_summary=matters,
        data={
            "signal_id": signal_post.get("signal_id"),
            "kind": kind,
            "current": signal_post.get("current_str"),
        },
    )


def draft_policy_read(item):
    """B) POLICY READ — Fed / Treasury / ECB / BoJ release.

    Filter: source in POLICY_SOURCES OR (TITLE matches both a central-bank
    name AND a policy-action verb). Title-only match prevents stories
    that merely *mention* a central bank in a body summary from getting
    classified as policy reads."""
    if not item:
        return None
    source = (item.get("source") or "").upper()
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    # Strict: source from POLICY_SOURCES OR title (not summary) matches
    # the bank+action pattern.
    is_policy_source = source in POLICY_SOURCES
    title_match = POLICY_KEYWORDS.search(title) is not None
    if not is_policy_source and not title_match:
        return None

    if is_policy_source:
        actor = source
    else:
        bank_m = re.search(
            r"\b(Fed|FOMC|Federal\s+Reserve|ECB|BoJ|Bank\s+of\s+Japan|"
            r"Bank\s+of\s+England|BoE|PBOC|People'?s\s+Bank\s+of\s+China|"
            r"Treasury|Powell|Yellen|Lagarde|Ueda|Bailey)\b",
            title, re.IGNORECASE,
        )
        actor = bank_m.group(1) if bank_m else "Central bank"

    body_paras = item.get("_body_paras") or []
    sentences = _compose_body_from_article(title, summary, body_paras,
                                            want_sentences=4)
    if not sentences:
        return None  # No body or summary content — skip rather than ship the title.
    sentences = _decorate_rss_body(sentences, "POLICY_READ", item)
    body = "\n\n".join(sentences)
    return _make_draft(
        fmt="POLICY_READ",
        body=body,
        primary_source=actor,
        source_url=item.get("link", ""),
        source_title=title,
        source_summary=summary,
        data={"actor": actor},
    )


_INSIDER_OPENERS = [
    # Pack voice
    "Did you know this?",
    "The pack is watching.",
    "Heads up, pack.",
    "Tracks in the snow.",
    "Sniff this out.",
    "Caught one.",
    "Don't sleep on this.",
    "While you weren't looking,",
    "Worth a look.",
    "Howl on this.",
    "The pack picks up scents. Here's one.",
    "Eyes up, pack.",
    "Quick one for the pack.",
    # Sharp-salesman voice
    "Two minutes. That's all I need.",
    "Pop quiz — when's the last time you saw this?",
    "If you only read one trade today, make it this one.",
    "Smart money signs Form 4.",
    "Insiders don't tweet. They file.",
    "Found this in the SEC weeds.",
    "Hot off Form 4.",
    "Form 4 just hit.",
    "While the talking heads were busy,",
    "The tape filed today.",
    "Boring SEC website, banger trade.",
    "Closed-door meetings end with these filings.",
    "Skip the noise. Look at this.",
    "Quietly, this happened today:",
    "Here's one they don't put in the news.",
    "This one's not on the front page yet.",
    "Numbers walked into the room.",
    "I'll keep it short:",
    "Don't roll your eyes — read it.",
    "Some moves whisper. Then there's this:",
    "I love a good cluster buy.",
    "You see this? Because I see this.",
    "Here's a name you won't hear on CNBC.",
    "Pull up a chair, this one's good.",
    "Ten minutes ago, this was filed.",
    "You're going to want this.",
    "Wake up, the tape moved:",
    "Real-time update for the pack:",
    "Hot tape:",
    "Print this and put it on the fridge.",
    "I don't say this often, but pay attention:",
    "Wolves track moves. Here's one:",
]


def _pick_insider_opener(seed):
    """Deterministic opener selection so the same trade always gets the
    same line (stable across cron runs, no jitter on refresh)."""
    h = sum(ord(c) for c in str(seed)) if seed else 0
    return _INSIDER_OPENERS[h % len(_INSIDER_OPENERS)]


def _build_insider_kicker(ttype, num_insiders, dollar_value, pct_since):
    """A single line of pack-voice interpretation that reads on the
    strongest signal in the data — cluster size, dollar magnitude, or
    post-trade move. Returns one line, no period spam."""
    is_buy = ttype == "P"
    # Post-trade move is the sharpest tell when it's big.
    if is_buy and pct_since >= 15:
        return f"Stock already up {pct_since:.0f}% since they bought. They knew."
    if not is_buy and pct_since <= -15:
        return f"Stock down {abs(pct_since):.0f}% since they sold. Convenient timing."
    # Cluster buying/selling — multiple insiders moving together.
    if num_insiders >= 5:
        return f"{num_insiders} insiders moving together. That's not a coincidence."
    if num_insiders >= 2:
        verb = "buy" if is_buy else "exit"
        return f"Cluster {verb} — {num_insiders} insiders on the same day."
    # Pure dollar magnitude — solo but huge.
    if dollar_value >= 50_000_000:
        return f"${dollar_value/1_000_000:.0f}M from one insider. Not a routine trim."
    if dollar_value >= 10_000_000:
        return f"${dollar_value/1_000_000:.1f}M solo. Worth tracking."
    return "The kind of trade that pays attention to itself."


def draft_corruption_watch_from_insider(insider_post):
    """C-1) CORRUPTION WATCH from corporate insider trade (Form 4).

    Filter on dollar threshold so we surface only the meaningful trades,
    not director $5K nibbles."""
    if not insider_post:
        return None
    dv = insider_post.get("dollar_value", 0) or 0
    if dv < INSIDER_DOLLAR_THRESHOLD:
        return None
    ticker = insider_post.get("ticker", "")
    company = insider_post.get("company", "")
    ttype = insider_post.get("type", "")
    pct_since = insider_post.get("pct_since", 0) or 0
    qty = insider_post.get("qty", 0) or 0
    price = insider_post.get("price", 0) or 0
    trade_date = insider_post.get("trade_date", "")
    num_insiders = insider_post.get("num_insiders", 1) or 1
    chart_path = insider_post.get("chart_path")

    verb = "bought" if ttype == "P" else "sold"
    noun = "purchase" if ttype == "P" else "sale"
    cluster_note = f", {num_insiders} insiders" if num_insiders > 1 else ""
    sign = "+" if pct_since >= 0 else ""

    seed = f"{ticker}_{trade_date}_{ttype}"
    opener = _pick_insider_opener(seed)
    kicker = _build_insider_kicker(ttype, num_insiders, dv, pct_since)

    # CTA is appended by _make_draft so it lands AFTER the source URL.
    body = (
        f"{opener} ${ticker} insider {verb} ${dv:,.0f} on {trade_date}{cluster_note}. \U0001f440\n\n"
        f"{company}. {qty:,.0f} shares at ${price:,.2f}.\n\n"
        f"{sign}{pct_since:.1f}% since the {noun}.\n\n"
        f"{kicker}"
    )
    return _make_draft(
        fmt="CORRUPTION_WATCH_INSIDER",
        body=body,
        primary_source="SEC Form 4 via openinsider",
        source_url=f"http://openinsider.com/screener?s={ticker}",
        source_title=f"{company} ({ticker}) insider {verb} ${dv:,.0f}",
        source_summary=f"{num_insiders} insider(s) {verb} {qty:,.0f} shares at ${price:,.2f} on {trade_date}.",
        image_path=chart_path,
        data={
            "ticker": ticker,
            "type": ttype,
            "dollar_value": dv,
            "pct_since": pct_since,
        },
    )


def draft_corruption_watch_from_rss(item):
    """C-2) CORRUPTION WATCH from RSS — items already classified as
    corruption by the upstream filter (caller passes pre-filtered)."""
    if not item:
        return None
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    source = item.get("source", "")
    body_paras = item.get("_body_paras") or []
    sentences = _compose_body_from_article(title, summary, body_paras,
                                            want_sentences=4)
    if not sentences:
        return None
    sentences = _decorate_rss_body(sentences, "CORRUPTION_WATCH", item)
    body = "\n\n".join(sentences)
    return _make_draft(
        fmt="CORRUPTION_WATCH",
        body=body,
        primary_source=source,
        source_url=item.get("link", ""),
        source_title=title,
        source_summary=summary,
        data={"source": source},
    )


def draft_global_desk(item):
    """D) GLOBAL DESK — non-US story FinTwit ignores. Filter on region
    hints in title or summary."""
    if not item:
        return None
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    blob = title + " " + summary
    if not GLOBAL_DESK_REGION_HINTS.search(blob):
        return None
    source = item.get("source", "")
    body_paras = item.get("_body_paras") or []
    sentences = _compose_body_from_article(title, summary, body_paras,
                                            want_sentences=4)
    if not sentences:
        return None
    sentences = _decorate_rss_body(sentences, "GLOBAL_DESK", item)
    body = "\n\n".join(sentences)
    return _make_draft(
        fmt="GLOBAL_DESK",
        body=body,
        primary_source=source,
        source_url=item.get("link", ""),
        source_title=title,
        source_summary=summary,
        data={"source": source},
    )


def draft_data_drop(item):
    """E) DATA DROP — economic release. Filter on title containing one
    of CPI / NFP / GDP / etc."""
    if not item:
        return None
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    if not DATA_DROP_KEYWORDS.search(title + " " + summary):
        return None
    source = item.get("source", "")
    body_paras = item.get("_body_paras") or []
    sentences = _compose_body_from_article(title, summary, body_paras,
                                            want_sentences=4)
    if not sentences:
        return None
    sentences = _decorate_rss_body(sentences, "DATA_DROP", item)
    body = "\n\n".join(sentences)
    return _make_draft(
        fmt="DATA_DROP",
        body=body,
        primary_source=source,
        source_url=item.get("link", ""),
        source_title=title,
        source_summary=summary,
        data={"source": source},
    )


def draft_loud_howl(top_item):
    """LOUD HOWL — the daily flagship pick. Same item pick_top_story
    selected for the site's Loudest Howl, drafted as a tweet in the
    new editorial voice. One per run.

    Lead with the consequence. The user heavily edits in review.html
    before approving — drafter just sets up the skeleton with real
    fact-check data."""
    if not top_item:
        return None
    title = top_item.get("title", "") or ""
    summary = top_item.get("summary", "") or ""
    source = top_item.get("source", "")
    body_paras = top_item.get("_body_paras") or []
    sentences = _compose_body_from_article(title, summary, body_paras,
                                            want_sentences=4)
    if not sentences:
        return None
    sentences = _decorate_rss_body(sentences, "LOUD_HOWL", top_item)
    body = "\n\n".join(sentences)
    return _make_draft(
        fmt="LOUD_HOWL",
        body=body,
        primary_source=source,
        source_url=top_item.get("link", ""),
        source_title=title,
        source_summary=summary,
        data={"source": source},
    )


def draft_the_take():
    """F) THE TAKE — manual only. Reads the_take.md if present.
    Format: first non-empty line is the lede, rest is the body."""
    if not THE_TAKE_PATH.exists():
        return None
    try:
        raw = THE_TAKE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    return _make_draft(
        fmt="THE_TAKE",
        body=raw,
        primary_source="Howl Street editorial",
        source_url="",
        source_title="THE TAKE",
        source_summary=raw[:400],
        data={"manual": True},
    )


# ────────────────────────────────────────────────────────────────────
# COLLECTION ENTRY POINT
# ────────────────────────────────────────────────────────────────────

def collect_drafts(items, signal_posts=None, insider_posts=None,
                   rss_corruption_items=None, megacap_filter=None,
                   top_item=None):
    """Top-level call from update.py. Pulls all format drafters, dedupes
    against posted.json, caps per-format count, writes drafts.json +
    review.html.

    rss_corruption_items: pre-filtered list of RSS items already classified
                          as corruption by update.py (so drafter doesn't
                          re-import the regex).
    megacap_filter:      callable returning True if an item mentions a
                          mega-cap. Used to bias GLOBAL DESK selection."""
    items = items or []
    signal_posts = signal_posts or []
    insider_posts = insider_posts or []
    rss_corruption_items = rss_corruption_items or []

    posted = _load_posted()
    drafts = []

    # Pre-fetch article bodies for items likely to produce drafts so each
    # format function can compose multi-sentence content from real article
    # text instead of [FILL] placeholders. Parallel fetch.
    fetch_targets = []
    seen_urls = set()
    for it in (items or []):
        url = it.get("link", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            fetch_targets.append(it)
    if top_item and top_item.get("link") and top_item["link"] not in seen_urls:
        fetch_targets.append(top_item)
        seen_urls.add(top_item["link"])
    # Cap the body-fetch budget so a busy day doesn't stall the run.
    fetch_targets = fetch_targets[:60]
    if fetch_targets:
        urls = [t["link"] for t in fetch_targets]
        with ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(_fetch_article_body, urls))
        for t, body in zip(fetch_targets, results):
            t["_body_paras"] = body
        with_body = sum(1 for t in fetch_targets if t.get("_body_paras"))
        print(f"  drafter body-fetch: {with_body}/{len(fetch_targets)} articles")

    def _take(format_name, candidates):
        kept = []
        for d in candidates:
            if d is None:
                continue
            if _is_already_posted(d["content_hash"], d.get("source_url"), posted):
                continue
            kept.append(d)
            if len(kept) >= MAX_DRAFTS_PER_FORMAT_PER_RUN:
                break
        return kept

    # 0) LOUD HOWL — the daily flagship pick (same item the site's
    # Loudest Howl uses). Always first in the queue.
    if top_item:
        loud = draft_loud_howl(top_item)
        if loud and not _is_already_posted(loud["content_hash"], loud.get("source_url"), posted):
            drafts.append(loud)

    # A) MARKET MOVE
    drafts += _take("MARKET_MOVE",
                    [draft_market_move(sp) for sp in signal_posts])

    # B) POLICY READ
    policy_candidates = sorted(
        [i for i in items
         if (i.get("source", "").upper() in POLICY_SOURCES
             or POLICY_KEYWORDS.search((i.get("title", "") or "") + " " + (i.get("summary", "") or "")))],
        key=lambda x: x.get("ts", datetime(2000, 1, 1, tzinfo=NY)),
        reverse=True,
    )
    drafts += _take("POLICY_READ",
                    [draft_policy_read(i) for i in policy_candidates])

    # C) CORRUPTION WATCH — insider first, then RSS
    drafts += _take("CORRUPTION_WATCH_INSIDER",
                    [draft_corruption_watch_from_insider(ip) for ip in insider_posts])
    rss_corruption_sorted = sorted(
        rss_corruption_items,
        key=lambda x: x.get("ts", datetime(2000, 1, 1, tzinfo=NY)),
        reverse=True,
    )
    drafts += _take("CORRUPTION_WATCH",
                    [draft_corruption_watch_from_rss(i) for i in rss_corruption_sorted])

    # D) GLOBAL DESK
    global_candidates = sorted(
        items,
        key=lambda x: x.get("ts", datetime(2000, 1, 1, tzinfo=NY)),
        reverse=True,
    )
    drafts += _take("GLOBAL_DESK",
                    [draft_global_desk(i) for i in global_candidates])

    # E) DATA DROP
    data_candidates = sorted(
        [i for i in items
         if DATA_DROP_KEYWORDS.search((i.get("title", "") or "") + " " + (i.get("summary", "") or ""))],
        key=lambda x: x.get("ts", datetime(2000, 1, 1, tzinfo=NY)),
        reverse=True,
    )
    drafts += _take("DATA_DROP",
                    [draft_data_drop(i) for i in data_candidates])

    # F) THE TAKE
    take = draft_the_take()
    if take and not _is_already_posted(take["content_hash"], take.get("source_url"), posted):
        drafts.append(take)

    # No og:image fetch — X auto-renders the article card from the
    # source URL embedded in the tweet body. The review queue is text-only.

    # Persist + render
    _save_drafts(drafts)
    write_review_html(drafts)
    print(f"  drafter: emitted {len(drafts)} drafts across "
          f"{len({d['format'].split('_')[0] for d in drafts})} formats")
    return drafts


# ────────────────────────────────────────────────────────────────────
# review.html RENDERER — local-only, client-side. No localStorage —
# state lives in-page only; user pastes JSON blob into posted.json
# and commits.
# ────────────────────────────────────────────────────────────────────

_FORMAT_LABELS = {
    "LOUD_HOWL":   ("LOUD HOWL", "#00ff88"),
    "MARKET_MOVE": ("MARKET MOVE", "#00bfff"),
    "POLICY_READ": ("POLICY READ", "#ffaa00"),
    "CORRUPTION_WATCH": ("CORRUPTION WATCH", "#b042ff"),
    "CORRUPTION_WATCH_INSIDER": ("INSIDER TRADING ALERT", "#ff66c4"),
    "GLOBAL_DESK": ("GLOBAL DESK", "#cccccc"),
    "DATA_DROP":   ("DATA DROP", "#ff4d4d"),
    "THE_TAKE":    ("THE TAKE", "#ffffff"),
}


def write_review_html(drafts):
    """Generate review.html — local approve/edit/reject UI. State stays
    in-page (no localStorage so it's device-portable); user clicks "Copy
    state to clipboard" and pastes into posted.json + commits."""
    drafts = drafts or []
    now_str = datetime.now(NY).strftime("%Y-%m-%d %H:%M EDT")
    formats_present = sorted({d["format"].replace("_INSIDER", "") for d in drafts})

    cards_html = []
    for d in drafts:
        label, color = _FORMAT_LABELS.get(
            d["format"], (d["format"], "#888"))
        # No image preview — X handles the article card from the URL in
        # the tweet body. Review queue is text-only, just the draft text.
        img_block = ""
        source_block = (
            f'<div class="source-block">'
            f'  <div class="source-label">SOURCE — fact-check before approving</div>'
            f'  <div class="source-headline">{html_lib.escape(d.get("source_title", ""))}</div>'
            f'  <div class="source-snippet">{html_lib.escape(d.get("source_summary", ""))}</div>'
        )
        if d.get("source_url"):
            source_block += (
                f'  <a class="source-link" href="{html_lib.escape(d["source_url"], quote=True)}" '
                f'target="_blank" rel="noopener">View source ↗</a>'
            )
        source_block += '</div>'

        cards_html.append(
            f'<article class="draft" data-id="{html_lib.escape(d["id"])}" '
            f'data-format="{html_lib.escape(d["format"])}" '
            f'data-hash="{html_lib.escape(d["content_hash"])}" '
            f'data-url="{html_lib.escape(d.get("source_url", ""), quote=True)}">'
            f'  <header class="draft-head">'
            f'    <span class="badge" style="background:{color};color:#000;">{html_lib.escape(label)}</span>'
            f'    <span class="meta">{html_lib.escape(d.get("primary_source", ""))}</span>'
            f'    <span class="status-pill" data-status="pending">PENDING</span>'
            f'    <span class="check" aria-hidden="true">✓ POSTED</span>'
            f'  </header>'
            f'  {source_block}'
            f'  <textarea class="draft-text">{html_lib.escape(d["draft_text"])}</textarea>'
            f'  <div class="char-counter">— chars</div>'
            f'  {img_block}'
            f'  <div class="actions">'
            f'    <button class="btn btn-postx" onclick="postOnX(this)">Post on X ↗</button>'
            f'    <button class="btn btn-copy" onclick="copyDraft(this)">Copy</button>'
            f'    <button class="btn btn-posted" onclick="markPosted(this)">Mark posted</button>'
            f'  </div>'
            f'</article>'
        )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Howl Street — Review Queue</title>
<style>
  :root {{ --bg:#000; --fg:#ddd; --dim:#777; --green:#00ff88; --border:#1f1f1f; --card:#0a0a0a; }}
  * {{ box-sizing: border-box; }}
  body {{ background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,monospace; margin:0; padding:24px; }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ color:var(--green); font-size:22px; margin:0 0 4px; letter-spacing:1px; }}
  .summary {{ color:var(--dim); font-size:12px; margin-bottom:16px; }}
  .top-actions {{ position:sticky; top:0; background:var(--bg); padding:8px 0 16px; border-bottom:1px solid var(--border); margin-bottom:24px; z-index:10; }}
  .draft {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:18px; transition: opacity 0.2s, border-color 0.2s; }}
  .draft.posted {{ border-color: var(--green); opacity: 0.55; }}
  .draft.posted textarea.draft-text {{ opacity: 0.7; }}
  .draft-head {{ display:flex; align-items:center; gap:10px; font-size:11px; margin-bottom:10px; }}
  .badge {{ padding:3px 8px; border-radius:3px; font-weight:bold; letter-spacing:0.5px; font-size:10px; }}
  .meta {{ color:var(--dim); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .status-pill {{ font-size:10px; color:var(--dim); padding:2px 6px; border:1px solid var(--border); border-radius:3px; }}
  .check {{ display:none; font-size:11px; color:var(--green); font-weight:bold; letter-spacing:0.5px; }}
  .draft.posted .status-pill {{ display:none; }}
  .draft.posted .check {{ display:inline; }}
  .source-block {{ background:#050505; border:1px dashed #222; border-radius:4px; padding:10px; margin-bottom:12px; font-size:12px; }}
  .source-label {{ color:var(--dim); font-size:10px; letter-spacing:0.5px; margin-bottom:4px; }}
  .source-headline {{ color:#bbb; font-weight:bold; margin-bottom:4px; }}
  .source-snippet {{ color:#888; line-height:1.4; }}
  .source-link {{ color:var(--green); font-size:11px; text-decoration:none; display:inline-block; margin-top:4px; }}
  textarea.draft-text {{ width:100%; min-height:140px; background:#050505; color:var(--fg); border:1px solid var(--border); border-radius:4px; padding:10px; font:13px/1.5 -apple-system,monospace; resize:vertical; white-space:pre-wrap; }}
  .char-counter {{ color:var(--dim); font-size:10px; text-align:right; margin-top:4px; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
  .btn {{ font-size:12px; padding:7px 14px; border-radius:4px; cursor:pointer; border:none; font-weight:bold; letter-spacing:0.4px; }}
  .btn-postx {{ background:#000; color:#fff; border:1px solid #fff; }}
  .btn-postx:hover {{ background:#fff; color:#000; }}
  .btn-copy {{ background:var(--green); color:#000; }}
  .btn-posted {{ background:#1a1a1a; color:var(--green); border:1px solid var(--green); }}
  .btn-posted:hover {{ background:var(--green); color:#000; }}
  .draft.posted .btn-posted {{ background:var(--green); color:#000; }}
  .btn-state {{ background:#1a1a1a; color:#fff; border:1px solid var(--border); padding:8px 16px; }}
  .btn-state:hover {{ border-color: var(--green); color: var(--green); }}
  /* PIN gate */
  #pin-gate {{ position:fixed; inset:0; background:#000; z-index:9999; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:18px; }}
  #pin-gate.hidden {{ display:none; }}
  #pin-gate h2 {{ color:var(--green); font-size:18px; letter-spacing:3px; margin:0; font-family:-apple-system,monospace; }}
  #pin-gate .pin-sub {{ color:var(--dim); font-size:12px; letter-spacing:1px; }}
  #pin-input {{ background:#050505; border:1px solid var(--border); color:var(--fg); font:24px/1 -apple-system,monospace; padding:14px 18px; width:200px; text-align:center; letter-spacing:8px; border-radius:4px; outline:none; }}
  #pin-input:focus {{ border-color:var(--green); box-shadow:0 0 0 1px var(--green); }}
  #pin-error {{ color:#ff4d4d; font-size:11px; height:14px; letter-spacing:1px; }}
  #app-content.hidden {{ display:none; }}
</style>
</head>
<body>
<div id="pin-gate">
  <h2>HOWL STREET — REVIEW QUEUE</h2>
  <div class="pin-sub">Enter PIN to access</div>
  <input id="pin-input" type="password" inputmode="numeric" maxlength="4" autocomplete="off" autofocus />
  <div id="pin-error"></div>
</div>
<div id="app-content" class="hidden">
<div class="wrap">
  <header>
    <h1>HOWL STREET — REVIEW QUEUE</h1>
    <div class="summary" id="summary">{len(drafts)} drafts pending across {len(formats_present)} formats — last updated {now_str}</div>
  </header>
  <div class="top-actions">
    <button class="btn btn-state" onclick="copyStateBlob()">Copy state blob to clipboard</button>
    <span class="meta" style="margin-left:12px;font-size:11px;">paste output into posted.json and commit so the cron stops re-drafting</span>
  </div>
  <main>
{chr(10).join(cards_html) if cards_html else '<div class="meta">No pending drafts. Nothing surfaced this run.</div>'}
  </main>
</div>
</div>
<script>
// PIN gate — required to view the queue. Once unlocked in this browser,
// stays unlocked (sessionStorage so a fresh browser session re-prompts).
const PIN_KEY = 'howlstreet_review_pin_unlocked';
const PIN_VALUE = '0470';

function unlockApp() {{
  document.getElementById('pin-gate').classList.add('hidden');
  document.getElementById('app-content').classList.remove('hidden');
}}

if (sessionStorage.getItem(PIN_KEY) === '1') {{
  unlockApp();
}} else {{
  const input = document.getElementById('pin-input');
  const errEl = document.getElementById('pin-error');
  input.addEventListener('input', () => {{
    if (input.value.length === 4) {{
      if (input.value === PIN_VALUE) {{
        sessionStorage.setItem(PIN_KEY, '1');
        unlockApp();
      }} else {{
        errEl.textContent = 'Wrong PIN';
        input.value = '';
        setTimeout(() => {{ errEl.textContent = ''; }}, 1500);
      }}
    }}
  }});
}}

// State keyed by content_hash (stable across cron re-draft of the same
// story) so a post stays marked posted even when drafts.json regenerates.
const STORAGE_KEY = 'howlstreet_review_state_v2';

function loadState() {{
  try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}'); }}
  catch (e) {{ return {{}}; }}
}}
function saveState(s) {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); }}
const STATE = loadState();

document.querySelectorAll('textarea.draft-text').forEach(ta => {{
  const counter = ta.parentElement.querySelector('.char-counter');
  function update() {{ counter.textContent = ta.value.length + ' chars'; }}
  ta.addEventListener('input', () => {{
    update();
    const card = ta.closest('.draft');
    const hash = card.dataset.hash;
    if (STATE[hash]) {{
      STATE[hash].edited_text = ta.value;
      saveState(STATE);
    }}
  }});
  update();
}});

// Rehydrate posted state on page load — keyed by content_hash so the
// same story stays marked even after a re-draft.
document.querySelectorAll('article.draft').forEach(card => {{
  const hash = card.dataset.hash;
  const saved = STATE[hash];
  if (!saved) return;
  if (saved.edited_text) {{
    card.querySelector('textarea.draft-text').value = saved.edited_text;
    card.querySelector('.char-counter').textContent = saved.edited_text.length + ' chars';
  }}
  if (saved.status === 'posted') {{
    card.classList.add('posted');
  }}
}});

function copyDraft(btn) {{
  const card = btn.closest('.draft');
  const ta = card.querySelector('textarea.draft-text');
  navigator.clipboard.writeText(ta.value).then(() => {{
    const orig = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => {{ btn.textContent = orig; }}, 1500);
  }});
}}

function postOnX(btn) {{
  const card = btn.closest('.draft');
  const ta = card.querySelector('textarea.draft-text');
  const url = 'https://twitter.com/intent/tweet?text=' + encodeURIComponent(ta.value);
  window.open(url, '_blank', 'noopener');
  // Auto-mark as posted — the user is going to send the tweet now.
  markPosted(btn);
}}

function markPosted(btn) {{
  const card = btn.closest('.draft');
  const hash = card.dataset.hash;
  const ta = card.querySelector('textarea.draft-text');
  STATE[hash] = {{
    status: 'posted',
    format: card.dataset.format,
    content_hash: hash,
    source_url: card.dataset.url || '',
    edited_text: ta.value,
    ts: new Date().toISOString(),
  }};
  saveState(STATE);
  card.classList.add('posted');
}}

function copyStateBlob() {{
  // Emit only posted entries — these are what go into posted.json.
  const out = [];
  for (const [hash, s] of Object.entries(STATE)) {{
    if (s.status === 'posted') {{
      out.push({{
        content_hash: s.content_hash,
        source_url: s.source_url,
        format: s.format,
        posted_at: s.ts,
        tweet_text: s.edited_text,
      }});
    }}
  }}
  if (out.length === 0) {{
    alert('Nothing posted yet.');
    return;
  }}
  const blob = JSON.stringify(out, null, 2);
  navigator.clipboard.writeText(blob).then(() => {{
    alert(out.length + ' entries copied. Paste into posted.json (merge with existing array).');
  }});
}}
</script>
</body>
</html>
"""
    REVIEW_PATH.write_text(page, encoding="utf-8")
