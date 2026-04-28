"""
HOWL STREET — site updater
Pulls live market data + headlines, rebuilds index.html from template.
Runs on GitHub Actions on a schedule.
"""

import os
import socket
import sys
import html
import json
import re
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

import yfinance as yf
import feedparser
from zoneinfo import ZoneInfo

import signals  # phase 2: macro signal detector + chart engine
import insider_trades  # phase 3: corporate insider trades (Form 4 data)
import cards as card_renderer  # phase 4: image cards for image-first X posts

# Cap per-feed network wait so one slow source can't stall the build
socket.setdefaulttimeout(15)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent
TEMPLATE_PATH = REPO_ROOT / "template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"
HERO_PATH = REPO_ROOT / "hero.md"
SITEMAP_PATH = REPO_ROOT / "sitemap.xml"
FEED_PATH = REPO_ROOT / "feed.xml"
QUEUE_PATH = REPO_ROOT / "queue.html"
HERO_LOCK_PATH = REPO_ROOT / "hero_lock.json"
SITE_URL = "https://howlstreet.github.io"

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
BERLIN = ZoneInfo("Europe/Berlin")
PARIS = ZoneInfo("Europe/Paris")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
SHANGHAI = ZoneInfo("Asia/Shanghai")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# Mega-cap watchlist. The queue surfaces ONLY items that mention one of
# these names or tickers (plus macro-signal posts and CORRUPTION items,
# which already cross-aisle by their nature). The point: posts on @HowlStreet
# need to be about names everyone on FinTwit recognizes, otherwise they
# don't generate engagement. Curate aggressively, expand if a name keeps
# coming up that's missing.
MEGA_CAP_TICKERS = {
    # FAANGM + extras
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    # Top US by market cap
    "BRK.A", "BRK.B", "BRK", "JPM", "V", "MA", "UNH", "XOM", "JNJ", "WMT",
    "PG", "HD", "AVGO", "KO", "PEP", "COST", "NFLX", "ABBV", "BAC",
    "TMO", "PFE", "ADBE", "CSCO", "CRM", "ORCL", "AMD", "INTC", "BA",
    "DIS", "GS", "MS", "C", "WFC", "T", "VZ", "CMCSA", "CVX",
    # Hot semis / AI
    "TSM", "ASML", "MU", "QCOM", "ARM", "PLTR", "SMCI", "AI",
    # China megacaps
    "BABA", "NIO", "BIDU", "JD", "PDD",
    # Tech / consumer
    "SPOT", "UBER", "ABNB", "DASH", "SHOP", "SNOW", "ZM",
    # Fintech
    "PYPL", "SQ", "BLOCK", "AFRM", "SOFI",
    # Autos
    "F", "GM", "RIVN", "LCID",
    # Pharma
    "MRK", "LLY", "MRNA", "BMY", "GILD", "AMGN",
    # Consumer
    "NKE", "SBUX", "MCD", "CMG",
    # Airlines
    "AAL", "DAL", "UAL", "LUV",
    # More banks
    "PNC", "COF", "USB",
    # Crypto-exposed equities
    "COIN", "MSTR", "HOOD", "MARA", "RIOT",
    # Defense / energy / industrial
    "LMT", "RTX", "GE", "OXY", "DVN", "EOG", "CAT", "DE",
    # Big retail / restaurant
    "TGT", "LOW", "TJX", "BBY", "DIS",
    # Notable single-issue tickers
    "GME", "AMC", "DJT", "TWLO",
}

# Lowercase company names that count as a mega-cap mention even without
# a cashtag in the headline. Used by _matches_megacap.
MEGA_CAP_NAMES = (
    "apple", "microsoft", "google", "alphabet", "amazon", "meta platforms", "facebook",
    "nvidia", "tesla", "spacex", "berkshire hathaway", "berkshire", "jpmorgan",
    "jp morgan", "visa", "mastercard", "unitedhealth", "exxonmobil", "exxon",
    "johnson & johnson", "walmart", "broadcom", "coca-cola", "coca cola", "pepsico",
    "costco", "netflix", "abbvie", "bank of america", "thermo fisher", "pfizer",
    "adobe", "cisco", "salesforce", "oracle", "amd ", "intel", "boeing", "disney",
    "goldman sachs", "morgan stanley", "verizon", "comcast", "chevron",
    "alibaba", "spotify", "uber", "airbnb", "doordash", "paypal", "square inc",
    "block inc", "ford motor", "general motors", "rivian", "lucid motors",
    "merck", "eli lilly", "moderna", "nike", "starbucks", "mcdonald",
    "chipotle", "american airlines", "delta air", "united airlines",
    "coinbase", "microstrategy", "robinhood", "lockheed", "raytheon",
    "occidental petroleum", "devon energy", "caterpillar", "deere",
    "target ", "lowe's", "best buy",
    # Crypto majors
    "bitcoin", "ethereum", "solana", "polygon network",
    # Notable individuals (often more searchable than ticker)
    "elon musk", "warren buffett", "jamie dimon", "jensen huang", "tim cook",
    "satya nadella", "sundar pichai", "andy jassy", "mark zuckerberg",
    "powell ", "yellen ", "lagarde",
    # Mega-themes — events that move every mega-cap (oil, Fed, geopolitics)
    "opec", "opec+", "saudi aramco", "strait of hormuz",
    "fed meeting", "fomc decision", "fomc minutes", "rate cut", "rate hike",
    "rate decision", "fed cuts", "fed holds", "fed raises",
    "us cpi", "us inflation report", "nonfarm payrolls", "jobs report",
    "gdp report", "retail sales report",
    "ai chip", "chip ban", "chip export", "chip war",
    "russia sanctions", "china tariff", "trade war",
)


def _matches_megacap(item):
    """True if title or summary mentions a mega-cap ticker (cashtag or
    parenthesized) or a mega-cap company / executive name. Aggressive
    filter — most foreign-small-cap content will fail this and drop out
    of the queue."""
    title = item.get("title", "") or ""
    summary = item.get("summary", "") or ""
    blob = title + " " + summary
    # Cashtag / paren-ticker match
    for m in re.finditer(r"\$([A-Z]{1,6})\b", blob):
        if m.group(1).upper() in MEGA_CAP_TICKERS:
            return True
    for m in re.finditer(r"\(([A-Z]{1,6})(?:[:\.][A-Z]+)?\)", blob):
        if m.group(1).upper() in MEGA_CAP_TICKERS:
            return True
    # Plain ticker match in title (Apple-style "GOOG up 2%")
    title_upper_tokens = re.findall(r"\b([A-Z]{2,6})\b", title)
    for t in title_upper_tokens:
        if t in MEGA_CAP_TICKERS:
            return True
    # Company name match (case-insensitive)
    blob_lower = blob.lower()
    for name in MEGA_CAP_NAMES:
        if name in blob_lower:
            return True
    return False

# NYSE holidays. Hardcoded list — extend as years go on.
NYSE_HOLIDAYS = {
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),
    date(2027, 6, 18),   # Juneteenth observed
    date(2027, 7, 5),    # Independence Day observed
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas Day observed
}

# Tickers we pull. Format: (display_name, yfinance_symbol, format_type)
US_EQUITIES = [
    ("S&P 500",     "^GSPC", "price"),
    ("Nasdaq 100",  "^NDX",  "price"),
    ("Dow Jones",   "^DJI",  "price"),
    ("Russell 2000","^RUT",  "price"),
    ("VIX",         "^VIX",  "price"),
]

GLOBAL_INDICES = [
    ("FTSE 100",   "^FTSE",   "price"),
    ("DAX",        "^GDAXI",  "price"),
    ("CAC 40",     "^FCHI",   "price"),
    ("Nikkei 225", "^N225",   "price"),
    ("Hang Seng",  "^HSI",    "price"),
    ("Shanghai",   "000001.SS","price"),
]

# Treasuries: (display_name, yfinance_fallback_symbol, fred_series_id)
TREASURIES = [
    ("US 2Y",  "^IRX",  "DGS2"),
    ("US 5Y",  "^FVX",  "DGS5"),
    ("US 10Y", "^TNX",  "DGS10"),
    ("US 30Y", "^TYX",  "DGS30"),
]

FX_PAIRS = [
    ("DXY",      "DX-Y.NYB", "price"),
    ("EUR/USD",  "EURUSD=X", "fx"),
    ("GBP/USD",  "GBPUSD=X", "fx"),
    ("USD/JPY",  "JPY=X",    "fx2"),
    ("USD/CNH",  "CNH=X",    "fx"),
    ("AUD/USD",  "AUDUSD=X", "fx"),
]

COMMODITIES = [
    ("WTI Crude", "CL=F", "price"),
    ("Brent",     "BZ=F", "price"),
    ("Nat Gas",   "NG=F", "price"),
    ("Gold",      "GC=F", "price"),
    ("Silver",    "SI=F", "price"),
    ("Copper",    "HG=F", "price"),
]

CRYPTO = [
    ("Bitcoin",  "BTC-USD", "crypto"),
    ("Ethereum", "ETH-USD", "price"),
    ("Solana",   "SOL-USD", "price"),
    ("XRP",      "XRP-USD", "price"),
]

# S&P sector ETFs — quick read on where money is moving today
SECTORS = [
    ("Technology",     "XLK",  "price"),
    ("Financials",     "XLF",  "price"),
    ("Energy",         "XLE",  "price"),
    ("Health Care",    "XLV",  "price"),
    ("Industrials",    "XLI",  "price"),
    ("Materials",      "XLB",  "price"),
    ("Utilities",      "XLU",  "price"),
    ("Staples",        "XLP",  "price"),
    ("Discretionary",  "XLY",  "price"),
    ("Real Estate",    "XLRE", "price"),
    ("Comm Services",  "XLC",  "price"),
]

# The names that drive the index
MEGACAPS = [
    ("Apple",        "AAPL",  "price"),
    ("Microsoft",    "MSFT",  "price"),
    ("Nvidia",       "NVDA",  "price"),
    ("Alphabet",     "GOOGL", "price"),
    ("Amazon",       "AMZN",  "price"),
    ("Meta",         "META",  "price"),
    ("Tesla",        "TSLA",  "price"),
    ("Berkshire B",  "BRK-B", "price"),
    ("JPMorgan",     "JPM",   "price"),
    ("UnitedHealth", "UNH",   "price"),
]

TICKER_BAR = [
    ("SPX", "^GSPC"), ("NDX", "^NDX"), ("DJIA", "^DJI"), ("RUT", "^RUT"),
    ("VIX", "^VIX"), ("US10Y", "^TNX"), ("US2Y", "^IRX"),
    ("DXY", "DX-Y.NYB"), ("EURUSD", "EURUSD=X"), ("USDJPY", "JPY=X"),
    ("BTC", "BTC-USD"), ("ETH", "ETH-USD"),
    ("WTI", "CL=F"), ("BRENT", "BZ=F"), ("GOLD", "GC=F"),
    ("SILVER", "SI=F"), ("COPPER", "HG=F"), ("NAT GAS", "NG=F"),
]

RSS_FEEDS = [
    # Official institutions — highest signal, primary sources
    ("FED",            "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("TREASURY",       "https://home.treasury.gov/news/press-releases/feed"),
    ("BIS",            "https://www.bis.org/rss/home.rss"),
    ("IMF",            "https://www.imf.org/en/News/RSS?Language=ENG"),

    # Major global wires
    ("REUTERS",        "https://news.google.com/rss/search?q=site%3Areuters.com+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("AP",             "https://news.google.com/rss/search?q=site%3Aapnews.com+business+OR+economy+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("BLOOMBERG",      "https://news.google.com/rss/search?q=site%3Abloomberg.com+markets+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("WSJ",            "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),

    # UK / Europe
    ("BBC",            "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("GUARDIAN",       "https://www.theguardian.com/business/rss"),
    ("TELEGRAPH",      "https://www.telegraph.co.uk/business/rss.xml"),
    ("DW",             "https://rss.dw.com/rdf/rss-en-bus"),
    ("EURONEWS",       "https://www.euronews.com/rss?level=vertical&name=business"),

    # Asia / Pacific (kept only the mega-cap-relevant Asia outlets — Nikkei
    # covers TSMC/SoftBank/Sony, SCMP covers Alibaba/Tencent/Baidu, Caixin
    # covers China megas. Pruned the Indian / Australian small-cap feeds.)
    ("NIKKEI",         "https://news.google.com/rss/search?q=site%3Aasia.nikkei.com+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("SCMP",           "https://news.google.com/rss/search?q=site%3Ascmp.com+business+OR+economy+when%3A1d&hl=en-US&gl=US&ceid=US%3Aen"),
    ("CAIXIN",         "https://www.caixinglobal.com/rss/"),

    # Middle East (Al Jazeera kept for OPEC / Saudi / Iran energy stories
    # that move oil markets; rest pruned)
    ("AL JAZEERA",     "https://www.aljazeera.com/xml/rss/all.xml"),

    # Americas (non-US) — Canada is mega-cap-adjacent (Brookfield, Shopify, etc.)
    ("GLOBE & MAIL",   "https://www.theglobeandmail.com/business/rss/"),

    # More US — broader business coverage
    ("YAHOO FINANCE",  "https://finance.yahoo.com/news/rssindex"),
    ("FORBES",         "https://www.forbes.com/business/feed/"),
    ("BUSINESS INSIDER","https://www.businessinsider.com/rss"),
    ("FORTUNE",        "https://fortune.com/feed/"),
    ("AXIOS",          "https://api.axios.com/feed/markets"),
    ("THE HILL",       "https://thehill.com/policy/finance/feed/"),
    ("POLITICO US",    "https://www.politico.com/rss/economy.xml"),

    # More Europe
    ("FRANCE 24",      "https://www.france24.com/en/business-economic/rss"),
    ("SKY NEWS",       "https://feeds.skynews.com/feeds/rss/business.xml"),
    ("POLITICO EU",    "https://www.politico.eu/feed/"),
    ("SPIEGEL",        "https://www.spiegel.de/international/index.rss"),

    # More Canada (kept — Canadian outlets cover Shopify, Brookfield, etc.)
    ("FINANCIAL POST", "https://financialpost.com/feed"),

    # Crypto-specific (relevant for digital currency / CBDC themes)
    ("COINDESK",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("COINTELEGRAPH",  "https://cointelegraph.com/rss"),
    ("THE BLOCK",      "https://www.theblock.co/rss.xml"),

    # Specialized markets / commodities / FX
    ("INVESTING",      "https://www.investing.com/rss/news.rss"),
    ("MINING",         "https://www.mining.com/feed/"),
    ("FOREXLIVE",      "https://www.forexlive.com/feed/news"),

    # Macro / geopolitics specialty
    ("FOREIGN POLICY", "https://foreignpolicy.com/feed/"),
    ("PROJECT SYND",   "https://www.project-syndicate.org/rss"),

    # US right-leaning / contrarian
    ("FOX BUSINESS",   "https://moxie.foxbusiness.com/google-publisher/markets.xml"),
    ("NY POST",        "https://nypost.com/business/feed/"),
    ("ZEROHEDGE",      "https://www.zerohedge.com/fullrss.xml"),
    ("EPOCH TIMES",    "https://www.theepochtimes.com/c-business/feed"),
    ("FREE PRESS",     "https://www.thefp.com/feed"),

    # Specialized — gold / energy / FX matter for macro themes
    ("KITCO",          "https://www.kitco.com/rss/KitcoNews.xml"),
    ("OILPRICE",       "https://oilprice.com/rss/main"),

    # US center / left
    ("NPR",            "https://feeds.npr.org/1006/rss.xml"),

    # US retail
    ("CNBC",           "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("MARKETWATCH",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),

    # ── Round 2 expansion: more US national ──
    ("USA TODAY",      "https://rssfeeds.usatoday.com/usatoday-newstopstories"),
    ("LA TIMES",       "https://www.latimes.com/business/rss2.0.xml"),
    ("WAPO",           "https://feeds.washingtonpost.com/rss/business"),
    ("WASH TIMES",     "https://www.washingtontimes.com/rss/headlines/business/"),
    ("DAILY CALLER",   "https://dailycaller.com/section/business/feed/"),
    ("REALCLEAR",      "https://www.realclearmarkets.com/index.xml"),
    ("BENZINGA",       "https://www.benzinga.com/feed"),

    # Think tanks / policy shops (broad spectrum)
    ("AEI",            "https://www.aei.org/feed/"),
    ("CATO",           "https://www.cato.org/rss/recent-content"),
    ("ATLANTIC CNCL",  "https://www.atlanticcouncil.org/feed/"),
    ("CSIS",           "https://www.csis.org/analysis/rss.xml"),

    # Specialized macro / FX (covers Fed / ECB / BoJ moves that affect mega-caps)
    ("DAILYFX",        "https://www.dailyfx.com/feeds/market-news"),
    ("FXSTREET",       "https://www.fxstreet.com/rss/news"),

    # More crypto (CBDC + digital currency themes)
    ("BITCOIN MAG",    "https://bitcoinmagazine.com/feed"),
    ("DECRYPT",        "https://decrypt.co/feed"),

    # ── Phase 1: stock-specific outlets (cashtag-style, retail trader audience) ──
    ("SEEKING ALPHA",  "https://seekingalpha.com/market_currents.xml"),
    ("MARKETBEAT",     "https://www.marketbeat.com/feed/"),
    ("MOTLEY FOOL",    "https://www.fool.com/feeds/index.aspx"),
    ("INVESTORPLACE",  "https://investorplace.com/feed/"),
    ("247 WALL ST",    "https://247wallst.com/feed/"),
    ("IBD",            "https://www.investors.com/feed/"),

    # ── Phase 1: earnings-first wires (companies file here within minutes) ──
    ("PR NEWSWIRE",    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss"),
    ("BUSINESS WIRE",  "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJfXltfWAo5"),
    ("GLOBENEWSWIRE",  "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    # SEC 8-K returns 403 to default UAs — re-enable when we wire a custom UA fetcher.

    # ── Phase 1: more crypto / DeFi ──
    ("CRYPTOSLATE",    "https://cryptoslate.com/feed/"),
    ("BEINCRYPTO",     "https://beincrypto.com/feed/"),
    ("THE DEFIANT",    "https://thedefiant.io/feed/"),
    ("CRYPTO BRIEFING","https://cryptobriefing.com/feed/"),

    # ── Investigative / corruption watchdog (the populist-wolf angle) ──
    ("PROPUBLICA",     "https://www.propublica.org/feeds/propublica/main"),
    ("WALL ST PARADE", "https://wallstreetonparade.com/feed/"),
    ("NAKED CAPITAL",  "https://www.nakedcapitalism.com/feed"),
    ("INTERCEPT",      "https://theintercept.com/feed/?lang=en"),
]

# Used by the auto Loudest Howl picker. Weighted by signal quality (institutional
# credibility + reporting depth), NOT by political slant. Diverse perspectives are
# included on purpose — the picker rewards substance, the wire panel shows breadth.
SOURCE_WEIGHT = {
    # Official primary sources
    "FED":          6, "TREASURY":     6, "BIS":          6, "IMF":          6,
    # Major global wires
    "REUTERS":      5, "AP":           5, "BLOOMBERG":    5, "WSJ":          5,
    # UK / Europe
    "BBC":          4, "GUARDIAN":     4, "TELEGRAPH":    3, "DW":           3,
    "EURONEWS":     3, "FRANCE 24":    3, "SKY NEWS":     3, "POLITICO EU":  3, "SPIEGEL":     3,
    # Asia / Pacific
    "NIKKEI":       4, "SCMP":         3, "CAIXIN":       4, "ASIA TIMES":   3,
    "KOREA HERALD": 3, "ECON TIMES IN":4, "TIMES INDIA":  2, "ABC AUSTRALIA":4, "SMH":          3,
    "CHANNEL NEWS": 4, "JAPAN TIMES":  3, "STRAITS TIMES":3, "MINT INDIA":   3,
    "BIZ STD INDIA":3, "HINDU BIZLINE":3,
    # Middle East
    "AL JAZEERA":   4, "TIMES ISRAEL": 3, "ARAB NEWS":    3, "GULF NEWS":    3, "THE NATIONAL": 3,
    # Russia (independent)
    "MOSCOW TIMES": 3,
    # Africa
    "ALL AFRICA":   3, "BIZNEWS SA":   2,
    # Latin America
    "MERCOPRESS":   2,
    # Americas (non-US)
    "GLOBE & MAIL": 4, "CBC BUSINESS": 4, "FINANCIAL POST":3,
    # Crypto / digital currency
    "COINDESK":     3, "COINTELEGRAPH":2, "THE BLOCK":    3,
    # Specialized markets / commodities / FX
    "INVESTING":    2, "MINING":       3, "FOREXLIVE":    3,
    # Macro / geopolitics specialty
    "FOREIGN POLICY":4, "PROJECT SYND":3,
    # US right-leaning / contrarian
    "FOX BUSINESS": 3, "NY POST":      2, "ZEROHEDGE":    2, "EPOCH TIMES":  2, "FREE PRESS":   3,
    # Specialized commodity / energy
    "KITCO":        3, "OILPRICE":     3,
    # US center / left
    "NPR":          3,
    # US broader business
    "YAHOO FINANCE":2, "FORBES":       2, "BUSINESS INSIDER":2, "FORTUNE":   2,
    "AXIOS":        3, "THE HILL":     2, "POLITICO US":  3,
    # US retail (kept for variety; rarely wins Loudest Howl)
    "CNBC":         1, "MARKETWATCH":  1,
    # Round 2 expansion
    "USA TODAY":    2, "LA TIMES":     3, "WAPO":         4, "WASH TIMES":   2,
    "DAILY CALLER": 1, "REALCLEAR":    2, "BENZINGA":     1,
    "AEI":          3, "CATO":         3, "ATLANTIC CNCL":4, "CSIS":         4,
    "LE MONDE":     4, "SWISSINFO":    3, "ANSA ITALY":   3,
    "BANGKOK POST": 3, "INQUIRER PH":  3, "MAINICHI JP":  3, "YONHAP":       3,
    "HAARETZ":      3, "AL ARABIYA":   3,
    "DAILY MAVERICK":3, "EAST AFRICAN":3, "PREMIUM TIMES":3,
    "BA TIMES":     2, "RIO TIMES":    2,
    "TRADING ECON": 4, "DAILYFX":      3, "FXSTREET":     3, "HELLENIC SHIP":3,
    "BITCOIN MAG":  2, "DECRYPT":      2,
    # Phase 1: stock-specific outlets (high signal for individual tickers)
    "SEEKING ALPHA": 4, "MARKETBEAT":   3, "MOTLEY FOOL":  2,
    "INVESTORPLACE": 2, "247 WALL ST":  2, "IBD":          3,
    # Phase 1: earnings-first PR wires (raw company filings, highest signal for breaking earnings)
    "PR NEWSWIRE":   5, "BUSINESS WIRE":5, "GLOBENEWSWIRE":5,
    # Phase 1: more crypto
    "CRYPTOSLATE":   2, "BEINCRYPTO":   2, "THE DEFIANT":  3, "CRYPTO BRIEFING": 2,
    # Phase 1: Congressional trades from Senate/House Stock Watcher (primary STOCK Act data)
    "CONGRESS":      6,
    # Investigative / corruption watchdog (the populist-wolf angle)
    "PROPUBLICA":    5, "WALL ST PARADE": 4, "NAKED CAPITAL": 3, "INTERCEPT":    3,
}

# Keyword score boosts (lowercase, substring match against title).
# Heavily weights hard-data releases, central bank action, real-world events with
# financial impact, and macro/monetary-system shifts.
KEYWORD_BOOSTS = {
    # Central banks / rate decisions
    "fomc": 5, "fed ": 3, "powell": 3, "ecb": 4, "lagarde": 3, "boj": 4, "ueda": 3,
    "boe": 3, "pboc": 4, "rba": 3, "snb": 2, "rate cut": 4, "rate hike": 4,
    "rate decision": 4, "interest rate": 3, "monetary policy": 3, "qe": 2, "qt": 2,
    # Hard-data releases
    "cpi": 4, "ppi": 3, "pce": 4, "core inflation": 3, "inflation": 2,
    "gdp": 3, "jobs report": 4, "nonfarm": 3, "payrolls": 4, "unemployment": 3,
    "retail sales": 2, "ism": 2, "pmi": 2, "consumer confidence": 2,
    # Trade / sanctions / geopolitics with market impact
    "tariff": 4, "sanction": 3, "embargo": 3, "trade war": 4, "export control": 3,
    "war": 3, "invasion": 3, "ceasefire": 2, "missile": 2, "strike": 2,
    # Specific shipping / energy chokepoints
    "strait of hormuz": 5, "hormuz": 4, "red sea": 4, "houthi": 4, "suez": 3,
    "panama canal": 3, "shipping": 1,
    # Energy / commodities
    "opec": 4, "opec+": 4, "saudi": 2, "crude": 2, "natural gas": 2, "lng": 2,
    "oil price": 3, "gas price": 2,
    # Crisis / systemic
    "crisis": 3, "default": 4, "bailout": 4, "downgrade": 3, "credit rating": 3,
    "bank failure": 4, "liquidity": 2, "contagion": 3, "systemic": 3,
    # Monetary-system shifts (CBDCs, de-dollarization, reserve regime)
    "cbdc": 5, "central bank digital": 5, "digital dollar": 5, "digital euro": 4,
    "digital yuan": 4, "e-cny": 4, "digital pound": 3,
    "brics": 4, "de-dollarization": 5, "dedollarization": 5, "petroyuan": 4,
    "reserve currency": 4, "dollar hegemony": 4, "dollar dominance": 3,
    "imf": 3, "sdr": 4, "special drawing rights": 5, "world bank": 2, "bis ": 3,
    "mbridge": 5, "swift alternative": 4, "cross-border payment": 4,
    "wef": 3, "davos": 3, "g7": 2, "g20": 2,
    # Digital-ID / surveillance-finance intersection
    "digital id": 4, "digital identity": 4, "biometric": 2, "social credit": 3,
    # Corporate / M&A
    "earnings": 1, "guidance": 1, "ipo": 2, "merger": 2, "acquisition": 2,
    "buyout": 2, "lbo": 2, "spinoff": 2, "bankruptcy": 3,
    # Regions
    "china": 1, "russia": 2, "ukraine": 2, "iran": 3, "israel": 2, "gaza": 2,
    "taiwan": 2, "north korea": 2, "venezuela": 1, "argentina": 1,
    # Magnitude / movement
    "trillion": 2, "billion": 1, "record high": 2, "record low": 2, "all-time": 2,
    "selloff": 2, "crash": 3, "rout": 2,
}

# Penalize click-bait + pundit content + speculation framing
# Specific phrases only — broad ones like "here's what" caught legitimate
# earnings-preview reporting ("Here's what Wall Street expects").
KEYWORD_PENALTIES = {
    "stocks to buy": -5, "stocks to watch": -4, "watchlist": -4, "best stocks": -4,
    "what to know": -4, "things to know": -4, "what to watch": -3,
    "wall street loves": -4, "10 things": -4, "5 things": -4, "3 things": -3,
    "cramer": -3, "jim cramer": -3,
    "should you": -3, "is it time": -3,
}

# Sources whose feed is finance/markets-focused — items always pass the
# relevance gate even without explicit keyword matches.
FINANCIAL_SOURCES = {
    # Institutional
    "FED", "TREASURY", "BIS", "IMF",
    # Markets-only / business-section-only feeds
    "BLOOMBERG", "WSJ", "MARKETWATCH",
    "AP",
    "BBC", "GUARDIAN", "TELEGRAPH", "DW", "EURONEWS", "FRANCE 24", "SKY NEWS",
    "SMH", "ECON TIMES IN", "MINT INDIA", "BIZ STD INDIA", "HINDU BIZLINE",
    "JAPAN TIMES", "STRAITS TIMES", "CHANNEL NEWS",
    "GLOBE & MAIL", "CBC BUSINESS", "FINANCIAL POST",
    "GULF NEWS", "THE NATIONAL",
    "ALL AFRICA", "BIZNEWS SA",
    "CAIXIN", "FOX BUSINESS", "NY POST", "EPOCH TIMES",
    "YAHOO FINANCE", "FORBES", "BUSINESS INSIDER", "FORTUNE",
    "INVESTING", "MINING", "FOREXLIVE",
    "COINDESK", "COINTELEGRAPH", "THE BLOCK",
    "AXIOS", "THE HILL", "POLITICO US", "POLITICO EU", "FREE PRESS",
    # Round 2: business-section feeds
    "LA TIMES", "WAPO", "WASH TIMES", "DAILY CALLER", "REALCLEAR", "BENZINGA",
    "ANSA ITALY", "BANGKOK POST", "INQUIRER PH", "MAINICHI JP", "YONHAP",
    "HAARETZ", "EAST AFRICAN", "TRADING ECON", "DAILYFX", "FXSTREET",
    "HELLENIC SHIP", "BITCOIN MAG", "DECRYPT",
    # Commodities/macro focused
    "KITCO", "OILPRICE", "ZEROHEDGE",
}

# Region classification for the per-region wire cap and regional desk panels.
SOURCE_REGION = {
    # United States
    "FED": "US", "TREASURY": "US", "IMF": "US",
    "BLOOMBERG": "US", "WSJ": "US", "AP": "US", "MARKETWATCH": "US",
    "CNBC": "US", "NPR": "US", "NY POST": "US", "FOX BUSINESS": "US",
    "ZEROHEDGE": "US", "EPOCH TIMES": "US", "KITCO": "US", "OILPRICE": "US",
    "FREE PRESS": "US", "YAHOO FINANCE": "US", "FORBES": "US",
    "BUSINESS INSIDER": "US", "FORTUNE": "US", "AXIOS": "US",
    "THE HILL": "US", "POLITICO US": "US", "USA TODAY": "US",
    "LA TIMES": "US", "WAPO": "US", "WASH TIMES": "US", "DAILY CALLER": "US",
    "REALCLEAR": "US", "BENZINGA": "US", "AEI": "US", "CATO": "US",
    "ATLANTIC CNCL": "US", "CSIS": "US", "INVESTING": "US",
    "BITCOIN MAG": "US", "DECRYPT": "US", "COINDESK": "US",
    "COINTELEGRAPH": "US", "THE BLOCK": "US", "FOREXLIVE": "US",
    "FOREIGN POLICY": "US", "DAILYFX": "US",
    # Europe (incl. UK + international institutions HQ'd there)
    "BIS": "EU", "REUTERS": "EU", "BBC": "EU", "GUARDIAN": "EU",
    "TELEGRAPH": "EU", "DW": "EU", "EURONEWS": "EU", "FRANCE 24": "EU",
    "SKY NEWS": "EU", "POLITICO EU": "EU", "SPIEGEL": "EU", "LE MONDE": "EU",
    "SWISSINFO": "EU", "ANSA ITALY": "EU", "MOSCOW TIMES": "EU",
    "PROJECT SYND": "EU", "TRADING ECON": "EU", "FXSTREET": "EU",
    "HELLENIC SHIP": "EU",
    # Asia / Pacific
    "NIKKEI": "ASIA", "SCMP": "ASIA", "CAIXIN": "ASIA", "ASIA TIMES": "ASIA",
    "KOREA HERALD": "ASIA", "ECON TIMES IN": "ASIA", "TIMES INDIA": "ASIA",
    "ABC AUSTRALIA": "ASIA", "SMH": "ASIA", "CHANNEL NEWS": "ASIA",
    "JAPAN TIMES": "ASIA", "STRAITS TIMES": "ASIA", "MINT INDIA": "ASIA",
    "BIZ STD INDIA": "ASIA", "HINDU BIZLINE": "ASIA", "BANGKOK POST": "ASIA",
    "INQUIRER PH": "ASIA", "MAINICHI JP": "ASIA", "YONHAP": "ASIA",
    # Middle East
    "AL JAZEERA": "ME", "TIMES ISRAEL": "ME", "ARAB NEWS": "ME",
    "GULF NEWS": "ME", "THE NATIONAL": "ME", "HAARETZ": "ME", "AL ARABIYA": "ME",
    # Africa
    "ALL AFRICA": "AF", "BIZNEWS SA": "AF", "DAILY MAVERICK": "AF",
    "EAST AFRICAN": "AF", "PREMIUM TIMES": "AF",
    # Americas (non-US: Canada + Latin America)
    "GLOBE & MAIL": "AMERICAS", "CBC BUSINESS": "AMERICAS",
    "FINANCIAL POST": "AMERICAS", "MERCOPRESS": "AMERICAS",
    "BA TIMES": "AMERICAS", "RIO TIMES": "AMERICAS",
}

REGION_LABEL = {
    "US": "United States",
    "EU": "Europe",
    "ASIA": "Asia / Pacific",
    "ME": "Middle East",
    "AF": "Africa",
    "AMERICAS": "Americas (Non-US)",
}

# Mixed-content sources. Items must have at least one financial-keyword hit
# (in title or summary) to make it into the wire panel — this filters out
# general-news drift (cartel violence, lifestyle, sports, etc.).
MIXED_CONTENT_SOURCES = {
    "REUTERS",
    "AL JAZEERA", "ARAB NEWS", "TIMES ISRAEL", "MOSCOW TIMES", "AL ARABIYA",
    "NIKKEI", "SCMP", "ASIA TIMES", "KOREA HERALD", "TIMES INDIA",
    "ABC AUSTRALIA",
    "MERCOPRESS", "BA TIMES", "RIO TIMES",
    "SPIEGEL", "LE MONDE", "SWISSINFO",
    "FOREIGN POLICY", "PROJECT SYND",
    "AEI", "CATO", "ATLANTIC CNCL", "CSIS",
    "DAILY MAVERICK", "PREMIUM TIMES",
    "USA TODAY",
    "NPR", "CNBC",
}

# ----------------------------------------------------------------------------
# DATA FETCH
# ----------------------------------------------------------------------------

def fetch_quote(symbol, retries=1):
    """Return (last, change, pct_change) or (None, None, None) on failure. Retries once on transient errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(symbol)
            last = t.fast_info.get("last_price")
            prev = t.fast_info.get("previous_close")
            if last is None or prev is None or prev == 0:
                hist = t.history(period="2d")
                if len(hist) >= 2:
                    last = float(hist["Close"].iloc[-1])
                    prev = float(hist["Close"].iloc[-2])
                elif len(hist) == 1:
                    last = float(hist["Close"].iloc[-1])
                    prev = float(hist["Open"].iloc[-1])
                else:
                    raise RuntimeError("no history")
            chg = last - prev
            pct = (chg / prev) * 100 if prev else 0
            return float(last), float(chg), float(pct)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1)
    print(f"  ! {symbol}: {last_err}", file=sys.stderr)
    return None, None, None


def fetch_article_summary(url, timeout=8):
    """Fetch the article URL and pull og:description / meta description.
    Used to populate the Loudest Howl body when the RSS summary is empty
    or just repeats the title (common with Google News-sourced items).
    Returns the description string or None on failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0; +https://howlstreet.github.io)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read(400_000).decode(charset, errors="replace")
    except Exception as e:
        print(f"    ! summary fetch failed: {e}", file=sys.stderr)
        return None

    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:description["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']twitter:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
    )
    for pattern in patterns:
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            desc = html.unescape(m.group(1)).strip()
            if desc and len(desc) > 30:
                return desc
    return None


def fetch_treasury_fred(series_id):
    """Fetch latest two observations from FRED. Returns (yield_pct, change_pct) or (None, None).
    Requires FRED_API_KEY env var; returns (None, None) silently if not set."""
    if not FRED_API_KEY:
        return None, None
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&file_type=json&sort_order=desc&limit=10"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        valid = []
        for obs in data.get("observations", []):
            v = obs.get("value", ".")
            if v and v != ".":
                try:
                    valid.append(float(v))
                except ValueError:
                    pass
            if len(valid) >= 2:
                break
        if not valid:
            return None, None
        if len(valid) < 2:
            return valid[0], 0.0
        last, prev = valid[0], valid[1]
        return last, last - prev
    except Exception as e:
        print(f"  ! FRED {series_id}: {e}", file=sys.stderr)
        return None, None


def fmt_price(v, kind="price"):
    if v is None:
        return "—"
    if kind == "yield":
        return f"{v:.3f}%"
    if kind == "fx":
        return f"{v:.4f}"
    if kind == "fx2":
        return f"{v:.2f}"
    if kind == "crypto":
        return f"{v:,.0f}"
    if v >= 1000:
        return f"{v:,.2f}"
    return f"{v:.2f}"


def fmt_pct(p):
    if p is None:
        return ("—", "")
    cls = "up" if p >= 0 else "down"
    sign = "+" if p >= 0 else ""
    return (f"{sign}{p:.2f}%", cls)


def fmt_chg(c, kind="price"):
    if c is None:
        return ("—", "")
    cls = "up" if c >= 0 else "down"
    sign = "+" if c >= 0 else ""
    if kind == "yield":
        return (f"{sign}{c*100:.1f}bp", cls)
    if abs(c) < 1:
        return (f"{sign}{c:.3f}", cls)
    return (f"{sign}{c:,.2f}", cls)


# ----------------------------------------------------------------------------
# HTML BUILDERS
# ----------------------------------------------------------------------------

def build_table_row(name, last, chg, pct, kind):
    last_s = fmt_price(last, kind)
    chg_s, chg_cls = fmt_chg(chg, kind)
    pct_s, pct_cls = fmt_pct(pct)
    pill = f'<span class="pct-pill {pct_cls}">{pct_s}</span>' if pct_cls else pct_s
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last_s}</td>'
        f'<td class="r {chg_cls}">{chg_s}</td>'
        f'<td class="r">{pill}</td></tr>'
    )


def build_table_row_3col(name, last, pct, kind):
    last_s = fmt_price(last, kind)
    pct_s, pct_cls = fmt_pct(pct)
    pill = f'<span class="pct-pill {pct_cls}">{pct_s}</span>' if pct_cls else pct_s
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last_s}</td>'
        f'<td class="r">{pill}</td></tr>'
    )


def build_yield_row(name, last, chg):
    if last is None:
        return f'<tr><td class="sym-cell">{html.escape(name)}</td><td class="r">—</td><td class="r">—</td></tr>'
    chg_bp_val = (chg or 0) * 100
    chg_cls = "up" if chg_bp_val >= 0 else "down"
    chg_sign = "+" if chg_bp_val >= 0 else ""
    return (
        f'<tr><td class="sym-cell">{html.escape(name)}</td>'
        f'<td class="r">{last:.3f}%</td>'
        f'<td class="r {chg_cls}">{chg_sign}{chg_bp_val:.1f}</td></tr>'
    )


def build_ticker_item(label, symbol):
    last, chg, pct = fetch_quote(symbol)
    if last is None:
        return ""
    if symbol in ("^TNX", "^FVX", "^IRX", "^TYX"):
        last_str = f"{last:.3f}%"
        chg_bp = (chg or 0) * 100
        cls = "up" if chg_bp >= 0 else "down"
        sign = "+" if chg_bp >= 0 else ""
        chg_str = f"{sign}{chg_bp:.1f}bp"
    else:
        last_str = fmt_price(last, "crypto" if "BTC" in symbol or "ETH" in symbol else "price")
        pct_str, cls = fmt_pct(pct)
        chg_str = pct_str
    return (
        f'<span class="ticker-item">'
        f'<span class="sym">{html.escape(label)}</span>'
        f'<span class="px">{last_str}</span>'
        f'<span class="{cls}">{chg_str}</span>'
        f'</span>'
    )


def _exchange_open(tz, open_hm, close_hm, lunch=None, holidays=None):
    """True if the given exchange is currently within its regular session."""
    local = datetime.now(timezone.utc).astimezone(tz)
    if local.weekday() >= 5:
        return False
    if holidays and local.date() in holidays:
        return False
    hm = (local.hour, local.minute)
    if not (open_hm <= hm < close_hm):
        return False
    if lunch and lunch[0] <= hm < lunch[1]:
        return False
    return True


def global_indices_status_label():
    """Compose a label naming which non-US regions are currently trading.
    Examples: 'CLOSED', 'EU LIVE', 'JP·HK·CN LIVE'.
    Holiday lists not maintained for non-US exchanges — weekend-aware only."""
    eu_open = (
        _exchange_open(LONDON, (8, 0), (16, 30))                                # LSE / FTSE
        or _exchange_open(BERLIN, (9, 0), (17, 30))                             # XETRA / DAX
        or _exchange_open(PARIS,  (9, 0), (17, 30))                             # Euronext / CAC
    )
    jp_open = _exchange_open(TOKYO, (9, 0), (15, 0), lunch=((11, 30), (12, 30)))  # TSE / Nikkei
    cn_hk_open = (
        _exchange_open(HONG_KONG, (9, 30), (16, 0), lunch=((12, 0), (13, 0)))   # HKEX / Hang Seng
        or _exchange_open(SHANGHAI, (9, 30), (15, 0), lunch=((11, 30), (13, 0)))  # SSE / Shanghai
    )

    open_regions = []
    if eu_open:
        open_regions.append("EU")
    if jp_open:
        open_regions.append("JP")
    if cn_hk_open:
        open_regions.append("HK·CN")

    if not open_regions:
        return "CLOSED"
    return "·".join(open_regions) + " LIVE"


def is_us_treasury_open():
    """US Treasury cash market — SIFMA recommended hours: 8am-5pm ET, Mon-Fri.
    Honors NYSE holidays as a proxy (Treasury usually follows NYSE closings)."""
    return _exchange_open(NY, (8, 0), (17, 0), holidays=NYSE_HOLIDAYS)


def is_any_major_market_open():
    """True if any of NYSE / LSE / TSE is currently in regular session.
    Drives the header LIVE/STANDBY indicator."""
    return (
        _exchange_open(NY,     (9, 30), (16, 0), holidays=NYSE_HOLIDAYS)
        or _exchange_open(LONDON, (8, 0),  (16, 30))
        or _exchange_open(TOKYO,  (9, 0),  (15, 0), lunch=((11, 30), (12, 30)))
    )


def build_live_indicator():
    """LIVE pulse when a major market is open, STANDBY (dim, no animation) otherwise."""
    if is_any_major_market_open():
        return '<span class="live-dot">LIVE</span>'
    return '<span class="live-dot standby">STANDBY</span>'


def _nyse_status():
    """Granular NYSE state: PRE (4-9:30 ET), OPEN (regular), POST (16-20 ET),
    or CLOSED. Pre-market and after-hours are real trading windows that
    matter to traders watching the tape — calling them "CLOSED" hides
    activity that's actually happening."""
    if _exchange_open(NY, (9, 30), (16, 0), holidays=NYSE_HOLIDAYS):
        return "OPEN"
    if _exchange_open(NY, (4, 0), (9, 30), holidays=NYSE_HOLIDAYS):
        return "PRE"
    if _exchange_open(NY, (16, 0), (20, 0), holidays=NYSE_HOLIDAYS):
        return "POST"
    return "CLOSED"


def build_market_sessions():
    """NYSE / LSE / TSE state indicator for the header. NYSE reports PRE /
    OPEN / POST / CLOSED so pre- and after-market activity reads as live;
    LSE and TSE stay simple OPEN / CLOSED."""
    nyse = _nyse_status()
    lse_open = _exchange_open(LONDON, (8, 0), (16, 30))
    tse_open = _exchange_open(TOKYO, (9, 0), (15, 0), lunch=((11, 30), (12, 30)))

    sessions = [
        ("NYSE", nyse, nyse != "CLOSED"),
        ("LSE", "OPEN" if lse_open else "CLOSED", lse_open),
        ("TSE", "OPEN" if tse_open else "CLOSED", tse_open),
    ]
    parts = []
    for name, label, is_active in sessions:
        cls = "up" if is_active else "down"
        parts.append(f'<span>{name}: <span class="{cls}">{label}</span></span>')
    return "\n      ".join(parts)


def _clean_title(title, source):
    """Strip ' - SourceName' / ' | SourceName' / ' — SourceName' suffixes that
    Google News appends, and decode HTML entities."""
    title = html.unescape(title).strip()
    # Common suffix patterns
    for sep in (" - ", " — ", " | "):
        idx = title.rfind(sep)
        if idx > 0 and idx >= len(title) - 60:
            tail = title[idx + len(sep):].strip()
            # If the tail looks like a publisher/byline (short-ish, mostly letters), strip it
            if 0 < len(tail) <= 40 and re.match(r"^[A-Za-z0-9 .&'\-]+$", tail):
                title = title[:idx].strip()
                break
    return title


def _clean_summary(raw):
    """Decode entities, strip HTML tags, collapse whitespace."""
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fetch_one_feed(source_url):
    """Worker for ThreadPoolExecutor. Returns a list of item dicts."""
    source, url = source_url
    epoch_min = datetime(2000, 1, 1, tzinfo=NY)
    out = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:6]:
            title = _clean_title(entry.get("title") or "", source)
            if not title:
                continue
            summary = _clean_summary(entry.get("summary") or "")
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                ts = datetime(*published[:6], tzinfo=timezone.utc).astimezone(NY)
            else:
                ts = epoch_min
            out.append({
                "source": source,
                "title": title,
                "summary": summary,
                "link": entry.get("link", "#"),
                "ts": ts,
            })
    except Exception as e:
        print(f"  ! RSS {source}: {e}", file=sys.stderr)
    return out


_TRENDS_CACHE = {"set": None, "fetched_at": None}


def fetch_trending_topics():
    """Scrape current X (Twitter) trending topics from trends24.in (US).
    Returns a set of normalized lowercase tokens. Defensive — any HTTP or
    parse failure returns an empty set so the rest of the pipeline keeps
    working. Cached for the lifetime of the python process so we don't
    refetch within a single update run.

    X's official trends API is locked behind their $5k/mo Pro tier;
    trends24 publishes a free public mirror with ~30 min lag, which is
    plenty fresh for our 30-min cron cadence."""
    if _TRENDS_CACHE["set"] is not None:
        return _TRENDS_CACHE["set"]
    try:
        req = urllib.request.Request(
            "https://trends24.in/united-states/",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            page = resp.read(400_000).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! trending topics fetch failed: {e}", file=sys.stderr)
        _TRENDS_CACHE["set"] = set()
        return _TRENDS_CACHE["set"]

    raw = re.findall(
        r'<a[^>]+href="https://twitter\.com/search\?q=[^"]*"[^>]*>([^<]+)</a>',
        page,
    )
    out = set()
    for t in raw[:60]:  # current-window trends, ignore older hourly snapshots
        clean = t.strip().lstrip("#").lower()
        if 3 <= len(clean) <= 40 and not clean.isdigit():
            out.add(clean)
    print(f"  trending now ({len(out)} topics): {', '.join(sorted(out)[:8])}…")
    _TRENDS_CACHE["set"] = out
    return out


def fetch_all_headlines():
    """Fetch every RSS feed in parallel and return a flat list of items.
    Each item: {source, title, summary, link, ts (NY tz)}."""
    # Pre-fetch X trends so they're available to score_item during ranking.
    fetch_trending_topics()
    items = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        for chunk in ex.map(_fetch_one_feed, RSS_FEEDS):
            items.extend(chunk)
    # Congressional trades fetcher exists (fetch_congress_trades) but the
    # public Stock Watcher S3 dumps went 403. Re-enable when we wire a
    # working data source (Capitol Trades scrape, Quiver Quant, or a
    # successor project to housestockwatcher.com).
    return items


# Senate / House Stock Watcher publish public JSON of every Congressional
# stock transaction (filed under the STOCK Act). Surfacing these on the wire
# gives @HowlStreet "Pelosi tracker" style insider-trading transparency.
_CONGRESS_FEEDS = (
    ("HOUSE", "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"),
    ("SENATE", "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"),
)


def _amount_label(raw):
    """Stock Act discloses amounts as bands ('$1,001 - $15,000'). Normalize
    to a clean label or fall back to the raw value."""
    if not raw:
        return ""
    s = str(raw).replace("$", "").replace(",", "").strip()
    return f"${str(raw).strip()}" if "$" not in str(raw) else str(raw).strip()


def _fetch_one_congress(source_url):
    """Worker: pull the JSON dump, parse the last ~30 days of trades into
    wire-item dicts. Defensive — bad data, schema change, or HTTP error
    silently returns []."""
    source, url = source_url
    out = []
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  ! Congress {source}: {e}", file=sys.stderr)
        return out

    if not isinstance(data, list):
        return out

    cutoff = datetime.now(NY) - timedelta(days=14)
    for row in data:
        if not isinstance(row, dict):
            continue
        person = row.get("senator") or row.get("representative") or row.get("name") or ""
        ticker = (row.get("ticker") or "").strip()
        asset = row.get("asset_description") or row.get("asset") or ticker
        ttype = (row.get("type") or row.get("transaction_type") or "").lower()
        amount = row.get("amount") or ""
        date_str = row.get("transaction_date") or row.get("disclosure_date") or ""
        link = row.get("ptr_link") or row.get("link") or ""

        if not person or not (ticker or asset):
            continue

        try:
            ts = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=NY)
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue

        verb = "bought" if "purchase" in ttype or "buy" in ttype else (
               "sold" if "sale" in ttype or "sell" in ttype else ttype or "traded")
        ticker_tag = f"${ticker.upper()}" if ticker and ticker not in ("--", "N/A") else asset
        amt = _amount_label(amount)
        chamber = "Sen." if source == "SENATE" else "Rep."
        title = f"{chamber} {person} {verb} {ticker_tag}" + (f" ({amt})" if amt else "")
        summary = (
            f"Disclosed Congressional trade. Person: {person}. "
            f"Asset: {asset}. Type: {ttype or 'unspecified'}. "
            f"Amount: {amt or 'undisclosed'}. Filed: {date_str[:10]}."
        )
        out.append({
            "source": "CONGRESS",
            "title": title,
            "summary": summary,
            "link": link or "https://efdsearch.senate.gov/search/",
            "ts": ts,
        })

    # Sort newest first, cap to 25 to avoid drowning the wire
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:25]


def fetch_congress_trades():
    """Combined House + Senate trades from the Stock Watcher S3 dumps."""
    out = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        for chunk in ex.map(_fetch_one_congress, _CONGRESS_FEEDS):
            out.extend(chunk)
    if out:
        print(f"  fetched {len(out)} Congressional trades")
    return out


def _kw_match(text, keyword):
    """Word-boundary match so 'war' doesn't hit 'ward'.
    Multi-word keywords ('strait of hormuz') still match — \b handles each end."""
    return re.search(r'\b' + re.escape(keyword) + r'\b', text) is not None


def is_financially_relevant(item):
    """Strict gate: title + RSS summary must contain at least one concrete
    finance signal — a price, percentage, market term, central bank,
    rating agency, ticker, or finance acronym. Pure geopolitical, sports,
    entertainment, or general news without a market hook gets dropped,
    even when the source is a 'financial' outlet.

    Howl Street is a finance terminal; if the article doesn't tie to
    markets, it doesn't belong on the wire."""
    text = (item.get("title", "") or "") + " " + (item.get("summary", "") or "")
    return _has_financial_signal(text)


def score_item(item):
    """Score a wire item for Loudest Howl candidacy. Higher = more newsworthy."""
    score = SOURCE_WEIGHT.get(item["source"], 1)

    # Recency — lose 1 point per hour, capped at -12
    now = datetime.now(NY)
    age_hours = max(0, (now - item["ts"]).total_seconds() / 3600)
    score -= min(age_hours, 12)

    title_lower = item["title"].lower()
    for kw, bonus in KEYWORD_BOOSTS.items():
        if _kw_match(title_lower, kw):
            score += bonus
    for phrase, penalty in KEYWORD_PENALTIES.items():
        if _kw_match(title_lower, phrase):
            score += penalty

    # X-trends boost: if the title contains a topic that's currently trending
    # on X, bump the score so timely takes rise to the top. Only applied to
    # items that already passed the finance-relevance gate, so we never push
    # off-topic political trends — only the financial-angle stories on a
    # trending topic (e.g., when "OPEC" trends, oil stories rise).
    trends = _TRENDS_CACHE.get("set")
    if trends:
        for trend in trends:
            if trend and _kw_match(title_lower, trend):
                score += 3
                break

    return score


# Minimum score for an auto-picked Loudest Howl. Below this, hero stays hidden.
HERO_MIN_SCORE = 4.0


def pick_top_story(items):
    """Highest-scoring recent (last 24h) item that clears the quality
    threshold AND has a concrete finance signal AND mentions a mega-cap
    ticker / company / executive. Returns the item dict or None.

    Triple gate enforced here so the Loudest Howl on the site and the
    Howl of the Day in the queue can NEVER be a political poll, regional
    small-cap, or off-topic piece. Names everyone on FinTwit recognizes
    only — that's the whole point of the brand."""
    if not items:
        return None
    now = datetime.now(NY)
    recent = [i for i in items
              if (now - i["ts"]).total_seconds() < 24 * 3600
              and is_financially_relevant(i)
              and _matches_megacap(i)]
    if not recent:
        return None
    scored = sorted(((score_item(i), i) for i in recent), key=lambda x: x[0], reverse=True)
    top_score, top = scored[0]
    if top_score < HERO_MIN_SCORE:
        return None
    return top


def _pick_finance_relevant_hero(items):
    """Among the top-scored candidates, return the first whose RSS summary
    or article body has a concrete finance signal (price, percentage, market
    term, central bank, etc.). Returns None if the top 5 candidates all
    fail — caller should fall back to pick_top_story so the X account
    still gets a flagship even on a slow news day."""
    if not items:
        return None
    now = datetime.now(NY)
    recent = [i for i in items if (now - i["ts"]).total_seconds() < 24 * 3600]
    if not recent:
        return None
    scored = sorted(((score_item(i), i) for i in recent), key=lambda x: x[0], reverse=True)
    candidates = [i for s, i in scored[:5] if s >= HERO_MIN_SCORE]

    for cand in candidates:
        rss = _clean_summary(cand.get("summary", ""))
        if rss and _has_financial_signal(rss):
            print(f"    finance-relevant hero candidate (RSS): {cand['source']}")
            return cand
        briefing = fetch_article_briefing(cand["link"], cand["title"])
        if briefing and _has_financial_signal(briefing):
            print(f"    finance-relevant hero candidate (body): {cand['source']}")
            return cand
        print(f"    skipped {cand['source']}: no finance signal in RSS or body")
    return None


def pick_locked_hero(items):
    """Howl of the Day for the X feed/queue — locked once per NY-calendar
    day so @HowlStreet has one consistent flagship story. Subsequent builds
    in the same day return the same hero. The site's LOUDEST HOWL stays on
    pick_top_story() = live rotation.

    State persisted to hero_lock.json so the lock survives across cron runs.
    Returns None if nothing has cleared the quality threshold yet today."""
    today = datetime.now(NY).strftime("%Y-%m-%d")

    if HERO_LOCK_PATH.exists():
        try:
            data = json.loads(HERO_LOCK_PATH.read_text(encoding="utf-8"))
            if data.get("lock_date") == today and data.get("hero_link"):
                stored_link = data["hero_link"]
                # Prefer current pool entry — has a fresh ts and the latest
                # summary if the source updated the article.
                for it in items:
                    if it["link"] == stored_link:
                        return it
                # Story rolled out of the 24h pool. Reconstruct from saved data.
                stored = data.get("hero_data") or {}
                if stored.get("link") and stored.get("title"):
                    ts_iso = stored.get("ts")
                    try:
                        ts = datetime.fromisoformat(ts_iso) if ts_iso else datetime.now(NY)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=NY)
                    except (TypeError, ValueError):
                        ts = datetime.now(NY)
                    return {
                        "title": stored["title"],
                        "link": stored["link"],
                        "source": stored.get("source", ""),
                        "summary": stored.get("summary", ""),
                        "ts": ts,
                    }
        except Exception as e:
            print(f"  ! hero lock read failed: {e}", file=sys.stderr)

    # No valid lock for today — pick fresh, preferring stories with a clear
    # finance angle so we don't lock in a "big headline" that doesn't actually
    # tie to markets.
    new_hero = _pick_finance_relevant_hero(items) or pick_top_story(items)
    if new_hero:
        try:
            ts = new_hero["ts"]
            ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            payload = {
                "lock_date": today,
                "hero_link": new_hero["link"],
                "hero_data": {
                    "title": new_hero["title"],
                    "link": new_hero["link"],
                    "source": new_hero["source"],
                    "summary": new_hero.get("summary", ""),
                    "ts": ts_iso,
                },
            }
            HERO_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"    locked Howl of the Day for {today}: {new_hero['source']}")
        except Exception as e:
            print(f"  ! hero lock save failed: {e}", file=sys.stderr)
    return new_hero


def build_hero_auto(items):
    """Render the picked top story as hero HTML. Empty string if no winner."""
    top = pick_top_story(items)
    if not top:
        return ""

    # Summary already cleaned in fetch_all_headlines. Strip the source name
    # if Google News duplicated it at the end.
    summary_text = top["summary"]
    src_lower = top["source"].lower()
    while summary_text.lower().endswith(src_lower):
        summary_text = summary_text[: -len(src_lower)].rstrip(" ,.;:|—-")
    # If the summary is just the title repeated (Google News pattern), discard it.
    if summary_text.strip().lower() == top["title"].strip().lower():
        summary_text = ""
    # If still nothing useful, OR the summary lacks any finance signal, fetch
    # a body briefing — that fetcher prefers sentences with prices/percentages/
    # market terms so the hero's subtext makes the finance angle clear.
    if not summary_text or not _has_financial_signal(summary_text):
        fetched = fetch_article_briefing(top["link"], top["title"])
        if fetched:
            summary_text = _clean_summary(fetched)
    # Strip wire datelines / editorial prefixes too so the subtext leads clean.
    if summary_text:
        summary_text = _clean_briefing_lead(summary_text)
    # Trim to first sentence — terminal-feel, scannable.
    if summary_text:
        m = re.match(r"^(.{30,}?[.!?])(?:\s|$)", summary_text)
        if m:
            summary_text = m.group(1)
        elif len(summary_text) > 200:
            summary_text = summary_text[:197].rstrip(" ,.;:") + "…"

    label = (
        "LOUDEST HOWL · "
        + top["source"]
        + " · "
        + top["ts"].strftime("%b %d %H:%M ")
        + top["ts"].tzname()
    )

    body_tag = f'  <p class="hero-body">{html.escape(summary_text)}</p>\n' if summary_text else ""

    return (
        f'<a class="hero-link" href="{html.escape(top["link"])}" target="_blank" rel="noopener">\n'
        '<section class="hero">\n'
        f'  <div class="hero-label">▸ {html.escape(label)} <span class="hero-arrow">↗</span></div>\n'
        f'  <h2 class="hero-headline">{html.escape(top["title"])}</h2>\n'
        f'{body_tag}'
        '</section>\n'
        '</a>'
    )


def build_hero_from_md():
    """Read hero.md and render the hero <section>. Empty string if missing or has no headline.

    Format:
        LABEL: LOUDEST HOWL          (optional — auto-generated if missing)
        LINK:  https://example.com   (optional — wraps hero in clickable link)
        # Headline goes here

        Body paragraph goes here. Markdown *emphasis* becomes <em>.
    """
    if not HERO_PATH.exists():
        return ""
    text = HERO_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return ""

    label = None
    link = None
    headline = None
    body_lines = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            if line.startswith("LABEL:"):
                label = line[len("LABEL:"):].strip()
                continue
            if line.startswith("LINK:"):
                link = line[len("LINK:"):].strip()
                continue
            if line.lstrip().startswith("#"):
                headline = line.lstrip("# ").strip()
                in_body = True
                continue
        else:
            body_lines.append(line)

    if not headline:
        return ""

    body = "\n".join(body_lines).strip()
    body_html = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", html.escape(body))
    headline_html = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", html.escape(headline))

    if not label:
        now_ny = datetime.now(NY)
        label = "LOUDEST HOWL · " + now_ny.strftime("%b %d %H:%M ") + now_ny.tzname()

    label_html = f'<div class="hero-label">▸ {html.escape(label)}'
    if link:
        label_html += ' <span class="hero-arrow">↗</span>'
    label_html += '</div>'

    inner = (
        f'  {label_html}\n'
        f'  <h2 class="hero-headline">{headline_html}</h2>\n'
        f'  <p class="hero-body">{body_html}</p>\n'
    )

    if link:
        return (
            f'<a class="hero-link" href="{html.escape(link)}" target="_blank" rel="noopener">\n'
            '<section class="hero">\n'
            f'{inner}'
            '</section>\n'
            '</a>'
        )
    return (
        '<section class="hero">\n'
        f'{inner}'
        '</section>'
    )


def build_headlines_from_items(items, exclude_link=None, exclude_sources=None,
                                max_per_source=2, max_per_region=3, total=10):
    """Render the wire panel from already-fetched items, sorted recency-first.
    Filters to mega-cap mentions OR corruption items; caps per-source and
    per-region so no single outlet or region dominates."""
    pool = [i for i in items
            if is_financially_relevant(i)
            and (_matches_megacap(i) or _is_corruption_item(i))]
    if exclude_sources:
        pool = [i for i in pool if i["source"] not in exclude_sources]
    if exclude_link:
        pool = [i for i in pool if i["link"] != exclude_link]
    pool.sort(key=lambda x: x["ts"], reverse=True)

    selected = []
    per_source_count = {}
    per_region_count = {}
    for item in pool:
        if per_source_count.get(item["source"], 0) >= max_per_source:
            continue
        region = SOURCE_REGION.get(item["source"], "OTHER")
        if per_region_count.get(region, 0) >= max_per_region:
            continue
        selected.append(item)
        per_source_count[item["source"]] = per_source_count.get(item["source"], 0) + 1
        per_region_count[region] = per_region_count.get(region, 0) + 1
        if len(selected) >= total:
            break

    html_parts = []
    for item in selected:
        time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else ""
        html_parts.append(
            f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta"><span class="source-tag">{html.escape(item["source"])}</span><span>{html.escape(time_str)}</span></div>'
            f'<div class="headline-text">{html.escape(item["title"])}</div>'
            f'</div></a>'
        )
    return "\n".join(html_parts) if html_parts else '<div class="headline"><div class="headline-text" style="color:var(--text-dim)">Headlines unavailable.</div></div>'


def build_corruption_watch(items, exclude_link=None, total=8):
    """Render the Corruption Watch panel — items from accountability-focused
    sources OR matching corruption keywords (fraud, indicted, SEC charges,
    insider trading, etc.). The brand's differentiator on the home page."""
    pool = [i for i in items if _is_corruption_item(i)]
    if exclude_link:
        pool = [i for i in pool if i["link"] != exclude_link]
    pool.sort(key=lambda x: x["ts"], reverse=True)

    seen = set()
    selected = []
    per_source = {}
    for item in pool:
        if item["link"] in seen:
            continue
        if per_source.get(item["source"], 0) >= 2:
            continue
        selected.append(item)
        seen.add(item["link"])
        per_source[item["source"]] = per_source.get(item["source"], 0) + 1
        if len(selected) >= total:
            break

    if not selected:
        return ('<div class="headline"><div class="headline-text" '
                'style="color:var(--text-dim)">No flagged corruption items right now '
                '— the pack is watching.</div></div>')

    parts = []
    for item in selected:
        time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else ""
        parts.append(
            f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta">'
            f'<span class="source-tag" style="color:#b042ff;">{html.escape(item["source"])}</span>'
            f'<span>{html.escape(time_str)}</span>'
            f'</div>'
            f'<div class="headline-text">{html.escape(item["title"])}</div>'
            f'</div></a>'
        )
    return "\n".join(parts)


def build_regional_panels(items, exclude_link=None):
    """Per-continent wire panels for the regional desk.
    Returns dict: region_code → rendered HTML for that panel's body."""
    panels = {}
    for region in REGION_LABEL:
        region_items = [
            i for i in items
            if SOURCE_REGION.get(i["source"]) == region
            and is_financially_relevant(i)
            and i["source"] != "TRADING ECON"  # calendar handled separately
            and (not exclude_link or i["link"] != exclude_link)
        ]
        region_items.sort(key=lambda x: x["ts"], reverse=True)

        # 1 per source for diversity in small panels; cap at 4 items
        selected = []
        seen_sources = set()
        for item in region_items:
            if item["source"] in seen_sources:
                continue
            selected.append(item)
            seen_sources.add(item["source"])
            if len(selected) >= 4:
                break

        if not selected:
            panels[region] = (
                '<div class="headline"><div class="headline-text" '
                'style="color:var(--text-dim)">No recent items.</div></div>'
            )
            continue

        html_parts = []
        for item in selected:
            time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else ""
            html_parts.append(
                f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" '
                f'style="text-decoration:none;color:inherit;">'
                f'<div class="headline">'
                f'<div class="headline-meta">'
                f'<span class="source-tag">{html.escape(item["source"])}</span>'
                f'<span>{html.escape(time_str)}</span>'
                f'</div>'
                f'<div class="headline-text">{html.escape(item["title"])}</div>'
                f'</div></a>'
            )
        panels[region] = "\n".join(html_parts)
    return panels


def build_economic_calendar(items):
    """Render Trading Economics RSS items as a calendar list.
    Most recent calendar entries first; max 12 rows."""
    cal_items = [i for i in items if i["source"] == "TRADING ECON"]
    cal_items.sort(key=lambda x: x["ts"], reverse=True)
    cal_items = cal_items[:12]

    if not cal_items:
        return (
            '<div class="headline"><div class="headline-text" '
            'style="color:var(--text-dim)">Calendar feed unavailable.</div></div>'
        )

    rows = []
    for item in cal_items:
        time_str = item["ts"].strftime("%b %d %H:%M") if item["ts"].year > 2001 else "—"
        # Trading Economics often packs the full event description in title;
        # if there's a useful summary, append it.
        desc = item["summary"]
        desc_html = (
            f'<div style="color:var(--text-dim);font-size:0.85em;margin-top:2px;">'
            f'{html.escape(desc[:140])}</div>' if desc and desc.lower() != item["title"].lower() else ""
        )
        rows.append(
            f'<a href="{html.escape(item["link"])}" target="_blank" rel="noopener" '
            f'style="text-decoration:none;color:inherit;">'
            f'<div class="headline">'
            f'<div class="headline-meta">'
            f'<span class="source-tag" style="color:var(--green);">{html.escape(time_str)}</span>'
            f'</div>'
            f'<div class="headline-text">{html.escape(item["title"])}</div>'
            f'{desc_html}'
            f'</div></a>'
        )
    return "\n".join(rows)


def write_atom_feed(items, hero_item=None):
    """Emit /feed.xml — an Atom feed of the Loudest Howl + top wire items.
    Used by external services (dlvr.it, Buffer, Zapier) to auto-post to X / social.

    hero_item: optional dict (same shape as wire items) for the auto-Loudest-Howl
               so it appears as the first feed entry, tagged 'LOUDEST HOWL'.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pool = [i for i in items
            if is_financially_relevant(i)
            and (_matches_megacap(i) or _is_corruption_item(i))]
    pool.sort(key=lambda x: x["ts"], reverse=True)

    feed_items = []
    seen_links = set()
    if hero_item:
        feed_items.append(("LOUDEST HOWL", hero_item))
        seen_links.add(hero_item["link"])
    for item in pool:
        if item["link"] in seen_links:
            continue
        feed_items.append(("WIRE", item))
        seen_links.add(item["link"])
        if len(feed_items) >= 20:
            break

    HASHTAGS = "#HowlStreet #Markets"

    entries_xml = []
    for category, item in feed_items:
        ts_iso = item["ts"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prefix = "[LOUDEST HOWL] " if category == "LOUDEST HOWL" else ""
        title = html.escape(f"{prefix}{item['title']} {HASHTAGS}")
        link = html.escape(item["link"], quote=True)
        summary = html.escape(item.get("summary", "") or item["title"])
        source_label = html.escape(item["source"])
        entries_xml.append(
            "  <entry>\n"
            f"    <title>{title}</title>\n"
            f'    <link href="{link}" />\n'
            f"    <id>{link}</id>\n"
            f"    <updated>{ts_iso}</updated>\n"
            f"    <summary>{summary}</summary>\n"
            f'    <category term="{category}" />\n'
            f'    <category term="{source_label}" />\n'
            "  </entry>"
        )

    content = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        '  <title>Howl Street — Your Wolf of Wall Street</title>\n'
        '  <subtitle>They howl for themselves. We howl for you. No-BS finance signals from 100+ sources.</subtitle>\n'
        f'  <link href="{SITE_URL}/" />\n'
        f'  <link rel="self" type="application/atom+xml" href="{SITE_URL}/feed.xml" />\n'
        f'  <id>{SITE_URL}/</id>\n'
        f"  <updated>{now_utc}</updated>\n"
        '  <author><name>Howl Street</name></author>\n'
        + "\n".join(entries_xml) + "\n"
        '</feed>\n'
    )
    FEED_PATH.write_text(content, encoding="utf-8")


_DASH_RE = re.compile(r"\s*[—–]\s*")
# Trailing junk separators left behind when RSS feeds append "- Source" / "| Site"
# attributions and the source name gets stripped elsewhere. We strip the
# orphan separator to avoid output like "in March -." after we add a period.
_TRAIL_SEP_RE = re.compile(r"\s*[\-–—|·:,;]+\s*$")
_TITLE_NORM_RE = re.compile(r"\W+")
_PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_BOILERPLATE = (
    "follow us", "sign up for", "subscribe to", "all rights reserved",
    "cookie policy", "privacy policy", "newsletter", "click here",
    "read more at", "this article was", "advertisement", "share this",
    "get the latest", "you can read", "originally appeared", "view comments",
)
# Sentences containing these phrases are process-y filler ("Ueda is addressing
# the press conference") and get deprioritized when picking briefing copy.
_FILLER_PHRASES = (
    "is addressing", "is speaking", "will speak", "spoke about",
    "is discussing", "will discuss", "discussing the",
    "press conference", "press briefing", "addressing the press",
    "explaining the reason", "explaining the rationale",
    "holding a press", "gave remarks", "made remarks", "told reporters",
    "is set to speak", "is expected to discuss", "is delivering remarks",
)
# Pattern: leading editorial label like 'BREAKING:', 'Ueda Speech:', 'ANALYSIS:'
# that we strip to avoid double-colon when prefixing with 'Howl of the Day:'.
_LABEL_PREFIX_RE = re.compile(r"^([A-Z][\w']+(?:\s+[A-Z][\w']+){0,2}):\s+")

# Earnings-release title patterns. PR Newswire / Business Wire / GlobeNewswire
# items that match here become EARNINGS-tagged queue cards with a "JUST IN:
# $TICKER reports earnings" lede instead of the standard wire format.
_CORRUPTION_RE = re.compile(
    r"\b(?:"
    r"fraud|fraudulent|defraud"
    r"|indicted|indictment|charged|charges|guilty\s+plea|pleaded\s+guilty"
    r"|insider\s+trading|stock\s+manipulation|market\s+manipulation"
    r"|money\s+laundering|laundered|wire\s+fraud|securities\s+fraud|accounting\s+fraud"
    r"|bribery|bribed|kickback|kickbacks"
    r"|conflict\s+of\s+interest|self[- ]dealing|self[- ]enrich"
    r"|whistleblower|whistle[- ]blower|leaked\s+documents"
    r"|SEC\s+(?:probe|investigation|charges|fines|enforcement|complaint|settlement|sued|files)"
    r"|DOJ\s+(?:probe|investigation|charges|fines|settlement|sued)"
    r"|FTC\s+(?:probe|investigation|charges|sued)"
    r"|CFTC\s+(?:probe|investigation|charges|fines)"
    r"|FINRA\s+(?:probe|investigation|fines)"
    r"|class[- ]action\s+(?:lawsuit|suit)"
    r"|tax\s+evasion|tax\s+fraud|offshore\s+accounts|panama\s+papers|paradise\s+papers|pandora\s+papers"
    r"|ponzi\s+scheme|pump[- ]and[- ]dump"
    r"|cooked\s+the\s+books|earnings\s+manipulation"
    r"|corruption|corrupt"
    r"|insider\s+trade(?:r|d|s)?|insider\s+selling|insider\s+buying"
    r"|dark\s+money|shell\s+company|shell\s+companies"
    r"|crony|cronyism|self[- ]dealing"
    r"|too\s+big\s+to\s+(?:fail|jail)"
    r"|regulatory\s+capture|revolving\s+door"
    r"|mass\s+layoff(?:s)?|stock\s+buyback\s+(?:while|despite)"
    r")\b",
    re.IGNORECASE,
)
# Sources whose entire mission is corruption / accountability journalism.
# Items from these always classify as CORRUPTION even without keyword match.
_CORRUPTION_SOURCES = {"PROPUBLICA", "WALL ST PARADE", "NAKED CAPITAL", "INTERCEPT"}

_BREAKING_RE = re.compile(
    r"\b(?:breaking|urgent|developing|just\s+in|live[- ]update|live\s+blog|alert)\b",
    re.IGNORECASE,
)

_EARNINGS_TITLE_RE = re.compile(
    r"\b(?:Q[1-4]|first[- ]?quarter|second[- ]?quarter|third[- ]?quarter|fourth[- ]?quarter|full[- ]?year|fiscal[- ]?year|FY\d*)\s+"
    r"(?:results|earnings|financial\s+results|revenue|profit|loss)"
    r"|\bReports?\s+(?:Q[1-4]|first|second|third|fourth)\s+(?:Quarter|Year)"
    r"|\b(?:just\s+)?(?:reports|posts|announces|delivers|reported|announced|posted|delivered)\s+"
    r"(?:its\s+|their\s+|record\s+|strong\s+|Q[1-4]\s+|first[- ]?quarter\s+|second[- ]?quarter\s+|third[- ]?quarter\s+|fourth[- ]?quarter\s+|fiscal\s+|full[- ]?year\s+)*"
    r"(?:earnings|results|revenue|profit|EPS|loss)"
    r"|\bbeats?\s+(?:on|earnings|EPS|estimates|consensus|expectations|street|profit|revenue)"
    r"|\bmisses?\s+(?:on|earnings|EPS|estimates|consensus|expectations|street|profit|revenue)"
    r"|\b(?:tops|exceeds|trails)\s+(?:Wall\s+Street|consensus|estimates|expectations|forecasts)"
    r"|\bearnings\s+(?:beat|miss)\b",
    re.IGNORECASE,
)
# Cashtag detector: $AAPL style tickers. Used to extract the ticker from
# earnings titles so we can prefix tweets with "$TICKER reports earnings".
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
# Fallback ticker detector: ALL-CAPS standalone words 2-5 chars in title that
# look like tickers (e.g. "Apple Inc. (AAPL) reports..."). Loose, last resort.
_TICKER_PAREN_RE = re.compile(r"\(([A-Z]{2,5})(?:[:\.][A-Z]+)?\)")

# Sentences/items with concrete financial signals (prices, percentages,
# market terms, central banks, rating agencies, etc.). Used both to prefer
# substantive briefing sentences AND as the strict gate that drops items
# without any finance content from the wire panel and hero pool.
_FINANCE_SIGNAL_RE = re.compile(
    r"\$\s?\d|€\s?\d|£\s?\d|¥\s?\d|₹\s?\d"
    r"|\b\d+(?:[\.,]\d+)?\s*(?:percent|%|bps|basis\s+points|pct)\b"
    r"|\b\d+(?:[\.,]\d+)?\s*(?:billion|trillion|million|bn|tn|mn)\b"
    r"|\b(?:up|down|rose|fell|jumped|dropped|gained|lost|surged|plunged|climbed|declined|slipped|advanced|tumbled|rallied)\s+(?:to\s+)?\$?\d"
    r"|\b(?:stock|stocks|shares|equit(?:y|ies)|bond|bonds|yield|yields|treasur(?:y|ies)|index|indices|futures|crude|oil|brent|wti|gold|silver|copper|natgas|nasdaq|s&p|dow\s+jones|ftse|dax|cac|nikkei|hang\s+seng|sensex|nifty)\b"
    r"|\b(?:Fed|Federal\s+Reserve|FOMC|ECB|BoJ|PBOC|BOE|BoE|BOC|RBA|RBNZ|SNB|IMF|World\s+Bank|BIS)\b"
    r"|\b(?:Fitch|Moody'?s|S&P\s+Global|Standard\s+Chartered|Goldman|Morgan\s+Stanley|JPMorgan|Citi|Barclays|UBS|HSBC|Wells\s+Fargo|BlackRock|Vanguard)\b"
    r"|\b(?:earnings|revenue|EPS|guidance|forecast|profit|profits|loss|losses|dividend|buyback|IPO|merger|acquisition|valuation|capex|opex)\b"
    r"|\b(?:CPI|PPI|GDP|PCE|NFP|nonfarm|payrolls|jobless\s+claims|inflation|deflation|recession|stagflation|disinflation)\b"
    r"|\b(?:FII|FDI|FPI|FOMC|OECD|OPEC|G7|G20|WTO)\b"
    r"|\b(?:downgrade|upgrade|rating|ratings|outlook|hawkish|dovish|tightening|easing|pivot)\b"
    r"|\b(?:peso|dollar|euro|yen|pound\s+sterling|yuan|renminbi|rupee|ruble|won|franc|krona|krone|real|rand|lira|baht|ringgit|dirham|riyal|forex|fx)\b"
    r"|\b(?:bank|banks|banking|hedge\s+fund|private\s+equity|asset\s+manager|fund\s+manager|broker|brokerage|venture\s+capital|VC)\b"
    r"|\b(?:rate\s+(?:cut|hike|hold|decision|move)|interest\s+rate|policy\s+rate|benchmark\s+rate|repo\s+rate|prime\s+rate)\b"
    r"|\b(?:bullish|bearish|rally|sell-?off|correction|drawdown|breakout|short\s+squeeze|margin\s+call)\b"
    r"|\b(?:bitcoin|ethereum|crypto|cryptocurrency|stablecoin|defi|blockchain\s+(?:fund|etf))\b"
    r"|\b(?:Treasury\s+(?:Secretary|yields?|note|bond|bill)|Powell|Yellen|Lagarde|Ueda|Bailey)\b"
    r"|\b(?:bond\s+market|stock\s+market|equity\s+market|fx\s+market|credit\s+market|commodity\s+market)\b",
    re.IGNORECASE,
)


def _has_financial_signal(sentence):
    """True if sentence contains a price, percentage, market term, central
    bank, or financial-data keyword. Used to prefer concrete-fact sentences
    over scene-setting ones when picking briefing copy."""
    return bool(_FINANCE_SIGNAL_RE.search(sentence))

# All-caps editorial markers that some publishers prepend to article body
# ("UPDATED FOR AFTERNOON TRADING", "BREAKING:", "EXCLUSIVE"). Strip from
# the start of briefings so they lead with actual content.
_EDITORIAL_PREFIX_RE = re.compile(
    r"^(?:"
    r"UPDATED(?:\s+(?:FOR|AFTER)\s+[A-Z\s]+(?:TRADING|SESSION|HOURS|UPDATE))?"
    r"|UPDATE"
    r"|BREAKING(?:\s+NEWS)?"
    r"|EXCLUSIVE"
    r"|DEVELOPING(?:\s+STORY)?"
    r"|LIVE(?:\s+UPDATES?)?"
    r"|TIMELINE"
    r"|ANALYSIS"
    r"|EXPLAINER"
    r"|WATCH"
    r"|OPINION"
    r")[:\s]+(?=[A-Z])"
)

# News datelines: "MANILA, Philippines,", "NEW YORK (Reuters),",
# "WASHINGTON, May 28," etc. Strip so the briefing leads with the
# actual lede sentence.
_DATELINE_RE = re.compile(
    r"^[A-Z][A-Z]+(?:\s+[A-Z][A-Z]+)*"   # ALL CAPS city (one or more words)
    r"(?:\s*\([^)]+\))?"                  # optional "(Reuters)" or similar
    r",\s*"                                # mandatory comma
    r"(?:[\w\s\.\(\)]{0,50}?,\s*)?"       # optional country/date/source + comma
    r"(?=[A-Z])"                           # content begins with capital
)


def _strip_dashes(text):
    """Replace em dashes / en dashes with comma + space. Hyphens in compound
    words like 'data-dependent' are left alone."""
    if not text:
        return text
    return _DASH_RE.sub(", ", text).strip()


def _strip_trailing_seps(text):
    """Remove orphan trailing separators (- — | · : etc.) that RSS feeds
    leave behind after source-name stripping. Called before we append our
    own punctuation to a title or briefing."""
    if not text:
        return text
    return _TRAIL_SEP_RE.sub("", text).rstrip()


_TRAIL_ELLIPSIS_RE = re.compile(r"\s*(?:\.{2,}|…)+\s*$")
# "Continue reading", "Read more", "Read the full article", etc. that RSS
# summaries and og:descriptions append. Strip from the END so we don't ship
# a tweet whose last words are a CTA to leave the post.
_CONTINUE_READING_RE = re.compile(
    r"\s*[\.\-—|·:]?\s*"
    r"(?:continue\s+reading|read\s+(?:more|the\s+full\s+(?:story|article|piece)|on)"
    r"|click\s+here(?:\s+to\s+(?:read|continue|learn))?"
    r"|see\s+more|view\s+(?:more|original)"
    r"|appeared\s+first\s+on|originally\s+published|via\s+\w+)"
    r"[^.!?]*\.?\s*$",
    re.IGNORECASE,
)
# WordPress feed boilerplate: "The post {headline} appeared first on Site."
# Strip the entire clause as one unit, otherwise we leave a dangling
# "The post {headline}" sentence with no end.
_WP_BOILERPLATE_RE = re.compile(
    r"\s*[\.!?]?\s*The\s+post\s+[^.!?]{1,300}\bappeared\s+first\s+on\b[^.!?]*\.?\s*$",
    re.IGNORECASE,
)


def _strip_trailing_ellipsis(text):
    """Strip any trailing "..." / "…" so X posts never end on a half-thought.
    User-facing rule: every word that's in the tweet should be complete."""
    if not text:
        return text
    cleaned = _TRAIL_ELLIPSIS_RE.sub("", text).rstrip()
    return cleaned or text


def _strip_continue_reading(text):
    """Strip 'continue reading' / 'read more' style tails that RSS feeds
    and og:descriptions tack on. Also strips WordPress 'The post X
    appeared first on Y' boilerplate as a whole clause so we don't leave
    a dangling 'The post X' sentence."""
    if not text:
        return text
    # WordPress boilerplate first (matches the whole clause).
    out = _WP_BOILERPLATE_RE.sub("", text).rstrip()
    prev = None
    # Then iterate continue-reading / read-more / etc. CTAs in case stacked.
    while out != prev:
        prev = out
        out = _CONTINUE_READING_RE.sub("", out).rstrip()
    return _strip_trailing_ellipsis(out)


def _format_briefing_as_bullets(briefing):
    """When a briefing has 2+ sentences, render as "- " bullets with spacing
    between them. Makes long posts scan-able on X (especially the long
    corruption / deep-dive items)."""
    if not briefing:
        return briefing
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(briefing) if s.strip()]
    if len(sentences) < 2:
        return briefing
    bullets = []
    for s in sentences:
        # Drop the trailing period — bullets read cleaner without one
        s = s.rstrip(" .")
        bullets.append(f"- {s}")
    return "\n".join(bullets)


def _smart_truncate(text, max_len, require_full_sentence=False):
    """Truncate to <= max_len ending cleanly at a sentence boundary.
    If require_full_sentence and no full sentence fits, return None so the
    caller can drop the content rather than emit a half-thought with a
    fake period. With require_full_sentence=False (titles), falls back to
    a clean word-boundary cut and does NOT append a fake period."""
    if len(text) <= max_len:
        return text
    candidate = text[:max_len]
    last_sent = max(
        candidate.rfind(". "), candidate.rfind("! "), candidate.rfind("? ")
    )
    if last_sent >= int(max_len * 0.4):
        return candidate[: last_sent + 1].rstrip()
    if require_full_sentence:
        return None
    last_space = candidate.rfind(" ")
    if last_space > 0:
        return candidate[:last_space].rstrip(" ,;:")
    return candidate.rstrip(" ,;:")


def _title_norm_prefix(title, n=30):
    """First n chars of normalized (alphanumeric-only, lowercased) title.
    Used to detect when a paragraph just rephrases the headline."""
    norm = _TITLE_NORM_RE.sub("", (title or "").lower())
    return norm[:n]


def _paragraph_too_similar(text, title_prefix):
    """True if `text` starts with the same first ~30 alphanumeric chars as
    the title — i.e., the paragraph is essentially restating the headline."""
    if not title_prefix:
        return False
    text_norm = _TITLE_NORM_RE.sub("", text.lower())
    return text_norm.startswith(title_prefix)


def _strip_label_prefix(title):
    """Drop a leading editorial label like 'BREAKING:', 'Ueda Speech:',
    'ANALYSIS:' so it doesn't collide with our 'Howl of the Day:' prefix."""
    return _LABEL_PREFIX_RE.sub("", title or "", count=1)


def _is_filler_sentence(sentence):
    """True if the sentence is process-y filler ('is addressing the press
    conference') rather than a substantive fact."""
    s = sentence.lower()
    return any(p in s for p in _FILLER_PHRASES)


def _is_earnings_title(title):
    """True if the title looks like an earnings release / report."""
    if not title:
        return False
    return bool(_EARNINGS_TITLE_RE.search(title))


def _is_corruption_item(item):
    """True if the item is from a corruption-focused source OR matches
    corruption / accountability keywords (fraud, indicted, SEC charges,
    insider trading, etc.). This is the brand's bread and butter — the
    'Wolf of Wall Street for the people' angle."""
    if item.get("source") in _CORRUPTION_SOURCES:
        return True
    text = (item.get("title", "") or "") + " " + (item.get("summary", "") or "")
    return bool(_CORRUPTION_RE.search(text))


def _is_breaking_title(title):
    """True if the title self-flags as breaking / urgent / developing."""
    if not title:
        return False
    return bool(_BREAKING_RE.search(title))


def _extract_ticker(title, summary=""):
    """Pull a stock ticker out of a title or summary. Returns the ticker
    string (with $ prefix) or empty string if none found."""
    text = title + " " + (summary or "")
    m = _CASHTAG_RE.search(text)
    if m:
        return f"${m.group(1)}"
    m = _TICKER_PAREN_RE.search(text)
    if m:
        return f"${m.group(1)}"
    return ""


def _clean_briefing_lead(text):
    """Strip editorial prefixes and news datelines from the start of a
    briefing so it opens with actual content instead of wire-service noise.
    Examples stripped: 'UPDATED FOR AFTERNOON TRADING ', 'MANILA, Philippines, ',
    'NEW YORK (Reuters), '."""
    if not text:
        return text
    cleaned = _EDITORIAL_PREFIX_RE.sub("", text, count=1)
    cleaned = _DATELINE_RE.sub("", cleaned, count=1).strip()
    return cleaned or text


def _is_substantive_summary(summary, title):
    """True if the RSS summary adds info beyond the title and isn't all
    process-y filler ('is addressing the press conference')."""
    if not summary or len(summary) < 80:
        return False

    title_norm = _TITLE_NORM_RE.sub("", (title or "").lower())
    summary_norm = _TITLE_NORM_RE.sub("", summary.lower())

    # Catch the "Exclusive: {title} Reuters" wrapper pattern: if the full
    # normalized title sits inside the normalized summary, the summary is
    # just the title with some chrome (publisher tag, "Exclusive:", etc.).
    if title_norm and len(title_norm) >= 30 and title_norm in summary_norm:
        leftover = summary_norm.replace(title_norm, "", 1)
        if len(leftover) < 60:
            return False

    title_prefix = _title_norm_prefix(title)
    if _paragraph_too_similar(summary, title_prefix):
        rest = summary_norm[len(title_prefix):]
        if len(rest) < 50:
            return False

    # Reject filler-heavy summaries so we fall through to article-body fetch.
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(summary) if s.strip()]
    if sentences:
        substantive = [s for s in sentences if not _is_filler_sentence(s)]
        if not substantive:
            return False
    return True


def fetch_article_briefing(url, title, timeout=8):
    """Fetch the article and return 1-2 sentences of body text that go
    *beyond* the headline. Falls back to og:description, but only if it
    isn't just a title reword. Returns None on failure or if everything
    we find duplicates the title."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HowlStreet/1.0; +https://howlstreet.github.io)",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read(600_000).decode(charset, errors="replace")
    except Exception as e:
        print(f"    ! briefing fetch failed: {e}", file=sys.stderr)
        return None

    title_prefix = _title_norm_prefix(title)

    # Strategy 1: scrape <p> tags from body, skip boilerplate + title rewords
    candidates = []
    for raw in _PARA_RE.findall(page):
        text = _clean_summary(raw)
        if len(text) < 80:
            continue
        low = text.lower()
        if any(j in low for j in _BOILERPLATE):
            continue
        if _paragraph_too_similar(text, title_prefix):
            continue
        candidates.append(text)
        if len(candidates) >= 5:
            break

    if candidates:
        combined = " ".join(candidates)
        sentences = [s.strip() for s in _SENT_SPLIT_RE.split(combined) if s.strip()]
        # Only keep substantive (non-filler) sentences. If none, fall through
        # to og:description rather than emit filler.
        substantive = [s for s in sentences if not _is_filler_sentence(s)]
        if substantive:
            # Prefer sentences with concrete financial signals so the
            # briefing makes the market relevance clear (e.g., surfaces
            # "Brent crude rose 1.2%" over "Trump met advisers").
            financial = [s for s in substantive if _has_financial_signal(s)]
            picked = financial if financial else substantive
            # X Premium gives us 4000 chars, so we can fit 3-4 substantive
            # sentences instead of cutting at 2.
            briefing = " ".join(picked[:4]).strip()
            if 80 <= len(briefing) <= 800:
                return _strip_dashes(briefing)

    # Strategy 2: og:description / meta description (one-shot, no second fetch)
    meta_patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:description["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']twitter:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']description["\']',
    )
    for pattern in meta_patterns:
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            desc = html.unescape(m.group(1)).strip()
            if len(desc) > 50 and not _paragraph_too_similar(desc, title_prefix):
                return _strip_dashes(desc)
    return None


def write_queue_html(items, hero_item=None, signal_posts=None, insider_posts=None):
    """Emit /queue.html — a hidden, noindex page listing the top 20 stories as
    pre-built tweets ready to copy-paste or one-click open in X's compose window.

    Each tweet is a real briefing: '{Howl of the Day: }{Title}. {1-2 sentence
    summary}' followed by the URL + hashtags. Briefing comes from the RSS
    summary when substantive, otherwise og:description fetched from the article.

    Not linked from the main site. Accessible only by direct URL. Disallowed in
    robots.txt and tagged noindex,nofollow.
    """
    HASHTAGS = "#HowlStreet #Markets"
    HASHTAGS_LEN = len(HASHTAGS)  # 20
    URL_LEN = 23  # X auto-shortens URLs to 23 (t.co)
    # X Premium: 4000 char limit per tweet (vs 280 on free tier).
    # We still cap titles tightly so the briefing has room to lead with substance.
    MAX = 4000
    TITLE_CAP = 110

    # Queue surfaces only mega-cap mentions OR corruption items (the brand's
    # cross-aisle "expose the rats" content). Everything else dropped.
    pool = [i for i in items
            if is_financially_relevant(i)
            and (_matches_megacap(i) or _is_corruption_item(i))]
    pool.sort(key=lambda x: x["ts"], reverse=True)

    # Classify each item into a card category, then round-robin them so the
    # queue shows VARIETY at the top instead of clumping (e.g. 6 EARNINGS
    # in a row). Priority order tries CORRUPTION → EARNINGS → BREAKING →
    # JUST_IN → WIRE on each pass.
    now_ny = datetime.now(NY)
    JUST_IN_WINDOW_MIN = 60

    def classify(item):
        if _is_corruption_item(item):
            return "CORRUPTION"
        if _is_earnings_title(item.get("title", "")):
            return "EARNINGS"
        if _is_breaking_title(item.get("title", "")):
            return "BREAKING"
        age_min = (now_ny - item["ts"]).total_seconds() / 60
        if age_min < JUST_IN_WINDOW_MIN:
            return "JUST_IN"
        return "WIRE"

    queue = []
    seen_links = set()
    if hero_item:
        queue.append(("HOWL_OF_THE_DAY", hero_item))
        seen_links.add(hero_item["link"])

    buckets = {"CORRUPTION": [], "EARNINGS": [], "BREAKING": [], "JUST_IN": [], "WIRE": []}
    for item in pool:
        if item["link"] in seen_links:
            continue
        cat = classify(item)
        buckets[cat].append(item)

    # Round-robin pull in priority order; CORRUPTION first because it's the
    # brand differentiator. Cap total queue at 30 (X Premium = no shortage
    # of room for posts).
    priority = ["CORRUPTION", "EARNINGS", "BREAKING", "JUST_IN", "WIRE"]
    QUEUE_CAP = 30
    while len(queue) < QUEUE_CAP:
        progress = False
        for cat in priority:
            if buckets[cat]:
                item = buckets[cat].pop(0)
                queue.append((cat, item))
                seen_links.add(item["link"])
                progress = True
                if len(queue) >= QUEUE_CAP:
                    break
        if not progress:
            break

    # Briefing per item: RSS summary if substantive (and not a title reword),
    # else scrape the article body for a real briefing.
    briefings = {}
    needs_fetch = []  # list of (url, title)
    for category, item in queue:
        rss = _clean_summary(item.get("summary", ""))
        if _is_substantive_summary(rss, item["title"]):
            cleaned = _strip_continue_reading(_clean_briefing_lead(_strip_trailing_seps(_strip_dashes(rss))))
            if len(cleaned) >= 60:
                briefings[item["link"]] = cleaned
            else:
                needs_fetch.append((item["link"], item["title"]))
        else:
            needs_fetch.append((item["link"], item["title"]))

    if needs_fetch:
        print(f"  fetching article body for {len(needs_fetch)} queue items...")
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda p: fetch_article_briefing(p[0], p[1]), needs_fetch))
        for (link, _t), briefing in zip(needs_fetch, results):
            if briefing:
                cleaned = _strip_trailing_ellipsis(_clean_briefing_lead(_strip_trailing_seps(briefing)))
                if len(cleaned) >= 60:
                    briefings[link] = cleaned

    # Subtle Pack-themed leads sprinkled on every 4th wire post (rotating).
    # Howl of the Day always stays "Howl of the Day:". Most wire posts get
    # no prefix at all — keeps the feed feeling like real news, not a bot.
    WIRE_LEADS = ["Pack alert: ", "From the Pack: ", "Tracked by the Pack: "]

    cards = []

    # ── Phase 2: macro signal cards ──
    # Show first (above wire) so the most newsworthy original signal is the
    # first thing a poster sees when they open the queue. Each signal card
    # has a branded chart image attached and its own tweet template with
    # signal-specific hashtags (#Oil for WTI, #Bitcoin for BTC, etc.).
    site_url_for_signals = "howlstreet.github.io"
    for sp in (signal_posts or []):
        sig_id = sp["signal_id"]
        chart_path = sp["chart_path"]
        headline = sp["headline"]
        matters = sp["matters"]
        source = sp["source"]
        # Per-signal hashtags (preferred), with #HowlStreet as the brand anchor
        sig_tags = sp.get("hashtags") or "#Markets"
        sig_hashtags = f"#HowlStreet {sig_tags}"
        sig_hashtags_len = len(sig_hashtags)

        # Tweet text: headline + why it matters + site link + signal-specific tags.
        signal_tweet = f"{headline}\n\n{matters}\n\n{site_url_for_signals} {sig_hashtags}"
        signal_counted = len(headline) + 2 + len(matters) + 2 + URL_LEN + 1 + sig_hashtags_len
        if signal_counted > MAX:
            # Trim 'matters' to fit budget while keeping the headline whole.
            budget = MAX - (len(headline) + 2 + 2 + URL_LEN + 1 + sig_hashtags_len)
            matters_trim = _smart_truncate(matters, budget) if budget > 50 else ""
            signal_tweet = f"{headline}\n\n{matters_trim}\n\n{site_url_for_signals} {sig_hashtags}".strip()
            signal_counted = len(headline) + 2 + len(matters_trim) + 2 + URL_LEN + 1 + sig_hashtags_len

        intent_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(signal_tweet, safe="")
        sig_dom_id = re.sub(r"[^A-Za-z0-9_-]", "_", sig_id)

        cards.append(
            f'<div class="card card-signal">'
            f'  <div class="card-head">'
            f'    <span class="badge badge-signal">MACRO SIGNAL</span>'
            f'    <span class="meta">{html.escape(source)}</span>'
            f'    <span class="counter">{signal_counted}/4000</span>'
            f'  </div>'
            f'  <div class="signal-headline">{html.escape(headline)}</div>'
            f'  <div class="signal-matters">{html.escape(matters)}</div>'
            f'  <img class="signal-chart" src="{html.escape(chart_path)}" alt="{html.escape(sp["label"])} chart" loading="lazy">'
            f'  <textarea id="s{sig_dom_id}" readonly>{html.escape(signal_tweet)}</textarea>'
            f'  <div class="actions">'
            f'    <a class="btn btn-x" href="{html.escape(intent_url, quote=True)}" target="_blank" rel="noopener">Open on X</a>'
            f'    <button class="btn btn-copy" onclick="copySignal(\'{sig_dom_id}\', this)">Copy</button>'
            f'    <a class="btn btn-link" href="{html.escape(chart_path)}" download>Download chart</a>'
            f'  </div>'
            f'</div>'
        )

    wire_idx = 0
    for i, (category, item) in enumerate(queue):
        is_top = category == "HOWL_OF_THE_DAY"
        is_earnings = category == "EARNINGS"
        is_just_in = category == "JUST_IN"
        is_breaking = category == "BREAKING"
        is_corruption = category == "CORRUPTION"
        ticker = _extract_ticker(item.get("title", ""), item.get("summary", ""))
        if is_top:
            prefix = "Howl of the Day: "
        elif is_corruption:
            prefix = "Corruption uncovered: "
        elif is_earnings:
            # "Earnings Howl:" — brand-tied. Inject the ticker if it's not
            # already in the title so the cashtag is always visible.
            if ticker and ticker.lower() not in item.get("title", "").lower():
                prefix = f"Earnings Howl: {ticker} — "
            else:
                prefix = "Earnings Howl: "
        elif is_breaking:
            prefix = "BREAKING: "
        elif is_just_in:
            prefix = "Fresh Howl: "
        else:
            if wire_idx % 4 == 0:
                prefix = WIRE_LEADS[(wire_idx // 4) % len(WIRE_LEADS)]
            else:
                prefix = ""
            wire_idx += 1
        title = _strip_continue_reading(_strip_trailing_seps(_strip_dashes(item["title"])))
        link = item["link"]

        # Strip leading "BREAKING:" / "Ueda Speech:" labels so we don't get
        # "Howl of the Day: Ueda Speech: ..." double-colons. Only strip when
        # we're prepending our own labeled prefix.
        if prefix and prefix.endswith(": "):
            stripped = _strip_label_prefix(title)
            if len(stripped) >= 30:
                title = stripped

        # Howl of the Day gets a tighter title cap so the briefing has more
        # room to breathe and lead with substance.
        cap = 90 if is_top else TITLE_CAP
        if len(title) > cap:
            title = _smart_truncate(title, cap)
        # Re-strip trailing seps in case truncation surfaced one
        title = _strip_trailing_seps(title)

        briefing = briefings.get(item["link"])
        # One more pass on the briefing to drop "continue reading" tails and
        # "..." that may have sat in the persisted RSS summary.
        if briefing:
            briefing = _strip_continue_reading(briefing)
        # Bullet-format briefings with 2+ sentences so long posts (especially
        # corruption + earnings) read scan-ably with proper spacing.
        if briefing:
            briefing = _format_briefing_as_bullets(briefing)

        # Format: {prefix}{title}. {briefing}\n\n{url} {hashtags}
        title_punct = title.rstrip()
        if not title_punct.endswith(('.', '!', '?', ':', ';', '"', "'", ')')):
            title_punct += '.'

        # Image-first format: {prefix}{title}\n\n{briefing}\n\n{hashtags}
        # No external link in the tweet body — the rendered image card is
        # the centerpiece (Polymarket Money / Bull Theory style). The user
        # downloads the image from the queue and attaches it when posting.
        if briefing:
            fixed = len(prefix) + len(title_punct) + 2 + 2 + HASHTAGS_LEN
            briefing_budget = MAX - fixed
            if briefing_budget >= 60 and len(briefing) > briefing_budget:
                briefing = _smart_truncate(briefing, briefing_budget, require_full_sentence=True)

        if briefing:
            tweet = f"{prefix}{title_punct}\n\n{briefing}\n\n{HASHTAGS}"
            counted_len = len(prefix) + len(title_punct) + 2 + len(briefing) + 2 + HASHTAGS_LEN
        else:
            tweet = f"{prefix}{title_punct}\n\n{HASHTAGS}"
            counted_len = len(prefix) + len(title_punct) + 2 + HASHTAGS_LEN

        ts_str = item["ts"].astimezone(NY).strftime("%H:%M EDT · %b %d")
        source_label = html.escape(item["source"])
        intent_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(tweet, safe="")

        if is_top:
            badge_class, badge_text = "badge-howl", "HOWL OF THE DAY"
        elif is_corruption:
            badge_class, badge_text = "badge-corrupt", "CORRUPTION"
        elif is_earnings:
            badge_class, badge_text = "badge-earnings", "EARNINGS HOWL"
        elif is_breaking:
            badge_class, badge_text = "badge-breaking", "BREAKING"
        elif is_just_in:
            badge_class, badge_text = "badge-justin", "FRESH HOWL"
        else:
            badge_class, badge_text = "badge-wire", "WIRE"

        card_extra_class = ""
        if is_corruption:
            card_extra_class = " card-corrupt"
        elif is_earnings:
            card_extra_class = " card-earnings"
        elif is_breaking:
            card_extra_class = " card-breaking"
        elif is_just_in:
            card_extra_class = " card-justin"

        # Phase 4: render an image card so the X post is image-first.
        # The tweet text + image carry the post; the link is optional.
        post_image_path = None
        try:
            post_image_path = card_renderer.render_card(
                category=category,
                headline=f"{prefix}{title_punct}".strip(),
                briefing=briefing or "",
                ticker=ticker or "",
                source=item.get("source", ""),
                ts_label=ts_str,
                post_id=f"{category.lower()}_{i}_{item['link'][-40:]}",
            )
        except Exception as e:
            print(f"  ! card render skipped: {e}", file=sys.stderr)
        img_block = ""
        download_btn = ""
        if post_image_path:
            img_block = (
                f'  <img class="signal-chart" src="{html.escape(post_image_path)}"'
                f' alt="{html.escape(badge_text)} card" loading="lazy">'
            )
            download_btn = (
                f'    <a class="btn btn-link" href="{html.escape(post_image_path)}"'
                f' download>Download image</a>'
            )

        cards.append(
            f'<div class="card{card_extra_class}">'
            f'  <div class="card-head">'
            f'    <span class="badge {badge_class}">{badge_text}</span>'
            f'    <span class="meta">{source_label} · {html.escape(ts_str)}</span>'
            f'    <span class="counter">{counted_len}/4000</span>'
            f'  </div>'
            f'{img_block}'
            f'  <textarea id="t{i}" readonly>{html.escape(tweet)}</textarea>'
            f'  <div class="actions">'
            f'    <a class="btn btn-x" href="{html.escape(intent_url, quote=True)}" target="_blank" rel="noopener">Open on X</a>'
            f'    <button class="btn btn-copy" onclick="copyTweet({i}, this)">Copy</button>'
            f'{download_btn}'
            f'    <a class="btn btn-link" href="{html.escape(link, quote=True)}" target="_blank" rel="noopener">Article</a>'
            f'  </div>'
            f'</div>'
        )

    now_str = datetime.now(NY).strftime("%H:%M EDT · %b %d, %Y")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Howl Street — Post Queue</title>
<style>
  :root {{ --green: #00ff88; --bg: #000; --fg: #ccc; --dim: #666; --card: #0a0a0a; --border: #1a1a1a; }}
  * {{ box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--fg); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace; margin: 0; padding: 24px; }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ color: var(--green); font-size: 22px; margin: 0 0 4px; letter-spacing: 1px; }}
  .sub {{ color: var(--dim); font-size: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 6px; padding: 14px; margin-bottom: 14px; }}
  .card-head {{ display: flex; align-items: center; gap: 10px; font-size: 11px; margin-bottom: 8px; }}
  .badge {{ padding: 2px 6px; border-radius: 3px; font-weight: bold; letter-spacing: 0.5px; font-size: 10px; }}
  .badge-howl {{ background: var(--green); color: #000; }}
  .badge-wire {{ background: #1a1a1a; color: var(--green); border: 1px solid var(--green); }}
  .badge-signal {{ background: #ffaa00; color: #000; }}
  .badge-earnings {{ background: #00bfff; color: #000; }}
  .badge-justin {{ background: #ff4d4d; color: #fff; }}
  .badge-breaking {{ background: #ff4d4d; color: #fff; animation: pulse 2s infinite; }}
  .badge-corrupt {{ background: #b042ff; color: #fff; }}
  .card-earnings {{ border-color: #00bfff; }}
  .card-justin {{ border-color: #ff4d4d; }}
  .card-breaking {{ border-color: #ff4d4d; }}
  .card-corrupt {{ border-color: #b042ff; }}
  @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.6; }} 100% {{ opacity: 1; }} }}
  .card-signal {{ border-color: #ffaa00; }}
  .signal-headline {{ color: var(--fg); font-size: 15px; font-weight: bold; line-height: 1.4; margin-bottom: 6px; }}
  .signal-matters {{ color: var(--dim); font-size: 13px; line-height: 1.5; margin-bottom: 10px; }}
  .signal-chart {{ display: block; width: 100%; max-width: 100%; height: auto; border-radius: 4px; margin-bottom: 10px; border: 1px solid var(--border); }}
  .meta {{ color: var(--dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .counter {{ color: var(--dim); font-variant-numeric: tabular-nums; }}
  textarea {{ width: 100%; min-height: 110px; background: #050505; color: var(--fg); border: 1px solid var(--border); border-radius: 4px; padding: 8px; font: 13px/1.5 -apple-system, monospace; resize: vertical; white-space: pre-wrap; }}
  .actions {{ display: flex; gap: 8px; margin-top: 8px; }}
  .btn {{ font-size: 12px; padding: 6px 12px; border-radius: 4px; cursor: pointer; border: none; text-decoration: none; display: inline-block; font-weight: bold; letter-spacing: 0.5px; }}
  .btn-x {{ background: var(--green); color: #000; }}
  .btn-x:hover {{ filter: brightness(1.1); }}
  .btn-copy {{ background: #1a1a1a; color: var(--fg); border: 1px solid var(--border); }}
  .btn-copy:hover {{ border-color: var(--green); color: var(--green); }}
  .btn-copy.copied {{ background: var(--green); color: #000; }}
  .btn-link {{ background: transparent; color: var(--dim); border: 1px solid var(--border); }}
  .btn-link:hover {{ color: var(--fg); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>HOWL STREET — POST QUEUE</h1>
  <div class="sub">Generated {now_str} · Macro signals (with charts) and top wire posts. Click "Open on X" to compose. For signal posts, also click "Download chart" and attach the image when posting. Page is noindex; not linked from the public site.</div>
  {chr(10).join(cards)}
</div>
<script>
function copyTweet(i, btn) {{
  const ta = document.getElementById('t' + i);
  navigator.clipboard.writeText(ta.value).then(() => {{
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500);
  }});
}}
function copySignal(id, btn) {{
  const ta = document.getElementById('s' + id);
  navigator.clipboard.writeText(ta.value).then(() => {{
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1500);
  }});
}}
</script>
</body>
</html>
"""
    QUEUE_PATH.write_text(page, encoding="utf-8")


def write_sitemap():
    today = datetime.now(NY).strftime("%Y-%m-%d")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n'
        '    <loc>https://howlstreet.github.io/</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        '    <changefreq>hourly</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '  </url>\n'
        '</urlset>\n'
    )
    SITEMAP_PATH.write_text(content, encoding="utf-8")


# ----------------------------------------------------------------------------
# MAIN BUILD
# ----------------------------------------------------------------------------

def main():
    print("HOWL STREET updater — fetching data...")

    print("  US equities...")
    us_rows = []
    for name, sym, kind in US_EQUITIES:
        last, chg, pct = fetch_quote(sym)
        us_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Global indices...")
    global_rows = []
    for name, sym, kind in GLOBAL_INDICES:
        last, chg, pct = fetch_quote(sym)
        global_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Treasuries...")
    treas_rows = []
    used_fred = False
    for name, yf_sym, fred_sym in TREASURIES:
        last, chg = fetch_treasury_fred(fred_sym)
        if last is not None:
            used_fred = True
        else:
            last, chg, _ = fetch_quote(yf_sym)
        treas_rows.append(build_yield_row(name, last, chg))
    if used_fred:
        print("    (FRED)")
    elif FRED_API_KEY:
        print("    (FRED key set but request failed; fell back to yfinance)")
    else:
        print("    (yfinance — set FRED_API_KEY env to use FRED)")

    print("  FX...")
    fx_rows = []
    for name, sym, kind in FX_PAIRS:
        last, chg, pct = fetch_quote(sym)
        fx_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Commodities...")
    cmdty_rows = []
    for name, sym, kind in COMMODITIES:
        last, chg, pct = fetch_quote(sym)
        cmdty_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Crypto...")
    crypto_rows = []
    for name, sym, kind in CRYPTO:
        last, chg, pct = fetch_quote(sym)
        crypto_rows.append(build_table_row_3col(name, last, pct, kind))

    print("  Sectors...")
    sector_rows = []
    for name, sym, kind in SECTORS:
        last, chg, pct = fetch_quote(sym)
        sector_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Mega-caps...")
    megacap_rows = []
    for name, sym, kind in MEGACAPS:
        last, chg, pct = fetch_quote(sym)
        megacap_rows.append(build_table_row(name, last, chg, pct, kind))

    print("  Ticker bar...")
    ticker_items = [build_ticker_item(lbl, sym) for lbl, sym in TICKER_BAR]
    ticker_html = "\n".join(t for t in ticker_items if t)

    print("  Wires (all feeds)...")
    all_items = fetch_all_headlines()
    print(f"    fetched {len(all_items)} items from {len(RSS_FEEDS)} sources")

    print("  Market sessions...")
    sessions_html = build_market_sessions()
    global_indices_status = global_indices_status_label()
    treasury_status = "LIVE" if is_us_treasury_open() else "CLOSED"
    live_indicator_html = build_live_indicator()

    print("  Hero (Loudest Howl)...")
    hero_html = build_hero_from_md()
    hero_link = None
    auto_hero_item = None
    if hero_html:
        print("    (manual override from hero.md)")
    else:
        auto_hero_item = pick_top_story(all_items)
        if auto_hero_item:
            hero_html = build_hero_auto(all_items)
            hero_link = auto_hero_item["link"]
            print(f"    (Loudest Howl + Howl of the Day: {auto_hero_item['source']})")
        else:
            print("    (nothing cleared the quality threshold — hero hidden)")

    print("  Wire panel...")
    headlines_html = build_headlines_from_items(
        all_items, exclude_link=hero_link, exclude_sources={"TRADING ECON"},
    )

    print("  Corruption Watch...")
    corruption_html = build_corruption_watch(all_items, exclude_link=hero_link)

    print("  Regional desk...")
    regional = build_regional_panels(all_items, exclude_link=hero_link)

    print("  Economic calendar...")
    calendar_html = build_economic_calendar(all_items)

    # Timestamp — actual NY tz so DST is handled (EST winter / EDT summer)
    now_ny = datetime.now(NY)
    tz_label = now_ny.tzname()
    ts_str = now_ny.strftime("%b %d %H:%M ") + tz_label
    ts_short = now_ny.strftime("%H:%M ") + tz_label

    print("  Building HTML...")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    output = (
        template
        .replace("{{TICKER_BAR}}", ticker_html)
        .replace("{{HERO_SECTION}}", hero_html)
        .replace("{{US_EQUITIES}}", "\n".join(us_rows))
        .replace("{{GLOBAL_INDICES}}", "\n".join(global_rows))
        .replace("{{TREASURIES}}", "\n".join(treas_rows))
        .replace("{{FX}}", "\n".join(fx_rows))
        .replace("{{COMMODITIES}}", "\n".join(cmdty_rows))
        .replace("{{CRYPTO}}", "\n".join(crypto_rows))
        .replace("{{HEADLINES}}", headlines_html)
        .replace("{{CORRUPTION_WATCH}}", corruption_html)
        .replace("{{SECTORS}}", "\n".join(sector_rows))
        .replace("{{MEGACAPS}}", "\n".join(megacap_rows))
        .replace("{{REGIONAL_US}}", regional.get("US", ""))
        .replace("{{REGIONAL_EU}}", regional.get("EU", ""))
        .replace("{{REGIONAL_ASIA}}", regional.get("ASIA", ""))
        .replace("{{REGIONAL_ME}}", regional.get("ME", ""))
        .replace("{{REGIONAL_AF}}", regional.get("AF", ""))
        .replace("{{REGIONAL_AMERICAS}}", regional.get("AMERICAS", ""))
        .replace("{{ECONOMIC_CALENDAR}}", calendar_html)
        .replace("{{MARKET_SESSIONS}}", sessions_html)
        .replace("{{LIVE_INDICATOR}}", live_indicator_html)
        .replace("{{GLOBAL_INDICES_STATUS}}", global_indices_status)
        .replace("{{TREASURY_STATUS}}", treasury_status)
        .replace("{{TIMESTAMP}}", ts_str)
        .replace("{{TIMESTAMP_SHORT}}", ts_short)
    )

    OUTPUT_PATH.write_text(output, encoding="utf-8")
    write_sitemap()
    write_atom_feed(all_items, hero_item=auto_hero_item)

    # Phase 2: detect macro signals (multi-year highs/lows, big moves) and
    # render branded charts. Defensive — any failure in the signal pipeline
    # must not break the wire/queue build.
    try:
        signal_posts = signals.collect_signal_posts()
    except Exception as e:
        print(f"  ! signals pipeline failed: {e}", file=sys.stderr)
        signal_posts = []

    # Phase 3: corporate insider trades from openinsider (SEC Form 4 data).
    try:
        insider_posts = insider_trades.collect_insider_posts()
    except Exception as e:
        print(f"  ! insider trades pipeline failed: {e}", file=sys.stderr)
        insider_posts = []

    write_queue_html(all_items, hero_item=auto_hero_item,
                     signal_posts=signal_posts, insider_posts=insider_posts)
    print(f"  Wrote {OUTPUT_PATH} ({len(output):,} bytes)")
    print(f"  Wrote {FEED_PATH}")
    print(f"  Updated at {ts_str}")


if __name__ == "__main__":
    main()
