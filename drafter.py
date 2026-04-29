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


def _pick_body_sentences(body_paras, title="", max_sentences=4, min_chars=200):
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

def _compose_body_from_article(title, summary, body_paras, want_sentences=4):
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

    # 2. Fall back to the RSS summary when we couldn't fetch the body.
    if not out and summary:
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
    # Trailer is URLs only — no "via Source" text. Source URL first so X
    # auto-renders the article's og:image as the link card; site URL
    # second so the brand still gets a link.
    if body:
        body = body.rstrip()
        already_trailed = (body.endswith("howlstreet.github.io") or
                           (source_url and body.endswith(source_url)))
        if not already_trailed:
            if source_url:
                body = f"{body}\n\n{source_url}\nhowlstreet.github.io"
            else:
                body = f"{body}\n\nhowlstreet.github.io"
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
]

_MARKET_KICKERS = {
    "high": "Multi-year high. The pack remembers what comes after these.",
    "low": "Multi-year low. Pressure has somewhere to go.",
    "move_up": "Sharpest move on this tape in a while. Stay sharp.",
    "move_down": "Sharpest drop on this tape in a while. Stay nimble.",
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
        # Split matters into sentences and pick the most concrete one —
        # the one with named groups, percentages, or dollar amounts. The
        # first sentence is often a generic "X is the live Y market" filler.
        sents = _split_sentences(matters)
        sents = [s for s in sents if s.endswith((".", "!", "?"))]
        if sents:
            scored = [(s, len(_BODY_FACT_SIGNAL.findall(s))) for s in sents]
            scored.sort(key=lambda x: x[1], reverse=True)
            best = scored[0][0] if scored else sents[0]
            parts.append(best)
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

    opener = _pick_insider_opener(f"{ticker}_{trade_date}_{ttype}")
    kicker = _build_insider_kicker(ttype, num_insiders, dv, pct_since)

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
</style>
</head>
<body>
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
<script>
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
